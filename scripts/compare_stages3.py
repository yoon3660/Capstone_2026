"""UE · S0 · S1 을 **같은 시드로만** 비교한다 (#83).

    python scripts/compare_stages3.py --config config/scenario_seollal_up_adoption.yaml

## 왜 `compare_stages.py` 를 안 쓰나

그쪽은 두 단계를 비교한다. 여기는 셋이고, **세 단계 모두 수렴한 시드만** 써야 한다 —
UE 가 실패한 시드를 S0·S1 에만 돌리면 세 단계가 다른 차 집합을 상대하게 된다 (#59).

## ⚠ 평균 대기만 보면 틀린다

S0 는 못 기다리는 차를 **코리도 밖으로 내보낸다.** 나간 차는 충전기를 안 쓰므로
남은 사람의 평균이 좋아진다 — 한 시드에서 S0 가 294대, S1 이 51대를 내보냈다.

> **힘든 사람을 내보내고 평균이 좋아진 것**을 "엔진이 대기를 줄였다" 고 읽으면 안 된다.

그래서 **총 사회적 비용**을 따로 낸다.

    총비용 = 총 체류(시간) + 이탈한 차 × 이탈 비용

이탈은 "사라진 것" 이 아니라 **그 사람이 시내를 다녀온 시간**이다. 그걸 안 세면
"내보내면 이긴다" 가 된다.
"""

from __future__ import annotations

import argparse
import sqlite3

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds, summarize_kpis  # noqa: E402

STAGES = ("UE", "S0", "S1")

SHOW = ["n_ev", "n_ev_charging", "wait_mean_min", "wait_p95_min", "wait_max_min",
        "wait_worst_station_min", "dwell_total_h", "bottleneck_slots",
        "n_escaped", "n_escaped_balked", "n_escaped_stranded"]


def done_seeds(cfg, stage: str, seeds: list[int], conn) -> set[int]:
    want = {make_run_id(cfg.scenario_id, stage, s, cfg.policy.participation): s for s in seeds}
    rows = conn.execute(
        f"SELECT run_id FROM run WHERE status='DONE' AND run_id IN "  # noqa: S608
        f"({','.join('?' * len(want))})", list(want)).fetchall()
    return {want[r[0]] for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-20")
    args = ap.parse_args()

    cfg = load_config(args.config)
    seeds = parse_seeds(args.seeds)

    with sqlite3.connect(default_db_path()) as conn:
        by_stage = {st: done_seeds(cfg, st, seeds, conn) for st in STAGES}

    shared = set.intersection(*by_stage.values())
    if not shared:
        raise SystemExit(
            "\n[중단] 세 단계가 모두 끝낸 시드가 없다.\n"
            + "\n".join(f"  {st}: {len(s)}개" for st, s in by_stage.items())
        )

    for st, s in by_stage.items():
        extra = sorted(s - shared)
        if extra:
            print(f"  [제외] {st} 에만 있는 시드 {len(extra)}개 — 같은 세계만 비교한다")

    print(f"\n{cfg.scenario_id}  **세 단계 공통 시드 {len(shared)}개**")
    print(f"수요: {cfg.demand_label}\n")

    cols, labels = {}, {}
    for st in STAGES:
        ids = [make_run_id(cfg.scenario_id, st, s, cfg.policy.participation)
               for s in sorted(shared)]
        s = summarize_kpis(ids).set_index("metric")
        labels.update(s["label"].to_dict())
        cols[st] = s["mean"]

    table = pd.DataFrame(cols).reindex(SHOW)
    table.index = [labels.get(m, m) for m in SHOW]
    print(table.to_string(float_format=lambda v: f"{v:,.1f}"))

    # 같은 차 집합인지 — 다르면 비교가 성립하지 않는다 (#59)
    ev = pd.DataFrame(cols).loc["n_ev_charging"]
    if ev.nunique() > 1:
        raise SystemExit(
            f"\n[중단] 단계마다 충전 필요 EV 가 다르다: {ev.to_dict()}\n"
            "  --stage 는 세계를 바꾸지 않는다. 다르면 다른 수요를 비교하는 것이다."
        )

    # ⚠ 핵심 — 이탈까지 넣은 총 사회적 비용
    print("\n=== 총 사회적 비용 (이탈을 안 세면 '내보내면 이긴다' 가 된다) ===")
    esc_h = float(cfg.demand.escape_cost_min) / 60.0
    cost = pd.DataFrame(cols).loc[["dwell_total_h", "n_escaped"]]
    total = cost.loc["dwell_total_h"] + cost.loc["n_escaped"] * esc_h
    out = pd.DataFrame({
        "총 체류(h)": cost.loc["dwell_total_h"],
        f"이탈 {cfg.demand.escape_cost_min:.0f}분(h)": cost.loc["n_escaped"] * esc_h,
        "합계(h)": total,
        "UE 대비": (total / total["UE"] - 1.0) * 100.0,
    })
    print(out.to_string(float_format=lambda v: f"{v:,.1f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
