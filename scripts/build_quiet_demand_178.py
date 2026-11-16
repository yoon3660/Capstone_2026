"""2026 평시(3/2~3/11) 수요 입력과 교통량 비교·CTM 검증 (#178).

    python scripts/build_quiet_demand_178.py --raw data/raw/ex_vds_quiet202603_<...> \\
        --out data/processed/issue178_quiet202603

#146 추석(`build_chuseok_demand.py`·`validate_chuseok_inputs.py`)과 같은 함수로 만든다.
TCS 혼합은 하지 않는다 — 설·추석 config 모두 `od_profile: null` 이고 추석에서 혼합이
구간 통과량 오차를 키웠다(#146). 진입 시각·대수는 VDS 가 준다.

날짜·방향마다 (out/<날짜>/<방향>/)
    volume.csv · entry_exit.csv · through.csv     시나리오 config 가 가리키는 프로파일
    ctm_entries.parquet · hourly_od.parquet        진입점 · 위험률 OD (검산용)

out/ 에
    traffic_gyeongbu_quiet.parquet · speed_gyeongbu_quiet.parquet   정리본 (이 기간만)
    missing_summary.csv        콘존·날짜별 결측률
    traffic_comparison.csv     평시·설·추석 같은 기준 비교 (첫 단면 진입량·모형 진입량·
                               경부선 VDS 구간 평균 통과량)
    section_wape.csv           위험률 OD 로 다시 낸 구간 통과량 vs VDS (05시 이후, #146 과 같다)
    ctm_validation.csv         CTM 속도 MAPE·정체 재현율·오경보율 (#56·#146 과 같은 기준)

⚠ 구간 통과량 비교는 수요 생성에 쓴 VDS 를 다시 비교하는 것이라 독립 검증이 아니다(#146).
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT
from build_chuseok_demand import OD_COLUMNS, check_od, hazard_od, score, vds_profiles
from validate_chuseok_inputs import validate_speed
from validate_sections import scoring_grid

from evdt.io import traffic
from evdt.io.demand_profile import through_share
from evdt.io.route import GyeongbuRoute
from evdt.runner import load_config

PERIOD = "quiet202603"
DAYS = pd.date_range("2026-03-02", "2026-03-11")
DIRECTIONS = ("DOWN", "UP")
SEOLLAL_DAYS = pd.date_range("2026-02-13", "2026-02-22")
CHUSEOK_RAW = ROOT / "data/raw/issue145_chuseok/chuseok_vds"
CHUSEOK_DAYS = pd.date_range("2026-09-24", "2026-09-27")


def positions(processed: Path) -> pd.DataFrame:
    p = pd.read_parquet(processed / "conzone_gyeongbu.parquet").drop(columns="period").drop_duplicates()
    if p.conzone_id.duplicated().any():
        raise ValueError("Conflicting existing conzone positions")
    return p


def tidy(raw: Path, pos: pd.DataFrame, period: str, days: pd.DatetimeIndex, *,
         check_daily: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = traffic.read_dataset(raw, "volume")
    if source.duplicated(["conzone_id", "date", "hour"]).any() or not source.hour.between(0, 23).all():
        raise ValueError("Invalid VDS hour keys")
    if check_daily:
        bad = traffic.check_daily_totals(source, traffic.read_dataset(raw, "volume_daily"))
        if not bad.empty:
            raise ValueError(f"Complete hourly VDS days disagree with daily source: {len(bad)}")
    vol = traffic.tidy(source, pos, "volume", period, days)
    spd = traffic.tidy(traffic.read_dataset(raw, "speed"), pos, "speed", period, days)
    return vol, spd


def mean_section_volume(vol: pd.DataFrame, direction: str, day) -> float:
    """콘존 길이 가중 하루 통과량(대/일). 24시간이 다 있는 콘존만."""

    g = vol[(vol.direction == direction) & (vol.date == day)]
    full = g.groupby("conzone_id").volume_veh.agg(lambda s: s.notna().sum() == 24)
    g = g[g.conzone_id.isin(full[full].index)]
    day_sum = g.groupby("conzone_id").agg(v=("volume_veh", "sum"),
                                          a=("offset_km_start", "first"),
                                          b=("offset_km_end", "first"))
    w = (day_sum.b - day_sum.a).abs()
    return float((day_sum.v * w).sum() / w.sum()) if w.sum() > 0 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / f"data/processed/issue178_{PERIOD}")
    ap.add_argument("--skip-ctm", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    processed = ROOT / "data/processed"
    pos = positions(processed)
    length = GyeongbuRoute.load().length_km

    vol, spd = tidy(args.raw, pos, PERIOD, DAYS, check_daily=True)
    vol.to_parquet(args.out / "traffic_gyeongbu_quiet.parquet", index=False)
    spd.to_parquet(args.out / "speed_gyeongbu_quiet.parquet", index=False)
    traffic.missing_summary(vol, "volume_veh").to_csv(args.out / "missing_summary.csv", index=False)

    seollal = pd.read_parquet(processed / "traffic_gyeongbu.parquet")
    seollal = seollal[seollal.period == "seollal2026"]
    chuseok = None
    if CHUSEOK_RAW.is_dir():
        chuseok, _ = tidy(CHUSEOK_RAW, pos, "chuseok2026", CHUSEOK_DAYS, check_daily=False)

    comparison, wape, ctm = [], [], []
    for direction in DIRECTIONS:
        cfg = load_config(ROOT / f"config/scenario_seollal_{direction.lower()}.yaml")
        for day in DAYS:
            ramps, head, points = vds_profiles(vol, PERIOD, direction, day)
            base = hazard_od(points, ramps, length)
            check_od(base, points, length)
            folder = args.out / str(day.date()) / direction
            folder.mkdir(parents=True, exist_ok=True)
            head.rename(columns={"entry_veh": "volume_veh"})[["hour", "volume_veh"]].to_csv(
                folder / "volume.csv", index=False)
            ramps.to_csv(folder / "entry_exit.csv", index=False)
            through_share(vol, period=PERIOD, direction=direction, date=day).to_csv(
                folder / "through.csv", index=False)
            points.to_parquet(folder / "ctm_entries.parquet", index=False)
            base.groupby(OD_COLUMNS[:-1], as_index=False).veh.sum().to_parquet(
                folder / "hourly_od.parquet", index=False)
            wape.append(dict(day=day.date(), direction=direction,
                             section_wape=score(base, scoring_grid(ramps))))
            comparison.append(dict(period=PERIOD, day=day.date(), direction=direction,
                                   head_traffic=head.entry_veh.sum(),
                                   total_entries=points.entry_veh.sum(),
                                   mean_section_veh=mean_section_volume(vol, direction, day)))
            if not args.skip_ctm:
                scoped = replace(cfg, demand=replace(
                    cfg.demand, volume_profile=str(folder / "volume.csv"),
                    entry_exit_profile=str(folder / "entry_exit.csv")))
                empty = pd.DataFrame(columns=["date", "segment_id", "hour", "length_km",
                                              "travel_time_seconds_candidate"])
                ctm.append(validate_speed(scoped, day, spd, vol, empty))
                pd.DataFrame(ctm).to_csv(args.out / "ctm_validation.csv", index=False)
            print(f"  {direction} {day.date()}: 진입 {points.entry_veh.sum():,.0f}", flush=True)
        for name, frame, days in (("seollal2026", seollal, SEOLLAL_DAYS),
                                  ("chuseok2026", chuseok, CHUSEOK_DAYS)):
            if frame is None:
                continue
            for day in days:
                _, head, points = vds_profiles(frame, name, direction, day)
                comparison.append(dict(period=name, day=day.date(), direction=direction,
                                       head_traffic=head.entry_veh.sum(),
                                       total_entries=points.entry_veh.sum(),
                                       mean_section_veh=mean_section_volume(frame, direction, day)))
    pd.DataFrame(comparison).to_csv(args.out / "traffic_comparison.csv", index=False)
    pd.DataFrame(wape).to_csv(args.out / "section_wape.csv", index=False)
    c = pd.DataFrame(comparison)
    print(c.groupby(["period", "direction"])[["head_traffic", "total_entries", "mean_section_veh"]]
          .mean().round(0).to_string())
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
