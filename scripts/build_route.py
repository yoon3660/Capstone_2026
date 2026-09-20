"""경부선 노선 좌표계를 만들어 저장한다 (T-06 / T-07 공용).

    python scripts/build_route.py

도로공사 API 에서 경부선 IC/JCT, 휴게소, 전국 IC 목록을 받아
    data/raw/ex_route_<시각>/             원본 (ic_gyeongbu.json, rest_gyeongbu.json, ic_all.json)
    data/processed/gyeongbu_route.json    노선 폴리라인 + 누적거리
를 만든다. 휴게소 적재(load_chargers.py)와 교통량·속도 정리(build_traffic.py)가
이 노선 하나를 같이 쓴다. .env 의 EVDT_EX_API_KEY 가 필요하다.
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import urlopen

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd

from evdt.io.charger_ingest import (
    fetch_gyeongbu_interchanges,
    fetch_gyeongbu_rest_areas,
    get_offset_origins,
    load_ex_api_key,
)
from evdt.io.route import GyeongbuRoute
from evdt.paths import DATA_PROCESSED_DIR, DATA_RAW_DIR
from evdt.world.geometry import Polyline

EX_IC_API_URL = "https://data.ex.co.kr/openapi/locationinfo/locationinfoIc"

def build_centerline_route(
    origins: dict[str, tuple[float, float]],
) -> GyeongbuRoute:
    """보정된 중심선에서 구서IC~양재IC 구간의 공통 노선을 만든다."""

    path = DATA_PROCESSED_DIR / "centerline_gyeongbu.parquet"

    df = pd.read_parquet(path).sort_values("offset_km")

    line = Polyline.from_points(
        df[["lat", "lon"]].itertuples(index=False, name=None),
        offsets=df["offset_km"],
    )

    # 구서IC = 상행 기점, 양재IC = 하행 기점
    start, start_snap_km = line.offset_of(*origins["UP"])
    end, end_snap_km = line.offset_of(*origins["DOWN"])

    # 기점 좌표가 중심선에서 지나치게 멀면 조용히 진행하지 않는다.
    if start_snap_km > 0.25 or end_snap_km > 0.25:
        raise ValueError(
            "IC 기점의 중심선 스냅거리가 250m를 초과했습니다: "
            f"구서IC={start_snap_km * 1000:.1f}m, "
            f"양재IC={end_snap_km * 1000:.1f}m"
        )

    if start >= end:
        raise ValueError("구서IC와 양재IC의 이정 순서가 올바르지 않습니다.")

    # 두 IC 사이의 원본 중심선 점만 선택한다.
    inner = df[
        (df["offset_km"] > start)
        & (df["offset_km"] < end)
    ]

    # 양 끝은 IC를 중심선에 스냅한 정확한 위치를 삽입한다.
    points = (
        line.point_at(start),
        *inner[["lat", "lon"]].itertuples(index=False, name=None),
        line.point_at(end),
    )

    # 구서IC가 0km가 되도록 모든 이정을 변환한다.
    mileposts = (
        0.0,
        *(float(s) - start for s in inner["offset_km"]),
        end - start,
    )

    return GyeongbuRoute(
        points=tuple(points),
        mileposts=tuple(mileposts),
    )

def fetch_all_interchanges(api_key: str) -> list[dict]:
    """전국 IC/JCT 목록. 경부선 콘존 끝점 중 다른 노선 소속 JC(도동·금호 등)를 찾는 데 쓴다."""

    url = EX_IC_API_URL + "?" + urlencode(
        {"key": api_key, "type": "json", "numOfRows": "5000", "pageNo": "1"}
    )

    with urlopen(url, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8-sig"))

    items = data.get("list") or []

    if data.get("code") != "SUCCESS" or len(items) != int(data.get("count", -1)):
        raise RuntimeError(f"전국 IC 목록을 모두 받지 못했습니다: {data.get('message')}")

    return items


def main() -> int:
    api_key = load_ex_api_key()

    ics = fetch_gyeongbu_interchanges(api_key)
    rest_areas = fetch_gyeongbu_rest_areas(api_key)
    all_ics = fetch_all_interchanges(api_key)

    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    raw_dir = DATA_RAW_DIR / f"ex_route_{stamp}"
    raw_dir.mkdir(parents=True, exist_ok=False)

    for name, items in (
        ("ic_gyeongbu.json", ics),
        ("rest_gyeongbu.json", rest_areas),
        ("ic_all.json", all_ics),
    ):
        (raw_dir / name).write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # origins = get_offset_origins(ics)
    # route = GyeongbuRoute.build(
    #     origins,
    #     {(float(r["yValue"]), float(r["xValue"])) for r in rest_areas},
    #     ics,
    # )
    origins = get_offset_origins(ics)
    route = build_centerline_route(origins)

    path = route.save(
        raw_dir=raw_dir.name,
        n_interchanges=len(ics),
        n_rest_areas=len(rest_areas),
        origins={"UP": "구서IC 0010I00001", "DOWN": "양재IC 0010I00045"},
    )

    print("raw:", raw_dir)
    print("route:", path)
    print(f"노선 총연장 (구서IC~양재IC): {route.length_km:.1f} km, 점 {len(route.points)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
