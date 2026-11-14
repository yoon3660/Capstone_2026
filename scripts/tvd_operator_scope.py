"""설 실측 비교를 운영사 범위에 맞춘다 — 모델도 환경부(ME) 충전기 기록만 센다 (#173 B).

    python scripts/tvd_operator_scope.py --data-root <evdt 저장소 루트> --seeds 1-3

## 왜

설 실측(#167, `환경부_충전기_충전이력`)은 **환경부 충전기만** 담는다. 민간 충전기는 없다.
모델(BASE215)에는 ME 95기와 민간(NONME) 120기가 함께 있다. #168 은 모델 전체의 휴게소
점유율을 실측과 비교했으므로, 남은 오차(TVD 약 0.28)의 일부는 운전자 선택이 아니라
**운영사 범위 차이**일 수 있다.

## 무엇을 하나

#168 `calibrate_mnl_seollal.py` 와 같은 무대·같은 모델(S0, MNL θ 목록, w=1, 이탈은 S0
규칙)을 다시 돌리고, 충전 기록마다 운영사를 붙여 TVD 를 두 범위로 낸다.

    all   모델 전체 충전 (#168 과 같은 값 — 재현 확인)
    ME    모델의 환경부 충전기 충전만 (실측과 같은 범위)

#168 은 요약만 저장해서 충전 기록이 없다. 그래서 다시 돌린다(1회 약 1초).
운영사는 #64 연구실의 `model_unit_to_operator.csv`(충전기 한 기 → 운영사)로 붙인다.

`--events-dir` 를 주면 시뮬레이션 없이 그 폴더의 충전 기록(`run_sue_seollal.py` 출력)만
채점한다.

판정은 #168 과 같은 홀짝 날 접기다. 접기마다 보정용 날에서 고른 θ 로 검증용 날의
`MNL − S0` 를 시드 대응으로 본다. 범위(all·ME)마다 따로 낸다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from calibrate_mnl_seollal import (  # noqa: E402
    DATES,
    FOLDS,
    OBS_REL,
    db_copy,
    legacy,
    load_observed,
    scenario_config,
    station_tvd,
)
from run_mnl_world import BASE, _ci, _git_sha, prepare, run_cell  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import MnlSettings  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402

SCOPES = ("all", "ME")
DEFAULT_THETAS = "0.000001,0.01,0.05,0.1,0.2,1"


def operator_map(root: Path, daily) -> dict[str, str]:
    """충전기 한 기 ID → 운영사 구분(ME / NONME). 상·하행 연구실이 같은 매핑을 쓴다."""

    path = root / daily.SOURCE_LABS["DOWN"] / "model_unit_to_operator.csv"
    m = pd.read_csv(path, encoding="utf-8-sig")
    return dict(zip(m["charger_id"].astype(str), m["operator_bucket"].astype(str), strict=True))


def score(events: pd.DataFrame, lookup: pd.Series, ops: dict[str, str],
          o_station: pd.Series) -> dict[str, float]:
    """그날 시작한 충전만, 관측 휴게소 안 점유율 TVD 를 범위별로."""

    f = events[(events["t_start_min"] >= 0) & (events["t_start_min"] < 1440)].copy()
    f["op"] = f["charger_id"].astype(str).map(ops)
    if f["op"].isna().any():
        bad = f.loc[f["op"].isna(), "charger_id"].head(3).tolist()
        raise SystemExit(f"[중단] 운영사 매핑이 없는 충전기: {bad}")
    out = {}
    for scope in SCOPES:
        g = f if scope == "all" else f[f["op"] == "ME"]
        m_station = g["station_id"].map(lookup).value_counts()
        out[f"tvd_{scope}"] = station_tvd(o_station, m_station)
        out[f"starts_{scope}"] = int(m_station.reindex(o_station.index).fillna(0).sum())
    return out


def _weighted(g: pd.DataFrame, days: list[str], col: str) -> pd.DataFrame:
    s = g[g["date"].isin(days)]
    return (s.assign(wt=s[col] * s["obs_starts"])
            .groupby(["model", "theta", "seed"], as_index=False)[["wt", "obs_starts"]].sum()
            .assign(tvd=lambda x: x["wt"] / x["obs_starts"]).drop(columns="wt"))


def judge(g: pd.DataFrame, seeds: list[int]) -> dict:
    out: dict = {}
    for scope in SCOPES:
        col = f"tvd_{scope}"
        allw = _weighted(g, sorted(set(g["date"])), col)
        curve = (allw.groupby(["model", "theta"], as_index=False)
                 .agg(tvd=("tvd", "mean"), ci=("tvd", _ci)))
        res = {"all_days": curve.to_dict("records")}
        if set(g["date"]) == set(DATES) and "S0" in set(g["model"]):
            for fold, (cal, val) in FOLDS.items():
                c = _weighted(g, cal, col)
                cm = c[c["model"] == "MNL"].groupby("theta")["tvd"].mean()
                th = float(cm.idxmin())
                v = _weighted(g, val, col).set_index(["model", "theta", "seed"])["tvd"]
                mnl = v.xs(("MNL", th), level=["model", "theta"]).reindex(seeds)
                s0 = v.xs(("S0", math.inf), level=["model", "theta"]).reindex(seeds)
                d = mnl - s0
                ci = _ci(d)
                res[fold] = {"chosen_theta": th, "val_mnl": float(mnl.mean()),
                             "val_s0": float(s0.mean()), "diff": float(d.mean()),
                             "diff_ci95": ci,
                             "pass": bool(d.mean() + ci < 0) if not math.isnan(ci) else False,
                             "cal_curve": {f"{k:g}": float(x) for k, x in cm.items()}}
        out[scope] = res
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--thetas", default=DEFAULT_THETAS)
    ap.add_argument("--dates", default="")
    ap.add_argument("--directions", default="DOWN,UP")
    ap.add_argument("--events-dir", default=None,
                    help="시뮬레이션 없이 이 폴더의 <dir>_<date>_s<seed>_<model>.parquet 만 채점")
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
    out = Path(args.out) if args.out else RUNS_DIR / "tvd_operator_scope"
    out.mkdir(parents=True, exist_ok=True)
    work = Path(args.db_work) if args.db_work else out / "db"

    _units, overlay, _ = daily.get_units(root)
    ops = operator_map(root, daily)
    obs = load_observed(root, previous.norm)
    models = [("S0", math.inf)] + [("MNL", t) for t in thetas]
    manifest = {
        "issue": 173, "part": "B", "script": "scripts/tvd_operator_scope.py",
        "data_root": str(root), "observed": str(OBS_REL),
        "observed_scope": "환경부 충전기만 (민간 미포함)", "model_scopes": list(SCOPES),
        "operator_map": str(daily.SOURCE_LABS["DOWN"]) + "/model_unit_to_operator.csv",
        "n_units_by_bucket": pd.Series(ops).value_counts().to_dict(),
        "dates": dates, "directions": directions, "seeds": seeds, "thetas": thetas,
        "wait_weight": 1.0, "escape_mode": "s0", "events_dir": args.events_dir,
        "git": _git_sha(), "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    rows: list[dict] = []
    quiet = lambda *_: None  # noqa: E731
    for direction in directions:
        db = db_copy(root, direction, daily, work)
        os.environ["EVDT_DB_PATH"] = str(db)
        lookup = previous.read_station_lookup(db, direction).set_index("station_id")["key"]
        for day in dates:
            o_day = obs[(obs["direction"] == direction) & (obs["date"] == day)]
            o_station = o_day.groupby("key")["starts"].sum()
            o_station = o_station[o_station > 0]
            base = {"direction": direction, "date": day, "obs_starts": int(o_station.sum())}
            if args.events_dir:
                for p in sorted(Path(args.events_dir).glob(f"{direction}_{day}_s*_*.parquet")):
                    _d, _day, s, name = p.stem.split("_", 3)
                    if int(s[1:]) not in seeds:
                        continue
                    th = math.inf if name == "UE" else float(name.split("_", 1)[1])
                    rows.append({**base, "seed": int(s[1:]), "model": name.split("_")[0],
                                 "theta": th, **score(pd.read_parquet(p), lookup, ops, o_station)})
                continue
            cfg = R.load_config(scenario_config(root, direction, day, daily))
            for seed in seeds:
                t0 = time.perf_counter()
                with daily.adapter(R, overlay):
                    world = prepare(cfg, seed, root=root, log=quiet)
                for name, th in models:
                    mnl = MnlSettings(theta=th, wait_weight=1.0, seed=seed, escape_mode="s0")
                    _row, ev = run_cell(world, BASE, p=0.0, mnl=mnl, strength=0.0, oracle=None,
                                        s3_window=None, log=quiet)
                    rows.append({**base, "seed": seed, "model": name, "theta": th,
                                 **score(pd.DataFrame(ev), lookup, ops, o_station)})
                pd.DataFrame(rows).to_csv(out / "grid.csv", index=False, encoding="utf-8-sig")
                print(f"  {direction} {day} 시드 {seed}: {time.perf_counter() - t0:.0f}초", flush=True)

    g = pd.DataFrame(rows)
    g.to_csv(out / "grid.csv", index=False, encoding="utf-8-sig")
    verdict = judge(g, seeds)
    (out / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2,
                                                 default=float), encoding="utf-8")
    for scope in SCOPES:
        print(f"\n=== 범위 {scope}: 20개 날짜·방향 TVD (시드 평균) ===")
        for r in verdict[scope]["all_days"]:
            print(f"  {r['model']:<4} θ={r['theta']:<8g} {r['tvd']:.4f} ± {r['ci']:.4f}")
        for fold in FOLDS:
            if fold in verdict[scope]:
                x = verdict[scope][fold]
                print(f"  접기 {fold}: θ={x['chosen_theta']:g} · MNL {x['val_mnl']:.4f} vs "
                      f"S0 {x['val_s0']:.4f} · 차이 {x['diff']:+.4f} ± {x['diff_ci95']:.4f} → "
                      f"{'통과' if x['pass'] else '미달'}")
    print(f"\n출력: {out}")
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
