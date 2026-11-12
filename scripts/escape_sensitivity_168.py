"""#168 결과의 이탈 분해와 이탈 비용 민감도 — 새 실행 없이 병합 결과만 다시 센다.

    python scripts/escape_sensitivity_168.py \\
        --cells runs/mnl_world/seollal_up_adoption_cmp/merged/cells_merged.csv

## 왜

#168 의 "27.5% → 19%" 는 총비용 = 체류 + 이탈 × 120분 위에서 나왔다. 그런데 MNL 바닥의
이탈(약 315대)은 대부분 **줄 이탈**(MNL 이 이탈 선택지에도 같은 잡음을 줘서 생김)이고,
예외 규칙 R1 이 빼는 **정차 소진 이탈**은 12대뿐이다. 그래서 R2·R3 의 두 번째 기준
(소진 제외)으로는 이 몫이 걸러지지 않는다. 이 스크립트는

1. 이탈을 유형별로 나눈다: 정차 소진 / 줄 이탈 / 기타
2. 바닥 → 천장 비용 감소를 체류 몫과 이탈 유형별 몫으로 나눈다
3. 이탈 1대 비용 E 를 바꿔 가며 UE 기준 격차 축소율을 다시 낸다 (E = 0 이 체류만)
4. MNL 바닥이 S0 바닥보다 싸지는 E 의 경계를 낸다

## 하지 않는 것

- 아무것도 다시 돌리지 않는다. 시뮬레이션 결과는 그대로이고 비용 식의 가중치만 바꾼다
- 이탈 비용의 "맞는 값" 을 정하지 않는다. 결론이 이 가정에 얼마나 기대는지만 보인다
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import _ci  # noqa: E402

E_GRID_H = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0)
CURRENT_E_H = 2.0

# (세계, 역할, cells_merged.csv 의 cell)
CELLS = (
    ("기존", "바닥", "classic:S0"),
    ("기존", "S2", "classic:S2"),
    ("기존", "천장", "classic:ORACLE"),
    ("MNL", "바닥", "MNL"),
    ("MNL", "천장", "ORACLE@p1"),
    ("공통", "UE", "classic:UE"),
)


def pick(cells: pd.DataFrame, which: str) -> pd.DataFrame:
    """칸마다 시드별 한 행. 천장은 which = 'final'(마지막 회차) 또는 'round1'."""

    out = []
    for world, role, cell in CELLS:
        c = cells[cells["cell"] == cell]
        if c.empty:
            raise SystemExit(f"[중단] {cell} 칸이 없다")
        if c["round"].max() > 0:
            c = c[c["round"] >= 1]
            idx = (c.groupby("seed")["round"].idxmax() if which == "final"
                   else c.groupby("seed")["round"].idxmin())
            c = c.loc[idx]
        c = c.assign(world=world, role=role)
        out.append(c)
    t = pd.concat(out, ignore_index=True)
    t["n_other"] = t["n_escaped"] - t["n_escaped_balked"] - t["n_exhausted"]
    return t


def cost(t: pd.DataFrame, e_h: float, *, drop_exhausted: bool = False) -> pd.Series:
    n = t["n_escaped"] - (t["n_exhausted"] if drop_exhausted else 0)
    return t["dwell_total_h"] + e_h * n


def closure(t: pd.DataFrame, world: str, top: str, e_h: float, *,
            drop_exhausted: bool = False) -> pd.Series:
    """(바닥 − top) / (바닥 − UE), 시드별."""

    def by(role, w=world):
        s = t[(t["world"] == w) & (t["role"] == role)].set_index("seed")
        return cost(s, e_h, drop_exhausted=drop_exhausted)

    floor, cell, ue = by("바닥"), by(top), by("UE", "공통")
    return (floor - cell) / (floor - ue) * 100.0


def decompose(t: pd.DataFrame, world: str, e_h: float) -> dict:
    """바닥 → 천장 감소를 체류 / 줄 이탈 / 소진 / 기타로 (시드 평균, h)."""

    f = t[(t["world"] == world) & (t["role"] == "바닥")].set_index("seed")
    c = t[(t["world"] == world) & (t["role"] == "천장")].set_index("seed")
    parts = {
        "체류": (f["dwell_total_h"] - c["dwell_total_h"]).mean(),
        "줄 이탈": ((f["n_escaped_balked"] - c["n_escaped_balked"]) * e_h).mean(),
        "정차 소진": ((f["n_exhausted"] - c["n_exhausted"]) * e_h).mean(),
        "기타 이탈": ((f["n_other"] - c["n_other"]) * e_h).mean(),
    }
    total = sum(parts.values())
    return {"world": world, "감소_h": total,
            **{f"{k}_h": v for k, v in parts.items()},
            **{f"{k}_pct": v / total * 100.0 for k, v in parts.items()}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cells", required=True, help="run_mnl_world.py --merge 의 cells_merged.csv")
    ap.add_argument("--out", default=None, help="기본: cells 파일 옆 escape_sensitivity/")
    args = ap.parse_args()

    src = Path(args.cells)
    out = Path(args.out) if args.out else src.parent / "escape_sensitivity"
    out.mkdir(parents=True, exist_ok=True)
    cells = pd.read_csv(src)
    pd.set_option("display.width", 200)
    fmt = lambda v: f"{v:,.1f}"  # noqa: E731

    report: dict = {"source": str(src.resolve()), "e_grid_h": E_GRID_H}
    for which in ("final", "round1"):
        t = pick(cells, which)
        tag = "마지막 회차" if which == "final" else "1회차"

        # 1. 이탈 유형 (시드 평균)
        kinds = (t.groupby(["world", "role"], sort=False)[
            ["dwell_total_h", "n_escaped", "n_escaped_balked", "n_exhausted", "n_other"]]
            .mean().reset_index())
        kinds.to_csv(out / f"escape_kinds_{which}.csv", index=False, encoding="utf-8-sig")

        # 2. 감소 분해 (현재 가정 E = 2h)
        dec = pd.DataFrame([decompose(t, w, CURRENT_E_H) for w in ("기존", "MNL")])
        dec.to_csv(out / f"drop_decomposition_{which}.csv", index=False, encoding="utf-8-sig")

        # 3. 민감도: E 별 격차 축소율 + 시드 대응 차이
        rows = []
        for e in E_GRID_H:
            for drop_ex in (False, True):
                a = closure(t, "기존", "천장", e, drop_exhausted=drop_ex)
                b = closure(t, "MNL", "천장", e, drop_exhausted=drop_ex)
                s2 = closure(t, "기존", "S2", e, drop_exhausted=drop_ex)
                d = b - a
                rows.append({"E_h": e, "기준": "소진 제외" if drop_ex else "총비용",
                             "기존_천장_pct": a.mean(), "기존_천장_ci": _ci(a),
                             "기존_S2_pct": s2.mean(),
                             "MNL_천장_pct": b.mean(), "MNL_천장_ci": _ci(b),
                             "차이_pctp": d.mean(), "차이_ci": _ci(d),
                             "MNL이_낮은_시드": int((d < 0).sum()), "n_seeds": int(d.notna().sum())})
        sens = pd.DataFrame(rows)
        sens.to_csv(out / f"sensitivity_{which}.csv", index=False, encoding="utf-8-sig")

        # 4. MNL 바닥 = S0 바닥이 되는 E (시드별, 시드 평균)
        f0 = t[(t["world"] == "기존") & (t["role"] == "바닥")].set_index("seed")
        f1 = t[(t["world"] == "MNL") & (t["role"] == "바닥")].set_index("seed")
        dn = f1["n_escaped"] - f0["n_escaped"]
        be = (f0["dwell_total_h"] - f1["dwell_total_h"]) / dn.where(dn != 0)
        be_mean = ((f0["dwell_total_h"] - f1["dwell_total_h"]).mean() / dn.mean()
                   if dn.mean() else math.nan)

        print(f"\n######## 천장 = 완전예측 {tag} ########")
        print("\n[1] 이탈 유형 (시드 평균)")
        print(kinds.to_string(index=False, float_format=fmt))
        print(f"\n[2] 바닥 → 천장 감소 분해 (이탈 1대 = {CURRENT_E_H:g}h)")
        print(dec.to_string(index=False, float_format=fmt))
        print("\n[3] 이탈 비용 민감도 (UE 기준 격차 축소율 %, ± 는 95% CI 반폭)")
        print(sens.to_string(index=False, float_format=fmt))
        print(f"\n[4] MNL 바닥이 S0 바닥과 같아지는 이탈 비용: 평균 기준 {be_mean:.2f}h · "
              f"시드별 {', '.join(f'{v:.2f}' for v in be)}h")
        report[which] = {"breakeven_e_h_mean": be_mean,
                         "breakeven_e_h_by_seed": {int(k): float(v) for k, v in be.items()}}

    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"\n출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
