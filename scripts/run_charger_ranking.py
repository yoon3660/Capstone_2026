"""증설 랭킹 — **어느 휴게소에 충전기를 더 놓는 것이 가장 이득인가** (발표 §8-2).

    python scripts/run_charger_ranking.py --seeds 1-5
    python scripts/run_charger_ranking.py --seeds 1-5 --units 1,3 --stages S0,S4
    python scripts/run_charger_ranking.py --report            # 이미 돈 것만 집계

## 답하려는 문장

  "@@IC 구간이 막힐 때에는 안성에 1대 놓는게 칠곡에 3대 놓는 것보다 낫다"

## 무엇을 재나

휴게소 × 증설 대수 마다 세계를 다시 만들어 돌리고, **증설 없는 같은 시드**와
짝지어 사회적 비용 차이를 본다. 랭킹의 자는 **1대당 줄인 비용 (h/대)** 이다.

    1대당 효과 = (증설 없음 − 증설) / 더 놓은 대수

총 절감이 아니라 1대당으로 보는 이유: "3대 놓으면 더 줄어든다" 는 당연하다.
정책이 알고 싶은 것은 **한 대를 어디에 놓느냐**다.

## 왜 바닥과 엔진 둘 다에서 재나

  바닥(S0 또는 MNL)  엔진 없이 "충전기만 더 놓으면" — 지금 현실의 증설
  S4                 엔진까지 쓸 때의 증설

둘이 다르면 그 자체가 결과다. **엔진이 쏠림을 이미 풀었다면 증설의 이득은
작아진다** — "엔진이 증설을 대체한다" 는 말을 숫자로 할 수 있다. 반대로
엔진을 써도 증설 이득이 크면 그 휴게소는 **용량이 진짜 부족한** 곳이다.

## ⚠ run_id

`with_charger_delta` 가 scenario_id 에 꼬리표를 붙인다 (`__addch-a00077x3-100kw`).
안 붙이면 run_id 가 같아서 **증설 안 한 run 을 덮어쓴다** (#123). 이 스크립트는
그 함수만 쓰므로 직접 cfg 를 만들지 말 것.

## 비용 정의

`runner.social_cost_h` — 체류 + 이탈 비용 합 + 포기 전 대기 (#147). 세 항이
다 있어야 하고, 없으면 그 함수가 멈춘다.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

# ★ 엔진 설정(strength·윈도우·S4 리드)을 **사다리에서 그대로 가져온다.**
#   여기서 다시 적으면 기준선(사다리가 돌려 둔 run)과 증설 run 이 다른 설정으로
#   돌 수 있다. `reusable_run` 은 기준선의 불일치만 잡고, 증설 run 은 처음 도는
#   것이라 조용히 다른 세계가 된다 — 그러면 차이가 증설 탓인지 설정 탓인지 모른다
from run_chuseok_ladder import S4_LEAD, STRENGTH, WINDOW, kw_for  # noqa: E402

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    load_config,
    parse_seeds,
    reusable_run,
    run_once,
    social_cost_h,
    with_charger_delta,
)

#: 후보 휴게소 — 추석 혼잡 무대에서 MNL 바닥의 대기·밀어낸 차 상위 (10시드).
#: **하드코딩이지만 근거가 있다.** 전 휴게소를 돌리면 16곳 × 대수 × 시드로 비싸고,
#: 대기가 0 인 곳의 증설 효과는 재 보나 마나 0 이다.
CANDIDATES: dict[str, str] = {
    "gyeongbu_up_a00077": "건천",          # 대기 32.8분 · 밀어냄 48.9대 · 현재 2기
    "gyeongbu_up_a00069": "칠곡",          # 대기 26.5분 · 밀어냄 10.1대 · 현재 8기
    "gyeongbu_up_a00050": "입장거봉포도",   # 대기 18.7분 · 현재 5기
    "gyeongbu_up_a00132": "양산",          # 대기 18.3분 · 현재 4기
    "gyeongbu_up_a00002": "죽전",          # 대기 14.4분 · 현재 8기
    "gyeongbu_up_a00070": "경산",          # 대기 12.9분 · 현재 8기
    "gyeongbu_up_a00004": "안성",          # 대기 8.5분 · 현재 12기 — 대조군 성격
}


def kpis(run_id: str) -> dict[str, float]:
    with get_conn(default_db_path(), readonly=True) as conn:
        rows = conn.execute(
            "SELECT metric, value FROM run_kpi WHERE run_id = ?", (run_id,)).fetchall()
    return {m: float(v) for m, v in rows}


def measure(cfg, stage: str, seed: int, *, log) -> dict[str, float]:
    """한 칸. **이미 돈 run 은 다시 돌리지 않는다.**

    기준선(증설 없음)은 사다리가 이미 돌려 둔 run 이다. 그걸 또 돌릴 이유가
    없고, `overwrite=True` 로 덮으면 사다리 결과를 이 스크립트가 재생산한
    값으로 갈아치운다 — 같은 값이 나와야 하지만 "나와야 한다" 는 검사가 아니다.

    재사용 여부는 `reusable_run` 이 판정한다. DONE 인 것만 보지 않고
    `params_json` 을 config 와 대조한다 (`stale_reason`) — 진입 EV 가 같으면서
    세계가 다른 경우가 실제로 있었다 (#55).
    """

    kw = kw_for(stage)
    rid = make_run_id(cfg.scenario_id, stage, seed, cfg.policy.participation)
    ok, why = reusable_run(default_db_path(), rid, cfg, **kw)
    if ok:
        log(f"    재사용 {rid}")
    else:
        log(f"    새로 돈다 {rid} ({why})")
        rid = run_once(cfg, seed=seed, overwrite=True, log=log, gap_plot=False, **kw)
    k = kpis(rid)
    if not k:
        raise SystemExit(f"[중단] {rid} 의 KPI 가 비었다 — run_kpi 를 확인할 것")
    return {
        "run_id": rid, "total_h": social_cost_h(k),
        "wait_mean_min": k.get("wait_mean_min", float("nan")),
        "wait_p95_min": k.get("wait_p95_min", float("nan")),
        "n_escaped": k.get("n_escaped", float("nan")),
        "bottleneck_slots": k.get("bottleneck_slots", float("nan")),
    }


def ci95(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return float("nan")
    mu = sum(values) / n
    sd = math.sqrt(sum((v - mu) ** 2 for v in values) / (n - 1))
    return 1.96 * sd / math.sqrt(n)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_congested.yaml")
    ap.add_argument("--seeds", default="1-5")
    ap.add_argument("--stages", default="S0,S4")
    ap.add_argument("--units", default="1,2,3", help="더 놓을 대수")
    ap.add_argument("--power-kw", type=float, default=100.0,
                    help="증설 충전기 출력. 휴게소마다 **같아야** 배치의 비교가 된다")
    ap.add_argument("--out", default="runs/sweeps/charger_ranking.csv")
    ap.add_argument("--report", action="store_true",
                    help="돌리지 않고 --out 의 CSV 로 표만 다시 낸다")
    args = ap.parse_args()

    out = ROOT / args.out
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    units = [int(u) for u in args.units.split(",") if u.strip()]
    seeds = parse_seeds(args.seeds)

    if args.report:
        if not out.is_file():
            raise SystemExit(f"[중단] {out} 가 없다 — 먼저 --report 없이 돌릴 것")
        return report(pd.read_csv(out), stages)

    quiet = lambda *_: None                                        # noqa: E731
    rows: list[dict] = []
    t0 = time.perf_counter()

    print(f"\n증설 랭킹 — 휴게소 {len(CANDIDATES)}곳 × 대수 {units} × 칸 {stages} "
          f"× 시드 {len(seeds)}개")
    print(f"  config {args.config}  ·  증설 출력 {args.power_kw:g} kW")
    print(f"  엔진 설정 (사다리와 같다): strength {STRENGTH} · 윈도우 {WINDOW} · "
          f"S4 리드 {S4_LEAD:g}분")
    print(f"  = 기준선 {len(stages) * len(seeds)}회 + 증설 "
          f"{len(CANDIDATES) * len(units) * len(stages) * len(seeds)}회\n")

    # --- 기준선: 증설 없음 ---------------------------------------------------
    base: dict[tuple[str, int], float] = {}
    for stage in stages:
        cfg = load_config(args.config, stage=stage)
        for seed in seeds:
            got = measure(cfg, stage, seed, log=quiet)
            base[(stage, seed)] = got["total_h"]
            rows.append({"station": "(없음)", "name": "증설 없음", "units": 0,
                         "stage": stage, "seed": seed, **got})
        print(f"  기준선 {stage:<4} "
              f"{sum(base[(stage, s)] for s in seeds) / len(seeds):>9,.1f}h")

    # --- 증설 ---------------------------------------------------------------
    for sid, name in CANDIDATES.items():
        for n in units:
            for stage in stages:
                cfg = with_charger_delta(load_config(args.config, stage=stage),
                                         {sid: n}, power_kw=args.power_kw)
                saved = []
                for seed in seeds:
                    got = measure(cfg, stage, seed, log=quiet)
                    saved.append(base[(stage, seed)] - got["total_h"])
                    rows.append({"station": sid, "name": name, "units": n,
                                 "stage": stage, "seed": seed, **got})
                mu = sum(saved) / len(saved)
                print(f"  {name:<8} +{n}기 {stage:<4} 절감 {mu:>8,.1f}h "
                      f"(1대당 {mu / n:>7,.1f}h)  ±{ci95(saved):,.1f}")
                out.parent.mkdir(parents=True, exist_ok=True)
                pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")

    print(f"\n  {time.perf_counter() - t0:,.0f}초 · {out}")
    return report(pd.DataFrame(rows), stages)


def report(df: pd.DataFrame, stages: list[str]) -> int:
    base = (df[df["units"] == 0].set_index(["stage", "seed"])["total_h"])
    add = df[df["units"] > 0].copy()
    if add.empty:
        raise SystemExit("[중단] 증설 칸이 없다")
    add["saved_h"] = add.apply(
        lambda r: base.get((r["stage"], r["seed"]), float("nan")) - r["total_h"], axis=1)
    add["per_unit_h"] = add["saved_h"] / add["units"]

    for stage in stages:
        g = add[add["stage"] == stage]
        if g.empty:
            continue
        b = base.xs(stage, level="stage").mean()
        print(f"\n=== {stage} — 1대당 절감 랭킹 (기준선 {b:,.1f}h) ===")
        print(f"  {'휴게소':<10}{'대수':>5}{'총 절감':>11}{'1대당':>10}"
              f"{'95% CI':>18}{'기준선 대비':>11}")
        tab = (g.groupby(["name", "units"])
                .agg(saved=("saved_h", "mean"), per=("per_unit_h", "mean"),
                     seeds=("seed", "count"),
                     ci=("per_unit_h", lambda s: ci95(list(s))))
                .reset_index().sort_values("per", ascending=False))
        for r in tab.itertuples(index=False):
            print(f"  {r.name:<10}{r.units:>5}{r.saved:>10,.1f}h{r.per:>9,.1f}h"
                  f"  [{r.per - r.ci:>+7.1f}, {r.per + r.ci:>+7.1f}]"
                  f"{r.saved / b * 100:>10.2f}%")
    print("\n★ 랭킹의 자는 **1대당 절감**이다 — '3대 놓으면 더 줄어든다' 는 당연하고,")
    print("  정책이 묻는 것은 '한 대를 어디에 놓느냐' 다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
