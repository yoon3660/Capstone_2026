from __future__ import annotations
from math import atan2, cos, radians, sin, sqrt

import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from evdt.paths import PROJECT_ROOT

EX_REST_API_URL = (
    "https://data.ex.co.kr/openapi/locationinfo/locationinfoRest"
)

EX_IC_API_URL = (
    "https://data.ex.co.kr/openapi/locationinfo/locationinfoIc"
)

GYEONGBU_ROUTE_NO = "0010"
DOWN_ORIGIN_IC_CODE = "0010I00045"  # 양재IC
UP_ORIGIN_IC_CODE = "0010I00001"    # 구서IC

CHARGER_TYPE_MAP = {
    "01": "DC_CHADEMO",
    "02": "AC_SLOW",
    "03": "DC_CHADEMO_AC3",
    "04": "DC_COMBO",
    "05": "DC_CHADEMO_DC_COMBO",
    "06": "DC_CHADEMO_AC3_DC_COMBO",
    "07": "AC3",
    "08": "DC_COMBO_SLOW",
    "09": "NACS",
    "10": "DC_COMBO_NACS",
}

STATION_NAME_ALIASES = {
    "워터경주휴게소": "경주휴게소",
    "워터칠곡휴게소": "칠곡휴게소",
}

def load_raw_chargers(raw_dir: Path) -> list[dict]:
    """환경부 충전소 API의 page_*.json을 읽어 충전기 목록으로 합친다."""

    json_files = sorted(raw_dir.glob("page_*.json"))

    if not json_files:
        raise FileNotFoundError(
            f"raw JSON 파일을 찾을 수 없습니다: {raw_dir}"
        )

    chargers: list[dict] = []

    for json_file in json_files:
        with json_file.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)

        items = data.get("items") or {}

        if not isinstance(items, dict):
            raise RuntimeError(
                f"예상과 다른 items 구조입니다: {json_file}"
            )

        page_chargers = items.get("item") or []

        if isinstance(page_chargers, dict):
            page_chargers = [page_chargers]

        if not isinstance(page_chargers, list):
            raise RuntimeError(
                f"예상과 다른 item 구조입니다: {json_file}"
            )

        chargers.extend(page_chargers)

    return chargers

def normalize_station_name(name: str) -> str:
    """API별 표현 차이를 제거해 휴게소의 기본 이름을 만든다."""

    name = name.strip()

    # 환경부 데이터에 붙는 분류 표기
    name = re.sub(r"\(고속도로\)", "", name)

    # 명시적인 서울/부산 방향 표기 제거
    name = re.sub(
        r"\((서울|부산)\s*(방향|방면)?\)",
        "",
        name,
    )
    name = re.sub(
        r"(서울|부산)\s*(방향|방면)",
        "",
        name,
    )

    # 환경부 충전소명에 붙을 수 있는 부가 표현
    name = re.sub(r"\(급\)", "", name)
    name = re.sub(r"E[\s-]?pit", "", name, flags=re.IGNORECASE)
    name = re.sub(r"전기차\s*충전소", "", name)

    # 공백 차이 제거
    name = re.sub(r"\s+", "", name)
    # 운영사 브랜드명이 붙은 예외 이름 보정
    name = STATION_NAME_ALIASES.get(name, name)

    return name

def detect_direction(name: str) -> str | None:
    """충전소명에 명시된 서울/부산 방향을 판별한다."""

    down = bool(
        re.search(r"\(부산\)|부산\s*(방향|방면)", name)
    )
    up = bool(
        re.search(r"\(서울\)|서울\s*(방향|방면)", name)
    )

    # 이상 데이터가 양쪽 표현을 모두 포함하면 추측하지 않는다.
    if up and down:
        return None

    if down:
        return "DOWN"

    if up:
        return "UP"

    return None

def load_ex_api_key() -> str:
    """프로젝트 .env에서 한국도로공사 API 키를 읽는다."""

    env_path = PROJECT_ROOT / ".env"

    if not env_path.exists():
        raise RuntimeError(".env 파일이 없습니다.")

    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()

        if line.startswith("EVDT_EX_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")

            if key:
                return key

    raise RuntimeError(
        ".env에 EVDT_EX_API_KEY를 설정해주세요."
    )


def fetch_gyeongbu_rest_areas(api_key: str) -> list[dict]:
    """한국도로공사 API에서 경부선 휴게소 목록을 가져온다."""

    params = {
        "key": api_key,
        "type": "json",
        "routeNo": GYEONGBU_ROUTE_NO,
        "numOfRows": "1000",
        "pageNo": "1",
    }

    url = EX_REST_API_URL + "?" + urlencode(params)

    try:
        with urlopen(url, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"한국도로공사 API HTTP 오류: {exc.code}"
        ) from None
    except URLError:
        raise RuntimeError(
            "한국도로공사 API 연결에 실패했습니다."
        ) from None

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError(
            "한국도로공사 API 응답이 올바른 JSON이 아닙니다."
        ) from None

    if data.get("code") != "SUCCESS":
        raise RuntimeError(
            f"한국도로공사 API 오류: {data.get('message', '알 수 없는 오류')}"
        )

    rest_areas = data.get("list") or []

    if not isinstance(rest_areas, list):
        raise RuntimeError(
            "한국도로공사 API의 list 구조가 예상과 다릅니다."
        )

    invalid = [
        item
        for item in rest_areas
        if item.get("routeNo") != GYEONGBU_ROUTE_NO
    ]

    if invalid:
        raise RuntimeError(
            "경부선 이외의 시설이 API 응답에 포함되어 있습니다."
        )

    expected_count = int(data.get("count", len(rest_areas)))

    if len(rest_areas) != expected_count:
        raise RuntimeError(
            "한국도로공사 경부선 시설을 모두 가져오지 못했습니다. "
            f"expected={expected_count}, actual={len(rest_areas)}"
        )

    return rest_areas

def station_match_key(name: str) -> tuple[str, str | None]:
    """휴게소 매칭에 사용할 (정규화 이름, 방향) 키를 만든다."""

    return (
        normalize_station_name(name),
        detect_direction(name),
    )


def filter_gyeongbu_chargers(
    chargers: list[dict],
    rest_areas: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    환경부 충전기 중 경부선 휴게소와 정확히 매칭되는 충전기만 추출한다.

    반환:
        matched_chargers:
            이름과 방향이 모두 일치한 환경부 충전기

        unmatched_rest_areas:
            방향은 확인됐지만 환경부 충전기와 매칭되지 않은 도로공사 시설

        unknown_direction_rest_areas:
            방향을 확인할 수 없는 도로공사 시설
    """

    official_by_key: dict[tuple[str, str], dict] = {}
    unknown_direction_rest_areas: list[dict] = []

    # 한국도로공사 경부선 시설을 기준 목록으로 만든다.
    for area in rest_areas:
        name = area.get("unitName", "")
        normalized_name, direction = station_match_key(name)

        if direction is None:
            unknown_direction_rest_areas.append(area)
            continue

        key = (normalized_name, direction)

        if key in official_by_key:
            raise RuntimeError(
                f"도로공사 경부선 시설 키가 중복됩니다: {key}"
            )

        official_by_key[key] = area

    matched_chargers: list[dict] = []
    matched_keys: set[tuple[str, str]] = set()

    # 환경부 충전기에서 이름 + 방향이 정확히 같은 것만 남긴다.
    for charger in chargers:
        name = charger.get("statNm", "")
        normalized_name, direction = station_match_key(name)

        if direction is None:
            continue

        key = (normalized_name, direction)

        if key not in official_by_key:
            continue

        matched_chargers.append(charger)
        matched_keys.add(key)

    # 도로공사에는 있지만 환경부 충전기가 없는 시설
    unmatched_rest_areas = [
        area
        for key, area in official_by_key.items()
        if key not in matched_keys
    ]

    return (
        matched_chargers,
        unmatched_rest_areas,
        unknown_direction_rest_areas,
    )

def build_station_candidates(
    matched_chargers: list[dict],
    rest_areas: list[dict],
) -> list[dict]:
    """
    매칭된 환경부 충전기가 존재하는 경부선 물리 휴게소 목록을 만든다.

    아직 offset_km를 계산하기 전 단계이므로
    최종 station DB 행이 아니라 station 후보 데이터이다.
    """

    matched_keys = {
        station_match_key(charger.get("statNm", ""))
        for charger in matched_chargers
    }

    stations: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for area in rest_areas:
        name = area.get("unitName", "")
        normalized_name, direction = station_match_key(name)

        if direction is None:
            continue

        key = (normalized_name, direction)

        # 환경부 충전기가 실제로 매칭된 휴게소만 사용
        if key not in matched_keys:
            continue

        if key in seen_keys:
            raise RuntimeError(
                f"중복된 물리 휴게소가 발견되었습니다: {key}"
            )

        try:
            lat = float(area["yValue"])
            lon = float(area["xValue"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                f"휴게소 좌표가 올바르지 않습니다: {name}"
            ) from None

        if not (-90 <= lat <= 90):
            raise RuntimeError(
                f"위도 범위가 올바르지 않습니다: {name} / {lat}"
            )

        if not (-180 <= lon <= 180):
            raise RuntimeError(
                f"경도 범위가 올바르지 않습니다: {name} / {lon}"
            )

        stations.append(
            {
                "name": normalized_name,
                "direction": direction,
                "lat": lat,
                "lon": lon,
                "service_area_code": area.get("serviceAreaCode"),
                "std_rest_cd": area.get("stdRestCd"),
                "unit_code": area.get("unitCode"),
                "official_name": name,
            }
        )

        seen_keys.add(key)

    return stations

def build_charger_candidates(
    matched_chargers: list[dict],
    station_candidates: list[dict],
) -> list[dict]:
    """
    환경부 개별 충전기를 DB charger 테이블 형태의 후보로 묶는다.

    같은 물리적 휴게소에서
    output과 chgerType이 같은 충전기는 하나의 그룹으로 만들고,
    실제 기수는 n_units에 저장한다.
    """

    station_by_key: dict[tuple[str, str], dict] = {}

    for station in station_candidates:
        key = (
            station["name"],
            station["direction"],
        )

        if key in station_by_key:
            raise RuntimeError(
                f"중복 station 후보가 있습니다: {key}"
            )

        station_by_key[key] = station

    grouped: dict[
        tuple[str, str, float, str],
        dict
    ] = {}

    for charger in matched_chargers:
        station_name, direction = station_match_key(
            charger.get("statNm", "")
        )

        if direction is None:
            raise RuntimeError(
                "방향 없는 충전기가 matched_chargers에 "
                f"포함되어 있습니다: {charger.get('statNm')}"
            )

        station_key = (
            station_name,
            direction,
        )

        if station_key not in station_by_key:
            raise RuntimeError(
                f"station 후보를 찾을 수 없습니다: {station_key}"
            )

        chger_type = charger.get("chgerType", "").strip()

        if not chger_type:
            raise RuntimeError(
                "chgerType이 없습니다: "
                f"{charger.get('statId')} / "
                f"{charger.get('chgerId')}"
            )

        try:
            power_kw = float(charger["output"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                "output 값이 올바르지 않습니다: "
                f"{charger.get('statId')} / "
                f"{charger.get('chgerId')}"
            ) from None

        if power_kw <= 0:
            raise RuntimeError(
                f"output은 0보다 커야 합니다: {power_kw}"
            )

        group_key = (
            station_name,
            direction,
            power_kw,
            chger_type,
        )

        if group_key not in grouped:
            grouped[group_key] = {
                "station_name": station_name,
                "direction": direction,
                "power_kw": power_kw,
                "chger_type": chger_type,
                "n_units": 0,
            }

        grouped[group_key]["n_units"] += 1

    return list(grouped.values())

def haversine_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """두 위경도 좌표 사이의 지표면 직선거리를 km로 계산한다."""

    earth_radius_km = 6371.0

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)

    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1))
        * cos(radians(lat2))
        * sin(dlon / 2) ** 2
    )

    c = 2 * atan2(
        sqrt(a),
        sqrt(1 - a),
    )

    return earth_radius_km * c


def add_offset_km(
    station_candidates: list[dict],
    origins: dict[str, tuple[float, float]],
) -> list[dict]:
    """
    방향별 기점과 휴게소 좌표 사이의 직선거리를 offset_km로 추가한다.

    origins 예시:
        {
            "DOWN": (서울_기점_위도, 서울_기점_경도),
            "UP": (부산_기점_위도, 부산_기점_경도),
        }
    """

    result: list[dict] = []

    for station in station_candidates:
        direction = station["direction"]

        if direction not in origins:
            raise RuntimeError(
                f"기점 좌표가 없습니다: {direction}"
            )

        origin_lat, origin_lon = origins[direction]

        offset_km = haversine_km(
            origin_lat,
            origin_lon,
            station["lat"],
            station["lon"],
        )

        station_with_offset = station.copy()
        station_with_offset["offset_km"] = round(
            offset_km,
            3,
        )

        result.append(station_with_offset)

    return result

def fetch_gyeongbu_interchanges(api_key: str) -> list[dict]:
    """한국도로공사 API에서 경부선 IC/JCT 위치 목록을 가져온다."""

    params = {
        "key": api_key,
        "type": "json",
        "routeNo": GYEONGBU_ROUTE_NO,
        "numOfRows": "1000",
        "pageNo": "1",
    }

    url = EX_IC_API_URL + "?" + urlencode(params)

    try:
        with urlopen(url, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"한국도로공사 IC API HTTP 오류: {exc.code}"
        ) from None
    except URLError:
        raise RuntimeError(
            "한국도로공사 IC API 연결에 실패했습니다."
        ) from None

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError(
            "한국도로공사 IC API 응답이 올바른 JSON이 아닙니다."
        ) from None

    if data.get("code") != "SUCCESS":
        raise RuntimeError(
            "한국도로공사 IC API 오류: "
            f"{data.get('message', '알 수 없는 오류')}"
        )

    interchanges = data.get("list") or []

    if not isinstance(interchanges, list):
        raise RuntimeError(
            "한국도로공사 IC API의 list 구조가 예상과 다릅니다."
        )

    # 경부선 이외의 IC/JCT가 섞였는지 확인
    invalid = [
        item
        for item in interchanges
        if item.get("routeNo") != GYEONGBU_ROUTE_NO
    ]

    if invalid:
        raise RuntimeError(
            "경부선 이외의 IC/JCT가 API 응답에 포함되어 있습니다."
        )

    # API가 알려준 전체 건수와 실제 수집 건수 비교
    expected_count = int(
        data.get("count", len(interchanges))
    )

    if len(interchanges) != expected_count:
        raise RuntimeError(
            "한국도로공사 경부선 IC/JCT를 모두 가져오지 못했습니다. "
            f"expected={expected_count}, actual={len(interchanges)}"
        )

    return interchanges

def get_offset_origins(
    interchanges: list[dict],
) -> dict[str, tuple[float, float]]:
    """경부선 상·하행 offset_km 계산에 사용할 기점 좌표를 반환한다."""

    origin_codes = {
        "DOWN": DOWN_ORIGIN_IC_CODE,
        "UP": UP_ORIGIN_IC_CODE,
    }

    by_code = {
        item.get("icCode"): item
        for item in interchanges
    }

    origins: dict[str, tuple[float, float]] = {}

    for direction, ic_code in origin_codes.items():
        if ic_code not in by_code:
            raise RuntimeError(
                f"{direction} 기점 IC를 찾을 수 없습니다: {ic_code}"
            )

        item = by_code[ic_code]

        try:
            lat = float(item["yValue"])
            lon = float(item["xValue"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                f"기점 IC 좌표가 올바르지 않습니다: "
                f"{item.get('icName')} / {ic_code}"
            ) from None

        origins[direction] = (lat, lon)

    return origins

def build_station_rows(
    station_candidates: list[dict],
) -> list[dict]:
    """offset_km 계산이 끝난 station 후보를 DB station 행으로 변환한다."""

    rows: list[dict] = []
    seen_ids: set[str] = set()

    for station in station_candidates:
        direction = station["direction"]
        service_area_code = station.get("service_area_code")

        if not service_area_code:
            raise RuntimeError(
                f"serviceAreaCode가 없습니다: {station['name']}"
            )

        if "offset_km" not in station:
            raise RuntimeError(
                f"offset_km가 없습니다: {station['name']}"
            )

        corridor_id = f"gyeongbu_{direction.lower()}"

        station_id = (
            f"{corridor_id}_{service_area_code.lower()}"
        )

        if station_id in seen_ids:
            raise RuntimeError(
                f"station_id가 중복됩니다: {station_id}"
            )

        rows.append(
            {
                "station_id": station_id,
                "corridor_id": corridor_id,
                "cell_id": None,
                "name": station["name"],
                "direction": direction,
                "offset_km": station["offset_km"],
                "lat": station["lat"],
                "lon": station["lon"],
                "n_parking": None,
                "source": "ex_api",
                "source_key": service_area_code,
                "collected_at": None,
            }
        )

        seen_ids.add(station_id)

    return rows

def build_charger_rows(
    charger_candidates: list[dict],
    station_rows: list[dict],
) -> list[dict]:
    """charger 후보를 DB charger 테이블 형태의 행으로 변환한다."""

    station_by_key = {
        (
            station["name"],
            station["direction"],
        ): station
        for station in station_rows
    }

    rows: list[dict] = []
    seen_ids: set[str] = set()

    for charger in charger_candidates:
        station_key = (
            charger["station_name"],
            charger["direction"],
        )

        if station_key not in station_by_key:
            raise RuntimeError(
                f"station을 찾을 수 없습니다: {station_key}"
            )

        station = station_by_key[station_key]
        station_id = station["station_id"]

        chger_type = charger["chger_type"]

        if chger_type not in CHARGER_TYPE_MAP:
            raise RuntimeError(
                f"알 수 없는 chgerType입니다: {chger_type}"
            )

        connector_type = CHARGER_TYPE_MAP[chger_type]
        power_kw = charger["power_kw"]

        # 200.0 -> "200"
        # 150.5 -> "150p5"
        power_token = (
            f"{power_kw:g}"
            .replace(".", "p")
        )

        charger_id = (
            f"{station_id}_{power_token}kw_t{chger_type}"
        )

        if charger_id in seen_ids:
            raise RuntimeError(
                f"charger_id가 중복됩니다: {charger_id}"
            )

        rows.append(
            {
                "charger_id": charger_id,
                "station_id": station_id,
                "power_kw": power_kw,
                "connector_type": connector_type,
                "n_units": charger["n_units"],
                "is_active": 1,
                "source": "moe_api",
                "source_key": None,
            }
        )

        seen_ids.add(charger_id)

    return rows