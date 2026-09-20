"""경부선 CTM 셀 생성 및 DB 적재 (T-17).

검증만:
    python scripts/seed_cells.py

검증 후 DB 저장 (셀이 비어 있을 때):
    python scripts/seed_cells.py --write

기존 셀을 지우고 다시 저장:
    python scripts/seed_cells.py --write --replace

분할 규칙은 src/evdt/world/cell_split.py 에 있다. 여기서는 DB·파일을 읽어서 넘기고,
결과를 검증하고, 좌표를 붙여 저장만 한다.
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
from evdt.io.route import GyeongbuRoute
from evdt.io.stations import read_station_chargers
from evdt.world.cell_split import build_direction_cells, cfl_min_cell_km
from evdt.world.geometry import Polyline

ROOT = Path(__file__).resolve().parents[1]
DIRECTIONS = ("UP", "DOWN")
EPS = 1e-8


def corridor_id_of(direction: str) -> str:
    return f"gyeongbu_{direction.lower()}"


def build_cells() -> tuple[pd.DataFrame, dict]:
    """셀을 생성하고 검증한다. DB 는 수정하지 않는다."""

    route = GyeongbuRoute.load()
    length_km = float(route.length_km)
    line = Polyline.from_points(route.points, offsets=route.mileposts)

    config = yaml.safe_load((ROOT / "config" / "flow_params.yaml").read_text(encoding="utf-8"))
    dt_min = float(config["dt_min"])
    cell_length_km = float(config["defaults"]["cell_length_km"])

    profile = pd.read_parquet(ROOT / "data" / "processed" / "lanes_gyeongbu.parquet")

    with get_conn(readonly=True) as conn:
        corridors = {
            row[0]: float(row[1])
            for row in conn.execute(
                "SELECT corridor_id, length_km FROM corridor"
                " WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')"
            )
        }

        # 코리도별로 읽는다. read_station_chargers 가 가짜 휴게소(smoke)가 섞여 있으면
        # 멈춘다 — 예전에 station 테이블 전체를 읽다가 가짜 3곳이 앵커가 됐다.
        stations = {
            direction: read_station_chargers(conn, corridor_id=corridor_id_of(direction))[0]
            for direction in DIRECTIONS
        }

    if set(corridors) != {"gyeongbu_up", "gyeongbu_down"}:
        raise ValueError("상행·하행 corridor 가 없습니다. 먼저 corridor 적재 상태를 확인하세요.")

    # 이정축이 사람마다 어긋나면(docs/debug_lanes_log.md §3-①) 여기서 잡힌다.
    for corridor_id, corridor_length in corridors.items():
        if abs(corridor_length - length_km) > 0.001:
            raise ValueError(
                f"{corridor_id}: DB 노선 길이 {corridor_length} km 와 "
                f"경로 파일 {length_km:.3f} km 가 다릅니다. build_route.py 를 다시 돌렸는지 확인할 것."
            )

    rows = []
    report: dict = {"length_km": length_km, "cell_length_km": cell_length_km, "dt_min": dt_min}

    for direction in DIRECTIONS:
        corridor_id = corridor_id_of(direction)
        lane = profile[profile["direction"] == direction]
        station_offsets = [float(s["offset_km"]) for s in stations[direction]]

        if lane.empty or not station_offsets:
            raise ValueError(f"{direction}: 차로 또는 휴게소 데이터가 없습니다.")

        params = config[direction]
        v_free = float(params["v_free_kmh"]["value"])
        w_back = float(params["w_back_kmh"]["value"])
        k_jam = float(params["k_jam_veh_km_lane"]["value"])

        # CFL: 한 스텝 동안 셀 하나 이상을 건너뛰지 않아야 한다.
        cfl_floor = cfl_min_cell_km(v_free, w_back, dt_min)

        cells, dropped = build_direction_cells(
            length_km,
            station_offsets,
            lane.to_dict("records"),
            cell_length_km=cell_length_km,
            cfl_floor_km=cfl_floor,
        )

        for seq, cell in enumerate(cells):
            start = float(cell["offset_km_start"])
            end = float(cell["offset_km_end"])

            # 경로 좌표는 구서IC → 양재IC 기준이다. 하행 offset 은 서울 쪽이 0 이라 뒤집는다.
            route_start, route_end = (start, end) if direction == "UP" else (
                length_km - start,
                length_km - end,
            )
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
                    "length_km": end - start,
                    "lanes": lanes,
                    "lanes_source": cell["lanes_source"],
                    "v_free_kmh": v_free,
                    "w_back_kmh": w_back,
                    "k_jam_veh_km_lane": k_jam,
                    "q_max_veh_h": lanes * q_per_lane(v_free, w_back, k_jam),
                    "lat_start": lat_start,
                    "lon_start": lon_start,
                    "lat_end": lat_end,
                    "lon_end": lon_end,
                }
            )

        short = [c for c in cells if c["length_km"] < cell_length_km - EPS]
        report[direction] = {
            "cells": len(cells),
            "stations": len(station_offsets),
            "cfl_floor_km": cfl_floor,
            "dropped_lane_changes": dropped,
            "short_cells": [(c["offset_km_start"], c["offset_km_end"]) for c in short],
        }

    df = pd.DataFrame(rows)
    validate_cells(df, stations, length_km, report)
    return df, report


def validate_cells(df: pd.DataFrame, stations: dict, length_km: float, report: dict) -> None:
    """완료조건을 실제 데이터로 한 번 더 검사한다. 같은 규칙을 tests/test_cell_split.py
    가 합성 데이터로 검사한다 — 이쪽은 실제 데이터에서만, 저쪽은 CI 에서 돈다."""

    if df.empty or df["cell_id"].duplicated().any():
        raise ValueError("셀 데이터가 비었거나 cell_id 가 중복되었습니다.")

    for direction in DIRECTIONS:
        part = (
            df[df["corridor_id"] == corridor_id_of(direction)]
            .sort_values("seq")
            .reset_index(drop=True)
        )
        cfl_floor = report[direction]["cfl_floor_km"]

        if part["seq"].tolist() != list(range(len(part))):
            raise ValueError(f"{direction}: seq 가 연속적이지 않습니다.")

        if abs(float(part.iloc[0]["offset_km_start"])) > EPS:
            raise ValueError(f"{direction}: 노선 시작점이 0 이 아닙니다.")

        if abs(float(part.iloc[-1]["offset_km_end"]) - length_km) > EPS:
            raise ValueError(f"{direction}: 노선 끝점이 일치하지 않습니다.")

        if abs(float(part["length_km"].sum()) - length_km) > 0.001:
            raise ValueError(f"{direction}: 셀 길이 합계가 노선 길이와 1 m 이상 다릅니다.")

        if (part["length_km"] < cfl_floor - EPS).any():
            raise ValueError(f"{direction}: CFL 하한 {cfl_floor:.3f} km 보다 짧은 셀이 있습니다.")

        ends = part["offset_km_end"].to_numpy()[:-1]
        starts = part["offset_km_start"].to_numpy()[1:]

        if (abs(ends - starts) > EPS).any():
            raise ValueError(f"{direction}: 인접 셀 사이에 빈틈 또는 중복이 있습니다.")

        boundaries = set(part["offset_km_start"]) | set(part["offset_km_end"])

        for station in stations[direction]:
            offset = float(station["offset_km"])

            if not any(abs(offset - b) < 1e-6 for b in boundaries):
                raise ValueError(f"{direction}: 휴게소 {station['name']} {offset} km 가 셀 경계에 없습니다.")

    for column in ("lat_start", "lon_start", "lat_end", "lon_end"):
        if not df[column].map(math.isfinite).all():
            raise ValueError(f"{column}: 유효하지 않은 좌표가 있습니다.")


def print_report(df: pd.DataFrame, report: dict) -> None:
    print(f"노선 길이 {report['length_km']:.3f} km  ·  셀 목표 {report['cell_length_km']} km"
          f"  ·  dt {report['dt_min']}분 ({int(1440 / report['dt_min'])} 스텝/일)")

    for direction in DIRECTIONS:
        r = report[direction]
        part = df[df["corridor_id"] == corridor_id_of(direction)]
        print(f"\n[{direction}] 셀 {r['cells']}개  (휴게소 {r['stations']}곳)")
        print(f"  셀 길이 {part['length_km'].min():.3f} ~ {part['length_km'].max():.3f} km"
              f"  평균 {part['length_km'].mean():.3f} km  ·  CFL 하한 {r['cfl_floor_km']:.3f} km")

        if r["short_cells"]:
            print(f"  목표보다 짧은 셀 {len(r['short_cells'])}개 (휴게소를 경계에 두려고 허용):")
            for a, b in r["short_cells"]:
                print(f"    {a:.3f} ~ {b:.3f} km ({(b - a) * 1000:.0f} m)")

        if r["dropped_lane_changes"]:
            print(f"  이웃 앵커와 너무 가까워 버린 차로수 변경 지점 {len(r['dropped_lane_changes'])}곳"
                  " (그 셀은 적은 쪽 차로수):")
            print("    " + ", ".join(f"{x:.3f}" for x in r["dropped_lane_changes"]))

    total = len(df)
    steps = int(1440 / report["dt_min"])
    print(f"\n전체 {total} 셀  ·  하루 셀 업데이트 {steps * total:,} 회")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="검증에 성공한 셀을 DB 에 저장한다.")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="--write 와 함께. 기존 gyeongbu 셀을 지우고 다시 저장한다.",
    )
    args = parser.parse_args()

    if args.replace and not args.write:
        parser.error("--replace 는 --write 와 함께 써야 합니다.")

    df, report = build_cells()
    print("===== 최종 검증 통과 =====")
    print_report(df, report)

    if not args.write:
        print("\n검증 전용 실행입니다. DB 는 수정하지 않았습니다.")
        return

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM cell WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')"
        ).fetchone()[0]

        if existing and not args.replace:
            raise SystemExit(
                f"기존 셀 {existing}개가 있어 덮어쓰지 않았습니다.\n"
                "지우고 다시 저장하려면:  python scripts/seed_cells.py --write --replace"
            )

        # 셀 수가 바뀌면 upsert 만으로는 예전 행이 남는다. 지우고 넣는다 (한 트랜잭션).
        # station.cell_id 는 ON DELETE SET NULL 이라 휴게소 행은 지워지지 않는다.
        if existing:
            conn.execute("DELETE FROM cell WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')")

        upsert_df(conn, "cell", df)

        saved = conn.execute(
            "SELECT COUNT(*) FROM cell WHERE corridor_id IN ('gyeongbu_up', 'gyeongbu_down')"
        ).fetchone()[0]

        if saved != len(df):
            raise SystemExit(f"DB 저장 건수 불일치: 예상 {len(df)}, 실제 {saved}")

    action = f"기존 {existing}개를 지우고 " if existing else ""
    print(f"\nDB 저장 완료: {action}{saved}개 셀")


if __name__ == "__main__":
    main()
