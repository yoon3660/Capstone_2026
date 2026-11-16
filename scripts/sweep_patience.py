"""인내 분포의 모양이 사다리를 바꾸나 (#147 후속 · 인내/우회 분리 뒤).

    python scripts/sweep_patience.py
    python scripts/sweep_patience.py --config config/scenario_chuseok_up_adoption.yaml
    python scripts/sweep_patience.py --medians 120 60 30 --halfwidths 0.25 0.5 0.75

## 왜 재나

인내를 분포로 바꾸는 것은 **가르기 전에는 할 수 없었다.** `escape_cost_min` 이
인내와 우회 비용을 동시에 맡고 있었고, 분포로 바꾸면 "인내 9분인 차 = 우회도
9분인 차" 가 되어 조급한 차에게 공짜 탈출구가 열렸다 (weibull:0.7 에서 S0 가
수요의 18.9% 를 잃었다). 지금은 갈라져 있어 **인내만** 흔들 수 있다.

## 균등분포를 **중앙값 보존**으로 모수화한다

    U( m(1−h),  m(1+h) )        0 < h < 1

중앙값이 구간의 중점이므로 언제나 정확히 `m` 이고, `h` 만 산포를 키운다
(표준편차 = m·h/√3, 즉 **CV = h/√3**). 이렇게 두지 않으면 모양을 바꿀 때
수준이 같이 움직여 둘의 효과가 섞인다 — `U(0, 120)` 은 상한이 120 이라
120 처럼 보이지만 **중앙값이 60** 이다.

## h = 1 은 쓰지 않는다 — 결정이다

h=1 이면 하한이 0 이 되어 **인내 0분인 차**가 생긴다. 그건 발길질과 겹친다:
우리 모형에서 "줄이 우회보다 비싸면 안 선다" 는 이미 발길질이 맡고 있고,
일본 설문(GoGoEV n=470)의 "전혀 안 기다림 19%" 도 기사 본문이 이유를 적어
뒀다 — *"먼저 차가 서 있으면 바로 **근처 충전기를 찾아 이동**한다"*. 즉 인내가
0인 유형이 아니라 **대안이 싸서 바로 나가는 합리적 선택**이다. 두 번 세지 않는다.

## ⚠ 재 보니 — **분포는 사다리를 부풀린다. 기본은 point 로 둔다**

2026-11-12, 추석 9/25 상행 EV 20% (혼잡 있는 무대), 시드 3개:

    경우            S0        S2       S0→S2 개선   기준 대비
    point       6,835.6   6,024.7     +11.86%     —
    m=120 h=0.5 6,443.9   5,362.2     +16.79%    +4.92%p
    m=120 h=.75 5,965.7   4,729.4     +20.72%    +8.86%p
    m=60  h=0.5 5,291.8   4,359.4     +17.62%    +5.76%p
    m=60  h=.75 5,032.6   3,970.2     +21.11%    +9.25%p

**모든 분포가 엔진을 더 좋아 보이게 한다** — 최대 +9.25%p. 어디서 오나
(S0, point → m=60 h=0.75):

    총비용      6,835.6 → 5,032.6   −1,803.0
    체류        5,610.9 → 3,150.0   −2,460.9
    이탈 비용      713.3 → 1,462.7     +749.4
    충전 마친 차   9,609   →  9,234      **−375대**

차 375대가 더 포기해서 체류가 2,461h 빠진 것이다. **엔진이 좋아진 게 아니라
문제가 26% 작아졌다.** 인내/우회 분리(#147 후속)가 **발길질** 문은 닫았지만
(가족을 바꿔도 이탈 비용 추출이 안 움직인다), **포기** 문은 열려 있다 —
그건 고장이 아니라 포기 자체의 성질이다.

따라서

    · 기본은 **point** 다 — 모든 분포 중 **가장 보수적**이다
    · 분포는 **민감도 축**으로 보고한다: *"우리 수치는 바닥이지 천장이 아니다"*
    · 분포 간 총비용을 **서로 비교하지 않는다** — 모집단(충전 마친 차)이 다르다

## 중앙값은 설문에서 **안 나온다**

같은 설문의 중앙값은 약 10분인데, 그건 "근처에 다른 충전기가 있을 때" 의 값이다.
응답자들이 직접 구분해 적었다 — *"고속도로 등이나 배터리 잔량에 따라서는 다른
스탠드로 갈 여유가 없어 **기다리지 않을 수 없다**"*. 경부선 피크의 대안은 IC 우회
120분이다. 그래서 설문은 **산포의 모양**을 주고, 수준은 **민감도 축**으로 둔다.
"""

from __future__ import annotations

import argparse
import sys
import time

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.runner import (  # noqa: E402
    SOCIAL_COST_TERMS,
    load_config,
    run_once,
    social_cost_h,
    summarize_kpis,
)

STRENGTH = 0.30
WINDOW = 4

METRICS = ("n_ev_charging", "n_escaped", "n_escaped_balked", "n_escaped_stranded",
           "wait_mean_min", "wait_p95_min", "wait_max_min", "bottleneck_slots",
           *SOCIAL_COST_TERMS)


def kw_for(stage: str) -> dict:
    if stage == "S2":
        return {"observed": ObservedSpec(strength=STRENGTH)}
    if stage == "S3":
        return {"observed": ObservedSpec(strength=STRENGTH), "s3_window": WINDOW}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_adoption.yaml")
    ap.add_argument("--stages", nargs="+", default=["S0", "S2"])
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--medians", type=float, nargs="+", default=[120.0, 60.0, 30.0])
    ap.add_argument("--halfwidths", type=float, nargs="+", default=[0.25, 0.5, 0.75])
    args = ap.parse_args()

    for h in args.halfwidths:
        if not 0.0 < h < 1.0:
            ap.error(f"반폭 h 는 0 < h < 1 이어야 한다 (받은 값: {h}) — "
                     "h=1 은 하한이 0 이라 발길질과 겹친다")

    base = load_config(args.config, stage=args.stages[0])
    price = float(base.demand.escape_cost_min)

    print(f"\n인내 분포 스윕 · {base.label}")
    print(f"  시드 {args.seeds} · 우회 비용(중앙값) {price:g}분 · 균등분포 U(m(1−h), m(1+h))\n")

    #: (라벨, 가족, 모양, 중앙값)
    cases: list[tuple[str, str, float, float]] = [("point (기준)", "point", 0.0, 0.0)]
    for m in args.medians:
        for h in args.halfwidths:
            cases.append((f"m={m:g} h={h:g}", "uniform", h, m))

    rows = []
    for label, kind, shape, median in cases:
        for stage in args.stages:
            cfg = load_config(args.config, stage=stage)
            # ★ **`variant` 를 써야 한다.** `dataclasses.replace` 로 물리만 바꾸면
            #   `scenario_id` 가 그대로라 `run_id` 도 그대로다 — 스윕이 **진짜 run 을
            #   덮어쓴다.** 2026-11-12 에 실제로 그랬고, 사다리 CSV 와 run 폴더가
            #   갈라져서 알았다 (설 S0 시드1: 총비용 6,999.8 ↔ 6,576.7).
            #   `variant` 는 scenario_id 뒤에 `__<tag>` 를 붙이고 검증도 다시 한다.
            tag = (f"pat-{kind}-{shape:g}-{median:g}".replace(".", "p")
                   if kind != "point" else "pat-point")
            cfg = cfg.variant(tag, {
                "demand.patience_kind": kind,
                "demand.patience_shape": shape,
                "demand.patience_median_min": median,
            })
            for seed in args.seeds:
                t0 = time.perf_counter()
                rid = run_once(cfg, seed=seed, overwrite=True,
                               log=lambda *_: None, **kw_for(stage))
                s = summarize_kpis([rid]).set_index("metric")["mean"]
                rows.append({"case": label, "kind": kind, "shape": shape,
                             "median": median or price, "stage": stage, "seed": seed,
                             "total_h": social_cost_h(s), "wall_s": time.perf_counter() - t0,
                             **{m: float(s.get(m, float("nan"))) for m in METRICS}})
            got = pd.DataFrame(rows)
            got = got[(got.case == label) & (got.stage == stage)]
            print(f"  {label:<14} {stage:<3} {got.total_h.mean():>8,.1f}h  "
                  f"체류 {got.dwell_total_h.mean():>7,.1f}h  "
                  f"발길질 {got.n_escaped_balked.mean():>5.1f}  "
                  f"포기 {(got.n_escaped - got.n_escaped_balked - got.n_escaped_stranded).mean():>6.1f}  "
                  f"몰아넣음 {got.n_escaped_stranded.mean():>5.1f}  "
                  f"포기전대기 {got.renege_wait_total_h.mean():>6,.1f}h")

    t = pd.DataFrame(rows)
    w = t.pivot_table(index="case", columns="stage", values="total_h", aggfunc="mean")
    order = [c[0] for c in cases]
    w = w.reindex(order)

    print(f"\n=== 사다리 격차 ({args.stages[0]} → {args.stages[-1]}) ===")
    lo, hi = args.stages[0], args.stages[-1]
    if lo in w and hi in w:
        print(f"  {'경우':<14}{lo:>10}{hi:>10}{'개선':>9}{'기준 대비':>11}")
        ref = (w[lo] - w[hi]).div(w[lo]).iloc[0] * 100
        for case in order:
            imp = (w.loc[case, lo] - w.loc[case, hi]) / w.loc[case, lo] * 100
            print(f"  {case:<14}{w.loc[case, lo]:>10,.1f}{w.loc[case, hi]:>10,.1f}"
                  f"{imp:>+8.2f}%{imp - ref:>+10.2f}%p")
        print("\n  기준 대비가 0 에 가까우면 **인내의 모양이 사다리를 안 바꾼다** —")
        print("  그러면 분포를 기본으로 올려도 판정이 흔들리지 않는다")

    out = ROOT / "runs" / "sweeps" / f"patience_{base.scenario_id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(out, index=False)
    print(f"\nCSV: {out}")
    if len(args.seeds) < 5:
        print("⚠ 시드가 적다. **판정이 아니다.**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
