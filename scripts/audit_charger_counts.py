from collections import defaultdict
from pathlib import Path

from evdt.io.charger_ingest import (
    load_raw_chargers,
    station_match_key,
)
from evdt.io.db import get_conn
from evdt.paths import default_db_path


RAW_DIR = Path(
    "data/raw/highway_chargers_20260917_081326_all"
)


def main() -> None:
    raw = load_raw_chargers(RAW_DIR)

    # 우리가 실제 시뮬레이션에 사용하는 33개 휴게소
    with get_conn(default_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT
                s.name,
                s.direction,
                s.offset_km,
                v.n_chargers
            FROM station s
            JOIN v_station_capacity v
              ON v.station_id = s.station_id
            WHERE s.corridor_id IN (
                'gyeongbu_down',
                'gyeongbu_up'
            )
            ORDER BY s.direction, s.offset_km
            """
        ).fetchall()

    stations = [dict(row) for row in rows]

    # 방향별로 33개 휴게소를 나눠둔다.
    stations_by_direction = {
        "DOWN": [],
        "UP": [],
    }

    for station in stations:
        stations_by_direction[station["direction"]].append(station)

    # 휴게소별 고유 충전기 (statId, chgerId)
    raw_chargers = defaultdict(set)

    # 어떤 환경부 이름으로 매칭됐는지도 기록
    raw_names = defaultdict(set)

    # exact가 아닌 suffix 매칭은 따로 기록
    suffix_matches = defaultdict(set)

    for charger in raw:
        # 삭제된 충전기는 제외
        if charger.get("delYn") == "Y":
            continue

        raw_name = charger.get("statNm", "")
        normalized_name, direction = station_match_key(raw_name)

        if direction not in stations_by_direction:
            continue

        candidates = stations_by_direction[direction]

        # 1. 기존 방식: 정확히 같은 이름
        exact = [
            station
            for station in candidates
            if normalized_name == station["name"]
        ]

        if exact:
            station = exact[0]

        else:
            # 2. 검증용 보완:
            # 워터칠곡휴게소 -> 칠곡휴게소
            suffix = [
                station
                for station in candidates
                if normalized_name.endswith(station["name"])
            ]

            if not suffix:
                continue

            # 혹시 여러 개가 걸리면 가장 긴 이름 우선
            station = max(
                suffix,
                key=lambda x: len(x["name"]),
            )

            suffix_matches[
                (station["name"], direction)
            ].add(raw_name)

        stat_id = charger.get("statId")
        chger_id = charger.get("chgerId")

        if not stat_id or not chger_id:
            continue

        key = (
            station["name"],
            direction,
        )

        raw_chargers[key].add(
            (stat_id, chger_id)
        )

        raw_names[key].add(raw_name)

    print()
    print("=== 휴게소별 충전기 대수 비교 ===")
    print()

    db_total = 0
    raw_total = 0

    for station in stations:
        key = (
            station["name"],
            station["direction"],
        )

        db_count = station["n_chargers"]
        public_count = len(raw_chargers[key])
        diff = public_count - db_count

        db_total += db_count
        raw_total += public_count

        marker = ""

        if diff != 0:
            marker = "  <-- 차이"

        print(
            f"{station['direction']:4} "
            f"{station['name']:<14} "
            f"DB={db_count:2} "
            f"공개데이터={public_count:2} "
            f"차이={diff:+3}"
            f"{marker}"
        )

    print()
    print("=== 총합 ===")
    print("현재 DB:", db_total)
    print("환경부 공개데이터:", raw_total)
    print("차이:", raw_total - db_total)

    print()
    print("=== 추가로 잡힌 운영사명 포함 충전소 ===")

    if not suffix_matches:
        print("없음")
    else:
        for (name, direction), source_names in sorted(
            suffix_matches.items()
        ):
            print(f"{direction} {name}")

            for source_name in sorted(source_names):
                print(f"  - {source_name}")


if __name__ == "__main__":
    main()