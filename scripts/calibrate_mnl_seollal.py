"""MNL θ 를 설 충전이력(#167)으로 정하고, 날짜를 나눠 검증한다 (#168 개선 2).

    python scripts/calibrate_mnl_seollal.py --data-root <evdt 저장소 루트> --seeds 1-5

## 왜

실측 재현성 검증은 설 충전이력으로만 한다. 추석 이력(#162)은 판정 보류 자료라서 (1/2)에서
추석으로 정한 θ = 0.05 는 잠정값이었다. 여기서 θ 를 설로 다시 정한다.

## 무엇을 하나

무대는 #64·#167 의 설 20개 시나리오(2/13~2/22 × 상·하행)다. BASE215 공급, EV 5%,
가정 레이어 없음. #167 이 S0·UE 를 돌린 것과 같은 설정이다(`daily.adapter` 로 공급을 덮는다).

**최소 설계 (#168):** 고치는 모수는 θ 하나다.
- w = 1.0 으로 고정한다. 1.5 는 민감도로만 본다
- 이탈은 S0 규칙이다 (`escape_mode = "s0"`, 개선 1)

**날짜 나누기 — 결과를 보기 전에 정한다:**

    접기 A   보정 = 13·15·17·19·21일     검증 = 14·16·18·20·22일    (상·하행 모두)
    접기 B   보정 = 14·16·18·20·22일     검증 = 13·15·17·19·21일

각 접기에서 보정용 날의 지표가 가장 좋은 θ 를 고르고, **검증용 날로만** 채점한다.

**지표:** 날짜·방향마다 관측 휴게소 안의 휴게소 점유율 TVD(그날 시작한 충전만, #64 규칙).
날짜·방향 값을 실측 시작 건수로 가중 평균한다(#167 의 `station_given_day` 와 같다).
총량은 판정하지 않는다(모집단 불일치).

**미리 정한 판정:** 두 접기 모두에서 검증용 날의 `MNL(고른 θ) − S0` 를 시드별로 내고,
그 95% CI 가 전부 0 보다 작으면 통과. 하나라도 아니면 미달.

## 하지 않는 것

- 검증용 날의 결과를 보고 θ 를 고치지 않는다
- #64·#167 의 원본 DB·run·결과에 쓰지 않는다 (DB 는 스크래치 사본, 결과는 run 기록 없이)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import BASE, _ci, _git_sha, prepare, run_cell  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import ESCAPE_MODES, MnlSettings  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402

DATES = [f"2026-02-{d:02d}" for d in range(13, 23)]
DIRECTIONS = ("DOWN", "UP")
ODD = [d for d in DATES if int(d[-2:]) % 2 == 1]
EVEN = [d for d in DATES if int(d[-2:]) % 2 == 0]
FOLDS = {"A": (ODD, EVEN), "B": (EVEN, ODD)}
DEFAULT_THETAS = "0.01,0.02,0.05,0.1,0.2,0.5,1"
OBS_REL = Path("data/processed/issue167_seollal/seollal_observed_station_day_hour.csv")


def legacy(root: Path):
    """#64 의 일자 실행 도구 (공급 덮기·이름 정규화). 이 저장소 scripts/ 의 같은 파일."""

    import sp3_64_daily_base215_runner as daily
    import sp3_64_validate_20day_base215 as previous

    return daily, previous


def scenario_config(root: Path, direction: str, day: str, daily) -> Path:
    """#64 의 그날 UE 실행 설정 (#167 S0 와 같은 출처)."""

    if day == daily.PEAK_DATES[direction]:
        base = root / daily.SOURCE_LABS[direction]
        summary = base / "run_summary.json"
    else:
        base = root / daily.ROOT_OUTPUT / direction.lower()
        summary = base / "days" / day / "run_summary.json"
    rid = json.loads(summary.read_text(encoding="utf-8-sig"))["run_id"]
    cfg = base / "runs" / rid / "config.yaml"
    if not cfg.is_file():
        raise SystemExit(f"[중단] 설정 없음: {cfg}")
    return cfg


def db_copy(root: Path, direction: str, daily, work: Path) -> Path:
    """#64 연구실 DB 를 스크래치에 백업 사본으로 (WAL 포함, 원본은 읽기 전용)."""

    src = root / daily.SOURCE_LABS[direction] / "isolated.db"
    dst = work / f"lab_{direction.lower()}.db"
    if not dst.exists():
        work.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True) as a, \
                sqlite3.connect(dst) as b:
            a.backup(b)
    return dst


def load_observed(root: Path, norm) -> pd.DataFrame:
    obs = pd.read_csv(root / OBS_REL)
    obs["key"] = obs["station_name"].map(norm)
    return obs.groupby(["direction", "date", "key", "hour"], as_index=False)["starts"].sum()


def station_tvd(obs_day: pd.Series, mod_day: pd.Series) -> float:
    """관측 휴게소 안의 점유율 TVD. 둘 다 key → 건수."""

    keys = obs_day.index
    m = mod_day.reindex(keys).fillna(0.0)
    o = obs_day
    if m.sum() <= 0 or o.sum() <= 0:
        return 1.0
    return 0.5 * float(np.abs(o / o.sum() - m / m.sum()).sum())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=str(PROJECT_ROOT),
                    help="#64·#167 자료가 있는 evdt 저장소 루트")
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--thetas", default=DEFAULT_THETAS)
    ap.add_argument("--wait-weights", default="1.0",
                    help="1.0 고정이 기본. 민감도는 예: 1.0,1.5")
    ap.add_argument("--escape-mode", choices=ESCAPE_MODES, default="s0")
    ap.add_argument("--dates", default="", help="일부만 (예: 2026-02-13). 비우면 10일 전부")
    ap.add_argument("--directions", default="DOWN,UP")
    ap.add_argument("--db-work", default=None, help="연구실 DB 사본을 둘 폴더 (기본: out/db)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    root = Path(args.data_root)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    daily, previous = legacy(root)
    seeds = R.parse_seeds(args.seeds)
    thetas = [float(x) for x in args.thetas.split(",") if x.strip()]
    weights = [float(x) for x in args.wait_weights.split(",") if x.strip()]
    dates = [d.strip() for d in args.dates.split(",") if d.strip()] or DATES
    directions = [d.strip() for d in args.directions.split(",") if d.strip()]
    out = Path(args.out) if args.out else RUNS_DIR / "mnl_calibration_seollal"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.db_work) if args.db_work else out / "db"

    _units, overlay, _by_unit = daily.get_units(root)
    obs = load_observed(root, previous.norm)
    models = [("S0", math.inf, 1.0)] + [("MNL", th, w) for th in thetas for w in weights]

    (out / "manifest.json").write_text(json.dumps({
        "issue": 168, "script": "scripts/calibrate_mnl_seollal.py", "data_root": str(root),
        "observed": str(OBS_REL), "observed_starts": int(obs["starts"].sum()),
        "dates": dates, "directions": directions, "seeds": seeds, "thetas": thetas,
        "wait_weights": weights, "escape_mode": args.escape_mode, "folds": FOLDS,
        "metric": "날짜·방향별 관측 휴게소 점유율 TVD, 실측 시작 건수 가중",
        "rule": "검증용 날을 보고 θ 를 고치지 않는다", "git": _git_sha(),
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
                t0 = time.perf_counter()
                with daily.adapter(R, overlay):
                    world = prepare(cfg, seed, root=root, log=quiet)
                for name, th, w in models:
                    mnl = MnlSettings(theta=th, wait_weight=w, seed=seed,
                                      escape_mode=args.escape_mode)
                    row, ev = run_cell(world, BASE, p=0.0, mnl=mnl, strength=0.0, oracle=None,
                                       s3_window=None, log=quiet)
                    f = pd.DataFrame(ev, columns=["station_id", "t_start_min"]) if ev else \
                        pd.DataFrame(columns=["station_id", "t_start_min"])
                    f = f[(f["t_start_min"] >= 0) & (f["t_start_min"] < 1440)]
                    m_station = f["station_id"].map(lookup).value_counts()
                    rows.append({
                        "direction": direction, "date": day, "seed": seed, "model": name,
                        "theta": th, "wait_weight": w,
                        "station_tvd": station_tvd(o_station, m_station),
                        "obs_starts": int(o_station.sum()),
                        "model_starts_obs_sites": int(m_station.reindex(o_station.index)
                                                      .fillna(0).sum()),
                        "n_ev_charging": int(row["n_ev_charging"]),
                        "n_escaped": int(row["n_escaped"]),
                        "n_balked": int(row["n_escaped_balked"]),
                        "n_exhausted": int(row["n_exhausted"]),
                        "wait_mean_min": float(row["wait_mean_min"]),
                    })
                pd.DataFrame(rows).to_csv(out / "grid.csv", index=False, encoding="utf-8-sig")
                print(f"  {direction} {day} 시드 {seed}: {len(world.built.trips):,}대 · "
                      f"{time.perf_counter() - t0:.0f}초", flush=True)

    g = pd.DataFrame(rows)
    _judge(g, out, seeds)
    with contextlib.suppress(OSError):
        shutil.rmtree(work)       # 사본만 지운다
    return 0


def _weighted(g: pd.DataFrame, days: list[str]) -> pd.DataFrame:
    """(모델, θ, w, 시드) 별 실측 가중 평균 TVD (주어진 날들, 상·하행 모두)."""

    s = g[g["date"].isin(days)]
    keys = ["model", "theta", "wait_weight", "seed"]
    return (s.assign(wt=s["station_tvd"] * s["obs_starts"])
            .groupby(keys, as_index=False)[["wt", "obs_starts", "n_escaped"]].sum()
            .assign(tvd=lambda x: x["wt"] / x["obs_starts"]).drop(columns="wt"))


def _judge(g: pd.DataFrame, out: Path, seeds: list[int]) -> None:
    if set(g["date"]) != set(DATES):
        print("\n[판정 생략] 10일이 다 있어야 접기 판정을 한다 (일부 날짜 실행)")
        return
    allw = _weighted(g, DATES)
    summ = (allw.groupby(["model", "theta", "wait_weight"], as_index=False)
            .agg(tvd=("tvd", "mean"), tvd_ci=("tvd", _ci), n_escaped=("n_escaped", "mean")))
    summ.to_csv(out / "summary_all_days.csv", index=False, encoding="utf-8-sig")
    print("\n=== 20개 날짜·방향 전체 (참고, 판정 아님) ===")
    print(summ.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    verdict = {}
    for fold, (cal, val) in FOLDS.items():
        c = _weighted(g, cal)
        cm = c[c["model"] == "MNL"].groupby(["theta", "wait_weight"])["tvd"].mean()
        th, w = cm[cm.index.get_level_values("wait_weight") == 1.0].idxmin()
        v = _weighted(g, val).set_index(["model", "theta", "wait_weight", "seed"])["tvd"]
        mnl = v.xs(("MNL", th, w), level=["model", "theta", "wait_weight"])
        s0 = v.xs(("S0", math.inf, 1.0), level=["model", "theta", "wait_weight"])
        d = (mnl - s0).reindex(seeds)
        ci = _ci(d)
        thetas = sorted(cm.index.get_level_values("theta").unique())
        sens = {}
        for w_alt in sorted(set(v.index.get_level_values("wait_weight")) - {1.0}):
            key = ("MNL", th, w_alt)
            if key in set(zip(*[v.index.get_level_values(i) for i in range(3)], strict=True)):
                alt = v.xs(key, level=["model", "theta", "wait_weight"])
                da = (alt - s0).reindex(seeds)
                sens[f"w={w_alt:g}"] = {"val_diff": float(da.mean()), "val_diff_ci95": _ci(da)}
        verdict[fold] = {
            "calibration_days": cal, "validation_days": val, "chosen_theta": th,
            "chosen_on_grid_edge": th in (thetas[0], thetas[-1]),
            "val_mnl_tvd": float(mnl.mean()), "val_s0_tvd": float(s0.mean()),
            "val_diff": float(d.mean()), "val_diff_ci95": ci,
            "pass": bool(d.mean() + ci < 0) if not math.isnan(ci) else False,
            "cal_curve": {f"{t:g}": float(cm.loc[(t, 1.0)]) for t in thetas},
            "wait_weight_sensitivity_at_chosen_theta": sens,
        }
    verdict["overall_pass"] = all(verdict[f]["pass"] for f in FOLDS)
    (out / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print("\n=== 판정 (검증용 날, MNL − S0 TVD, 시드 대응) ===")
    for fold in FOLDS:
        x = verdict[fold]
        print(f"  접기 {fold}: θ={x['chosen_theta']:g}{' (격자 끝)' if x['chosen_on_grid_edge'] else ''}"
              f" · MNL {x['val_mnl_tvd']:.4f} vs S0 {x['val_s0_tvd']:.4f}"
              f" · 차이 {x['val_diff']:+.4f} ± {x['val_diff_ci95']:.4f} → "
              f"{'통과' if x['pass'] else '미달'}")
    print(f"  전체: {'통과' if verdict['overall_pass'] else '미달'}\n출력: {out}")


if __name__ == "__main__":
    raise SystemExit(main())
