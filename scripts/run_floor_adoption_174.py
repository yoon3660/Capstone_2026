"""혼잡 무대(설 상행 adoption)에서 바닥 후보를 돌린다 — 모드 A θ 격자와 SUE (#174 A·C).

    python scripts/run_floor_adoption_174.py --config config/scenario_seollal_up_adoption.yaml \\
        --seeds 1 --mode-a-thetas 0.02,0.03,0.2,0.5,1 --sue-thetas 0.05 --out <폴더>

무대·DB·설정은 #168 `run_mnl_world.py --floor-only` 와 같다. 같은 세계(시드)에서

    모드 A   `run_mnl_world.run_cell(world, "MNL", ...)` — #168 바닥과 같은 계산
    SUE      `run_mnl_world.run_ue_cell` 을 `runner.solve_and_log` 만 SUE 로 바꿔 끼워 부른다

를 돌린다. 행의 열은 `run_mnl_world` cells.csv 와 같고, `floor_model`(A / SUE)·반복 횟수·
수렴 여부를 더한다. 기존 파일은 고치지 않는다. DB 에 쓰지 않는다.

SUE 한 칸은 UE 와 비슷하게 오래 걸린다(시드 하나 약 20~30분). 시드·θ 별로 나눠 여러
프로세스로 돌리고 cells.csv 를 합친다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import BASE, _git_sha, prepare, run_cell, run_ue_cell  # noqa: E402
from run_sue_seollal import use_sue  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import MnlSettings  # noqa: E402
from evdt.engine.sue import SueSettings  # noqa: E402
from evdt.engine.ue import UENotConverged  # noqa: E402
from evdt.paths import PROJECT_ROOT, default_db_path  # noqa: E402


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--mode-a-thetas", default="")
    ap.add_argument("--sue-thetas", default="")
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    cfg0 = R.load_config(args.config)
    seeds = R.parse_seeds(args.seeds)
    a_thetas, s_thetas = _floats(args.mode_a_thetas), _floats(args.sue_thetas)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    root = Path(args.data_root)
    (out / "manifest.json").write_text(json.dumps({
        "issue": 174, "script": "scripts/run_floor_adoption_174.py", "config": args.config,
        "scenario_id": cfg0.scenario_id, "seeds": seeds, "mode_a_thetas": a_thetas,
        "sue_thetas": s_thetas, "wait_weight": 1.0, "escape_mode": "s0",
        "escape_cost_min": cfg0.demand.escape_cost_min, "db_path": str(default_db_path()),
        "data_root": str(root), "git": _git_sha(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    rows: list[dict] = []
    quiet = lambda *_: None  # noqa: E731

    def keep(row: dict) -> None:
        rows.append(row)
        pd.DataFrame(rows).to_csv(out / "cells.csv", index=False, encoding="utf-8-sig")
        print(f"  s{row['seed']} {row['floor_model']:<3} θ={row['theta']:<6g} "
              f"총비용 {row.get('total_h', math.nan):>9,.1f}h 이탈 {row.get('n_escaped', -1):>4} "
              f"대기 {row.get('wait_mean_min', math.nan):5.1f}분 반복 {row.get('iterations', 0):>3} "
              f"{'수렴' if row['converged'] else '미수렴'} ({row['seconds']:.0f}초)", flush=True)

    for seed in seeds:
        world = prepare(cfg0, seed, root=root, log=quiet)
        for th in a_thetas:
            mnl = MnlSettings(theta=th, wait_weight=1.0, seed=seed, escape_mode="s0")
            row, _ev = run_cell(world, BASE, p=0.0, mnl=mnl, strength=0.0, oracle=None,
                                s3_window=None, log=quiet)
            keep({**row, "floor_model": "A", "iterations": 0, "converged": True})
        for th in s_thetas:
            hist: list[str] = []
            t0 = time.perf_counter()
            try:
                with use_sue(SueSettings(theta=th, seed=seed)):
                    row, _ev = run_ue_cell(world, hist.append)
                ok = True
            except UENotConverged as exc:
                row, ok = {"seed": seed, "error": str(exc)}, False
            it = [m for m in hist if m.strip().startswith("반복")]
            keep({**row, "family": "sue", "cell": f"SUE_{th:g}", "stage": "SUE", "theta": th,
                  "wait_weight": 1.0, "floor_model": "SUE", "iterations": len(it),
                  "last_iter_line": it[-1].strip() if it else "", "converged": ok,
                  "seconds": time.perf_counter() - t0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
