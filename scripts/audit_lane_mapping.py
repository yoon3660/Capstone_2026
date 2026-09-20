"""경부고속도로 표준 노드링크의 전체 매핑 상태를 검사한다.

T-24 최종 차로수 parquet 생성 전 검증용:
- 본선 후보 링크 수
- 차로수 분포
- m 증가/감소 방향 분포
- 중심선 snap 거리 분포
- 이상 후보 링크 확인
"""

from __future__ import annotations

from collections import Counter

import shapefile
from pyproj import Transformer
import pandas as pd

import _bootstrap  # noqa: F401

from evdt.io.route import GyeongbuRoute
from evdt.paths import DATA_RAW_DIR


SHP_PATH = DATA_RAW_DIR / "nodelink" / "MOCT_LINK.shp"
CACHE_PATH = DATA_RAW_DIR.parent / "processed" / "lane_mapping_audit.parquet"

def percentile(values: list[float], p: float) -> float:
    """간단한 percentile 계산."""
    if not values:
        return 0.0

    values = sorted(values)

    index = int((len(values) - 1) * p)
    return values[index]


def main() -> int:
    sf = shapefile.Reader(
        str(SHP_PATH),
        encoding="cp949",
    )

    fields = [field[0] for field in sf.fields[1:]]
    idx = {name: i for i, name in enumerate(fields)}

    transformer = Transformer.from_crs(
        "EPSG:5186",
        "EPSG:4326",
        always_xy=True,
    )

    route = GyeongbuRoute.load()

    total = 0

    lane_counts = Counter()
    speed_counts = Counter()
    movement_counts = Counter()
    connect_counts = Counter()

    snap_values = []
    suspicious = []
    results = []

    reason_counts = Counter()

    overlap_counts = Counter()

    for shape_record in sf.iterShapeRecords():
        record = shape_record.record

        road_name = str(record[idx["ROAD_NAME"]]).strip()
        road_rank = str(record[idx["ROAD_RANK"]]).strip()
        road_no = str(record[idx["ROAD_NO"]]).strip()
        road_use = str(record[idx["ROAD_USE"]]).strip()
        connect = str(record[idx["CONNECT"]]).strip()
        f_node = str(record[idx["F_NODE"]]).strip()
        t_node = str(record[idx["T_NODE"]]).strip()
        road_type = str(record[idx["ROAD_TYPE"]]).strip()

        if not (
                road_name == "경부고속도로"
                and road_rank == "101"  # 고속국도
                and road_no == "1"  # 경부고속도로 노선번호
                and road_use == "0"  # 사용 중인 도로
        ):
            continue

        points = shape_record.shape.points

        if len(points) < 2:
            continue

        start_x, start_y = points[0]
        end_x, end_y = points[-1]

        start_lon, start_lat = transformer.transform(
            start_x,
            start_y,
        )

        end_lon, end_lat = transformer.transform(
            end_x,
            end_y,
        )

        # 지도 확인용 링크 대표 좌표
        mid_x, mid_y = points[len(points) // 2]

        mid_lon, mid_lat = transformer.transform(
            mid_x,
            mid_y,
        )

        m_start, dist_start = route.project(
            start_lat,
            start_lon,
        )

        m_end, dist_end = route.project(
            end_lat,
            end_lon,
        )

        link_id = str(record[idx["LINK_ID"]]).strip()
        lanes = int(record[idx["LANES"]])
        max_spd = int(record[idx["MAX_SPD"]])
        source_length_m = float(record[idx["LENGTH"]])

        if m_end > m_start:
            movement = "INCREASE"
        elif m_end < m_start:
            movement = "DECREASE"
        else:
            movement = "SAME"

        max_snap_m = max(
            dist_start,
            dist_end,
        ) * 1000

        projected_length_km = abs(m_end - m_start)

        # 오래 걸리는 중심선 매핑 결과를 캐시로 저장하기 위해 기록
        results.append(
            {
                "link_id": link_id,
                "lanes": lanes,
                "max_spd": max_spd,
                "road_name": road_name,
                "road_rank": road_rank,
                "road_no": road_no,
                "road_use": road_use,
                "connect": connect,

                "f_node": f_node,
                "t_node": t_node,
                "road_type": road_type,

                "start_lat": start_lat,
                "start_lon": start_lon,
                "end_lat": end_lat,
                "end_lon": end_lon,
                "mid_lat": mid_lat,
                "mid_lon": mid_lon,

                "m_start": m_start,
                "m_end": m_end,
                "movement": movement,

                "max_snap_m": max_snap_m,
                "projected_length_km": projected_length_km,
                "source_length_m": source_length_m,
            }
        )

        total += 1

        lane_counts[lanes] += 1
        speed_counts[max_spd] += 1
        movement_counts[movement] += 1
        connect_counts[connect] += 1

        snap_values.extend(
            [
                dist_start * 1000,
                dist_end * 1000,
            ]
        )

        # 조사 조건
        is_far = max_snap_m > 100
        is_same = movement == "SAME"
        is_low_lanes = lanes <= 2
        is_high_lanes = lanes > 6
        is_low_speed = max_spd < 80
        is_zero_length = projected_length_km < 0.001

        # 각 조건별 개수
        if is_far:
            reason_counts["snap > 100m"] += 1

        if is_same:
            reason_counts["movement == SAME"] += 1

        if is_low_lanes:
            reason_counts["lanes <= 2"] += 1

        if is_high_lanes:
            reason_counts["lanes > 6"] += 1

        if is_low_speed:
            reason_counts["speed < 80"] += 1

        if is_zero_length:
            reason_counts["length < 1m"] += 1

        # 조건들이 서로 얼마나 겹치는지 확인
        if is_far and is_same:
            overlap_counts["far AND same"] += 1

        if is_low_lanes and is_low_speed:
            overlap_counts["low_lanes AND low_speed"] += 1

        if is_low_lanes and not is_far:
            overlap_counts["low_lanes AND snap<=100m"] += 1

        if is_low_speed and not is_far:
            overlap_counts["low_speed AND snap<=100m"] += 1

        if is_same and not is_far:
            overlap_counts["same AND snap<=100m"] += 1

        # 아직 제거하지 않고 조사 대상으로만 기록
        if (
                is_far
                or is_same
                or is_low_lanes
                or is_high_lanes
                or is_low_speed
                or is_zero_length
        ):
            suspicious.append(
                {
                    "link_id": link_id,
                    "lanes": lanes,
                    "max_spd": max_spd,
                    "movement": movement,
                    "m_start": m_start,
                    "m_end": m_end,
                    "length_km": projected_length_km,
                    "max_snap_m": max_snap_m,

                    # Google 지도 확인용
                    "lat": mid_lat,
                    "lon": mid_lon,
                }
            )

    print()
    print("=== 경부고속도로 본선 후보 전체 검사 ===")
    print(f"노선 길이: {route.length_km:.3f} km")
    print(f"후보 링크 수: {total}")

    print()
    print("=== LANES 분포 ===")

    for lanes, count in sorted(lane_counts.items()):
        print(f"{lanes}차로: {count}")

    print()
    print("=== MAX_SPD 분포 ===")

    for speed, count in sorted(speed_counts.items()):
        print(f"{speed} km/h: {count}")

    print()
    print("=== 진행 방향 ===")

    for movement, count in sorted(movement_counts.items()):
        print(f"{movement}: {count}")

    print()
    print("=== CONNECT 분포 ===")

    for connect_value, count in sorted(connect_counts.items()):
        print(f"CONNECT={connect_value}: {count}")

    print()
    print("=== 중심선 SNAP 거리 ===")

    if snap_values:
        print(f"median : {percentile(snap_values, 0.50):.1f} m")
        print(f"p95    : {percentile(snap_values, 0.95):.1f} m")
        print(f"p99    : {percentile(snap_values, 0.99):.1f} m")
        print(f"max    : {max(snap_values):.1f} m")

    print()
    print("=== 조사 조건별 개수 ===")

    for reason, count in reason_counts.items():
        print(f"{reason}: {count}")

    print()
    print("=== 조사 조건 교집합 ===")

    for condition, count in overlap_counts.items():
        print(f"{condition}: {count}")

    print()
    print("=== SNAP > 100m 이지만 SAME은 아닌 링크 ===")

    far_moving = [
        item
        for item in suspicious
        if item["max_snap_m"] > 100
           and item["movement"] != "SAME"
    ]

    print(f"개수: {len(far_moving)}")

    for item in sorted(
            far_moving,
            key=lambda x: x["max_snap_m"],
            reverse=True,
    ):
        print(
            f"LINK={item['link_id']} "
            f"lanes={item['lanes']} "
            f"speed={item['max_spd']} "
            f"movement={item['movement']} "
            f"m={item['m_start']:.3f}->{item['m_end']:.3f} "
            f"length={item['length_km']:.3f}km "
            f"snap={item['max_snap_m']:.1f}m "
            f"GPS={item['lat']:.6f},{item['lon']:.6f}"
        )
    print()
    print("=== 조사 필요 링크 ===")
    print(f"개수: {len(suspicious)}")

    # 너무 많이 출력되지 않도록 우선 30개만
    for item in sorted(
        suspicious,
        key=lambda x: x["max_snap_m"],
        reverse=True,
    )[:30]:

        print(
            f"LINK={item['link_id']} "
            f"lanes={item['lanes']} "
            f"speed={item['max_spd']} "
            f"movement={item['movement']} "
            f"m={item['m_start']:.3f}->{item['m_end']:.3f} "
            f"length={item['length_km']:.3f}km "
            f"snap={item['max_snap_m']:.1f}m"
        )

    # 중심선 매핑 결과 캐시 저장
    CACHE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    df = pd.DataFrame(results)

    df.to_parquet(
        CACHE_PATH,
        index=False,
    )

    print()
    print("=== 매핑 결과 캐시 저장 ===")
    print(f"저장 위치: {CACHE_PATH}")
    print(f"저장 링크 수: {len(df)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())