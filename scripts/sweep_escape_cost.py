"""이탈 비용 민감도 — 이 상수 하나가 결과를 얼마나 흔드나 (#78).

    python scripts/sweep_escape_cost.py --seeds 1-20
    python scripts/sweep_escape_cost.py --costs 60 120 180 240 --seeds 1-5

## 왜 이 일을 해야 하는지

`demand.escape_cost_min = 120` 은 **근거 없는 값**이다. 그런데 **최대 대기의 실질적인
상한을 정한다.**

#54 에서 최대 대기가 1,997분(33시간) → 106분이 됐는데, 모델을 고쳐서가 아니라
*"줄이 너무 길면 IC 로 빠져 시내에서 충전하고 돌아온다"* 는 선택지를 넣어서다. 즉
**120 이 바뀌면 최대 대기가 통째로 바뀐다.**

그리고 이탈 KPI 는 엔진 평가의 과녁이다 — `n_escaped_balked`(줄이 길어서)가 **엔진이
줄여야 할 대상**이다. **과녁의 크기가 근거 없는 상수에 달려 있다.**

## ⚠ 어느 시나리오에서 재는가가 중요하다

재현 기준선은 이탈이 **0.2대**다. 거기서 120 을 60 이나 180 으로 바꿔도 **아무 일도
안 일어난다** — 민감하지 않은 게 아니라 **잴 것이 없는 것**이다. 그 결과를 "민감도
낮음" 으로 보고하면 틀린다.

그래서 **이탈이 실제로 일어나는 시나리오**에서 잰다. 기본은 혼잡 시나리오다
(하행 194대 · 상행 614대).

| 시나리오 | 이탈 | 민감도를 잴 수 있나 |
|---|---:|---|
| 재현 | 0.2 | ❌ 잴 것이 없다 |
| 균형 시나리오 | 0.7 (하행) · 42 (상행) | 상행만 |
| **혼잡 시나리오** | **194 · 614** | ✅ |

## 읽는 법

**이탈 수가 변하는 것은 당연하다** (싸지면 더 나간다). 봐야 할 것은 **대기가 얼마나
흔들리나**다 — 이탈은 대기의 배출구라서, 비용이 바뀌면 **남아서 기다리는 사람의
경험**이 바뀐다.
"""

from __future__ import annotations

import argparse
import sqlite3

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.ue import UENotConverged  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds, run_once, summarize_kpis  # noqa: E402

#: 60 분 = 가까운 IC 바로 옆에 충전소가 있는 경우. 240 = 사실상 안 나간다.
DEFAULT_COSTS = (60.0, 120.0, 180.0)

SHOW = ["n_escaped", "n_escaped_balked", "wait_mean_min", "wait_p95_min", "wait_max_min",
        "wait_worst_station_min", "bottleneck_slots", "dwell_total_h", "n_ev_charging"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down_congested.yaml",
                    help="이탈이 실제로 일어나는 시나리오여야 한다 (위 docstring)")
    ap.add_argument("--costs", nargs="+", type=float, default=list(DEFAULT_COSTS))
    ap.add_argument("--seeds", default="1-20")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    base = load_config(args.config)
    print(f"{base.scenario_id}  시드 {len(seeds)}개 × 이탈비용 {len(args.costs)}가지\n")

    cols = {}
    for cost in args.costs:
        cfg = base.variant(f"esc{cost:g}", {"demand.escape_cost_min": float(cost)})
        ok = bad = 0
        for seed in seeds:
            try:
                run_once(cfg, seed=seed, overwrite=True, log=lambda *a: None)
                ok += 1
            except UENotConverged:
                bad += 1
        print(f"  이탈비용 {cost:>5.0f}분  성공 {ok:2d} · 실패 {bad}", flush=True)

        run_ids = [f"{cfg.scenario_id}__{cfg.policy.stage}__p"
                   f"{int(cfg.policy.participation * 100):03d}__s{s:04d}" for s in seeds]
        with sqlite3.connect(default_db_path()) as conn:
            done = {r[0] for r in conn.execute(
                f"SELECT run_id FROM run WHERE status='DONE' AND run_id IN "  # noqa: S608
                f"({','.join('?' * len(run_ids))})", run_ids).fetchall()}
        if not done:
            continue
        s = summarize_kpis(sorted(done)).set_index("metric")
        cols[f"{cost:.0f}분 (n={len(done)})"] = s["mean"]
        labels = s["label"]

    if not cols:
        raise SystemExit("\n[중단] 완료된 run 이 없다.")

    table = pd.DataFrame(cols).reindex(SHOW)
    table.index = [labels.get(m, m) for m in SHOW]
    print("\n=== 이탈 비용에 따라 ===")
    print(table.to_string(float_format=lambda v: f"{v:,.2f}"))

    # 기준(120분) 대비 몇 % 흔들리나 — 발표에서 쓸 숫자는 이쪽이다
    if "120분" in " ".join(cols):
        ref = next(c for c in cols if c.startswith("120분"))
        rel = table.div(table[ref], axis=0).sub(1.0).mul(100.0).drop(columns=[ref])
        print(f"\n=== {ref} 대비 변화율 (%) ===")
        print(rel.to_string(float_format=lambda v: f"{v:+.1f}"))
        worst = rel.abs().max().max()
        print(f"\n  가장 크게 흔들린 값: {worst:.1f}%")
        print("  ⚠ 이탈 수가 변하는 것은 당연하다. **대기**가 얼마나 흔들렸는지를 본다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
