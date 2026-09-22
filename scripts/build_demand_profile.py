"""교통량에서 UE 수요 프로파일 두 개를 만든다.

    python scripts/build_demand_profile.py                 # 설 2026 하행, 진입 최대일
    python scripts/build_demand_profile.py --date 2026-02-14

    data/processed/volume_gyeongbu_down_seollal.csv    hour, volume_veh  (언제 들어오는가)
    data/processed/through_gyeongbu_down_seollal.csv   offset_km, share  (어디까지 가는가)

두 파일 이름은 config/scenario_seollal_down.yaml 의 demand.volume_profile /
demand.through_profile 과 같다. 방법은 src/evdt/io/demand_profile.py 참조.
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402

from evdt.io.demand_profile import (  # noqa: E402
    entry_hourly_volume,
    entry_zone,
    peak_date,
    through_share,
)

TRAFFIC = ROOT / "data" / "processed" / "traffic_gyeongbu.parquet"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", default="seollal2026")
    ap.add_argument("--direction", default="DOWN", choices=["DOWN", "UP"])
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 진입 교통량 최대일)")
    ap.add_argument("--volume-out", default="data/processed/volume_gyeongbu_down_seollal.csv")
    ap.add_argument("--through-out", default="data/processed/through_gyeongbu_down_seollal.csv")
    args = ap.parse_args()

    if not TRAFFIC.is_file():
        print(f"교통량 파일이 없다: {TRAFFIC}\n먼저 T-07 교통량 적재를 실행할 것.")
        return 1

    traffic = pd.read_parquet(TRAFFIC)
    kw = dict(period=args.period, direction=args.direction)
    day = pd.Timestamp(args.date) if args.date else peak_date(traffic, **kw)

    volume = entry_hourly_volume(traffic, date=day, **kw)
    through = through_share(traffic, date=day, **kw)

    (ROOT / args.volume_out).parent.mkdir(parents=True, exist_ok=True)
    volume.to_csv(ROOT / args.volume_out, index=False)
    through.to_csv(ROOT / args.through_out, index=False)

    print(f"{args.period} {args.direction} {day.date()}  진입 콘존 {entry_zone(traffic, **kw)}")
    print(f"  진입 교통량 {volume['volume_veh'].sum():,.0f} 대/일  "
          f"(최대 {int(volume.loc[volume['volume_veh'].idxmax(), 'hour'])}시 "
          f"{volume['volume_veh'].max():,.0f} 대)")
    for km in (50, 100, 150, 200, 300):
        share = through.loc[through["offset_km"] <= km, "share"].iloc[-1]
        print(f"  {km:>3} km 을 지나가는 비율 {share:5.1%}")
    print(f"  -> {args.volume_out}\n  -> {args.through_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
