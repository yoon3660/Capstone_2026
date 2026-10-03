"""TCS 경부선 영업소를 기존 경부선 좌표계에 매핑한다.

실행:
    python scripts/build_tcs_offices.py

입력:
    data/processed/tcs_od_all.parquet
    data/processed/gyeongbu_route.json
    .env 의 EVDT_EX_API_KEY

출력:
    data/raw/ex_tcs_offices_<시각>/offices_gyeongbu.json
    data/processed/tcs_offices_gyeongbu.parquet

하는 일:
    1. 도로공사 영업소 위치 API에서 경부선(routeNo=001) 영업소 위치 조회
    2. tcs_od_all.parquet 의 TCS 영업소 코드/이름과 API 영업소명 매칭
    3. 기존 GyeongbuRoute에 위경도를 투영
    4. 공통 milepost_km 및 방향별 offset_km 계산

주의:
    경부선 route/offset 체계를 새로 만들지 않는다.
    기존 src/evdt/io/route.py 의 GyeongbuRoute를 그대로 재사용한다.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.io.route import (
    EXPECTED_ROUTE_KM,
    ROUTE_LENGTH_TOL_KM
)

from evdt.io.charger_ingest import (load_ex_api_key, normalize_station_name)  # noqa: E402
from evdt.io.route import GyeongbuRoute  # noqa: E402
from evdt.paths import DATA_PROCESSED_DIR, DATA_RAW_DIR  # noqa: E402


EX_UNIT_API_URL = (
    "https://data.ex.co.kr/openapi/locationinfo/locationinfoUnit"
)

# 주의:
# IC/휴게소 location API의 경부선 코드는 "0010"이지만,
# 영업소 위치(locationinfoUnit)는 "001"을 사용해야 실제 경부선 항목이 조회된다.
GYEONGBU_ROUTE_NO = "001"

OD_PATH = DATA_PROCESSED_DIR / "tcs_od_all.parquet"
OUTPUT_PATH = DATA_PROCESSED_DIR / "tcs_offices_gyeongbu.parquet"


def normalize_name(name: str) -> str:
    """TCS Matrix와 도로공사 API의 영업소명 표기 차이를 최소화한다."""

    name = str(name).strip()

    # 공백 제거
    name = re.sub(r"\s+", "", name)

    # 혹시 API에 '영업소'가 붙어 있으면 제거
    name = re.sub(r"영업소$", "", name)

    return name


def fetch_gyeongbu_offices(api_key: str) -> list[dict]:
    """도로공사 영업소 위치 API에서 경부선 영업소를 가져온다."""

    params = {
        "key": api_key,
        "type": "json",
        "routeNo": GYEONGBU_ROUTE_NO,
        "numOfRows": "1000",
        "pageNo": "1",
    }

    url = EX_UNIT_API_URL + "?" + urlencode(params)

    print("도로공사 경부선 영업소 위치 조회 중...")

    try:
        with urlopen(url, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"영업소 위치 API HTTP 오류: {exc.code}"
        ) from None
    except URLError as exc:
        raise RuntimeError(
            f"영업소 위치 API 연결 실패: {exc}"
        ) from None

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError(
            "영업소 위치 API 응답이 올바른 JSON이 아닙니다."
        ) from None

    if data.get("code") != "SUCCESS":
        raise RuntimeError(
            "영업소 위치 API 오류: "
            f"{data.get('message', '알 수 없는 오류')}"
        )

    items = data.get("list") or []

    if not isinstance(items, list):
        raise RuntimeError(
            "영업소 위치 API의 list 구조가 예상과 다릅니다."
        )

    expected = int(data.get("count", len(items)))

    if len(items) != expected:
        raise RuntimeError(
            "영업소 위치 API 결과를 모두 받지 못했습니다. "
            f"expected={expected}, actual={len(items)}"
        )

    return items


def read_tcs_offices() -> pd.DataFrame:
    """tcs_od_all.parquet에서 TCS 코드 ↔ 영업소명 표를 만든다."""

    if not OD_PATH.exists():
        raise FileNotFoundError(
            f"{OD_PATH} 가 없습니다.\n"
            "먼저 python scripts/build_tcs_od.py 를 실행하세요."
        )

    od = pd.read_parquet(
        OD_PATH,
        columns=[
            "start_office_code",
            "start_office",
        ],
    )

    offices = (
        od[
            [
                "start_office_code",
                "start_office",
            ]
        ]
        .drop_duplicates()
        .copy()
    )

    offices["start_office_code"] = (
        offices["start_office_code"]
        .astype(str)
        .str.strip()
    )

    offices["match_name"] = (
        offices["start_office"]
        .map(normalize_name)
    )

    # 같은 코드가 여러 이름을 갖는지 검사
    conflicts = (
        offices.groupby("start_office_code")["start_office"]
        .nunique()
    )

    bad = conflicts[conflicts > 1]

    if len(bad):
        raise ValueError(
            "하나의 TCS 코드에 여러 영업소명이 연결되어 있습니다:\n"
            + bad.to_string()
        )

    return offices


def api_offices_to_df(items: list[dict]) -> pd.DataFrame:
    rows = []

    for item in items:
        name = item.get("unitName")

        if not name:
            continue

        try:
            lat = float(item["yValue"])
            lon = float(item["xValue"])
        except (KeyError, TypeError, ValueError):
            print(
                f"  경고: 좌표 없는 API 항목 제외: {name}"
            )
            continue

        if not (33 <= lat <= 39):
            print(
                f"  경고: 위도 범위 이상으로 제외: "
                f"{name} / {lat}"
            )
            continue

        if not (124 <= lon <= 132):
            print(
                f"  경고: 경도 범위 이상으로 제외: "
                f"{name} / {lon}"
            )
            continue

        rows.append(
            {
                "api_office_name": str(name).strip(),
                "match_name": normalize_name(name),
                "lat": lat,
                "lon": lon,
                "coordinate_source": "tcs_api",
            }
        )

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError(
            "좌표가 있는 경부선 영업소를 하나도 읽지 못했습니다."
        )

    duplicate = (
        df.groupby("match_name")
        .size()
    )

    duplicate = duplicate[duplicate > 1]

    if len(duplicate):
        print(
            "\n[주의] API에서 같은 이름이 여러 번 나온 영업소:"
        )
        print(duplicate.to_string())

    return df


def match_tcs_and_api(
    tcs: pd.DataFrame,
    api: pd.DataFrame,
) -> pd.DataFrame:
    """TCS 영업소 코드와 도로공사 API 좌표를 영업소명으로 연결한다."""

    # API 쪽에서 중복 이름은 우선 제거하지 않고 검증한다.
    unique_api = (
        api.groupby("match_name")
        .filter(lambda x: len(x) == 1)
    )

    matched = tcs.merge(
        unique_api,
        on="match_name",
        how="inner",
        validate="many_to_one",
    )

    # API 경부선 영업소 중 실제 TCS Matrix에도 있는 것만 남는다.
    matched = matched[
        [
            "start_office_code",
            "start_office",
            "api_office_name",
            "lat",
            "lon",
            "coordinate_source",
        ]
    ].copy()

    matched = matched.rename(
        columns={
            "start_office_code": "office_code",
            "start_office": "office_name",
        }
    )

    return matched


def add_route_position(
    offices: pd.DataFrame,
) -> pd.DataFrame:
    """기존 GyeongbuRoute에 영업소 좌표를 투영한다."""

    route = GyeongbuRoute.load(validate=False)

    # 기존 route 파일의 geometry가 현재 프로젝트 기준과 같은지 직접 검증한다.
    if abs(route.length_km - EXPECTED_ROUTE_KM) > ROUTE_LENGTH_TOL_KM:
        raise RuntimeError(
            f"현재 route 길이 {route.length_km:.3f} km가 "
            f"기준 {EXPECTED_ROUTE_KM:.3f} km와 다릅니다."
        )

    if len(route.points) != 4153:
        raise RuntimeError(
            f"현재 route 점 개수가 {len(route.points):,}개입니다. "
            "기대값은 4,153개입니다."
        )

    print(
        f"기존 경부선 route 사용: "
        f"{route.length_km:.3f} km, "
        f"{len(route.points):,} points"
    )

    rows = []

    for row in offices.itertuples(index=False):
        milepost_km, snap_distance_km = route.project(
            float(row.lat),
            float(row.lon),
        )

        rows.append(
            {
                "office_code": row.office_code,
                "office_name": row.office_name,
                "lat": row.lat,
                "lon": row.lon,
                "coordinate_source": row.coordinate_source,

                # 공통 좌표: 구서IC -> 양재IC
                "milepost_km": round(
                    milepost_km,
                    3,
                ),

                # 기존 프로젝트 방향별 offset도 같이 저장
                "offset_up_km": round(
                    route.to_direction(
                        milepost_km,
                        "UP",
                    ),
                    3,
                ),

                "offset_down_km": round(
                    route.to_direction(
                        milepost_km,
                        "DOWN",
                    ),
                    3,
                ),

                "snap_distance_km": round(
                    snap_distance_km,
                    3,
                ),
            }
        )

    return (
        pd.DataFrame(rows)
        .sort_values("milepost_km")
        .reset_index(drop=True)
    )

def load_existing_route_raw() -> tuple[list[dict], list[dict]]:
    """기존 build_route.py가 저장한 IC/휴게소 raw 데이터를 읽는다."""

    route_json = DATA_PROCESSED_DIR / "gyeongbu_route.json"

    if not route_json.exists():
        raise FileNotFoundError(
            f"route 파일이 없습니다: {route_json}"
        )

    route_data = json.loads(
        route_json.read_text(encoding="utf-8")
    )

    raw_dir_name = (
        route_data.get("meta", {})
        .get("raw_dir")
    )

    if not raw_dir_name:
        raise RuntimeError(
            "gyeongbu_route.json의 meta.raw_dir가 없습니다."
        )

    raw_dir = DATA_RAW_DIR / raw_dir_name

    ic_path = raw_dir / "ic_all.json"
    rest_path = raw_dir / "rest_gyeongbu.json"

    if not ic_path.exists():
        raise FileNotFoundError(
            f"기존 IC 파일이 없습니다: {ic_path}"
        )

    if not rest_path.exists():
        raise FileNotFoundError(
            f"기존 휴게소 파일이 없습니다: {rest_path}"
        )

    ics = json.loads(
        ic_path.read_text(encoding="utf-8")
    )

    rests = json.loads(
        rest_path.read_text(encoding="utf-8")
    )

    print("\n기존 route raw 재사용:")
    print(" IC:", ic_path)
    print(" 휴게소:", rest_path)

    return ics, rests

def normalize_ic_name(name: str) -> str:
    name = str(name or "").strip()

    name = re.sub(r"\s+", "", name)

    # 일반 IC/JCT/JC 및 하이패스IC 표기 제거
    name = re.sub(
        r"(하이패스)?(IC|JCT|JC)$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    return name
def build_fallback_positions(
    missing_names: list[str],
    ics: list[dict],
    rests: list[dict],
    route: GyeongbuRoute,
) -> pd.DataFrame:
    """TCS API에 좌표가 없는 영업소를 기존 IC/휴게소 데이터로 보완한다."""

    rows = []

    # -----------------------------
    # 기존 IC 목록
    # -----------------------------
    ic_by_name: dict[str, list[dict]] = {}

    for item in ics:
        name = normalize_ic_name(
            item.get("icName", "")
        )

        if not name:
            continue

        ic_by_name.setdefault(name, []).append(item)

    # -----------------------------
    # 기존 경부선 휴게소
    # -----------------------------
    rest_by_name: dict[str, list[dict]] = {}

    for item in rests:
        name = normalize_station_name(
            item.get("unitName", "")
        )

        if not name:
            continue

        rest_by_name.setdefault(
            name,
            [],
        ).append(item)

    for original_name in missing_names:

        # 테스트 데이터는 실제 OD 영업소로 사용하지 않는다.
        if "테스트" in original_name:
            print(
                f"  제외: {original_name} "
                "(테스트 항목)"
            )
            continue

        # ---------------------------------
        # 1순위: 휴게소 이름
        # ---------------------------------
        rest_key = normalize_station_name(
            original_name
        )

        rest_candidates = rest_by_name.get(
            rest_key,
            [],
        )

        candidate = None
        source = None

        if len(rest_candidates) == 1:
            candidate = rest_candidates[0]
            source = "rest_fallback"

        elif len(rest_candidates) > 1:
            print(
                f"  경고: 휴게소 후보 여러 개: "
                f"{original_name}"
            )

        # ---------------------------------
        # 2순위: IC
        # ---------------------------------
        if candidate is None:
            ic_key = normalize_ic_name(
                original_name
            )

            ic_candidates = ic_by_name.get(
                ic_key,
                [],
            )

            if len(ic_candidates) == 1:
                candidate = ic_candidates[0]
                source = "ic_fallback"

            elif len(ic_candidates) > 1:
                # 같은 이름 IC가 여러 노선에 있을 수도 있다.
                # 경부선 중심선에 가장 가까운 것을 고른다.
                scored = []

                for item in ic_candidates:
                    try:
                        lat = float(
                            item["yValue"]
                        )
                        lon = float(
                            item["xValue"]
                        )
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                    ):
                        continue

                    _, dist = route.project(
                        lat,
                        lon,
                    )

                    scored.append(
                        (
                            dist,
                            item,
                        )
                    )

                if scored:
                    scored.sort(
                        key=lambda x: x[0]
                    )

                    candidate = scored[0][1]
                    source = "ic_fallback"

        if candidate is None:
            print(
                f"  미해결: {original_name}"
            )
            continue

        try:
            lat = float(candidate["yValue"])
            lon = float(candidate["xValue"])
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            print(
                f"  좌표 파싱 실패: "
                f"{original_name}"
            )
            continue

        milepost, snap_distance = (
            route.project(
                lat,
                lon,
            )
        )

        # 경부선에서 너무 먼 IC를 잘못 매칭하는 것 방지
        if snap_distance > 2.0:
            print(
                f"  경고: {original_name} "
                f"{source} 후보가 경부선에서 "
                f"{snap_distance:.3f}km 떨어져 있음"
            )
            continue

        print(
            f"  보완 성공: "
            f"{original_name} "
            f"<- {source} "
            f"(snap={snap_distance:.3f}km)"
        )

        rows.append(
            {
                "office_name": original_name,
                "lat": lat,
                "lon": lon,
                "coordinate_source": source,
            }
        )

    return pd.DataFrame(rows)


def main() -> int:
    print("=== 경부선 TCS 영업소 매핑 ===\n")

    # --------------------------------------------------
    # 1. 기존 TCS Matrix의 코드 ↔ 이름
    # --------------------------------------------------
    tcs = read_tcs_offices()

    print(
        f"TCS Matrix 전체 영업소: "
        f"{len(tcs):,}개"
    )

    # --------------------------------------------------
    # 2. 도로공사 경부선 영업소 위치 API
    # --------------------------------------------------
    api_key = load_ex_api_key()

    raw_items = fetch_gyeongbu_offices(
        api_key
    )

    print(
        f"도로공사 API 경부선 영업소: "
        f"{len(raw_items):,}개"
    )

    print("\nAPI 반환 항목:")
    for item in raw_items:
        print(
            " -",
            item.get("unitName"),
            "/",
            item.get("xValue"),
            item.get("yValue"),
        )

    # 원본은 그대로 저장
    stamp = (
        datetime.now()
        .astimezone()
        .strftime("%Y%m%d_%H%M%S")
    )

    raw_dir = (
        DATA_RAW_DIR
        / f"ex_tcs_offices_{stamp}"
    )

    raw_dir.mkdir(
        parents=True,
        exist_ok=False,
    )

    raw_path = (
        raw_dir
        / "offices_gyeongbu.json"
    )

    raw_path.write_text(
        json.dumps(
            raw_items,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    api = api_offices_to_df(raw_items)

    # --------------------------------------------------
    # 3. TCS 코드 ↔ API 영업소 위치
    # --------------------------------------------------
    matched = match_tcs_and_api(
        tcs,
        api,
    )
    # API 경부선 목록 중 TCS Matrix에도 있지만
    # 위치 좌표가 없는 영업소를 찾는다.
    tcs_names = set(
        tcs["start_office"]
        .astype(str)
        .str.strip()
    )

    missing_names = []

    for item in raw_items:
        name = str(
            item.get("unitName") or ""
        ).strip()

        if name not in tcs_names:
            continue

        x = item.get("xValue")
        y = item.get("yValue")

        if x in (None, "") or y in (None, ""):
            missing_names.append(name)

    print(
        "\n좌표 보완 대상:",
        missing_names,
    )

    route = GyeongbuRoute.load(
        validate=False
    )

    if (
            abs(
                route.length_km
                - EXPECTED_ROUTE_KM
            )
            > ROUTE_LENGTH_TOL_KM
    ):
        raise RuntimeError(
            f"현재 route 길이 "
            f"{route.length_km:.3f} km가 "
            f"기준 {EXPECTED_ROUTE_KM:.3f} km와 "
            "다릅니다."
        )

    if len(route.points) != 4153:
        raise RuntimeError(
            f"현재 route 점 개수가 "
            f"{len(route.points):,}개입니다."
        )

    ics, rests = load_existing_route_raw()

    fallback = build_fallback_positions(
        missing_names,
        ics,
        rests,
        route,
    )

    # fallback 결과에 TCS 코드 연결
    if not fallback.empty:
        fallback["match_name"] = (
            fallback["office_name"]
            .map(normalize_name)
        )

        tcs_lookup = (
            tcs[
                [
                    "start_office_code",
                    "start_office",
                    "match_name",
                ]
            ]
            .drop_duplicates()
        )

        fallback = fallback.merge(
            tcs_lookup,
            on="match_name",
            how="inner",
            validate="one_to_one",
        )

        fallback = fallback.rename(
            columns={
                "start_office_code": "office_code",
            }
        )

        fallback["office_name"] = fallback["start_office"]

        fallback = fallback[
            [
                "office_code",
                "office_name",
                "lat",
                "lon",
                "coordinate_source",
            ]
        ]

    print(
        f"TCS Matrix와 API 좌표 직접 매칭: "
        f"{len(matched):,}개"
    )

    # 기본 API 좌표 + fallback 좌표
    base = matched[
        [
            "office_code",
            "office_name",
            "lat",
            "lon",
            "coordinate_source",
        ]
    ].copy()

    if not fallback.empty:
        base = pd.concat(
            [
                base,
                fallback,
            ],
            ignore_index=True,
        )

    # 중복 검사
    if base["office_code"].duplicated().any():
        duplicated = base[
            base["office_code"].duplicated(keep=False)
        ].sort_values("office_code")

        raise RuntimeError(
            "TCS 영업소 코드가 중복 매핑되었습니다:\n"
            + duplicated.to_string(index=False)
        )

    # 기존 경부선 route에 projection
    mapped = add_route_position(base)

    # 2km 이상 떨어져 있다면 자동으로 신뢰하지 않는다.
    suspicious = mapped[
        mapped["snap_distance_km"] > 2.0
    ]

    if len(suspicious):
        print(
            "\n[주의] 경부선 중심선에서 "
            "2km 이상 떨어진 영업소:"
        )

        print(
            suspicious[
                [
                    "office_code",
                    "office_name",
                    "milepost_km",
                    "snap_distance_km",
                ]
            ].to_string(index=False)
        )

    # --------------------------------------------------
    # 5. 결과 확인
    # --------------------------------------------------
    print("\n=== 매핑 결과 ===")

    print(
        mapped[
            [
                "office_code",
                "office_name",
                "milepost_km",
                "offset_up_km",
                "offset_down_km",
                "snap_distance_km",
                "coordinate_source",
            ]
        ].to_string(index=False)
    )

    print(
        f"\n매핑 영업소 수: "
        f"{len(mapped)}개"
    )

    print(
        f"최대 중심선 스냅 거리: "
        f"{mapped['snap_distance_km'].max():.3f} km"
    )

    print(
        f"\n최종 유효 TCS 경부선 영업소: "
        f"{len(mapped)}개"
    )

    excluded_test = [
        name
        for name in missing_names
        if "테스트" in name
    ]

    if excluded_test:
        print(
            "분석 제외 테스트 항목:",
            ", ".join(excluded_test),
        )

        expected_names = {
            name
            for name in missing_names
            if "테스트" not in name
        }

        resolved_fallback_names = (
            set(fallback["office_name"])
            if not fallback.empty
            else set()
        )

        unresolved = sorted(
            expected_names - resolved_fallback_names
        )

        if unresolved:
            print(
                "\n[확인 필요] fallback으로도 위치를 찾지 못한 영업소:"
            )

            for name in unresolved:
                print(" -", name)

    # --------------------------------------------------
    # 6. 저장
    # --------------------------------------------------
    DATA_PROCESSED_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    mapped.to_parquet(
        OUTPUT_PATH,
        index=False,
    )

    print(
        f"\nraw 저장: {raw_path}"
    )

    print(
        f"processed 저장: {OUTPUT_PATH}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())