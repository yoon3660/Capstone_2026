"""MNL 설 독립 검증 — 추석에서 고정한 θ 로 설 실측 분배를 얼마나 맞추나 (#168).

    python scripts/validate_mnl_seollal.py --seeds 1-3 --data-root <evdt 저장소 루트> \\
        --observed <evdt>/data/processed/sp3_64_me_ue_validation_s0007_scopefix/validation_station_hour.csv

## 무엇을 하나

추석 보정으로 채택한 θ = 0.05, w = 1.0 을 **고치지 않고** 설 재현 무대에 그대로 넣는다.
#64 의 설 대조와 같은 무대·같은 날이다:

    하행  config/scenario_seollal_down.yaml   2026-02-13 (입력 프로파일과 일치한 하루)
    상행  config/scenario_seollal_up.yaml     2026-02-17

실측은 #64 의 휴게소×시간 표(`validation_station_hour.csv`, OBSERVED 칸만)다. 모델은
그날 00:00~24:00 에 **시작한** 충전만 센다 (#64 와 같은 규칙, 24시 이후는 이월로 뺀다).

비교는 추석 보정과 같은 지표다 (`evdt.eval.choice_fit`): 관측 휴게소 안에서의 **점유율 TVD**.
실측은 환경부 제공 이력이고 모델은 전체 운영사라 총량은 판정에 쓰지 않는다 (#64 판정 보류 사유).

    S0         θ = inf  (기존 바닥)
    MNL        θ = 0.05, w = 1.0  (추석 채택값, 재보정 없음)
    UE         참고 (같은 시드로 다시 푼다)

## 하지 않는 것

- **θ 를 다시 맞추지 않는다.** 설 결과를 보고 θ 를 바꾸면 독립 검증이 아니다
- DB·run 기록·원본 #64 폴더에 쓰지 않는다
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import (  # noqa: E402
    ADOPTED_THETA,
    BASE,
    _ci,
    _git_sha,
    prepare,
    run_cell,
    run_ue_cell,
)

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import ESCAPE_MODES, MnlSettings  # noqa: E402
from evdt.eval.choice_fit import fit_metrics, restrict_to_observed  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path  # noqa: E402

CASES = (
    ("DOWN", "2026-02-13", "config/scenario_seollal_down.yaml"),
    ("UP", "2026-02-17", "config/scenario_seollal_up.yaml"),
)


def load_observed(path: Path) -> pd.DataFrame:
    obs = pd.read_csv(path)
    obs = obs[obs["observed_flag"] == "OBSERVED"]
    rows = obs.loc[obs.index.repeat(obs["observed_starts"].fillna(0).astype(int))]
    return pd.DataFrame({"direction": rows["direction"].to_numpy(),
                         "station_name": rows["station_id"].to_numpy(),
                         "date": rows["date"].to_numpy(), "hour": rows["hour"].to_numpy()})


def to_frame(events: list[dict], direction: str, date: str) -> pd.DataFrame:
    if not events:
        return pd.DataFrame(columns=["direction", "station_name", "date", "hour"])
    f = pd.DataFrame(events)[["station_id", "t_start_min"]]
    f = f[(f["t_start_min"] >= 0) & (f["t_start_min"] < 1440)]     # 그날 시작만 (#64)
    return pd.DataFrame({"direction": direction, "station_name": f["station_id"].to_numpy(),
                         "date": date, "hour": (f["t_start_min"] // 60).astype(int).to_numpy()})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--observed", required=True, help="#64 validation_station_hour.csv")
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--theta", type=float, default=ADOPTED_THETA)
    ap.add_argument("--wait-weight", type=float, default=1.0)
    ap.add_argument("--escape-mode", choices=ESCAPE_MODES, default="s0",
                    help="이탈 판정: s0 = 잡음 없이 S0 규칙 (기본) · noisy = 이탈에도 잡음 (#168 첫 실행 방식, 민감도)")
    ap.add_argument("--no-ue", action="store_true")
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = Path(args.out) if args.out else RUNS_DIR / "mnl_seollal_holdout"
    out.mkdir(parents=True, exist_ok=True)
    root = Path(args.data_root)
    seeds = R.parse_seeds(args.seeds)
    obs = load_observed(Path(args.observed))
    print(f"설 실측 {len(obs):,}건 · 관측 휴게소 {obs.groupby(['direction', 'station_name']).ngroups}곳 · "
          f"θ {args.theta:g} (고정) · 시드 {seeds}")

    (out / "manifest.json").write_text(json.dumps({
        "issue": 168, "script": "scripts/validate_mnl_seollal.py",
        "observed": str(Path(args.observed).resolve()), "cases": CASES, "seeds": seeds,
        "theta": args.theta, "wait_weight": args.wait_weight,
        "escape_mode": args.escape_mode, "db": str(default_db_path()),
        "data_root": str(root), "git": _git_sha(), "rule": "θ 재보정 없음 (독립 검증)",
        "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    models = [("S0", math.inf), ("MNL", args.theta)]
    pieces: dict[tuple[str, int], list[pd.DataFrame]] = {}
    escapes: dict[tuple[str, int], int] = {}
    quiet = lambda *_: None  # noqa: E731

    for direction, date, cfg_path in CASES:
        cfg = R.load_config(cfg_path)
        for seed in seeds:
            world = prepare(cfg, seed, root=root, log=quiet)
            for name, theta in models:
                mnl = MnlSettings(theta=theta, wait_weight=args.wait_weight, seed=seed,
                                  escape_mode=args.escape_mode)
                row, ev = run_cell(world, BASE, p=0.0, mnl=mnl, strength=0.0, oracle=None,
                                   s3_window=None, log=quiet)
                pieces.setdefault((name, seed), []).append(to_frame(ev, direction, date))
                escapes[(name, seed)] = escapes.get((name, seed), 0) + int(row["n_escaped"])
            if not args.no_ue:
                row, ev = run_ue_cell(world, quiet)
                pieces.setdefault(("UE", seed), []).append(to_frame(ev, direction, date))
                escapes[("UE", seed)] = escapes.get(("UE", seed), 0) + int(row["n_escaped"])
            print(f"  {direction} {date} 시드 {seed}: 충전 필요 {len(world.built.trips):,}대", flush=True)

    rows = []
    for (name, seed), frames in pieces.items():
        model = restrict_to_observed(pd.concat(frames, ignore_index=True), obs)
        for r in fit_metrics(obs, model):
            rows.append({"model": name, "seed": seed, "n_escaped": escapes[(name, seed)], **r})
    g = pd.DataFrame(rows)
    g.to_csv(out / "metrics.csv", index=False, encoding="utf-8-sig")

    summ = []
    for (name, direction), s in g.groupby(["model", "direction"], sort=False):
        summ.append({"model": name, "direction": direction, "n_seeds": len(s),
                     "station_tvd": s["station_tvd"].mean(), "station_tvd_ci95": _ci(s["station_tvd"]),
                     "station_hour_tvd": s["station_hour_tvd"].mean(),
                     "hour_tvd": s["hour_tvd"].mean(),
                     "total_bias_pct": s["total_bias_pct"].mean(),
                     "n_escaped": s["n_escaped"].mean()})
    summary = pd.DataFrame(summ)
    summary.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n=== 설 독립 검증 (휴게소 점유율 TVD, 작을수록 실측 분배에 가깝다) ===")
    print(summary.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))

    # 시드 대응 차이: MNL − S0 (음수면 MNL 이 더 가깝다)
    both = g[g["direction"] == "BOTH"].pivot(index="seed", columns="model", values="station_tvd")
    diff = both["MNL"] - both["S0"]
    print(f"\nMNL − S0 (BOTH 휴게소 TVD, 시드 대응): {diff.mean():+.4f} ± {_ci(diff):.4f}")
    print(f"출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
