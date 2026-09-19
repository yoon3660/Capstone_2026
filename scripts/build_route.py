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

from evdt.io.charger_ingest import (
    fetch_gyeongbu_interchanges,
    fetch_gyeongbu_rest_areas,
    get_offset_origins,
    load_ex_api_key,
)
from evdt.io.route import GyeongbuRoute
from evdt.paths import DATA_RAW_DIR

EX_IC_API_URL = "https://data.ex.co.kr/openapi/locationinfo/locationinfoIc"


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

    origins = get_offset_origins(ics)
    route = GyeongbuRoute.build(
        origins,
        {(float(r["yValue"]), float(r["xValue"])) for r in rest_areas},
        ics,
    )
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
