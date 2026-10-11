"""격차 메우기 표 — 각 칸이 S0→UE 의 몇 %를 메웠나 (#121, 설계문서 §8.2).

    python scripts/gap_table.py --config config/scenario_seollal_up_adoption.yaml --seeds 1-4

## 왜 이 형태인가

단계별 절대값을 나열하면 *"그래서 얼마나 한 거냐"* 에 답이 안 된다. **천장과 바닥을
먼저 고정**하면 각 칸이 한 숫자가 된다.

    바닥  S0   가장 적은 정보 (지금 화면만)
    천장  UE   전원이 서로의 최종 선택을 안다 (반복 균형)

    메운 비율 = (S0 − 이 칸) / (S0 − UE)

## UE 를 무엇으로 쓰는가 — (A) 천장

UE 는 **하루 전체 계획을 열거하고 반복 균형을 돌린다.** 어떤 실시간 엔진보다 정보가
많다. 그래서 여기서는 **참고 천장**으로 쓴다 — 사다리는 UE 를 *넘는* 것이 아니라
*메우는* 구조다.

> ⚠ 설계문서 §8.2 는 UE 를 *"실측과 대조해 고른 행동 모델"* = **엔진이 이겨야 할
> 베이스라인**으로 적었다. 둘은 양립하지 않는다. 이 스크립트는 (A) 를 택했고,
> **§8.2 의 문구를 고치는 것이 남은 일이다.** 발표에서 *"왜 당신 엔진이
> 베이스라인보다 나쁜가"* 를 반드시 맞으므로, 고르지 않은 채로 두지 않는다.

## ⚠ S0 vs S1 의 우열은 가정 하나에 달려 있다

S0 는 못 기다리는 차를 **고속도로 밖으로 내보낸다.** 그래서 총비용에 이탈을 세는데,
**이탈 비용을 얼마로 보느냐가 순위를 뒤집는다.** 이 스크립트는 손익분기를 같이
낸다 — 가정값이 그 근처면 "S1 이 S0 보다 나쁘다" 는 **결론이 아니라 가정**이다.
"""

from __future__ import annotations

import argparse
import math
import sqlite3

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds  # noqa: E402

LADDER = ("S0", "S1", "S2", "S3", "S4")
FLOOR, CEIL = "S0", "UE"


def mean_ci(v: pd.Series) -> tuple[float, float]:
    """평균과 95% 신뢰구간 반폭 (t 분포). 한 시드로 발표하지 않기 위한 것."""

    n = len(v)
    if n < 2:
        return float(v.mean()), float("nan")
    from scipy import stats
    return float(v.mean()), float(stats.t.ppf(0.975, n - 1) * v.std(ddof=1) / math.sqrt(n))


def closed(value: float, floor: float, ceil: float) -> float:
    """바닥 대비 천장까지의 몇 %를 메웠나. 바닥=0%, 천장=100%."""

    span = floor - ceil
    return float("nan") if abs(span) < 1e-12 else (floor - value) / span * 100.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-20")
    args = ap.parse_args()

    cfg = load_config(args.config)
    esc_min = float(cfg.demand.escape_cost_min)

    with sqlite3.connect(default_db_path()) as conn:
        done = {r[0] for r in conn.execute("SELECT run_id FROM run WHERE status='DONE'")}
        kpi = pd.read_sql("SELECT run_id, metric, value FROM run_kpi", conn)
    wide = kpi.pivot(index="run_id", columns="metric", values="value")

    want = [CEIL, *LADDER]
    rows = []
    for seed in parse_seeds(args.seeds):
        ids = {st: make_run_id(cfg.scenario_id, st, seed, cfg.policy.participation)
               for st in want}
        # **네 단계가 모두 끝낸 시드만** 쓴다 (#59 · compare_stages3.py 와 같은 규칙)
        need = [st for st in (CEIL, "S0", "S1") if ids[st] not in done]
        if need:
            continue
        for st in want:
            rid = ids[st]
            if rid not in done:
                continue
            k = wide.loc[rid]
            rows.append({
                "seed": seed, "stage": st,
                "wait_mean_min": k["wait_mean_min"],
                "bottleneck_slots": k["bottleneck_slots"],
                "n_escaped": k["n_escaped"],
                "dwell_total_h": k["dwell_total_h"],
                "n_ev_charging": k["n_ev_charging"],
                "total_h": k["dwell_total_h"] + k["n_escaped"] * esc_min / 60.0,
            })

    if not rows:
        raise SystemExit("\n[중단] UE·S0·S1 을 모두 끝낸 시드가 없다.")

    t = pd.DataFrame(rows)

    # 같은 시드 안에서 단계마다 세계가 같은지 (#59)
    bad = {s: g["n_ev_charging"].unique().tolist()
           for s, g in t.groupby("seed") if g["n_ev_charging"].nunique() > 1}
    if bad:
        raise SystemExit(f"\n[중단] 같은 시드인데 단계마다 충전 필요 EV 가 다르다: {bad}")

    have = {st: sorted(t[t.stage == st].seed) for st in want}

    print(f"\n{cfg.scenario_id}   ·   이탈 비용 가정 {esc_min:.0f}분")
    print(f"수요: {cfg.demand_label}")
    for st in want:
        n = len(have[st])
        print(f"  {st:3} 시드 {n:2d}개" + ("" if n else "   ← 아직 없음"))
    print(f"\n기준점: 바닥 {FLOOR} = 0%  ·  천장 {CEIL} = 100%   (UE 는 **참고 천장**이다)")

    # === 표 ===============================================================
    out = []
    for metric, label in (("wait_mean_min", "평균 대기(분)"), ("total_h", "총 사회적 비용(h)")):
        base = {st: t[t.stage == st][metric].mean() for st in want if have[st]}
        for st in want:
            if not have[st]:
                out.append({"지표": label, "칸": st, "값": None, "±95%": None,
                            "메운%": None, "시드": 0})
                continue
            m, ci = mean_ci(t[t.stage == st][metric])
            out.append({
                "지표": label, "칸": st, "값": m, "±95%": ci,
                "메운%": 0.0 if st == FLOOR else (100.0 if st == CEIL
                        else closed(m, base[FLOOR], base[CEIL])),
                "시드": len(have[st]),
            })
    o = pd.DataFrame(out)
    print("\n=== 격차 메우기 ===")
    for label in o["지표"].unique():
        g = o[o["지표"] == label].drop(columns="지표")
        print(f"\n  {label}")
        print(g.to_string(index=False, na_rep="—",
                          float_format=lambda v: f"{v:,.1f}"))

    # === 이탈 비용 민감도 =================================================
    print("\n=== ⚠ S0 vs S1 — 이탈 비용 가정이 순위를 뒤집는다 ===")
    d = {st: (t[t.stage == st]["dwell_total_h"].mean(), t[t.stage == st]["n_escaped"].mean())
         for st in ("S0", "S1")}
    (d0, e0), (d1, e1) = d["S0"], d["S1"]
    print(f"  S0  체류 {d0:,.0f}h · 이탈 {e0:,.0f}대        "
          f"S1  체류 {d1:,.0f}h · 이탈 {e1:,.0f}대")
    if abs(e0 - e1) > 1e-9:
        be = (d1 - d0) / (e0 - e1) * 60.0
        print(f"\n  손익분기 이탈 비용 **{be:,.1f}분**   (우리가 쓴 값 {esc_min:.0f}분)")
        gap = abs(be - esc_min)
        if gap < 30:
            print(f"  ⇒ 가정값이 손익분기에서 **{gap:.0f}분** 떨어져 있을 뿐이다. "
                  "'S1 이 S0 보다 나쁘다' 는\n     **결론이 아니라 가정이다.** "
                  "발표에서 이 숫자를 먼저 말할 것")
        print(f"  ⇒ 이탈 비용이 {be:,.0f}분보다 "
              f"{'크면 S1 이, 작으면 S0 가' if d1 > d0 else '작으면 S1 이, 크면 S0 가'} 이긴다")

    print("\n  가정값을 바꿔 보면 (총 사회적 비용, h)")
    grid = []
    for c in (60, 90, 120, 150, 180, 240):
        r = {"이탈(분)": c}
        for st in want:
            if have[st]:
                s = t[t.stage == st]
                r[st] = (s["dwell_total_h"] + s["n_escaped"] * c / 60.0).mean()
        grid.append(r)
    print(pd.DataFrame(grid).to_string(index=False, na_rep="—",
                                       float_format=lambda v: f"{v:,.0f}"))

    print("\n  ※ S3·S4 행이 비어 있는 것이 **남은 작업 목록**이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
