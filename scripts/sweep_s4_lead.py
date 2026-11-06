"""S4 리드타임 스윕 — 요동이 **제약으로 잡히는 문제인가** (#142 → #143·#144).

    python scripts/sweep_s4_lead.py --leads 10,20,30,60
    python scripts/sweep_s4_lead.py --leads 20,60 --seeds 1,2

## 왜 이걸 먼저 재는가

S4 골격(#142)은 시드 1 에서 **S3 보다 나빴다**.

    S3  6,288.3h  대기 33.24  병목 109
    S4  6,510.5h  대기 35.21  병목  98   ← 총량은 222h 나쁘고, 병목은 11칸 좋다
        재배정 15,960회 (충전 차 1대당 2.2회)

**병목은 줄었는데 총량이 나빠졌다.** 이건 "시간축이 과녁이 아니다" 와 다른 모양이다.
옮기는 능력은 쓰이고 있고, **너무 많이 쓰이고 있다.** 요동(thrashing)이다.

그러면 갈림길은 하나다.

| 만약 | 뜻 | 다음 |
|---|---|---|
| 리드타임을 늘려서 **좋아진다** | 요동은 **제약 부족**이다 | #144 의 재배정 비용·terminal penalty 가 정답. 설계대로 간다 |
| 늘려도 **S3 쪽으로만 수렴**한다 | 재배정 자체가 값이 없다 | #143 의 목적함수를 다시 봐야 한다 — solver 가 같은 수를 더 잘 둬도 소용없다 |
| **중간에 최적점**이 있다 | 리드타임이 그 자체로 손잡이다 | 그 값을 고정하고 #143 으로 간다 |

**리드타임을 무한히 키우면 S3 다** (모든 재배정이 거부된다). 그래서 이 스윕은
S3 와 S4 사이를 잇는 선이고, 그 선의 모양이 #143·#144 의 설계를 가른다.

## ⚠ run_id 가 리드타임을 구분하지 못한다

S4 는 어느 리드타임으로 돌아도 `__S4__` 다. 그래서 이 스윕은 **같은 run_id 를
덮어쓰며** 돈다 (`overwrite=True`). 두 가지로 막는다.

1. `params_json` 에 `s4_lead_min` 을 적는다 (#142 에서 러너에 통로를 냈다) —
   다음에 누가 재사용하려 하면 `stale_reason` 이 *"기록 10.0 ≠ 지금 60.0"* 으로
   잡는다
2. 각 점의 KPI 를 **그 자리에서 CSV 로 받아 둔다** — DB 에는 마지막 점만 남는다

> 이 두 번째가 중요하다. 이걸 안 하면 스윕이 끝난 뒤 DB 를 보고 *"S4 는 6,2xx h"*
> 라고 읽게 되는데, 그건 **마지막 리드타임의 결과**다. #101·#123 과 같은 고장이다.

## ⚠ 기준선을 덮어쓰지 않는다 (이 스크립트가 한 번 저질렀다)

처음 돌릴 때 `--strength` 기본값이 **1.0** 이었다. 사다리는 **0.30** 을 쓴다
(`matched_ladder.py:OBSERVED_STRENGTH` — 이탈을 S1 근처로 맞추려고 #123 에서 쓸어서
찾은 값이다). 그래서 `reusable_run` 이 *"기록 0.3 ≠ 지금 1.0"* 으로 제대로 걸렀는데,
스크립트가 그걸 **"그럼 다시 돌리자"** 로 읽고 `overwrite=True` 로 사다리의
S3 시드 1 을 지웠다. **#123 과 같은 고장을, 그걸 막으려고 만든 방어를 지나서 냈다.**

지금은 두 가지가 다르다.

1. 기본 strength 가 `OBSERVED_STRENGTH` 다 — 사다리와 같은 세계에서 잰다
2. 저장된 S3 가 설정과 다르면 **멈춘다.** 덮어쓰려면 `--rebuild-baseline` 을
   명시해야 한다 (없던 run 은 그냥 돌린다)

> 교훈은 "재사용 가드를 믿지 말라" 가 아니다. **가드는 걸렸다.** 걸린 뒤에 무엇을
> 할지가 틀렸다 — 불일치의 기본 답은 *재실행*이 아니라 *정지*다.

## 이탈을 맞춘다

리드타임은 **이탈 수를 흔들 수 있다** — 재배정을 막으면 못 가는 차가 생길 수 있다.

> `docs/compare_rule.md`: **엔진을 비교할 때는 이탈을 맞춘다. 못 맞추면 격차
> 축소율을 보고하지 않는다.**

그래서 이 스크립트는 **격차 축소율을 찍지 않는다.** 총 사회적 비용(체류 + 이탈×120분)
과 이탈 수를 나란히 놓고, 이탈이 벌어진 점은 표에 ⚠ 를 붙인다. 판정은 #144 에서
시드를 늘려 한다 — **시드 하나는 판정이 아니다.**
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    load_config,
    parse_seeds,
    reusable_run,
    run_once,
    summarize_kpis,
)

#: 이탈 수가 이보다 더 벌어지면 비교가 성립하지 않는다고 본다 (docs/compare_rule.md)
ESCAPE_TOL = 5

#: 관측 예측을 얼마나 믿나. **사다리가 쓰는 값과 같아야 한다** —
#: `scripts/matched_ladder.py:OBSERVED_STRENGTH` 와 같은 0.30 이고, 그 값은
#: 이탈을 S1 근처로 맞추려고 #123 에서 쓸어서 찾은 것이다.
#:
#: ⚠ 여기를 1.0 (ObservedSpec 의 생 기본값) 으로 두면 **다른 세계를 재게 된다.**
#: 실제로 그렇게 한 번 돌렸고, S3 가 4,677h 로 나왔다 — 사다리의 S3 (6,288h) 와
#: 1,600h 차이다. 스윕 안에서는 일관되니 방향은 읽히지만, **사다리 표에 못 올린다.**
#: 그 기록은 `runs/sweeps/s4_lead_strength1.0.csv` 에 남겨 뒀다.
OBSERVED_STRENGTH = 0.30


def kpis(run_id: str, escape_cost_min: float) -> dict[str, float]:
    """한 run 의 사회적 비용과 그 재료. **총량과 이탈을 늘 같이 본다.**"""

    s = summarize_kpis([run_id]).set_index("metric")["mean"]

    def g(k: str) -> float:
        return float(s.get(k, float("nan")))

    esc = g("n_escaped")
    return {
        "dwell_total_h": g("dwell_total_h"),
        "n_escaped": esc,
        # 총 사회적 비용 = 총 체류 + 이탈 × 이탈비용 (DESIGN §1)
        "total_h": g("dwell_total_h") + esc * escape_cost_min / 60.0,
        "wait_mean_min": g("wait_mean_min"),
        "wait_p95_min": g("wait_p95_min"),
        "bottleneck_slots": g("bottleneck_slots"),
        "n_ev_charging": g("n_ev_charging"),
        # ★ 리드타임이 실제로 무엇을 막았나
        "s4_relocated": g("s4_relocated"),
        "s4_relocate_vetoed": g("s4_relocate_vetoed"),
        "s4_lead_min": g("s4_lead_min"),
        "s3_swaps": g("s3_swaps"),
        "s3_moves": g("s3_moves"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--leads", default="10,20,30,60",
                    help="재배정 리드타임(분). 크면 재배정을 더 막는다 → S3 쪽")
    ap.add_argument("--window", type=int, default=4)
    ap.add_argument("--strength", type=float, default=OBSERVED_STRENGTH,
                    help="관측 예측 신뢰도. 기본값은 **사다리와 같은 0.30** — "
                         "바꾸면 사다리 표에 못 올린다")
    ap.add_argument("--out", default=None,
                    help="CSV 경로 (기본: runs/sweeps/s4_lead_strength<strength>.csv)")
    ap.add_argument("--rebuild-baseline", action="store_true",
                    help="저장된 S3 가 설정과 달라도 **덮어쓴다**. 사다리가 그 run 을 "
                         "쓰므로 기본은 거부다")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    leads = [float(x) for x in str(args.leads).split(",")]
    db = default_db_path()
    spec = ObservedSpec(strength=args.strength)

    cfg3 = load_config(args.config, stage="S3")
    cfg4 = load_config(args.config, stage="S4")
    esc_cost = float(cfg4.demand.escape_cost_min)

    # ★ 이름에 strength 를 박는다. 안 박으면 strength 를 바꿔 돌릴 때
    #   **같은 파일을 덮어쓴다** — run_id 함정이 파일에도 그대로 있다
    out = ((RUNS_DIR / "sweeps" / f"s4_lead_strength{args.strength:g}.csv")
           if args.out is None else args.out)
    from pathlib import Path
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{cfg4.scenario_id}")
    print(f"시드 {seeds} · 윈도우 {args.window} · strength {args.strength:g}")
    print(f"리드타임 {[f'{x:g}' for x in leads]} 분 · 이탈 비용 {esc_cost:.0f}분")
    print(f"CSV → {out}\n")

    rows = []

    # --- 기준선: S3 (리드타임 무한대에 해당한다) -----------------------------
    for seed in seeds:
        rid = make_run_id(cfg3.scenario_id, "S3", seed, cfg3.policy.participation)
        ok, why = reusable_run(db, rid, cfg3, observed=spec, s3_window=args.window)
        if ok:
            print(f"  S3  시드 {seed}                 — 재사용")
        elif why == "없음" or args.rebuild_baseline:
            print(f"  S3  시드 {seed}                 — 실행 ({why})", flush=True)
            rid = run_once(cfg3, seed=seed, overwrite=True, observed=spec,
                           s3_window=args.window, log=lambda *_: None)
        else:
            # ★ 저장된 S3 는 **사다리가 쓰는 run** 이다. 설정이 안 맞는다고
            #   덮어쓰면 #123 이 다시 일어난다 — 실제로 이 스크립트가 처음
            #   돌 때 strength 1.0 으로 사다리의 S3 시드 1 을 지웠다
            print()
            print(f"저장된 S3 시드 {seed} 가 지금 설정과 다르다 — {why}")
            print("덮어쓰지 않는다. 사다리가 그 run 을 쓴다 (#123).")
            print("  · 스윕을 사다리에 맞추려면 --strength 를 그 기록에 맞춘다")
            print("  · 그 기록이 틀렸다고 확신하면 --rebuild-baseline 을 준다")
            return 2
        rows.append({"seed": seed, "stage": "S3", "lead_min": float("inf"),
                     **kpis(rid, esc_cost)})
        _save(rows, out)

    # --- S4 를 리드타임마다 -------------------------------------------------
    for seed in seeds:
        for lead in leads:
            print(f"  S4  시드 {seed} 리드 {lead:>4.0f}분      — 실행", flush=True)
            rid = run_once(cfg4, seed=seed, overwrite=True, observed=spec,
                           s3_window=args.window, s4_lead_min=lead,
                           log=lambda *_: None)
            rows.append({"seed": seed, "stage": "S4", "lead_min": lead,
                         **kpis(rid, esc_cost)})
            _save(rows, out)   # ★ 점마다 저장 — 중간에 끊겨도 앞 점은 남는다

    _report(pd.DataFrame(rows), out)
    return 0


def _save(rows: list[dict], out) -> None:
    pd.DataFrame(rows).to_csv(out, index=False)


def _report(t: pd.DataFrame, out) -> None:
    cols = ["seed", "stage", "lead_min", "total_h", "dwell_total_h", "n_escaped",
            "wait_mean_min", "bottleneck_slots", "s4_relocated", "s4_relocate_vetoed"]

    print("\n=== 리드타임 스윕 ===")
    print(t[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    for seed, g in t.groupby("seed"):
        base = g[g["stage"] == "S3"]
        if base.empty:
            continue
        b = base.iloc[0]
        print(f"\n--- 시드 {seed}: S3 대비 (총 사회적 비용 {b['total_h']:,.1f}h) ---")
        for _, r in g[g["stage"] == "S4"].sort_values("lead_min").iterrows():
            d = r["total_h"] - b["total_h"]
            de = r["n_escaped"] - b["n_escaped"]
            flag = "  ⚠ 이탈 차이 큼 — 비교 성립 안 함" if abs(de) > ESCAPE_TOL else ""
            print(f"  리드 {r['lead_min']:>4.0f}분  {d:+8.1f}h  "
                  f"(이탈 {de:+.0f}, 재배정 {r['s4_relocated']:,.0f}, "
                  f"거부 {r['s4_relocate_vetoed']:,.0f}){flag}")

    print(f"\nCSV: {out}")
    print("\n⚠ DB 에는 **마지막 리드타임의 run 만** 남는다 (run_id 가 리드타임을")
    print("   구분하지 못한다). 이 CSV 가 스윕의 유일한 기록이다.")
    print("⚠ 시드가 적으면 **판정이 아니다.** 방향만 본다 — 판정은 #144 에서.")


if __name__ == "__main__":
    raise SystemExit(main())
