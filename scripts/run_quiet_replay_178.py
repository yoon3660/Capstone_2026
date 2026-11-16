"""평시(3/2~3/11)·설(2/13~2/22) 재현을 같은 세계에서 돌리고 실측과 비교한다 (#178).

    python scripts/run_quiet_replay_178.py --period quiet --seeds 1-3 --temp measured
    python scripts/run_quiet_replay_178.py --period quiet --seeds 1-3 --temp seollal   # 기온 대조
    python scripts/run_quiet_replay_178.py --period seollal --seeds 1-3                # 같은 채점의 설

## 세계 — 설 20개 시나리오와 같다 (#64·#167·#168)

- 공급 BASE215 (`sp3_64_daily_base215_runner.adapter`), #64 연구실 DB 사본(읽기 전용 백업)
- 차량·충전 행태·정책 설정은 #64 설 일자 config 그대로: EV 5%, 출발 SoC `holiday`,
  목표 상한 0.8, 이탈 120분, `travel_time: fixed`, `od_profile: null`, FIFO·MAX_POWER_IDLE
- 평시에서 바꾸는 것은 **기간에 딸린 입력** 세 개뿐이다
    수요 프로파일  `build_quiet_demand_178.py` 출력 (VDS 3/2~3/11)
    기온          `fetch_kma_obs_178.py` 일평균(16개 표본) — `--temp seollal` 이면 설과 같은 −5 °C
    period        quiet202603
  공급은 3월 충전이력에 새 충전기가 없어 BASE215 그대로다(`issue178_observed_march.py`)

## 모델

    S0   runner 기본 S0 (가장 싼 곳, 지금 화면 대기)    `run_mnl_world.run_cell(classic=True)`
    UE   기존 UE 균형                                   `run_mnl_world.run_ue_cell`

## 채점 (날짜·방향·시드·모델마다, 그날 시작한 충전만)

    station_tvd        관측 휴게소 안 점유율 TVD (#167·#168 과 같다)
    hour_tvd           방향 전체 시간대 분포 TVD (관측 휴게소 안)
    station_hour_tvd   휴게소 × 시간 칸 TVD
    + 대기 평균·p95, 이탈, 대기 ≥ 10분인 휴게소×시간 칸 수(혼잡 칸)

총량은 판정하지 않는다(실측은 환경부·기후부 충전기만). 결과는 runs/ 에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from calibrate_mnl_seollal import DATES as SEOLLAL_DATES  # noqa: E402
from calibrate_mnl_seollal import (  # noqa: E402
    OBS_REL,
    db_copy,
    legacy,
    scenario_config,
    station_tvd,
)
from run_mnl_world import _git_sha, prepare, run_cell, run_ue_cell  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import MnlSettings  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402

QUIET_DATES = [f"2026-03-{d:02d}" for d in range(2, 12)]
QUIET_DIR = Path("data/processed/issue178_quiet202603")
CONGESTED_WAIT_MIN = 10.0


def tvd(o: pd.Series, m: pd.Series) -> float:
    idx = o.index.union(m.index)
    o, m = o.reindex(idx).fillna(0.0), m.reindex(idx).fillna(0.0)
    if o.sum() <= 0 or m.sum() <= 0:
        return 1.0
    return 0.5 * float(np.abs(o / o.sum() - m / m.sum()).sum())


def load_obs(root: Path, period: str, norm) -> pd.DataFrame:
    path = root / (QUIET_DIR / "observed_station_day_hour.csv" if period == "quiet" else OBS_REL)
    o = pd.read_csv(path)
    o["key"] = o["station_name"].map(norm)
    return o.groupby(["direction", "date", "key", "hour"], as_index=False)["starts"].sum()


def daily_temps(root: Path) -> dict[str, float]:
    d = pd.read_csv(root / QUIET_DIR / "kma_daily_mean.csv")
    if (d["n"] != 16).any():
        raise SystemExit(f"[중단] 기온 표본이 16개가 아닌 날이 있다: {d[d.n != 16].to_dict('records')}")
    return dict(zip(d["date"], d["temp_c"], strict=True))


def day_config(root: Path, period: str, direction: str, day: str, daily, temp: float | None):
    if period == "seollal":
        return R.load_config(scenario_config(root, direction, day, daily))
    tmpl = R.load_config(scenario_config(root, direction, daily.PEAK_DATES[direction], daily))
    folder = QUIET_DIR / day / direction
    demand = replace(tmpl.demand, period="quiet202603",
                     volume_profile=(folder / "volume.csv").as_posix(),
                     through_profile=(folder / "through.csv").as_posix(),
                     entry_exit_profile=(folder / "entry_exit.csv").as_posix())
    env = replace(tmpl.environment, temp_c=float(temp))
    return replace(tmpl, demand=demand, environment=env, day_type="WEEKDAY_BASE",
                   scenario_id=f"issue178_quiet_{direction.lower()}_{day}",
                   label=f"#178 평시 {day} 경부선 {direction} [BASE215]")


def score(events: list[dict], lookup: pd.Series, o_day: pd.DataFrame) -> dict:
    cols = ["station_id", "t_start_min", "wait_min"]
    f = pd.DataFrame(events, columns=cols) if events else pd.DataFrame(columns=cols)
    f = f[(f.t_start_min >= 0) & (f.t_start_min < 1440)].copy()
    f["key"] = f["station_id"].map(lookup)
    f["hour"] = (f["t_start_min"] // 60).astype(int)
    o_station = o_day.groupby("key")["starts"].sum()
    o_station = o_station[o_station > 0]
    m_obs = f[f["key"].isin(o_station.index)]
    o_hour = o_day[o_day.key.isin(o_station.index)].groupby("hour")["starts"].sum()
    o_sh = o_day[o_day.key.isin(o_station.index)].groupby(["key", "hour"])["starts"].sum()
    cong = f.groupby(["station_id", "hour"])["wait_min"].mean()
    return {
        "obs_starts": int(o_station.sum()),
        "model_starts_all": len(f), "model_starts_obs_sites": len(m_obs),
        "station_tvd": station_tvd(o_station, f["key"].value_counts()),
        "hour_tvd": tvd(o_hour, m_obs.groupby("hour").size()),
        "station_hour_tvd": tvd(o_sh[o_sh > 0], m_obs.groupby(["key", "hour"]).size()),
        "congested_station_hours": int((cong >= CONGESTED_WAIT_MIN).sum()),
        "share_wait_gt0": float((f["wait_min"] > 0).mean()) if len(f) else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--period", choices=("quiet", "seollal"), required=True)
    ap.add_argument("--temp", choices=("measured", "seollal"), default="measured")
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--models", default="S0,UE")
    ap.add_argument("--dates", default="")
    ap.add_argument("--directions", default="DOWN,UP")
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = Path(args.data_root)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    daily, previous = legacy(root)
    seeds = R.parse_seeds(args.seeds)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    dates = [d for d in args.dates.split(",") if d] or (
        QUIET_DATES if args.period == "quiet" else SEOLLAL_DATES)
    directions = [d.strip() for d in args.directions.split(",") if d.strip()]
    tag = f"{args.period}_{args.temp}" if args.period == "quiet" else "seollal"
    out = Path(args.out) if args.out else RUNS_DIR / "issue178_replay" / tag
    out.mkdir(parents=True, exist_ok=True)
    temps = daily_temps(root) if (args.period == "quiet" and args.temp == "measured") else {}

    _units, overlay, _ = daily.get_units(root)
    obs = load_obs(root, args.period, previous.norm)
    (out / "manifest.json").write_text(json.dumps({
        "issue": 178, "script": "scripts/run_quiet_replay_178.py", "period": args.period,
        "temp_mode": args.temp, "temps": temps or "설 config 그대로(−5 °C)",
        "dates": dates, "directions": directions, "seeds": seeds, "models": models,
        "supply": "BASE215 (sp3_64_daily_base215_runner.adapter)",
        "observed": str(QUIET_DIR / "observed_station_day_hour.csv") if args.period == "quiet"
        else str(OBS_REL),
        "congested_wait_min": CONGESTED_WAIT_MIN, "data_root": str(root), "git": _git_sha(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    rows: list[dict] = []
    quiet = lambda *_: None  # noqa: E731
    work = out / "db"
    for direction in directions:
        db = db_copy(root, direction, daily, work)
        os.environ["EVDT_DB_PATH"] = str(db)
        lookup = previous.read_station_lookup(db, direction).set_index("station_id")["key"]
        for day in dates:
            temp = temps.get(day) if temps else None
            if args.period == "quiet" and temp is None:
                temp = -5.0
            cfg = day_config(root, args.period, direction, day, daily, temp)
            o_day = obs[(obs.direction == direction) & (obs.date == day)]
            for seed in seeds:
                t0 = time.perf_counter()
                with daily.adapter(R, overlay):
                    world = prepare(cfg, seed, root=root, log=quiet)
                for model in models:
                    if model == "S0":
                        row, ev = run_cell(world, "S0", p=1.0, mnl=MnlSettings(seed=seed),
                                           strength=0.0, oracle=None, s3_window=None,
                                           log=quiet, classic=True)
                    elif model == "UE":
                        row, ev = run_ue_cell(world, quiet)
                    else:
                        raise SystemExit(f"모르는 모델: {model}")
                    rows.append({"period": args.period, "temp_mode": args.temp,
                                 "temp_c": float(cfg.environment.temp_c), "direction": direction,
                                 "date": day, "seed": seed, "model": model,
                                 "n_ev_total": int(world.built.n_ev),
                                 **{k: row[k] for k in ("n_ev_charging", "n_charge_visits",
                                                        "n_escaped", "n_exhausted",
                                                        "wait_mean_min", "wait_p95_min",
                                                        "dwell_total_h", "seconds")},
                                 "ue_final_gap": row.get("ue_final_gap", math.nan),
                                 **score(ev, lookup, o_day)})
                pd.DataFrame(rows).to_csv(out / "grid.csv", index=False, encoding="utf-8-sig")
                print(f"  {direction} {day} s{seed} ({cfg.environment.temp_c:.1f}°C): "
                      f"{time.perf_counter() - t0:.0f}초", flush=True)
    print(f"출력: {out}")
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
