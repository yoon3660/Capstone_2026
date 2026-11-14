"""SUE(모드 B)를 설 시나리오에서 돌린다 (#173 스모크 · #174 설 대조의 실행기).

    python scripts/run_sue_seollal.py --data-root <evdt 저장소 루트> \\
        --dates 2026-02-13 --directions DOWN --seeds 1 --thetas 0.05,0.1

무대는 #168 `calibrate_mnl_seollal.py` 와 같다 — #64 의 그날 설정, BASE215 공급 덮기,
#64 연구실 DB 의 스크래치 사본. 같은 세계에서 UE(θ=∞)와 SUE(θ 목록)를 푼다.

## 어떻게

기존 파일을 고치지 않는다. `runner._run_ue` 는 `runner.solve_and_log` 를 부른다. 그 이름을
잠깐 `sue.solve_and_log_sue(θ)` 로 바꿔 끼운다. 수요·DES·이탈 기록은 UE 와 같은 경로다.

## 출력 (`--out`, 기본 RUNS_DIR/sue_seollal)

    cells.csv                     칸마다 한 줄: 반복 횟수, 최종 gap, 수렴 여부, 시간, TVD(전체 운영사)
    events/<dir>_<date>_s<seed>_<model>.parquet   충전 기록 (운영사 거르기는 나중에)

TVD 는 #168 과 같은 정의(관측 휴게소 안 점유율, 그날 시작한 충전)다. 운영사를
환경부(ME)로 거른 비교는 `scripts/tvd_operator_scope.py` 가 이 events 로 한다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from calibrate_mnl_seollal import (  # noqa: E402
    DATES,
    OBS_REL,
    db_copy,
    legacy,
    load_observed,
    scenario_config,
    station_tvd,
)
from run_mnl_world import _git_sha, prepare, run_ue_cell  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.sue import SueSettings, solve_and_log_sue  # noqa: E402
from evdt.engine.ue import UENotConverged  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402


@contextlib.contextmanager
def use_sue(sue: SueSettings):
    """`runner.solve_and_log` 를 잠깐 SUE 로 바꾼다. 끝나면 반드시 되돌린다."""

    original = R.solve_and_log
    R.solve_and_log = solve_and_log_sue(sue)
    try:
        yield
    finally:
        R.solve_and_log = original


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--thetas", default="0.05,0.1")
    ap.add_argument("--dates", default="", help="비우면 10일 전부")
    ap.add_argument("--directions", default="DOWN,UP")
    ap.add_argument("--max-iter", type=int, default=0, help="0 이면 설정값 그대로")
    ap.add_argument("--db-work", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = Path(args.data_root)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    daily, previous = legacy(root)
    seeds = R.parse_seeds(args.seeds)
    thetas = [float(x) for x in args.thetas.split(",") if x.strip()]
    dates = [d.strip() for d in args.dates.split(",") if d.strip()] or DATES
    directions = [d.strip() for d in args.directions.split(",") if d.strip()]
    out = Path(args.out) if args.out else RUNS_DIR / "sue_seollal"
    (out / "events").mkdir(parents=True, exist_ok=True)
    work = Path(args.db_work) if args.db_work else out / "db"

    _units, overlay, _ = daily.get_units(root)
    obs = load_observed(root, previous.norm)
    models = [("UE", math.inf)] + [(f"SUE_{t:g}", t) for t in thetas]
    (out / "manifest.json").write_text(json.dumps({
        "issue": 173, "script": "scripts/run_sue_seollal.py", "data_root": str(root),
        "observed": str(OBS_REL), "dates": dates, "directions": directions, "seeds": seeds,
        "thetas": thetas, "max_iter_override": args.max_iter or None, "git": _git_sha(),
        "escape": "잡음 없는 비용으로 판정 (모드 A 의 escape_mode=s0 와 같은 원칙)",
        "wait_weight": "1.0 고정 (원장이 대기·충전을 나눠 주지 않음)",
        "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    rows: list[dict] = []
    quiet = lambda *_: None  # noqa: E731
    for direction in directions:
        db = db_copy(root, direction, daily, work)
        os.environ["EVDT_DB_PATH"] = str(db)
        lookup = previous.read_station_lookup(db, direction).set_index("station_id")["key"]
        for day in dates:
            cfg = R.load_config(scenario_config(root, direction, day, daily))
            o_day = obs[(obs["direction"] == direction) & (obs["date"] == day)]
            o_station = o_day.groupby("key")["starts"].sum()
            o_station = o_station[o_station > 0]
            for seed in seeds:
                with daily.adapter(R, overlay):
                    world = prepare(cfg, seed, root=root, log=quiet)
                if args.max_iter:
                    from dataclasses import replace
                    world.settings = replace(world.settings, max_iter=args.max_iter)
                for name, th in models:
                    t0 = time.perf_counter()
                    converged, row, ev = True, None, []
                    hist: list = []

                    def grab(msg: str, _h=hist) -> None:
                        _h.append(msg)

                    try:
                        with use_sue(SueSettings(theta=th, seed=seed)):
                            row, ev = run_ue_cell(world, grab)
                    except UENotConverged as exc:
                        converged = False
                        print(f"  [미수렴] {direction} {day} s{seed} {name}: {exc}", flush=True)
                    secs = time.perf_counter() - t0
                    iters = sum(1 for m in hist if m.strip().startswith("반복"))
                    rec = {"direction": direction, "date": day, "seed": seed, "model": name,
                           "theta": th, "converged": converged, "iterations": iters,
                           "seconds": secs, "n_ev_charging": len(world.built.trips),
                           "obs_starts": int(o_station.sum())}
                    if row is not None:
                        f = pd.DataFrame(ev)
                        f.to_parquet(out / "events" / f"{direction}_{day}_s{seed}_{name}.parquet",
                                     index=False)
                        fw = f[(f["t_start_min"] >= 0) & (f["t_start_min"] < 1440)]
                        m_station = fw["station_id"].map(lookup).value_counts()
                        last = [m for m in hist if m.strip().startswith("반복")]
                        rec.update({
                            "final_gap_line": last[-1].strip() if last else "",
                            "station_tvd": station_tvd(o_station, m_station),
                            "n_escaped": int(row["n_escaped"]),
                            "wait_mean_min": float(row["wait_mean_min"]),
                            "dwell_total_h": float(row["dwell_total_h"]),
                        })
                    rows.append(rec)
                    pd.DataFrame(rows).to_csv(out / "cells.csv", index=False, encoding="utf-8-sig")
                    print(f"  {direction} {day} s{seed} {name:<9} 반복 {iters:>3} "
                          f"{'수렴' if converged else '미수렴'} · {secs:.0f}초 · "
                          f"TVD {rec.get('station_tvd', float('nan')):.4f} · "
                          f"이탈 {rec.get('n_escaped', -1)}", flush=True)

    print(f"\n출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
