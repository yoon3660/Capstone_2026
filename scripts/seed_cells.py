"""경부선 CTM 셀 생성 및 DB 적재.

검증만:
    python scripts/seed_cells.py

검증 후 DB 저장:
    python scripts/seed_cells.py --write
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd
import yaml

from evdt.io.db import get_conn, upsert_df
from evdt.io.flow_params import q_per_lane
from evdt.io.lane_profile import lane_change_points
from evdt.io.route import GyeongbuRoute
from evdt.world.cell_split import (
    assign_lanes_to_cells,
    resolve_short_anchor_gaps,
    split_anchor_intervals,
)
from evdt.world.geometry import Polyline

ROOT = Path(__file__).resolve().parents[1]
DIRECTIONS = ("UP", "DOWN")
EPS = 1e-8


def build_cells() -> tuple[pd.DataFrame, pd.DataFrame, float, float]:
    """셀을 생성하고 검증한다. DB는 수정하지 않는다."""

    route = GyeongbuRoute.load()
    length_km = float(route.length_km)

    line = Polyline.from_points(
        route.points,
        offsets=route.mileposts,
    )

    config_path = ROOT / "config" / "flow_params.yaml"

    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    dt_min = float(config["dt_min"])
    min_length = float(config["defaults"]["min_cell_length_km"])

    if (
        not math.isfinite(dt_min)
        or not math.isfinite(min_length)
        or dt_min <= 0
        or min_length <= 0
    ):
        raise ValueError("dt_min 또는 최소 셀 길이가 올바르지 않습니다.")

    profile = pd.read_parquet(
        ROOT / "data" / "processed" / "lanes_gyeongbu.parquet"
    )

    changes = lane_change_points(profile)

    # 읽기 전용으로만 DB 접근
    with get_conn(readonly=True) as conn:
        stations = pd.read_sql_query(
            "SELECT direction, offset_km FROM station",
            conn,
        )

        corridors = pd.read_sql_query(
            """
            SELECT corridor_id, length_km
            FROM corridor
            WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')
            """,
            conn,
        )

    # 셀을 저장할 상행·하행 corridor가 이미 준비되어 있어야 한다.
    expected_ids = {"gyeongbu_up", "gyeongbu_down"}

    if set(corridors["corridor_id"]) != expected_ids:
        raise ValueError(
            "상행·하행 corridor가 없습니다. "
            "먼저 corridor 적재 상태를 확인하세요."
        )

    for corridor in corridors.itertuples(index=False):
        if abs(float(corridor.length_km) - length_km) > 0.001:
            raise ValueError(
                f"{corridor.corridor_id}: "
                "DB 노선 길이와 경로 파일 길이가 다릅니다."
            )

    rows = []

    for direction in DIRECTIONS:
        lane = profile[profile["direction"] == direction]
        station = stations[stations["direction"] == direction]
        change = changes[changes["direction"] == direction]

        if lane.empty or station.empty:
            raise ValueError(f"{direction}: 차로 또는 휴게소 데이터가 없습니다.")

        params = config[direction]

        v_free = float(params["v_free_kmh"]["value"])
        w_back = float(params["w_back_kmh"]["value"])
        k_jam = float(params["k_jam_veh_km_lane"]["value"])

        if not all(
            math.isfinite(x) and x > 0
            for x in (v_free, w_back, k_jam)
        ):
            raise ValueError(f"{direction}: 교통류 파라미터가 올바르지 않습니다.")

        # CFL: 한 시간 스텝 동안 셀 하나 이상을 건너뛰지 않아야 한다.
        cfl_length = max(v_free, w_back) * dt_min / 60.0

        if min_length < cfl_length - EPS:
            raise ValueError(
                f"{direction}: CFL 조건 위반 "
                f"(최소 셀 {min_length}km, 필요 {cfl_length:.6f}km)"
            )

        # 휴게소 경계를 보존하고 가까운 차로 변경 경계를 제거한다.
        anchors, removed = resolve_short_anchor_gaps(
            length_km=length_km,
            station_offsets=station["offset_km"].tolist(),
            lane_change_offsets=change["offset_km"].tolist(),
            min_cell_length_km=min_length,
        )

        intervals = split_anchor_intervals(
            anchors,
            min_length,
        )

        # 제거된 경계를 가로지르는 셀에는 적은 쪽 차로수를 적용한다.
        assigned = assign_lanes_to_cells(
            intervals,
            lane.to_dict("records"),
        )

        corridor_id = f"gyeongbu_{direction.lower()}"

        for seq, cell in enumerate(assigned):
            start = float(cell["offset_km_start"])
            end = float(cell["offset_km_end"])
            cell_length = end - start

            if cell_length < max(min_length, cfl_length) - EPS:
                raise ValueError(
                    f"{direction} seq={seq}: 셀이 너무 짧습니다."
                )

            # route 좌표는 구서IC → 양재IC 기준이다.
            # 하행은 offset 방향이 반대이므로 변환한다.
            if direction == "UP":
                route_start = start
                route_end = end
            else:
                route_start = length_km - start
                route_end = length_km - end

            lat_start, lon_start = line.point_at(route_start)
            lat_end, lon_end = line.point_at(route_end)

            lanes = int(cell["lanes"])

            rows.append(
                {
                    "cell_id": f"{corridor_id}_{seq:04d}",
                    "corridor_id": corridor_id,
                    "seq": seq,
                    "offset_km_start": start,
                    "offset_km_end": end,
                    "length_km": cell_length,
                    "lanes": lanes,
                    "lanes_source": cell["lanes_source"],
                    "v_free_kmh": v_free,
                    "w_back_kmh": w_back,
                    "k_jam_veh_km_lane": k_jam,
                    "q_max_veh_h": lanes
                    * q_per_lane(v_free, w_back, k_jam),
                    "lat_start": lat_start,
                    "lon_start": lon_start,
                    "lat_end": lat_end,
                    "lon_end": lon_end,
                }
            )

        print(f"\n[{direction}]")
        print("생성된 셀:", len(assigned))
        print("제거된 차로 변경 경계:", removed)
        print("CFL 필요 길이(km):", round(cfl_length, 6))

    df = pd.DataFrame(rows)

    # 최종 검증: ID 중복, 시작·끝, 연속성, 길이, 휴게소 경계
    if df.empty or df["cell_id"].duplicated().any():
        raise ValueError("셀 데이터가 비었거나 cell_id가 중복되었습니다.")

    for direction in DIRECTIONS:
        corridor_id = f"gyeongbu_{direction.lower()}"

        part = (
            df[df["corridor_id"] == corridor_id]
            .sort_values("seq")
            .reset_index(drop=True)
        )

        if part["seq"].tolist() != list(range(len(part))):
            raise ValueError(f"{direction}: seq가 연속적이지 않습니다.")

        if abs(float(part.iloc[0]["offset_km_start"])) > EPS:
            raise ValueError(f"{direction}: 노선 시작점이 0이 아닙니다.")

        if abs(float(part.iloc[-1]["offset_km_end"]) - length_km) > EPS:
            raise ValueError(f"{direction}: 노선 끝점이 일치하지 않습니다.")

        if abs(float(part["length_km"].sum()) - length_km) > 0.001:
            raise ValueError(f"{direction}: 셀 길이 합계가 일치하지 않습니다.")

        if (part["length_km"] < min_length - EPS).any():
            raise ValueError(f"{direction}: 최소 길이 미만 셀이 존재합니다.")

        for i in range(len(part) - 1):
            previous_end = float(part.iloc[i]["offset_km_end"])
            next_start = float(part.iloc[i + 1]["offset_km_start"])

            if abs(previous_end - next_start) > EPS:
                raise ValueError(
                    f"{direction}: seq {i}와 {i + 1} 사이에 "
                    "빈틈 또는 중복이 있습니다."
                )

        # 모든 휴게소가 셀 경계에 있어야 한다.
        boundaries = (
            part["offset_km_start"].tolist()
            + part["offset_km_end"].tolist()
        )

        station_offsets = stations.loc[
            stations["direction"] == direction,
            "offset_km",
        ]

        for offset in station_offsets:
            if not any(
                abs(float(offset) - boundary) < 1e-6
                for boundary in boundaries
            ):
                raise ValueError(
                    f"{direction}: 휴게소 {offset}km가 "
                    "셀 경계에 없습니다."
                )

    coordinate_columns = (
        "lat_start",
        "lon_start",
        "lat_end",
        "lon_end",
    )

    for column in coordinate_columns:
        if not df[column].map(math.isfinite).all():
            raise ValueError(f"{column}: 유효하지 않은 좌표가 있습니다.")

    return df, stations, length_km, min_length


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="검증에 성공한 셀을 DB에 저장합니다.",
    )
    args = parser.parse_args()

    df, stations, length_km, min_length = build_cells()

    print("\n===== 최종 검증 통과 =====")
    print("전체 셀 수:", len(df))
    print("노선 길이(km):", length_km)
    print("최소 허용 셀 길이(km):", min_length)
    print("실제 최소 셀 길이(km):", df["length_km"].min())
    print("휴게소 수:", len(stations))

    if not args.write:
        print("\n검증 전용 실행입니다. DB는 수정하지 않았습니다.")
        return

    # 기존 셀이 있으면 무조건 덮어쓰지 않는다.
    # 셀 수가 달라졌을 때 오래된 행이 남는 사고를 막기 위해서다.
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT COUNT(*)
            FROM cell
            WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')
            """
        ).fetchone()[0]

        if existing != 0:
            raise ValueError(
                f"기존 셀 {existing}개가 있습니다. "
                "자동으로 덮어쓰지 않았습니다."
            )

        inserted = upsert_df(conn, "cell", df)

        saved = conn.execute(
            """
            SELECT COUNT(*)
            FROM cell
            WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')
            """
        ).fetchone()[0]

        if saved != len(df):
            raise ValueError(
                f"DB 저장 건수 불일치: 예상 {len(df)}, 실제 {saved}"
            )

    print(f"\nDB 저장 완료: {inserted}개 셀")


if __name__ == "__main__":
    main()