"""추석 무대에서 사다리를 돌린다 — S0 · S1 · S2 · S3 · S4 + 완전예측 (#146).

    python scripts/run_chuseok_ladder.py
    python scripts/run_chuseok_ladder.py --seeds 1 2 3
    python scripts/run_chuseok_ladder.py --config config/scenario_seollal_up_adoption.yaml

천장은 **완전예측**이다 (UE 도 SO 도 쓰지 않는다, #146).
    UE 는 하루 전체를 열거하고 균형을 반복하는 오프라인 해라 어떤 실시간 엔진도
    못 닿는다 — 천장으로 쓰면 모든 칸이 나빠 보인다. 완전예측은 **같은 실시간
    구조에 미래만 완전히** 준 것이다.

    완전예측 만드는 법: S1 의 도착을 오라클로 넣어 1회차 → 그 결과의 도착을 다시
    넣어 2회차. 고정점 2회차를 기준으로 쓴다.

★ **완전예측은 S2 의 천장이다. S3·S4 의 천장이 아니다** (2026-11-11 에 재서 안다)

    `runner.py` 에서 `PERFECT_ROLLING` 은 `else: policy = S2Policy(...)` 로
    떨어진다 — 즉 **S2 구조 + 완전한 미래**다. 여기서 `kw_for(stage)` 를 주지
    않으므로 `s3_window` 도 `s4_lead_min` 도 없다.

    그래서 S4 가 천장을 뚫는다. 설 상행 보급 10시드에서 **6/10** 이고 평균으로는
    동점이다 (S4 가 천장보다 +0.48% 낮음, 95% CI [−1.01, +1.98]).
    S4 의 **구조**가 두 칸 위라서 **정보가 모자라도 따라잡는다** — 고장이 아니라
    천장의 이름이 틀린 것이다. 같은 10시드에서 S2 기준으로 읽으면:

        S2 → 완전예측 (예측만 완벽히)      +4.77%  [+1.60, +7.94]  8/10  유의
        S2 → S4 (구조만 더)              +5.32%  [+3.28, +7.36]  9/10  유의
        완전예측 → S4                    +0.48%  [−1.01, +1.98]  6/10  동점

    **미래를 완전히 아는 것과 구조를 두 칸 올리는 것이 같은 값어치다.**
    발표에서 쓸 수 있는 문장이고, 이 표가 그 근거다.

    ⚠ 그러므로 **S4 의 남은 여유는 아직 모른다.** 알려면 S4 구조에 오라클을 준
      칸이 필요한데(`S4Policy` + `OracleForecast`), 지금은 `runner.py` 가 S4 에
      `oracle=` 을 금지한다. 별도 스테이지로 만들어야 한다.

이탈 매칭
    총 사회적 비용 = 총 체류 + 이탈 비용 + 포기 전 대기라 **이탈을 품는다** —
    그래서 칸끼리의 총비용 비교는 이탈 차가 커도 유효하다 (가격 민감도는
    `breakeven_escape.py` 가 따로 잰다).

    그러나 '바닥–천장 격차 축소율(구간의 N%)' 은 다르다. 바닥과 천장의 이탈이
    다르면 분모가 같은 모집단이 아니다. 이탈 차가 허용(5)을 넘으면 **그 칸은
    찍지 않는다.**

    ⚠ 포기를 켠 뒤(#147) 이 허용은 **아무도 통과하지 못한다.** S0 이 425.6대를
      흘리고 윗칸은 81~117대라, 설 상행 10시드 × 5칸 **50행 전부** 허용을 넘는다
      (−268 ~ −377대). 즉 지금 무대에서 격차 축소율은 **쓸 수 없는 지표**다.
      허용값을 늘려서 통과시키면 안 된다 — 모집단이 실제로 다르다.
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
from evdt.runner import (  # noqa: E402
    load_config,
    make_run_id,
    run_once,
    social_cost_h,
    summarize_kpis,
)

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
    # ★ EV 보급률 축 (#146 "EV 대수 증가 실험"). `variant` 로 돌리므로 **scenario_id
    #   가 갈라진다** — 기본 config 의 run 을 덮어쓰지 않는다. 물리를 바꿔 돌리면서
    #   이름을 그대로 두면 진짜 run 이 날아간다 (2026-11-12 에 실제로 그랬다)
    ap.add_argument("--ev-share", type=float, default=None,
                    help="EV 보급 레이어의 비율을 덮어쓴다 (예: 0.10). "
                         "주면 scenario_id 뒤에 __ev<nn> 이 붙는다")
    args = ap.parse_args()

    _need_config(args.config)
    cfg = {st: load_config(args.config, stage=st) for st in args.stages}
    if args.ev_share is not None:
        if not 0.0 < args.ev_share <= 1.0:
            ap.error(f"--ev-share 는 (0, 1] 이어야 한다 (받은 값: {args.ev_share})")
        import yaml as _yaml
        tag = f"ev{round(args.ev_share * 100):02d}"
        for st, c in cfg.items():
            raw = _yaml.safe_load(c.raw_yaml)
            layers = []
            found = False
            for layer in raw["demand"].get("layers") or []:
                layer = dict(layer)
                if layer.get("kind") == "ev_adoption":
                    layer["ev_share"] = args.ev_share
                    found = True
                layers.append(layer)
            if not found:
                raise SystemExit(
                    f"{args.config} 에 ev_adoption 레이어가 없다 — "
                    "--ev-share 로 덮을 대상이 없다")
            cfg[st] = c.variant(tag, {"demand.layers": layers})
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
            matched = abs(de) <= ESCAPE_TOL
            flag = "" if matched else f"  ⚠ 이탈 {de:+.0f} (허용 {ESCAPE_TOL}) — 구간% 무효"
            # ★ 머리말의 규칙을 **실제로 지킨다.** 전에는 경고를 찍으면서 구간% 를
            #   같이 찍었는데, 사람은 숫자를 읽고 경고는 넘긴다 — 쓰지 말라고 한
            #   값을 읽게 된다. 이탈이 안 맞으면 **아예 안 찍는다**
            share = (f" · 구간의 {(floor - r['total_h']) / span * 100:5.1f}%"
                     if matched and span and span > 0 else "")
            print(f"    {NAME.get(stage, stage):<8} {r['total_h']:>9,.1f}h  "
                  f"{r['total_h'] - floor:+9,.1f}  "
                  f"({(floor - r['total_h']) / floor * 100:+5.1f}% 개선){share}{flag}")

    print(f"\nCSV: {out}")
    if len(args.seeds) == 1:
        print("⚠ 시드 하나다. **판정이 아니다.**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
