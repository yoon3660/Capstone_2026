"""사다리 개선율에서 "3회 정차 소진" 이탈의 몫을 떼어 낸다 (#168 진단 · #149 증거).

    python scripts/decompose_ladder_escapes.py --config config/scenario_chuseok_down.yaml \\
        --seeds 1-5 --stages S0,S1,S2,S3

## 왜

총비용 = 체류 + 이탈 × 120분 이다. 그런데 대기가 짧은 무대에서는 이탈의 대부분이
**정차 한도(`max_stops`)를 찔끔 충전으로 다 써서 "갈 곳 없음" 이 된 차**다
(`docs/meeting_memo_stranded_escape_20261112.md`). 이건 배정의 질이 아니라 모든 엔진이
같이 쓰는 충전 판단 규칙에서 나온다. 그래서 단계 간 개선율에 이 몫이 섞여 있으면
"엔진이 나아졌다" 와 "함정을 덜 밟았다" 를 구분할 수 없다.

## 비용을 셋으로 나눈다

    체류        Σ dwell_min / 60
    줄 이탈     reason = balked                  × escape_cost
    정차 소진   reason = no_plan · 정차 ≥ max_stops × escape_cost   ← 규칙의 몫
    기타 이탈   그 밖의 no_plan                 × escape_cost

그리고 인접 단계 개선율을 두 가지로 낸다 (시드 대응, 95% CI):

    전체        (J_prev − J_next) / J_prev                      기존 정의 그대로
    소진 제외   위 식에서 "정차 소진" 항을 뺀 비용으로             배정이 만든 몫

## 하지 않는 것

- 규칙을 고치지 않는다. 기존 정책을 **그대로** 돌리고 결과를 나눠 셀 뿐이다
- DB·run 기록에 쓰지 않는다 (`runner._run_policy_loop` 만 부른다)
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import _ci, _git_sha, prepare  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path  # noqa: E402

FORECAST_STAGES = ("S2", "S3", "S4")


def run_stage(world, stage: str, *, strength: float, s3_window: int | None) -> dict:
    cfg = R.with_stage(world.cfg, stage)
    obs = ObservedSpec(strength=strength) if stage in FORECAST_STAGES else None
    t0 = time.perf_counter()
    events, _snaps, escapes, _kpis = R._run_policy_loop(
        world.built, world.specs, world.offsets, cfg, world.settings,
        world.range_factor, world.cpf, None, obs, lambda *_: None,
        s3_window=s3_window if stage in ("S3", "S4") else None)
    secs = time.perf_counter() - t0

    stops = collections.Counter(e["ev_id"] for e in events)
    max_stops = int(world.cfg.policy.ue.max_stops)
    esc_h = world.escape_cost_min / 60.0
    n_balked = n_exhaust = n_other = 0
    for e in escapes:
        if e["reason"] == "balked":
            n_balked += 1
        elif stops[e["ev_id"]] >= max_stops:
            n_exhaust += 1
        else:
            n_other += 1
    dwell = sum(e["dwell_min"] for e in events) / 60.0
    return {
        "stage": stage, "n_ev_charging": len(world.built.trips),
        "dwell_h": dwell, "n_escaped": len(escapes), "n_balked": n_balked,
        "n_exhausted": n_exhaust, "n_other_no_plan": n_other,
        "total_h": dwell + len(escapes) * esc_h,
        "total_ex_exhausted_h": dwell + (n_balked + n_other) * esc_h,
        "exhausted_h": n_exhaust * esc_h,
        "wait_mean_min": (sum(e["wait_min"] for e in events) / len(events)) if events else 0.0,
        "seconds": secs,
    }


def paired(table: pd.DataFrame, stages: list[str]) -> pd.DataFrame:
    rows = []
    for a, b in zip(stages, stages[1:], strict=False):
        A = table[table["stage"] == a].set_index("seed")
        B = table[table["stage"] == b].set_index("seed").reindex(A.index)
        rec = {"step": f"{a} → {b}", "n_seeds": int(B["total_h"].notna().sum())}
        for col, name in (("total_h", "전체"), ("total_ex_exhausted_h", "소진제외"),
                          ("dwell_h", "체류만")):
            imp = (A[col] - B[col]) / A[col] * 100.0
            rec[f"{name}_pct"] = imp.mean()
            rec[f"{name}_ci95"] = _ci(imp)
        rec["소진이탈_차이_대"] = (A["n_exhausted"] - B["n_exhausted"]).mean()
        rec["줄이탈_차이_대"] = (A["n_balked"] - B["n_balked"]).mean()
        # 전체 비용 감소 중 "정차 소진 이탈이 줄어든 몫" 의 비율
        drop = (A["total_h"] - B["total_h"]).mean()
        rec["감소중_소진몫_pct"] = ((A["exhausted_h"] - B["exhausted_h"]).mean() / drop * 100.0
                                 if abs(drop) > 1e-9 else float("nan"))
        rows.append(rec)
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--seeds", default="1-5")
    ap.add_argument("--stages", default="S0,S1,S2,S3")
    ap.add_argument("--strength", type=float, default=0.30)
    ap.add_argument("--s3-window", type=int, default=None)
    ap.add_argument("--data-root", default=str(PROJECT_ROOT))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg0 = R.load_config(args.config)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    seeds = R.parse_seeds(args.seeds)
    out = Path(args.out) if args.out else RUNS_DIR / "ladder_decompose" / cfg0.scenario_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps({
        "script": "scripts/decompose_ladder_escapes.py", "config": args.config,
        "scenario_id": cfg0.scenario_id, "seeds": seeds, "stages": stages,
        "strength": args.strength, "s3_window": args.s3_window,
        "escape_cost_min": cfg0.demand.escape_cost_min, "db": str(default_db_path()),
        "data_root": args.data_root, "git": _git_sha(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"{cfg0.scenario_id} · 시드 {seeds} · {stages}\n수요: {cfg0.demand_label}\n")
    rows = []
    for seed in seeds:
        world = prepare(cfg0, seed, root=Path(args.data_root), log=lambda *_: None)
        for st in stages:
            r = {"seed": seed, **run_stage(world, st, strength=args.strength,
                                           s3_window=args.s3_window)}
            rows.append(r)
            print(f"  시드 {seed} {st:<4} 총 {r['total_h']:8.1f}h · 체류 {r['dwell_h']:7.1f}h · "
                  f"이탈 {r['n_escaped']:4d} (줄 {r['n_balked']} · 소진 {r['n_exhausted']} · "
                  f"기타 {r['n_other_no_plan']}) · {r['seconds']:.0f}초", flush=True)
            pd.DataFrame(rows).to_csv(out / "stages.csv", index=False, encoding="utf-8-sig")

    table = pd.DataFrame(rows)
    bad = {s: g["n_ev_charging"].unique().tolist()
           for s, g in table.groupby("seed") if g["n_ev_charging"].nunique() > 1}
    if bad:
        raise SystemExit(f"[중단] 같은 시드인데 단계마다 충전 필요 EV 가 다르다: {bad}")

    steps = paired(table, stages)
    steps.to_csv(out / "steps.csv", index=False, encoding="utf-8-sig")
    means = table.groupby("stage", sort=False)[[
        "total_h", "dwell_h", "n_escaped", "n_balked", "n_exhausted", "n_other_no_plan",
        "wait_mean_min"]].mean()
    print("\n=== 단계별 평균 ===")
    print(means.to_string(float_format=lambda v: f"{v:,.1f}"))
    print("\n=== 인접 단계 개선율 (시드 대응) ===")
    print(steps.to_string(index=False, float_format=lambda v: "nan" if math.isnan(v)
                          else f"{v:,.2f}"))
    print(f"\n출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
