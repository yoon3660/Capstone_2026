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
import unicodedata
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

#: 표에 함께 보여줄 충전기 출력 (우리 휴게소에 가장 흔한 출력)
REPORT_CHARGER_KW = 200.0

#: 공개 실측치(10→80%)를 잰 조건. 비교 열은 반드시 이 출력으로 계산해야 한다.
REFERENCE_CHARGER_KW = 350.0

#: 실측치와 이만큼 넘게 벌어지면 표에 표시한다 (tests/test_charging.py 와 같은 값)
REFERENCE_TOLERANCE = 0.15


def _display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·전각 문자는 두 칸이다.

    파이썬 format 의 폭은 글자 수라서, 한글이 섞이면 표가 어긋난다.
    """

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, align: str = "<") -> str:
    """표시 폭 기준으로 채운다. 폭을 넘으면 자른다."""

    while _display_width(text) > width:
        text = text[:-1]

    space = " " * (width - _display_width(text))
    return text + space if align == "<" else space + text


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

    print(f"\n=== 충전시간 ===   (20→80% 는 {args.charger_kw:.0f}kW 충전기 기준)")

    columns = [
        ("차종", 44, "<"),
        ("배터리", 9, ">"),
        ("차량최대", 10, ">"),
        (f"10→80({REFERENCE_CHARGER_KW:.0f}kW)", 14, ">"),
        ("공개실측", 10, ">"),
        ("20→80", 8, ">"),
        (f"20→80({args.temp:.0f}°C)", 14, ">"),
    ]
    print(" ".join(_pad(title, width, align) for title, width, align in columns))
    print(" ".join("-" * width for _, width, _ in columns))

    for row in class_rows:
        curve = curve_segments(curve_rows, row["vclass_id"])
        vehicle = (row["battery_kwh"], row["vmax_kw"])

        # 공개 실측치는 350kW 충전기에서 잰 값이다. 같은 조건으로 계산해야 비교가 된다.
        t_10_80 = charge_time_min(0.1, 0.8, *vehicle, REFERENCE_CHARGER_KW, curve)
        t_20_80 = charge_time_min(0.2, 0.8, *vehicle, args.charger_kw, curve)
        t_cold = charge_time_min(
            0.2, 0.8, *vehicle, args.charger_kw, curve, charge_power_factor=cold_power_factor
        )
        ref = reference[row["vclass_id"]]
        gap = abs(t_10_80 - ref) / ref
        mark = "" if gap <= REFERENCE_TOLERANCE else f"   <-- 실측과 {gap:.0%} 차이"

        cells = [
            row["name"],
            f"{row['battery_kwh']:.0f}kWh",
            f"{row['vmax_kw']:.0f}kW",
            f"{t_10_80:.1f}분",
            f"{ref:.0f}분",
            f"{t_20_80:.1f}분",
            f"{t_cold:.1f}분",
        ]
        line = " ".join(
            _pad(cell, width, align) for cell, (_, width, align) in zip(cells, columns, strict=True)
        )
        print(line + mark)

    print(
        f"\n외기온 {args.temp:.0f}°C 충전출력 계수 {cold_power_factor:.2f} "
        "(시나리오 config 의 environment.temp_c 로 지정)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
