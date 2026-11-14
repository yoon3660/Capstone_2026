"""혼잡 무대 바닥 후보 판정 — 모드 A θ 격자와 SUE 의 이탈 맞춤·격차 축소율 (#174 A·C).

    python scripts/analyze_floor_174.py --classic runs/mnl_w_sensitivity_173 \\
        --floors runs/floor_adoption_174 --out runs/floor_adoption_174/verdict

입력
    --classic   #168 기존 세계 칸(`classic_*/cells.csv`): S0 · 완전예측(ORACLE) · UE
    --floors    `run_floor_adoption_174.py` 출력 폴더들(하위 */cells.csv): 모드 A · SUE 바닥

계산은 `analyze_ceiling_v2_168.py` 와 같다.
    이탈 맞춤    바닥 이탈 / S0 이탈 (시드 평균). #174 아래쪽 조건: 0.65~1.35
    격차 축소율  (바닥 − 완전예측) / (바닥 − UE) × 100, E ∈ {0, 1, 2}h · 소진 제외
                 기존(S0 바닥) 값과의 차이를 시드 대응으로 본다
    바닥 − 완전예측  음수면 바닥이 이미 완전정보 천장보다 싸다 → 격차 축소율이 뜻을 잃는다
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from analyze_ceiling_v2_168 import E_GRID, _cost  # noqa: E402
from run_mnl_world import _ci  # noqa: E402

LOW, HIGH = 0.65, 1.35


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--classic", required=True)
    ap.add_argument("--floors", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cl = pd.concat([pd.read_csv(p) for p in sorted(Path(args.classic).glob("classic_*/cells.csv"))],
                   ignore_index=True)
    cl = cl[cl["family"] == "classic"]

    def cell(stage: str) -> pd.DataFrame:
        g = cl[cl["stage"] == stage]
        if stage == "ORACLE":
            g = g[g["round"] >= 1]
            g = g.loc[g.groupby("seed")["round"].idxmax()]
        return g.set_index("seed").sort_index()

    s0, ue, pf = cell("S0"), cell("UE"), cell("ORACLE")
    seeds = sorted(set(s0.index) & set(ue.index) & set(pf.index))

    fl = pd.concat([pd.read_csv(p) for p in sorted(Path(args.floors).glob("*/cells.csv"))],
                   ignore_index=True)
    fl = fl[fl["converged"].astype(bool) & fl["dwell_total_h"].notna()]

    def closure(f: pd.DataFrame, e: float, ex: bool) -> pd.Series:
        fc = _cost(f.reindex(seeds), e, ex_exhausted=ex)
        c = _cost(pf.reindex(seeds), e, ex_exhausted=ex)
        u = _cost(ue.reindex(seeds), e, ex_exhausted=ex)
        return (fc - c) / (fc - u) * 100.0

    bases = [(e, False) for e in E_GRID] + [(2.0, True)]
    rows, summ = [], []
    for (model, th), g in fl.groupby(["floor_model", "theta"]):
        g = g.drop_duplicates("seed").set_index("seed")
        have = [s for s in seeds if s in g.index]
        ratio = (g["n_escaped"].reindex(have) / s0["n_escaped"].reindex(have))
        gap_pf = _cost(g.reindex(have), 2.0) - _cost(pf.reindex(have), 2.0)
        rec = {"floor_model": model, "theta": th, "n_seeds": len(have),
               "escape_ratio": float(ratio.mean()), "escape_ratio_ci": _ci(ratio),
               "escape_ok": bool(LOW <= ratio.mean() <= HIGH),
               "floor_total_h_E2": float(_cost(g.reindex(have), 2.0).mean()),
               "floor_escaped": float(g["n_escaped"].reindex(have).mean()),
               "floor_wait_min": float(g["wait_mean_min"].reindex(have).mean()),
               "floor_minus_oracle_h": float(gap_pf.mean()), "floor_minus_oracle_ci": _ci(gap_pf),
               "seconds_mean": float(g["seconds"].reindex(have).mean()),
               "iterations_mean": float(g["iterations"].reindex(have).mean())}
        if len(have) == len(seeds):
            for e, ex in bases:
                m, c = closure(g, e, ex), closure(s0, e, ex)
                d = m - c
                rows.append({"floor_model": model, "theta": th, "E_h": e,
                             "basis": "소진제외" if ex else "총비용",
                             "closure_pct": m.mean(), "closure_ci": _ci(m),
                             "diff_vs_S0_pctp": d.mean(), "diff_ci": _ci(d)})
            main_ = [r for r in rows if r["floor_model"] == model and r["theta"] == th
                     and r["E_h"] == 2.0 and r["basis"] == "총비용"][0]
            rec.update({"closure_E2": main_["closure_pct"], "diff_E2": main_["diff_vs_S0_pctp"],
                        "diff_E2_ci": main_["diff_ci"]})
        summ.append(rec)

    base = {"S0": s0, "UE": ue, "ORACLE": pf}
    ref = {k: {"total_h_E2": float(_cost(v.reindex(seeds), 2.0).mean()),
               "escaped": float(v["n_escaped"].reindex(seeds).mean()),
               "wait_min": float(v["wait_mean_min"].reindex(seeds).mean()),
               "seconds": float(v["seconds"].reindex(seeds).mean())} for k, v in base.items()}
    s0c = closure(s0, 2.0, False)
    ref["S0"]["closure_E2"] = float(s0c.mean())
    pd.DataFrame(summ).to_csv(out / "floor_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(rows).to_csv(out / "closure_table.csv", index=False, encoding="utf-8-sig")
    (out / "reference.json").write_text(json.dumps({"seeds": seeds, **ref}, ensure_ascii=False,
                                                   indent=2), encoding="utf-8")
    pd.set_option("display.width", 220)
    print("=== 기준 (시드", seeds, ") ===")
    print(json.dumps(ref, ensure_ascii=False, indent=1))
    print("\n=== 바닥 후보 ===")
    print(pd.DataFrame(summ).to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print(f"\n출력: {out}")
    return 0 if not math.isnan(ref["S0"]["closure_E2"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
