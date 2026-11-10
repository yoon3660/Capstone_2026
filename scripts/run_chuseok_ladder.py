"""추석 무대에서 사다리를 돌린다 — S0 · S1 · S2 · S3 · S4 + 완전예측 (#146).

    python scripts/run_chuseok_ladder.py
    python scripts/run_chuseok_ladder.py --seeds 1 2 3
    python scripts/run_chuseok_ladder.py --config config/scenario_seollal_up_adoption.yaml

천장은 **완전예측**이다 (UE 도 SO 도 쓰지 않는다, #146).
    UE 는 하루 전체를 열거하고 균형을 반복하는 오프라인 해라 어떤 실시간 엔진도
    못 닿는다 — 천장으로 쓰면 모든 칸이 나빠 보인다. 완전예측은 **같은 실시간
    구조에 미래만 완전히** 준 것이라 **구조적으로 도달 가능한** 상한이다.

    완전예측 만드는 법 (matched_ladder.py 와 같다): S1 의 도착을 오라클로 넣어
    1회차 → 그 결과의 도착을 다시 넣어 2회차. 고정점 2회차를 기준으로 쓴다.

이탈 매칭
    총 사회적 비용 = 총 체류 + 이탈 × escape_cost_min 이라 이탈을 품는다. 그래도
    이탈 차가 허용(5)을 넘으면 '격차 축소율' 은 쓰지 않는다 — 표에 경고로 찍는다.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec, OracleSource  # noqa: E402
from evdt.paths import RUNS_DIR  # noqa: E402
from evdt.runner import load_config, make_run_id, run_once, summarize_kpis, social_cost_h  # noqa: E402

STRENGTH = 0.30
WINDOW = 4
S4_LEAD = 10.0
ESCAPE_TOL = 5

ARRIVAL_COLS = ["ev_id", "station_id", "t_arrive_min", "soc_in", "soc_out", "cold_factor"]

METRICS = ("dwell_total_h", "escape_cost_total_h",
           "renege_wait_total_h", "n_escaped", "n_escaped_balked", "n_escaped_stranded",
           "wait_mean_min", "wait_p95_min", "bottleneck_slots",
           "n_ev", "n_ev_charging", "s3_move_done", "s3_swap_done",
           "s4_relocated", "s4_locked")

#: (스테이지, 어떻게 돌리나)
LADDER = ["S0", "S1", "S2", "S3", "S4", "PERFECT_ROLLING"]
NAME = {"PERFECT_ROLLING": "완전예측"}


def kw_for(stage: str) -> dict:
    if stage in ("S2",):
        return {"observed": ObservedSpec(strength=STRENGTH)}
    if stage == "S3":
        return {"observed": ObservedSpec(strength=STRENGTH), "s3_window": WINDOW}
    if stage == "S4":
        return {"observed": ObservedSpec(strength=STRENGTH), "s3_window": WINDOW,
                "s4_lead_min": S4_LEAD}
    return {}


#: 추석 config 는 #146 (PR #159) 가 들여온다. 그 전에는 이 저장소에 없다 —
#: 없는 파일을 `load_config` 가 그냥 FileNotFoundError 로 터뜨리면 "내가 뭘
#: 잘못했나" 를 한참 찾게 된다.
def _need_config(path: str) -> None:
    if not (ROOT / path).is_file():
        raise SystemExit(
            f"config 가 없다: {path}\n"
            "  추석 시나리오는 #146 (PR #159) 가 들여온다. 그 브랜치를 머지하거나,\n"
            "  --config 로 지금 있는 시나리오를 지정할 것\n"
            "    예: --config config/scenario_seollal_up_adoption.yaml")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_adoption.yaml")
    ap.add_argument("--seeds", type=int, nargs="+", default=[1])
    ap.add_argument("--stages", nargs="+", default=LADDER)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    _need_config(args.config)
    cfg = {st: load_config(args.config, stage=st) for st in args.stages}
    any_cfg = next(iter(cfg.values()))
    escape_cost = float(any_cfg.demand.escape_cost_min)
    out = Path(args.out or (ROOT / "runs" / "sweeps" /
                            f"ladder_{any_cfg.scenario_id}.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n무대: {any_cfg.label}")
    print(f"  시나리오 {any_cfg.scenario_id} · 기간 {any_cfg.demand.period} · "
          f"기온 {any_cfg.environment.temp_c}°C")
    print(f"  시드 {args.seeds} · strength {STRENGTH} · 윈도우 {WINDOW} · "
          f"이탈비용 {escape_cost:.0f}분")
    print(f"  CSV → {out}\n")

    rows: list[dict] = []
    for seed in args.seeds:
        for stage in args.stages:
            label = NAME.get(stage, stage)
            t0 = time.perf_counter()
            print(f"  시드 {seed}  {label:<8} — 실행", end="", flush=True)

            if stage == "PERFECT_ROLLING":
                s1_id = make_run_id(cfg["PERFECT_ROLLING"].scenario_id, "S1", seed,
                                    cfg["PERFECT_ROLLING"].policy.participation)
                src_path = RUNS_DIR / s1_id / "charge_event.parquet"
                if not src_path.exists():
                    print("  ⚠ S1 결과가 없다 — 건너뛴다 (S1 을 먼저 돌릴 것)")
                    continue
                src = pd.read_parquet(src_path)[ARRIVAL_COLS].to_dict("records")
                for rnd in (1, 2):
                    rid = run_once(cfg[stage], seed=seed, overwrite=True,
                                   log=lambda *_: None,
                                   oracle=OracleSource(rows=src, source_run_id=s1_id,
                                                       round_no=rnd))
                    src = pd.read_parquet(RUNS_DIR / rid / "charge_event.parquet")[
                        ARRIVAL_COLS].to_dict("records")
            else:
                rid = run_once(cfg[stage], seed=seed, overwrite=True,
                               log=lambda *_: None, **kw_for(stage))

            s = summarize_kpis([rid]).set_index("metric")["mean"]
            k = {m: float(s.get(m, float("nan"))) for m in METRICS}
            total = social_cost_h(s)
            el = time.perf_counter() - t0
            print(f"  {el:7.1f}s   총 {total:9.1f}h  체류 {k['dwell_total_h']:8.1f}h  "
                  f"이탈 {k['n_escaped']:>4.0f}  대기 {k['wait_mean_min']:5.1f}분  "
                  f"병목 {k['bottleneck_slots']:>4.0f}")
            rows.append({"seed": seed, "stage": stage, "label": label,
                         "run_id": rid, "wall_s": el, "total_h": total, **k})
            pd.DataFrame(rows).to_csv(out, index=False)

    t = pd.DataFrame(rows)
    if t.empty:
        print("\n결과 없음")
        return 1

    print("\n" + "=" * 92)
    print("=== 사다리 ===")
    cols = ["seed", "label", "total_h", "dwell_total_h", "n_escaped",
            "wait_mean_min", "wait_p95_min", "bottleneck_slots", "n_ev_charging"]
    print(t[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    print("\n=== S0 대비 · 천장(완전예측) 대비 ===")
    for seed, g in t.groupby("seed"):
        g = g.set_index("stage")
        if "S0" not in g.index:
            continue
        floor = g.loc["S0", "total_h"]
        ceil = g.loc["PERFECT_ROLLING", "total_h"] if "PERFECT_ROLLING" in g.index else None
        span = (floor - ceil) if ceil is not None else None
        print(f"  [시드 {seed}]  바닥 S0 = {floor:,.1f}h"
              + (f" · 천장 완전예측 = {ceil:,.1f}h · 구간 {span:,.1f}h" if ceil else ""))
        for stage in args.stages:
            if stage == "S0" or stage not in g.index:
                continue
            r = g.loc[stage]
            de = r["n_escaped"] - g.loc["S0", "n_escaped"]
            flag = "" if abs(de) <= ESCAPE_TOL else f"  ⚠ 이탈 {de:+.0f} (허용 {ESCAPE_TOL})"
            share = (f" · 구간의 {(floor - r['total_h']) / span * 100:5.1f}%"
                     if span and span > 0 else "")
            print(f"    {NAME.get(stage, stage):<8} {r['total_h']:>9,.1f}h  "
                  f"{r['total_h'] - floor:+9,.1f}  "
                  f"({(floor - r['total_h']) / floor * 100:+5.1f}% 개선){share}{flag}")

    print(f"\nCSV: {out}")
    if len(args.seeds) == 1:
        print("⚠ 시드 하나다. **판정이 아니다.**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
