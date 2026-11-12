"""#168 개선 3 판정표 — θ·w 범위별 MNL 바닥, 격차 축소율, 천장 검사 (새 실행 없음).

    python scripts/analyze_ceiling_v2_168.py --root runs/mnl_world/seollal_up_adoption_v2

입력 (`run_mnl_world.py` 결과):
    classic_*/cells.csv        기존 세계 S0·S1·S2·S3·MEC·완전예측(ORACLE)·UE + MNL 바닥(θ 0.05, w 1.0)
    floor_theta<θ>[_w1.5]/     MNL 바닥만 (θ·w 범위)
    s4_seeds135/stages.csv     S4 (decompose_ladder_escapes.py, 있으면)

미리 정한 판정 (결과 문서 §5.1, 팀 보고 §5):
    기준 2   MNL 바닥 이탈 / S0 이탈 이 0.65~1.35 (시드 평균)
    기준 3   격차 축소율 방향(MNL − 기존)이 이탈 비용 E ∈ {0, 1, 2}h 와 소진 제외 기준에서 같다
    (가)/(나)/(다)   기준 2 를 통과한 θ·w 들의 MNL − 기존 차이:
             모두 CI 가 0 을 포함 → (가) · 모두 같은 부호(그중 CI 가 0 을 안 포함하는 것 있음) → (나)
             부호가 갈림 → (다)
    천장     엔진 − 완전예측 (마지막 회차·1회차), 시드 대응 CI 와 엔진이 더 싼 시드 수

천장은 두 세계 모두 기존 완전예측(1회차 미래 = S1)이다. p = 1 이면 같은 계산이다(결과 문서 §6).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import _ci  # noqa: E402

E_GRID = (0.0, 1.0, 2.0)


def _cost(df: pd.DataFrame, e_h: float, *, ex_exhausted: bool = False) -> pd.Series:
    n = df["n_escaped"] - (df["n_exhausted"] if ex_exhausted else 0)
    return df["dwell_total_h"] + e_h * n


def load(root: Path) -> tuple[pd.DataFrame, dict[tuple[float, float], pd.DataFrame]]:
    classic = pd.concat([pd.read_csv(p) for p in sorted(root.glob("classic_*/cells.csv"))],
                        ignore_index=True)
    classic["final"] = classic["final"].astype(bool)
    floors: dict[tuple[float, float], pd.DataFrame] = {}
    mnl = classic[classic["family"] == "mnl"]
    for (th, w), g in mnl.groupby(["theta", "wait_weight"]):
        floors[(float(th), float(w))] = g.set_index("seed")
    for d in sorted(root.glob("floor_theta*")):
        c = pd.read_csv(d / "cells.csv")
        c = c[c["stage"] == "MNL"]
        for (th, w), g in c.groupby(["theta", "wait_weight"]):
            floors.setdefault((float(th), float(w)), g.set_index("seed"))
    return classic, floors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = Path(args.root)
    out = Path(args.out) if args.out else root / "verdict"
    out.mkdir(parents=True, exist_ok=True)

    classic, floors = load(root)
    cl = classic[classic["family"] == "classic"]

    def cell(stage: str, which: str = "final") -> pd.DataFrame:
        g = cl[cl["stage"] == stage]
        if stage == "ORACLE":
            g = g[g["round"] >= 1]
            idx = (g.groupby("seed")["round"].idxmax() if which == "final"
                   else g.groupby("seed")["round"].idxmin())
            g = g.loc[idx]
        return g.set_index("seed").sort_index()

    s0, ue, pf, pf1 = cell("S0"), cell("UE"), cell("ORACLE"), cell("ORACLE", "round1")
    seeds = sorted(set(s0.index) & set(ue.index) & set(pf.index))

    def closure(floor: pd.DataFrame, ceil: pd.DataFrame, e_h: float, ex: bool) -> pd.Series:
        f = _cost(floor.reindex(seeds), e_h, ex_exhausted=ex)
        c = _cost(ceil.reindex(seeds), e_h, ex_exhausted=ex)
        u = _cost(ue.reindex(seeds), e_h, ex_exhausted=ex)
        return (f - c) / (f - u) * 100.0

    bases = [(e, False) for e in E_GRID] + [(2.0, True)]
    rows = []
    # 기존 세계 (S0 바닥)
    for e, ex in bases:
        a = closure(s0, pf, e, ex)
        rows.append({"floor": "S0", "theta": math.inf, "w": 1.0, "E_h": e,
                     "basis": "소진제외" if ex else "총비용", "closure_pct": a.mean(),
                     "closure_ci": _ci(a), "diff_vs_S0_pctp": 0.0, "diff_ci": 0.0,
                     "mnl_lower_seeds": 0, "n_seeds": len(seeds)})
    crit2 = {}
    for (th, w), fl in sorted(floors.items()):
        fl = fl.reindex(seeds)
        ratio = (fl["n_escaped"] / s0["n_escaped"].reindex(seeds)).mean()
        crit2[(th, w)] = {"escape_ratio": float(ratio), "pass": bool(0.65 <= ratio <= 1.35),
                          "floor_total_h": float(_cost(fl, 2.0).mean()),
                          "floor_escaped": float(fl["n_escaped"].mean()),
                          "floor_wait": float(fl["wait_mean_min"].mean())}
        for e, ex in bases:
            m = closure(fl, pf, e, ex)
            c = closure(s0, pf, e, ex)
            d = m - c
            rows.append({"floor": "MNL", "theta": th, "w": w, "E_h": e,
                         "basis": "소진제외" if ex else "총비용", "closure_pct": m.mean(),
                         "closure_ci": _ci(m), "diff_vs_S0_pctp": d.mean(), "diff_ci": _ci(d),
                         "mnl_lower_seeds": int((d < 0).sum()), "n_seeds": len(seeds)})
    tab = pd.DataFrame(rows)
    tab.to_csv(out / "closure_table.csv", index=False, encoding="utf-8-sig")

    # 판정 (가)/(나)/(다) — 기준 2 통과한 θ·w, E=2h 총비용 기준 · 방향은 모든 기준
    verdict_rows = []
    for (th, w), c2 in crit2.items():
        sub = tab[(tab["floor"] == "MNL") & (tab["theta"] == th) & (tab["w"] == w)]
        signs = {math.copysign(1, x) for x in sub["diff_vs_S0_pctp"] if abs(x) > 1e-9}
        main = sub[(sub["E_h"] == 2.0) & (sub["basis"] == "총비용")].iloc[0]
        verdict_rows.append({
            "theta": th, "w": w, **c2,
            "closure_E2": main["closure_pct"], "diff_E2": main["diff_vs_S0_pctp"],
            "diff_E2_ci": main["diff_ci"],
            "diff_E2_ci_has_0": bool(abs(main["diff_vs_S0_pctp"]) <= main["diff_ci"]),
            "direction_consistent": len(signs) <= 1,
            "direction": ("−" if signs == {-1.0} else "+" if signs == {1.0} else "혼재"),
        })
    v = pd.DataFrame(verdict_rows)
    v.to_csv(out / "verdict_by_theta.csv", index=False, encoding="utf-8-sig")
    passed = v[v["pass"]]
    if passed.empty:
        overall = "기준2 통과 없음 → C"
    elif not passed["direction_consistent"].all() or passed["direction"].nunique() > 1:
        overall = "(다) 방향이 갈림 → C"
    elif passed["diff_E2_ci_has_0"].all():
        overall = "(가) 27.5%와 CI 안에서 같음 → B"
    else:
        overall = "(나) 방향 같고 크기 다름 → B"

    # 천장 검사
    chk = []
    s4 = None
    s4p = root / "s4_seeds135" / "stages.csv"
    if s4p.is_file():
        s4 = pd.read_csv(s4p).set_index("seed")
        s4 = s4.rename(columns={"total_h": "_t"})
        s4["dwell_total_h"] = s4["dwell_h"]
    for name, eng in [("S2", cell("S2")), ("S3", cell("S3")), ("MEC", cell("MEC"))] + (
            [("S4", s4)] if s4 is not None else []):
        for which, ceil in (("마지막 회차", pf), ("1회차", pf1)):
            idx = sorted(set(eng.index) & set(ceil.index))
            d = _cost(eng.reindex(idx), 2.0) - _cost(ceil.reindex(idx), 2.0)
            ci = _ci(d)
            chk.append({"engine": name, "ceiling": which, "n_seeds": len(idx),
                        "diff_h": d.mean(), "ci95": ci,
                        "engine_cheaper_seeds": int((d < 0).sum()),
                        "verdict": ("통과" if d.mean() >= 0 else
                                    ("위반 확정" if not math.isnan(ci) and d.mean() + ci < 0
                                     else "위반 의심"))})
    chk = pd.DataFrame(chk)
    chk.to_csv(out / "ceiling_check.csv", index=False, encoding="utf-8-sig")

    summary = {
        "seeds": seeds,
        "classic_closure_E2": float(tab[(tab["floor"] == "S0") & (tab["E_h"] == 2.0)
                                        & (tab["basis"] == "총비용")]["closure_pct"].iloc[0]),
        "overall": overall,
        "means": {k: float(_cost(c, 2.0).mean()) for k, c in
                  (("S0", s0), ("UE", ue), ("ORACLE_final", pf), ("ORACLE_round1", pf1))},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    pd.set_option("display.width", 220)
    f = lambda x: f"{x:,.2f}"  # noqa: E731
    print("=== 평균 (E=2h 총비용) ===", json.dumps(summary["means"], ensure_ascii=False))
    print("\n=== θ·w 별 판정 ===")
    print(v.to_string(index=False, float_format=f))
    print("\n=== 격차 축소율 (UE 기준) ===")
    print(tab.to_string(index=False, float_format=f))
    print("\n=== 천장 검사 (엔진 − 완전예측) ===")
    print(chk.to_string(index=False, float_format=f))
    print(f"\n종합: {overall}\n출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
