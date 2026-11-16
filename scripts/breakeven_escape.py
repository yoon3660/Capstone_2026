"""사다리 판정이 '이탈 가격' 가정에 얼마나 매달려 있나 (#147 후속).

    python scripts/breakeven_escape.py
    python scripts/breakeven_escape.py --stages S0 S1 --seeds 1-10
    python scripts/breakeven_escape.py --config config/scenario_seollal_down_adoption.yaml

## 왜 재나

총 사회적 비용은 이탈한 차에 **가격**을 매긴다.

    총 = 총 체류 + 이탈 대수 x 이탈 가격 + 포기 전 대기

지금 가격은 **120분**이고, 이건 아직 출처가 없다 (config 주석: "⚠ 120분은
잠정값. 근거는 #64"). 그런데 칸마다 이탈 대수가 크게 다르다 — 설 상행에서
S0 는 425대, 나머지 칸은 100대 안팎이다. 즉 **가격을 바꾸면 판정이 움직일
수 있다.**

움직이는지 아닌지를 재지 않고 발표하면, 심사에서 이 질문이 온다.

    "엔진이 잘 나오는 가정을 고른 거 아닌가요?"

## 손익분기 가격

칸 X 가 S0 와 **같아지는** 가격을 푼다.

    체류_0 + n_0 * p/60 + 포기_0 = 체류_X + n_X * p/60 + 포기_X

    p* = [ (체류_X + 포기_X) - (체류_0 + 포기_0) ] / [ (n_0 - n_X) / 60 ]

읽는 법:

    p* < 0          X 는 체류도 낮고 이탈도 적다 → **가격과 무관하게 이긴다**
    p* > 지금 가격   지금 가격에서는 X 가 진다. 차이가 작으면 판정이 위태롭다
    p* < 지금 가격   X 가 이긴다. 여유가 그 차이만큼이다

## ⚠ 사후 재가격은 완전한 반사실이 아니다

이탈 가격은 발길질 **판단**에도 들어간다 (`s0.py`: `best_cost > escape_cost`).
가격을 진짜로 바꾸면 run 자체가 달라진다. 여기서 하는 것은 **주어진 run 에
값만 다르게 매기는 것**이다.

그래도 쓸모가 있다. 묻는 것이 *"이 판정이 가격 가정에 얼마나 민감한가"* 이고,
가격을 올리면 발길질이 **줄어들** 것이므로(나가는 게 비싸진다) 이탈 대수 차이는
**더 작아진다** — 즉 사후 재가격은 민감도를 **과대평가**한다. 보수적인 쪽이다.

제대로 된 반사실은 인내와 우회 비용을 가른 뒤에 가능하다
(`docs/ticket_patience_split.txt`). 지금 가격을 흔들면 인내까지 같이 움직인다.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.runner import (  # noqa: E402
    load_config,
    make_run_id,
    parse_seeds,
    run_once,
    social_cost_h,
    summarize_kpis,
)

STRENGTH = 0.30
WINDOW = 4
S4_LEAD = 10.0

#: 이 세 칸만 있으면 손익분기를 풀 수 있다
NEED = ("dwell_total_h", "n_escaped", "renege_wait_total_h", "n_ev_charging")
PRICES = (60, 120, 180, 240, 300, 360)
LABEL = {"PERFECT_ROLLING": "완전예측"}


def kw_for(stage: str) -> dict:
    if stage == "S2":
        return {"observed": ObservedSpec(strength=STRENGTH)}
    if stage in ("S3", "S4"):
        out = {"observed": ObservedSpec(strength=STRENGTH), "s3_window": WINDOW}
        if stage == "S4":
            out["s4_lead_min"] = S4_LEAD
        return out
    return {}


def total_at(k: pd.Series | dict, price_min: float) -> float:
    """이탈을 `price_min` 으로 값 매긴 총 사회적 비용 (시간)."""

    return (float(k["dwell_total_h"])
            + float(k["n_escaped"]) * float(price_min) / 60.0
            + float(k["renege_wait_total_h"]))


def breakeven(floor: dict, rung: dict) -> float:
    """`rung` 이 `floor` 와 같아지는 이탈 가격 (분). 이탈 대수가 같으면 nan."""

    den = (float(floor["n_escaped"]) - float(rung["n_escaped"])) / 60.0
    if abs(den) < 1e-12:
        return float("nan")
    num = ((float(rung["dwell_total_h"]) + float(rung["renege_wait_total_h"]))
           - (float(floor["dwell_total_h"]) + float(floor["renege_wait_total_h"])))
    return num / den


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--stages", nargs="+",
                    default=["S0", "S1", "S2", "S3", "S4", "PERFECT_ROLLING"])
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--floor", default="S0")
    ap.add_argument("--rerun", action="store_true",
                    help="저장된 run 을 무시하고 다시 돌린다")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    base = load_config(args.config, stage=args.stages[0])
    price_now = float(base.demand.escape_cost_min)

    print(f"\n이탈 가격 민감도 · {base.label}")
    print(f"  시드 {seeds} · 지금 가격 {price_now:g}분 · 바닥 {args.floor}\n")

    rows = []
    for seed in seeds:
        for stage in args.stages:
            cfg = load_config(args.config, stage=stage)
            rid = make_run_id(cfg.scenario_id, stage, seed, cfg.policy.participation)
            if args.rerun:
                rid = run_once(cfg, seed=seed, overwrite=True,
                               log=lambda *_: None, **kw_for(stage))
            try:
                s = summarize_kpis([rid]).set_index("metric")["mean"]
            except Exception:
                print(f"  [건너뜀] {rid} — 저장된 결과가 없다 (--rerun 으로 돌릴 것)")
                continue
            missing = [m for m in NEED if m not in s.index]
            if missing:
                print(f"  [건너뜀] {rid} — 없는 KPI {missing} (#147 로 물리가 바뀌었다)")
                continue
            rows.append({"seed": seed, "stage": stage,
                         "total_h": social_cost_h(s),
                         **{m: float(s[m]) for m in NEED}})

    if not rows:
        print("\n쓸 수 있는 run 이 없다.")
        return 1

    t = pd.DataFrame(rows)
    g = t.groupby("stage")[list(NEED)].mean()
    if args.floor not in g.index:
        print(f"\n바닥 {args.floor} 의 run 이 없다.")
        return 1
    floor = g.loc[args.floor]
    order = [s for s in args.stages if s in g.index]

    # -- 무엇을 주고 무엇을 받았나 -------------------------------------------
    print("=== 칸별 (시드 평균) ===")
    served0 = float(floor["n_ev_charging"]) - float(floor["n_escaped"])
    for st in order:
        k = g.loc[st]
        served = float(k["n_ev_charging"]) - float(k["n_escaped"])
        print(f"  {LABEL.get(st, st):<8} 체류 {k['dwell_total_h']:>8,.1f}h  "
              f"이탈 {k['n_escaped']:>6.1f}대  포기전대기 {k['renege_wait_total_h']:>6,.1f}h  "
              f"충전 마친 차 {served:>7,.0f}  ({served - served0:>+6.0f})")

    # -- 손익분기 ------------------------------------------------------------
    print(f"\n=== 손익분기 이탈 가격 ({args.floor} 대비) ===")
    print("  음수 = 가격과 무관하게 이긴다 (체류도 낮고 이탈도 적다)\n")
    fragile = []
    for st in order:
        if st == args.floor:
            continue
        be = breakeven(floor, g.loc[st])
        if be != be:                                   # nan
            print(f"  {LABEL.get(st, st):<8} 이탈 대수가 바닥과 같다 — 가격 무관")
            continue
        if be < 0:
            tag = "★ 가격과 무관하게 이긴다"
        else:
            gap = price_now - be
            tag = (f"지금 가격에 {gap:+.1f}분 여유" if gap > 0
                   else f"지금 가격보다 {-gap:.1f}분 높다 — **판정이 가격에 매달렸다**")
            if abs(gap) < 30:
                fragile.append((st, be, gap))
        print(f"  {LABEL.get(st, st):<8} 손익분기 {be:>8,.1f}분   {tag}")

    # -- 가격을 흔들어 본다 --------------------------------------------------
    print(f"\n=== 이탈 가격별 {args.floor} 대비 개선율 (%) ===")
    print("  칸      " + "".join(f"{p:>8}분" for p in PRICES))
    for st in order:
        if st == args.floor:
            continue
        cells = ""
        for p in PRICES:
            imp = (total_at(floor, p) - total_at(g.loc[st], p)) / total_at(floor, p) * 100
            cells += f"{imp:>+8.1f} "
        print(f"  {LABEL.get(st, st):<8}" + cells)

    # -- 순서가 유지되나 -----------------------------------------------------
    print("\n=== 순서가 유지되나 ===")
    rank0 = None
    stable = True
    for p in PRICES:
        rank = sorted(order, key=lambda st: total_at(g.loc[st], p))
        names = " < ".join(LABEL.get(s, s) for s in rank)
        if rank0 is None:
            rank0 = rank
        elif rank != rank0:
            stable = False
        print(f"  {p:>4}분  {names}")
    print(f"\n  {'★ 모든 가격에서 순서가 같다' if stable else '⚠ 가격에 따라 순서가 바뀐다'}")

    if fragile:
        print("\n⚠ 손익분기가 지금 가격에서 30분 안쪽인 칸 — 판정을 단정하지 말 것")
        for st, be, gap in fragile:
            print(f"    {LABEL.get(st, st)}: 손익분기 {be:,.1f}분 (여유 {gap:+.1f}분)")

    out = ROOT / "runs" / "sweeps" / f"breakeven_{base.scenario_id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(out, index=False)
    print(f"\nCSV: {out}")
    if len(seeds) < 5:
        print("⚠ 시드가 적다. **판정이 아니다.**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
