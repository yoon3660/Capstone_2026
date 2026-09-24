"""재현 기간 하나의 수요 프로파일을 한 번에 만든다 (#54).

    python scripts/build_replay.py --period seollal2026 --direction DOWN
    python scripts/build_replay.py --period chuseok2026 --direction UP

만드는 것 (한 방향, 진입 최대일 기준)

    data/processed/volume_gyeongbu_<방향>_<태그>.csv       hour, volume_veh
    data/processed/through_gyeongbu_<방향>_<태그>.csv      offset_km, share   (옛 방식)
    data/processed/entry_exit_gyeongbu_<방향>_<태그>.csv   중간 진입·진출

무대를 갈아끼우는 절차는 `src/evdt/replay.py` 문서 참조. 교통량 정리본
(`traffic_gyeongbu.parquet`)에 그 period 가 이미 들어 있어야 한다 —
`fetch_traffic.py --label <period>` → `build_traffic.py` 순서다.

⚠ **기온 · 충전기 현황 · EV 비중은 따라오지 않는다.** 그 셋은 시나리오 config 에서
직접 바꿔야 한다 (replay.py 의 표 참조).
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.demand_profile import (  # noqa: E402
    entry_hourly_volume,
    peak_date,
    through_share,
)
from evdt.io.entry_exit import (  # noqa: E402
    corridor_entry_hourly,
    entry_exit_profile,
    entry_points,
)
from evdt.replay import PERIODS, describe  # noqa: E402

TRAFFIC = ROOT / "data" / "processed" / "traffic_gyeongbu.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="seollal2026")
    ap.add_argument("--direction", default="DOWN", choices=("DOWN", "UP"))
    ap.add_argument("--date", default=None, help="비우면 진입 최대일")
    args = ap.parse_args()

    if not TRAFFIC.is_file():
        print(f"교통량 정리본이 없습니다: {TRAFFIC}\n먼저:  python scripts/build_traffic.py")
        return 1

    traffic = pd.read_parquet(TRAFFIC)
    have = sorted(traffic["period"].unique())

    if args.period not in have:
        print(f"교통량 정리본에 '{args.period}' 가 없습니다. 있는 기간: {have}\n"
              f"먼저:  python scripts/fetch_traffic.py --start ... --end ... "
              f"--label {args.period}\n       python scripts/build_traffic.py")
        return 1

    known = PERIODS.get(args.period)
    tag = known.profile_tag() if known else args.period
    day = pd.Timestamp(args.date) if args.date else peak_date(
        traffic, period=args.period, direction=args.direction)
    low = args.direction.lower()

    print(f"{describe(args.period)} · {args.direction} · 최대일 {day:%Y-%m-%d}")
    if known and known.note:
        print(f"  {known.note}")

    out = ROOT / "data" / "processed"
    out.mkdir(parents=True, exist_ok=True)

    volume = entry_hourly_volume(traffic, period=args.period, direction=args.direction, date=day)
    volume.to_csv(out / f"volume_gyeongbu_{low}_{tag}.csv", index=False)

    through = through_share(traffic, period=args.period, direction=args.direction, date=day)
    through.to_csv(out / f"through_gyeongbu_{low}_{tag}.csv", index=False)

    profile = entry_exit_profile(traffic, period=args.period, direction=args.direction, date=day)
    profile.to_csv(out / f"entry_exit_gyeongbu_{low}_{tag}.csv", index=False)

    head = corridor_entry_hourly(traffic, period=args.period, direction=args.direction, date=day)
    points = entry_points(profile, head)

    print(f"  기점 진입 {head['entry_veh'].sum():>9,.0f}대 · "
          f"중간 진입 {profile['entry_veh'].sum():>9,.0f}대 · "
          f"합계 {head['entry_veh'].sum() + profile['entry_veh'].sum():>9,.0f}대")
    print(f"  진입 지점 {points['offset_km'].nunique()}곳 · 경계 {profile['offset_km'].nunique()}개 · "
          f"결측으로 건너뛴 최대 {profile['gap_km'].max():.1f} km")
    print("\n시나리오 config 에 넣을 것:")
    print(f"  period:             {args.period}")
    print(f"  volume_profile:     data/processed/volume_gyeongbu_{low}_{tag}.csv")
    print(f"  through_profile:    data/processed/through_gyeongbu_{low}_{tag}.csv")
    print(f"  entry_exit_profile: data/processed/entry_exit_gyeongbu_{low}_{tag}.csv")
    print("  ⚠ 기온 · 충전기 수집본 · EV 비중은 따로 바꿔야 합니다")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
