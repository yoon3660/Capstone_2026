"""차종·충전곡선·온도효율을 DB 에 넣는다 (T-10 / T-11).

    python scripts/seed_vehicles.py
    python scripts/seed_vehicles.py --temp -10      # 다른 외기온으로 표를 출력

입력  config/vehicles.yaml, config/temp_efficiency.yaml  (값의 근거는 그 주석에)
출력  vehicle_class, charge_curve, temp_efficiency 테이블

적재 후 차종별 10→80 % / 20→80 % 충전시간을 계산해 공개 실측치와 나란히 출력한다
(T-10 완료 조건). 여기서 값이 이상하면 충전곡선을 의심할 것.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from evdt.io.db import get_conn, init_db, upsert_df, validate_master  # noqa: E402
from evdt.io.vehicles import (  # noqa: E402
    VEHICLES_PATH,
    curve_segments,
    read_temp_efficiency,
    read_vehicle_classes,
    temp_table,
)
from evdt.paths import default_db_path  # noqa: E402
from evdt.world.charging import charge_time_min, temp_factors  # noqa: E402

#: 표에 함께 보여줄 충전기 출력
REPORT_CHARGER_KW = 200.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--temp", type=float, default=-5.0, help="비교 출력에 쓸 외기온 (기본 -5)")
    ap.add_argument("--charger-kw", type=float, default=REPORT_CHARGER_KW)
    args = ap.parse_args()

    class_rows, curve_rows = read_vehicle_classes()
    temp_rows = read_temp_efficiency()

    db_path = args.db or default_db_path()
    init_db(db_path)

    with get_conn(db_path) as conn:
        upsert_df(conn, "vehicle_class", pd.DataFrame(class_rows))
        upsert_df(conn, "charge_curve", pd.DataFrame(curve_rows))
        upsert_df(conn, "temp_efficiency", pd.DataFrame(temp_rows))
        problems = validate_master(conn)

    print(f"DB: {db_path}")
    print(
        f"vehicle_class {len(class_rows)}종 / charge_curve {len(curve_rows)}구간 "
        f"/ temp_efficiency {len(temp_rows)}점"
    )

    if problems:
        for p in problems:
            print("  [FAIL]", p)
        return 1

    print("validate_master: 문제 없음 (share 합 1.0, 충전곡선 0~1 빈틈없음)")

    # --- 충전시간 대조 ------------------------------------------------------
    table = temp_table(temp_rows)
    _, cold_power_factor = temp_factors(args.temp, table)
    reference = {
        e["vclass_id"]: e["reference_10_80_min"]
        for e in yaml.safe_load(VEHICLES_PATH.read_text(encoding="utf-8"))["vehicle_classes"]
    }

    print(f"\n=== 충전시간 ({args.charger_kw:.0f}kW 충전기) ===")
    header = (
        f"{'차종':<34} {'배터리':>6} {'최대':>6} "
        f"{'10→80':>7} {'공개':>6} {'20→80':>7} {'20→80(-' + f'{abs(args.temp):.0f}' + '°C)':>12}"
    )
    print(header)

    for row in class_rows:
        curve = curve_segments(curve_rows, row["vclass_id"])
        args_common = (row["battery_kwh"], row["vmax_kw"], args.charger_kw, curve)
        t_10_80 = charge_time_min(0.1, 0.8, *args_common)
        t_20_80 = charge_time_min(0.2, 0.8, *args_common)
        t_cold = charge_time_min(0.2, 0.8, *args_common, charge_power_factor=cold_power_factor)
        ref = reference[row["vclass_id"]]
        mark = "" if abs(t_10_80 - ref) / ref <= 0.15 else "   <-- 실측과 15% 이상 차이"

        print(
            f"{row['name']:<34} {row['battery_kwh']:>5.0f}kWh {row['vmax_kw']:>5.0f}kW "
            f"{t_10_80:>6.1f}분 {ref:>5.0f}분 {t_20_80:>6.1f}분 {t_cold:>10.1f}분{mark}"
        )

    print(
        f"\n외기온 {args.temp:.0f}°C 충전출력 계수 {cold_power_factor:.2f} "
        "(시나리오 config 의 environment.temp_c 로 지정)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
