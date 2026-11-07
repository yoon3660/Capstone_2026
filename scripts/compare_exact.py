"""S3 휴리스틱 vs **전수 열거** — 그룹 안에서 얼마나 손해 보나 (#143).

    python scripts/compare_exact.py --seeds 1
    python scripts/compare_exact.py --seeds 1 --window 4 --reverify

## 무엇을 재는가

`docs/objective.md` §3.5 가 말한 것을 숫자로 만든다.

S3 의 이동·교환은 **이웃만** 본다 — 한 대를 옮기거나 두 대를 맞바꾼다. 그런데
이동 연산자의 후보가 **그룹이 이미 쓰는 휴게소로 제한**되므로 그룹 하나의 배정
조합에 엄격한 상한이 있다.

    조합 수  <=  window ** window  =  4 ** 4  =  256

**256 가지는 그냥 다 재면 된다.** 그리고 채점기(`_group_total`)는 근사가 아니라
정확한 Σd_i 를 순차 투영으로 계산한다. 즉 **참 목적함수의 진짜 최적해**가 나온다.

그래서 이 스크립트가 답하는 질문은 하나다.

> **S3 의 휴리스틱이 그룹 안에서 얼마나 지는가?**

- 거의 안 지면 → 같은 W 에서 MILP 를 붙일 가치가 없다. solver 의 값은 **W 를
  키우는 것**뿐이고, #143 은 그쪽으로 가야 한다
- 많이 지면 → 그룹 안에서도 남은 몫이 있다. 근사를 조심해서 MILP 를 붙일 값이 있다

**어느 쪽이든 지금까지 이 숫자가 없었다.**

## ⚠ S3_EXACT 는 사다리 칸이 아니다

진단이다. 그런데 **스테이지를 따로 받았다** — `exact_group` 만 켜서 돌리면 같은
`__S3__` run_id 를 덮어쓴다. #123 에서 밟고 #142 에서 또 밟은 함정이라, 이번에는
run_id 가 갈라지게 만들었다 (PERFECT_ROLLING 이 S2Policy 를 쓰면서 따로 스테이지를
받은 것과 같은 이유).

## --reverify 가 하는 일

`_exact_group` 을 넣으면서 휴리스틱 루프를 `_improve_group` 메서드로 **뽑아냈다.**
뽑아내기가 동작을 바꾸지 않았는지 확인해야 한다. `--reverify` 는

1. 저장된 S3 의 KPI 를 **먼저 읽어 두고**
2. S3 를 다시 돌려
3. 두 값을 비교한다

틀리면 거기서 멈춘다. 리팩터가 사다리 숫자를 조용히 바꾸는 것이 **가장 비싼
고장**이다 (#123 · #142).
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

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

#: 관측 예측 신뢰도. **사다리와 같은 값이어야 한다** (`matched_ladder.py`) — 다르면
#: 내부적으로 일관되면서도 사다리 표에 못 올라간다 (#142 에서 한 번 그랬다)
OBSERVED_STRENGTH = 0.30

#: 리팩터가 동작을 바꿨는지 볼 때 허용하는 차이. 같은 코드면 **비트 단위로 같아야**
#: 한다 — 부동소수 합산 순서까지 같으므로 0 에 가까운 값을 쓴다
REVERIFY_TOL = 1e-6

#: run_kpi 에 실제로 적히는 이름을 써야 한다. `s3_moves` · `s3_swaps` 처럼 없는
#: 이름을 쓰면 **조용히 NaN** 이 되고, 표가 비어 있는 이유를 한참 찾게 된다
METRICS = ("dwell_total_h", "n_escaped", "wait_mean_min", "wait_p95_min",
           "bottleneck_slots", "n_ev_charging",
           "s3_window", "s3_move_tried", "s3_move_done",
           "s3_swap_tried", "s3_swap_done", "s3_swap_ms",
           "exact_groups", "exact_better_groups", "exact_tried", "exact_gain_min")


def kpis(run_id: str) -> dict[str, float]:
    s = summarize_kpis([run_id]).set_index("metric")["mean"]
    return {k: float(s.get(k, float("nan"))) for k in METRICS}


def stored_kpis(run_id: str) -> dict[str, float] | None:
    """저장된 run 의 KPI. 없으면 None. **덮어쓰기 전에** 읽어 두는 데 쓴다."""

    with sqlite3.connect(default_db_path()) as conn:
        row = conn.execute("SELECT status FROM run WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row[0] != "DONE":
            return None
    return kpis(run_id)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--window", type=int, default=4)
    ap.add_argument("--strength", type=float, default=OBSERVED_STRENGTH,
                    help="관측 예측 신뢰도. 기본값은 **사다리와 같은 0.30**")
    ap.add_argument("--reverify", action="store_true",
                    help="S3 를 다시 돌려 저장값과 같은지 본다 (리팩터 검증)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    db = default_db_path()
    spec = ObservedSpec(strength=args.strength)

    cfg3 = load_config(args.config, stage="S3")
    cfgx = load_config(args.config, stage="S3_EXACT")

    out = Path(args.out) if args.out else (
        RUNS_DIR / "sweeps" / f"exact_vs_heuristic_w{args.window}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{cfgx.scenario_id}")
    print(f"시드 {seeds} · 윈도우 {args.window} · strength {args.strength:g}")
    print(f"조합 수 상한 {args.window ** args.window:,} 가지/그룹")
    print(f"CSV → {out}\n")

    rows = []
    for seed in seeds:
        # --- S3 (휴리스틱) -------------------------------------------------
        rid3 = make_run_id(cfg3.scenario_id, "S3", seed, cfg3.policy.participation)
        before = stored_kpis(rid3) if args.reverify else None

        ok, why = reusable_run(db, rid3, cfg3, observed=spec, s3_window=args.window)
        if ok and not args.reverify:
            print(f"  S3        시드 {seed} — 재사용")
        else:
            tag = "재검증" if args.reverify else f"실행 ({why})"
            print(f"  S3        시드 {seed} — {tag}", flush=True)
            rid3 = run_once(cfg3, seed=seed, overwrite=True, observed=spec,
                            s3_window=args.window, log=lambda *_: None)
        k3 = kpis(rid3)

        if before is not None:
            bad = {m: (before[m], k3[m]) for m in ("dwell_total_h", "n_escaped",
                                                   "bottleneck_slots", "wait_mean_min")
                   if not _same(before[m], k3[m])}
            if bad:
                print("\n⚠ **리팩터가 동작을 바꿨다.** S3 가 저장값과 다르다:")
                for m, (b, a) in bad.items():
                    print(f"    {m}: 저장 {b!r} → 지금 {a!r}")
                print("  `_improve_group` 뽑아내기를 다시 봐야 한다. 멈춘다.")
                return 2
            print(f"            ✓ 저장값과 같다 (체류 {k3['dwell_total_h']:,.4f}h)")

        rows.append({"seed": seed, "stage": "S3", **k3})

        # --- S3_EXACT (전수 열거) ------------------------------------------
        ridx = make_run_id(cfgx.scenario_id, "S3_EXACT", seed, cfgx.policy.participation)
        okx, whyx = reusable_run(db, ridx, cfgx, observed=spec, s3_window=args.window)
        if okx:
            print(f"  S3_EXACT  시드 {seed} — 재사용")
        else:
            print(f"  S3_EXACT  시드 {seed} — 실행 ({whyx})", flush=True)
            ridx = run_once(cfgx, seed=seed, overwrite=True, observed=spec,
                            s3_window=args.window, log=lambda *_: None)
        rows.append({"seed": seed, "stage": "S3_EXACT", **kpis(ridx)})
        pd.DataFrame(rows).to_csv(out, index=False)

    _report(pd.DataFrame(rows), out, args.window)
    return 0


def _same(a: float, b: float) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    return abs(float(a) - float(b)) <= REVERIFY_TOL


def _report(t: pd.DataFrame, out: Path, window: int) -> None:
    cols = ["seed", "stage", "dwell_total_h", "n_escaped", "wait_mean_min",
            "wait_p95_min", "bottleneck_slots", "s3_swap_ms"]
    print("\n=== 휴리스틱 vs 전수 열거 ===")
    print(t[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    for seed, g in t.groupby("seed"):
        h = g[g.stage == "S3"]
        x = g[g.stage == "S3_EXACT"]
        if h.empty or x.empty:
            continue
        h, x = h.iloc[0], x.iloc[0]

        print(f"\n--- 시드 {seed} ---")
        d = x["dwell_total_h"] - h["dwell_total_h"]
        print(f"  총 체류      {h['dwell_total_h']:>10,.1f}h → {x['dwell_total_h']:>10,.1f}h"
              f"   ({d:+,.1f}h)")
        print(f"  이탈         {h['n_escaped']:>10,.0f}  → {x['n_escaped']:>10,.0f}")
        print(f"  병목 칸      {h['bottleneck_slots']:>10,.0f}  → {x['bottleneck_slots']:>10,.0f}")
        print(f"  조정시간     {h['s3_swap_ms'] / 1000:>10,.1f}s → "
              f"{x['s3_swap_ms'] / 1000:>10,.1f}s")
        print(f"  이동 성공    {h['s3_move_done']:>10,.0f}  → {x['s3_move_done']:>10,.0f}")
        print(f"  교환 성공    {h['s3_swap_done']:>10,.0f}  → {x['s3_swap_done']:>10,.0f}")

        # ★ 같은 그룹을 두 번 풀어 직접 비교한 몫 — 런 단위 차이보다 이게 정확하다
        gain, grp, better, tried = (x["exact_gain_min"], x["exact_groups"],
                                    x["exact_better_groups"], x["exact_tried"])
        print(f"\n  그룹 단위 직접 비교 (그룹 {grp:,.0f}개 · 조합 {tried:,.0f}회 평가)")
        print(f"    열거가 이긴 그룹   {better:,.0f} / {grp:,.0f}"
              f"  ({100 * better / max(grp, 1):.1f}%)")
        print(f"    되찾은 체류 합     {gain:,.1f}분 = {gain / 60:,.1f}h")
        if grp:
            print(f"    그룹당 평균        {gain / grp:.4f}분")

    print(f"\nCSV: {out}")
    print(f"\n조합 수 상한은 window**window = {window ** window:,} 다. 열거가 거의 못 이기면")
    print("**같은 윈도우에서 MILP 를 붙일 값이 없다** — solver 의 값은 윈도우를 키우는")
    print("것이고, 그러면 #143 은 그쪽으로 가야 한다 (`docs/objective.md` §3.5).")
    print("\n⚠ 시드가 적으면 판정이 아니다. 방향만 본다.")


if __name__ == "__main__":
    raise SystemExit(main())
