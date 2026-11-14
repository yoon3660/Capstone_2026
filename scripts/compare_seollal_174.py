"""설 대조표 — S0 / UE / 모드 A / SUE 를 같은 TVD 표로 (#174 B).

    python scripts/compare_seollal_174.py \\
        --mode-a runs/tvd_operator_scope/grid.csv \\
        --sue runs/sue_seollal_174/scope/grid.csv \\
        --cells runs/sue_seollal_174/cells.csv --seeds 1-3 --out runs/sue_seollal_174/compare

입력
    --mode-a   `tvd_operator_scope.py` 가 시뮬레이션으로 낸 grid (S0 · 모드 A θ 격자)
    --sue      `tvd_operator_scope.py --events-dir` 로 `run_sue_seollal.py` 충전 기록을 채점한 grid
               (UE · SUE θ 격자)
    --cells    `run_sue_seollal.py` cells.csv — 반복 횟수·수렴·시간

TVD 는 두 범위다: all(모델 전체 충전) · ME(모델의 환경부 충전기만 = 실측과 같은 범위).
날짜·방향 합계는 관측 충전 수로 가중한다(#168 과 같다).

판정 (범위마다)
    곡선      20개 날짜·방향 전체, 모델·θ 별 시드 평균 ± 95% CI
    접기      #168 홀짝 날. 보정용 날에서 가족(A·SUE)마다 θ 를 고르고, 검증용 날에서
              시드 대응 차이를 본다: SUE − UE, SUE − S0, A − S0, SUE − A
              "가깝다" = 차이 + CI < 0
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from calibrate_mnl_seollal import DATES, FOLDS  # noqa: E402
from run_mnl_world import _ci  # noqa: E402

import evdt.runner as R  # noqa: E402

SCOPES = ("all", "ME")


def load(mode_a: Path, sue: Path, seeds: list[int]) -> pd.DataFrame:
    a = pd.read_csv(mode_a)
    a["model"] = a["model"].replace({"MNL": "A"})
    s = pd.read_csv(sue)
    g = pd.concat([a, s], ignore_index=True)
    g = g[g["seed"].isin(seeds)].copy()
    g["theta"] = g["theta"].astype(float)
    return g


def weighted(g: pd.DataFrame, days: list[str], col: str) -> pd.DataFrame:
    s = g[g["date"].isin(days)]
    return (s.assign(wt=s[col] * s["obs_starts"])
            .groupby(["model", "theta", "seed"], as_index=False)[["wt", "obs_starts"]].sum()
            .assign(tvd=lambda x: x["wt"] / x["obs_starts"]).drop(columns=["wt", "obs_starts"]))


def _pick(cal: pd.DataFrame, model: str) -> float:
    c = cal[cal["model"] == model].groupby("theta")["tvd"].mean()
    return float(c.idxmin())


def _series(v: pd.DataFrame, model: str, theta: float, seeds: list[int]) -> pd.Series:
    x = v[(v["model"] == model) & (v["theta"] == theta)].set_index("seed")["tvd"]
    return x.reindex(seeds)


def judge(g: pd.DataFrame, seeds: list[int]) -> dict:
    out: dict = {}
    for scope in SCOPES:
        col = f"tvd_{scope}"
        allw = weighted(g, sorted(set(g["date"])), col)
        curve = (allw.groupby(["model", "theta"], as_index=False)
                 .agg(tvd=("tvd", "mean"), ci=("tvd", _ci)))
        res: dict = {"curve": curve.to_dict("records"), "folds": {}}
        if set(g["date"]) >= set(DATES):
            for fold, (cal_days, val_days) in FOLDS.items():
                cal, val = weighted(g, cal_days, col), weighted(g, val_days, col)
                th = {"A": _pick(cal, "A"), "SUE": _pick(cal, "SUE")}
                v = {"S0": _series(val, "S0", math.inf, seeds),
                     "UE": _series(val, "UE", math.inf, seeds),
                     "A": _series(val, "A", th["A"], seeds),
                     "SUE": _series(val, "SUE", th["SUE"], seeds)}
                pairs = {}
                for a, b in (("SUE", "UE"), ("SUE", "S0"), ("A", "S0"), ("SUE", "A")):
                    d = v[a] - v[b]
                    ci = _ci(d)
                    pairs[f"{a}-{b}"] = {"diff": float(d.mean()), "ci95": ci,
                                         "closer": bool(d.mean() + ci < 0),
                                         "farther": bool(d.mean() - ci > 0)}
                res["folds"][fold] = {"theta": th,
                                      "val_tvd": {k: float(x.mean()) for k, x in v.items()},
                                      "pairs": pairs}
        out[scope] = res
    return out


def runtime(cells: Path) -> pd.DataFrame:
    c = pd.read_csv(cells)
    return (c.groupby(["model"], as_index=False)
            .agg(n=("seconds", "size"), converged=("converged", "mean"),
                 iter_mean=("iterations", "mean"), iter_max=("iterations", "max"),
                 sec_mean=("seconds", "mean"), sec_max=("seconds", "max"),
                 escaped_mean=("n_escaped", "mean"), wait_mean=("wait_mean_min", "mean")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode-a", required=True)
    ap.add_argument("--sue", required=True)
    ap.add_argument("--cells", default=None)
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    seeds = R.parse_seeds(args.seeds)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    g = load(Path(args.mode_a), Path(args.sue), seeds)
    g.to_csv(out / "grid_all_models.csv", index=False, encoding="utf-8-sig")
    verdict = judge(g, seeds)
    (out / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2,
                                                 default=float), encoding="utf-8")
    for scope in SCOPES:
        print(f"\n=== 범위 {scope}: 20개 날짜·방향 TVD (시드 {seeds} 평균) ===")
        for r in verdict[scope]["curve"]:
            print(f"  {r['model']:<4} θ={r['theta']:<8g} {r['tvd']:.4f} ± {r['ci']:.4f}")
        for fold, x in verdict[scope]["folds"].items():
            th = x["theta"]
            print(f"  접기 {fold}: θ_A={th['A']:g} θ_SUE={th['SUE']:g} · 검증 TVD "
                  + " ".join(f"{k} {t:.4f}" for k, t in x["val_tvd"].items()))
            for k, p in x["pairs"].items():
                tag = "가깝다" if p["closer"] else ("멀다" if p["farther"] else "구분 안 됨")
                print(f"      {k:<7} {p['diff']:+.4f} ± {p['ci95']:.4f} → {tag}")
    if args.cells:
        rt = runtime(Path(args.cells))
        rt.to_csv(out / "runtime.csv", index=False, encoding="utf-8-sig")
        print("\n=== 실행 시간·수렴 ===")
        print(rt.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print(f"\n출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
