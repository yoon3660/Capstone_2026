"""교통량에서 중간 진입·진출 프로파일을 만든다 (#54).

    python scripts/build_entry_exit_profile.py                      # 설 2026 하행
    python scripts/build_entry_exit_profile.py --direction UP
    python scripts/build_entry_exit_profile.py --date 2026-02-14

    data/processed/entry_exit_gyeongbu_down_seollal.csv

구간 교통량이 상류 콘존보다 늘었으면 그 사이에서 차가 붙은 것이고, 줄었으면 빠진
것이다. 방법과 한계는 src/evdt/io/entry_exit.py 참조.

이 파일 이름은 시나리오 config 의 demand.entry_exit_profile 과 같다. 이것이 있으면
러너가 through_profile 대신 이 표로 진입 지점과 목적지를 만든다.
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402

from evdt.io.demand_profile import peak_date  # noqa: E402
from evdt.io.entry_exit import (  # noqa: E402
    corridor_entry_hourly,
    entry_exit_profile,
    entry_points,
)

TRAFFIC = ROOT / "data" / "processed" / "traffic_gyeongbu.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="seollal2026")
    ap.add_argument("--direction", default="DOWN", choices=("DOWN", "UP"))
    ap.add_argument("--date", default=None, help="비우면 진입 최대일")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not TRAFFIC.is_file():
        print(f"교통량 정리본이 없습니다: {TRAFFIC}\n먼저 실행할 것:  python scripts/build_traffic.py")
        return 1

    traffic = pd.read_parquet(TRAFFIC)
    day = pd.Timestamp(args.date) if args.date else peak_date(
        traffic, period=args.period, direction=args.direction)

    profile = entry_exit_profile(traffic, period=args.period, direction=args.direction, date=day)
    head = corridor_entry_hourly(traffic, period=args.period, direction=args.direction, date=day)
    points = entry_points(profile, head)

    tag = "seollal" if args.period.startswith("seollal") else args.period
    out = ROOT / (args.out or
                  f"data/processed/entry_exit_gyeongbu_{args.direction.lower()}_{tag}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    profile.to_csv(out, index=False)

    print(f"{args.direction} {args.period} · 최대일 {day:%Y-%m-%d}")
    print(f"  경계 {profile['offset_km'].nunique()}개 · 행 {len(profile):,}")
    print(f"  코리도 진입 {head['entry_veh'].sum():,.0f}대 · "
          f"중간 진입 {profile['entry_veh'].sum():,.0f}대 · "
          f"진출 {profile['exit_veh'].sum():,.0f}대")
    print(f"  진입 지점 {points['offset_km'].nunique()}곳 · "
          f"결측으로 건너뛴 최대 거리 {profile['gap_km'].max():.1f} km")
    print(f"  저장: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
