from pathlib import Path

import pandas as pd

from evdt.io.charger_ingest import (
    add_offset_km,
    build_charger_candidates,
    build_charger_rows,
    build_station_candidates,
    build_station_rows,
    fetch_gyeongbu_interchanges,
    fetch_gyeongbu_rest_areas,
    filter_gyeongbu_chargers,
    get_offset_origins,
    load_ex_api_key,
    load_raw_chargers,
)
from evdt.io.db import get_conn, upsert_df
from evdt.paths import default_db_path


RAW_DIR = Path(
    "data/raw/highway_chargers_20260917_081326_all"
)


def main() -> None:
    # 1. 환경부 raw 충전기 데이터
    raw_chargers = load_raw_chargers(RAW_DIR)

    # 2. 도로공사 경부선 휴게소
    api_key = load_ex_api_key()
    rest_areas = fetch_gyeongbu_rest_areas(api_key)

    # 3. 경부선 충전기만 필터링
    matched_chargers, unmatched, unknown_direction = (
        filter_gyeongbu_chargers(
            raw_chargers,
            rest_areas,
        )
    )

    # 4. station 후보 생성
    stations = build_station_candidates(
        matched_chargers,
        rest_areas,
    )

    # 5. 방향별 기점 조회 후 offset_km 계산
    interchanges = fetch_gyeongbu_interchanges(api_key)
    origins = get_offset_origins(interchanges)

    stations = add_offset_km(
        stations,
        origins,
    )

    # 6. DB station 행 생성
    station_rows = build_station_rows(stations)

    # 7. charger 후보 생성
    charger_candidates = build_charger_candidates(
        matched_chargers,
        stations,
    )

    # 8. DB charger 행 생성
    charger_rows = build_charger_rows(
        charger_candidates,
        station_rows,
    )

    # 적재 전 검증
    if len(station_rows) != 33:
        raise RuntimeError(
            f"예상 station 수는 33인데 {len(station_rows)}개입니다."
        )

    n_units = sum(
        row["n_units"]
        for row in charger_rows
    )

    if n_units <= 0:
        raise RuntimeError(
            "충전기 데이터가 없습니다."
        )

    station_df = pd.DataFrame(station_rows)
    charger_df = pd.DataFrame(charger_rows)

    db_path = default_db_path()

    # 9. SQLite 적재
    with get_conn(db_path) as conn:
        # station이 corridor를 FK로 참조하므로
        # corridor가 먼저 존재하는지 확인
        corridor_ids = {
            row["corridor_id"]
            for row in conn.execute(
                """
                SELECT corridor_id
                FROM corridor
                WHERE corridor_id IN (
                    'gyeongbu_down',
                    'gyeongbu_up'
                )
                """
            )
        }

        required_corridors = {
            "gyeongbu_down",
            "gyeongbu_up",
        }

        missing = required_corridors - corridor_ids

        if missing:
            raise RuntimeError(
                f"DB에 corridor가 없습니다: {sorted(missing)}"
            )

        # FK 때문에 station 먼저
        upsert_df(
            conn,
            "station",
            station_df,
        )

        # charger는 station을 참조하므로 그 다음
        upsert_df(
            conn,
            "charger",
            charger_df,
        )

        # 10. 실제 DB 결과 확인
        station_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM station
            WHERE corridor_id IN (
                'gyeongbu_down',
                'gyeongbu_up'
            )
            """
        ).fetchone()[0]

        charger_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM charger c
            JOIN station s
              ON s.station_id = c.station_id
            WHERE s.corridor_id IN (
                'gyeongbu_down',
                'gyeongbu_up'
            )
            """
        ).fetchone()[0]

        db_n_units = conn.execute(
            """
            SELECT COALESCE(SUM(c.n_units), 0)
            FROM charger c
            JOIN station s
              ON s.station_id = c.station_id
            WHERE s.corridor_id IN (
                'gyeongbu_down',
                'gyeongbu_up'
            )
            """
        ).fetchone()[0]

    print()
    print("=== 경부선 충전 인프라 적재 완료 ===")
    print("DB:", db_path)
    print("station:", station_count)
    print("charger groups:", charger_count)
    print("physical chargers:", db_n_units)

    print()
    print("매칭 제외 directional 휴게소:")
    for area in unmatched:
        print(" -", area.get("unitName"))

    print()
    print("방향 미확정 휴게소:")
    for area in unknown_direction:
        print(" -", area.get("unitName"))


if __name__ == "__main__":
    main()