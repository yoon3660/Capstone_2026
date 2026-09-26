"""충전기 공백 — 엔진이 아니라 **증설**로 풀 부분을 짚는다 (#54).

    python scripts/report_charger_gaps.py

휴게소 간격 · 충전기 수 · 가동률 · **그 구간에서 차를 못 받아 코리도를 벗어난 대수**를
한 표로 놓는다. 마지막 열이 핵심이다 — 가동률이 낮은데 못 받은 차가 많으면
**용량이 아니라 간격**의 문제이고, 엔진이 손댈 수 없다.

읽는 법은 docs/charger_gaps.md.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn
from evdt.io.loaders import load_run_table
from evdt.paths import RUNS_DIR, default_db_path

#: 어느 run 을 볼 것인가. **하드코딩하지 않는다** — scenario_id 가 바뀌면 (#54 의
#: `__replay__` 처럼) 조용히 빈 표가 나오는 게 아니라 여기서 멈춰야 한다.
RUNS = {
    "down": "seollal_2026_down_base__UE__p100__s0007",
    "up": "seollal_2026_up_base__UE__p100__s0007",
}

for direction, run in RUNS.items():
    if not (RUNS_DIR / run / "charge_event.parquet").is_file():
        raise SystemExit(
            f"run 이 없습니다: {run}\n"
            f"  먼저:  python scripts/run_ue.py --config config/scenario_seollal_{direction}.yaml"
            " --seed 7\n"
            "  (scenario_id 를 바꿨다면 이 스크립트 위쪽 RUNS 도 같이 고칠 것)")
    with get_conn(default_db_path(), readonly=True) as conn:
        rows = conn.execute(
            "SELECT s.station_id, s.name, s.offset_km, COALESCE(SUM(c.n_units),0) u,"
            " COALESCE(SUM(c.n_units*c.power_kw),0)/NULLIF(SUM(c.n_units),0) kw"
            " FROM station s LEFT JOIN charger c ON c.station_id=s.station_id"
            f" WHERE s.corridor_id='gyeongbu_{direction}' GROUP BY s.station_id"
            " ORDER BY s.offset_km").fetchall()
    st = pd.DataFrame(rows, columns=["station_id", "name", "offset_km", "units", "kw"])
    st["gap_km"] = st["offset_km"].diff().fillna(st["offset_km"])

    esc = load_run_table(run, "escape_event", RUNS_DIR)
    no_plan = esc[esc["reason"] == "no_plan"]
    ce = load_run_table(run, "charge_event", RUNS_DIR)
    used = ce.groupby("station_id").agg(n=("ev_id", "size"), wait=("wait_min", "mean"),
                                        busy=("charge_min", "sum"))
    st = st.merge(used, left_on="station_id", right_index=True, how="left").fillna(
        {"n": 0, "wait": 0, "busy": 0})
    st["util"] = st["busy"] / (st["units"].clip(lower=1) * 24 * 60)

    # no_plan 차가 진입한 지점 바로 다음 휴게소 = 그 차를 받지 못한 곳
    nxt = np.searchsorted(st["offset_km"].to_numpy(), no_plan["entry_offset_km"].to_numpy(), "left")
    nxt = np.clip(nxt, 0, len(st) - 1)
    st["stranded"] = pd.Series(nxt).value_counts().reindex(range(len(st)), fill_value=0).to_numpy()

    print(f"\n{'='*86}\n[{direction.upper()}] 휴게소 {len(st)}곳 · 충전기 {int(st.units.sum())}기 "
          f"· no_plan 이탈 {len(no_plan):,}대\n{'='*86}")
    print(f"{'km':>7} {'휴게소':<14} {'앞과':>6} {'기':>3} {'kW':>5} {'가동':>5} {'대기':>6} {'못받은차':>8}")
    for _, r in st.iterrows():
        flag = ""
        if r.gap_km >= 25:
            flag += " ←공백"
        if r.units <= 3:
            flag += " ←소규모"
        if r.util >= 0.95:
            flag += " ←포화"
        print(f"{r.offset_km:>7.1f} {r['name'][:13]:<14} {r.gap_km:>6.1f} {int(r.units):>3} "
              f"{r.kw or 0:>5.0f} {r.util:>5.0%} {r.wait:>6.0f} {int(r.stranded):>8,}{flag}")

    print(f"\n  25km 이상 공백 {int((st.gap_km>=25).sum())}곳 · 3기 이하 {int((st.units<=3).sum())}곳")
    top = st.nlargest(3, "stranded")
    print("  차를 가장 많이 못 받은 구간:")
    for _, r in top.iterrows():
        print(f"    {r['name'][:12]} ({r.offset_km:.0f}km, 앞과 {r.gap_km:.0f}km, {int(r.units)}기) "
              f"— {int(r.stranded):,}대")
