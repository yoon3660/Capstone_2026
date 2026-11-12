"""#168 결정 문서 조건 ② 확인 — p = 0.6 에서 엔진이 비참여자를 아는 정도별 비교 (새 실행 없음).

    python scripts/analyze_p06_info_168.py --root runs/mnl_world/seollal_up_adoption_p06_info

입력: `run_mnl_world.py --p 0.6 --nonpart-info {none,present,plans}` 의 cells.csv 세 개.
천장(ORACLE@p0.6)은 정보 방식과 무관하므로 none 실행에서만 잰다.

내는 것 (시드 대응, 95% CI):
    엔진 − 바닥        엔진이 MNL 바닥보다 나쁜가 (조건 ② 의 증상)
    엔진 − 천장        천장 검사 (조건 ④)
    이탈 / S1@p0.6    이탈 맞춤 (조건 ③, ±35%)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import _ci  # noqa: E402

MODES = ("none", "present", "plans")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    args = ap.parse_args()
    root = Path(args.root)

    cells = {}
    for m in MODES:
        c = pd.read_csv(root / m / "cells.csv")
        c = c[c["final"].astype(bool)]
        cells[m] = c
    floor = cells["none"][cells["none"]["cell"] == "MNL"].set_index("seed")["total_h"]
    ceil = cells["none"][cells["none"]["cell"] == "ORACLE@p0.6"].set_index("seed")
    ceil_t = ceil["total_h"]

    rows = []
    for m in MODES:
        c = cells[m]
        s1_esc = c[c["cell"] == "S1@p0.6"].set_index("seed")["n_escaped"]
        for eng in ("S1@p0.6", "S2@p0.6"):
            g = c[c["cell"] == eng].set_index("seed")
            vs_floor = g["total_h"] - floor.reindex(g.index)
            vs_ceil = g["total_h"] - ceil_t.reindex(g.index)
            rows.append({
                "info": m, "engine": eng.split("@")[0], "total_h": g["total_h"].mean(),
                "n_escaped": g["n_escaped"].mean(),
                "escape_vs_S1": (g["n_escaped"] / s1_esc.reindex(g.index)).mean(),
                "minus_floor_h": vs_floor.mean(), "minus_floor_ci": _ci(vs_floor),
                "worse_than_floor_seeds": int((vs_floor > 0).sum()),
                "minus_ceiling_h": vs_ceil.mean(), "minus_ceiling_ci": _ci(vs_ceil),
                "cheaper_than_ceiling_seeds": int((vs_ceil < 0).sum()),
                "nonpart_booked": g.get("nonpart_booked", pd.Series(dtype=float)).mean(),
                "n_seeds": len(g)})
    t = pd.DataFrame(rows)
    out = root / "comparison.csv"
    t.to_csv(out, index=False, encoding="utf-8-sig")
    pd.set_option("display.width", 220)
    print(f"바닥(MNL) {floor.mean():,.1f}h · 천장(p=0.6) {ceil_t.mean():,.1f}h "
          f"(이탈 {ceil['n_escaped'].mean():.0f}, S1@none 대비 "
          f"{(ceil['n_escaped'] / cells['none'][cells['none']['cell'] == 'S1@p0.6'].set_index('seed')['n_escaped']).mean():.2f})")
    print(t.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print(f"출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
