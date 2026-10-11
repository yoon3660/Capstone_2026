"""사다리 한 장 — UE·S0·S1·S2 를 같은 색 구간으로 나란히 (#120).

    python scripts/plot_ladder.py --config config/scenario_seollal_up_adoption.yaml --seed 1
    python scripts/plot_ladder.py --seed 1 --stages UE,S0,S1,S2 --out figures/ladder.png

## 왜 이게 주력 산출물인가

**숫자와 그림이 같은 것이 하나 있다.**

    bottleneck_slots  =  히트맵에서 어두운 칸의 수
    (설계문서 §8.1: 휴게소 × 시간 평균 대기 ≥ 30분)

30분이 색 구간 경계(`WAIT_BINS_MIN`)와 **정확히 일치**한다 — "30–60 밴드부터 어두운
칸" 이 곧 병목 슬롯이다. 그래서 심사에서 숫자를 의심받으면 **칸을 세라고 하면 된다.**

**그 약속을 기계가 검사한다.** 그리는 격자에서 다시 세어 `run_kpi` 에 기록된 값과
대조하고, 다르면 그림을 저장하지 않고 멈춘다. 처음에 24시간으로 잘랐더니 KPI 137 vs
그림 128 로 어긋났다 — 귀성 심야 도착(최대 28시)이 축에서 떨어졌기 때문이다.
그래서 가로축을 데이터 끝까지 늘린다.

## ⚠ 히트맵 혼자서는 거짓말을 한다

S0 칸이 S1 칸보다 **밝게** 나온다. S0 가 294대를 고속도로 밖으로 밀어냈기
때문이다 (S1 은 51대). `--escaped` 로 이탈 그림을 같이 뽑아 **반드시 옆에 놓는다** —
없으면 "S0 가 제일 좋다" 로 읽힌다. `compare_stages3.py` 가 경고한
*"힘든 사람을 내보내면 이긴다"* 가 사다리 안에서 실제로 일어났다.

## 읽는 법

각 칸 제목에 세 숫자를 찍는다 — **병목 칸 · 평균 대기 · 이탈**. 셋을 같이 봐야 한다.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.event_log import load_sql  # noqa: E402
from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import BOTTLENECK_WAIT_MIN, load_config  # noqa: E402
from evdt.viz.plots import plot_corridor_heatmap  # noqa: E402

DEFAULT_STAGES = ("UE", "S0", "S1", "S2")

#: 각 칸 제목에 찍는 KPI
NOTE_KPIS = ("bottleneck_slots", "wait_mean_min", "n_escaped")


def stage_label(stage: str, kpi: pd.Series, n_cells: int) -> str:
    """칸 제목. **셋을 같이 적는다** — 병목만 적으면 이탈로 이긴 칸을 못 가린다."""

    extra = ""
    if stage == "S2" and pd.notna(kpi.get("oracle_round")):
        extra = f" · 오라클 {int(kpi['oracle_round'])}회차"
    return (f"{stage}{extra}\n"
            f"병목 {n_cells}칸 · 평균 대기 {kpi['wait_mean_min']:.1f}분 · "
            f"이탈 {int(kpi['n_escaped'])}대")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--stages", default=",".join(DEFAULT_STAGES))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    run_ids = [make_run_id(cfg.scenario_id, st, args.seed, cfg.policy.participation)
               for st in stages]

    db = default_db_path()
    with sqlite3.connect(db) as conn:
        have = {r[0] for r in conn.execute(
            f"SELECT run_id FROM run WHERE status='DONE' AND run_id IN "  # noqa: S608
            f"({','.join('?' * len(run_ids))})", run_ids)}
        kpis = pd.read_sql(
            f"SELECT run_id, metric, value FROM run_kpi WHERE run_id IN "  # noqa: S608
            f"({','.join('?' * len(run_ids))})", conn, params=run_ids
        ).pivot(index="run_id", columns="metric", values="value")

    missing = [r for r in run_ids if r not in have]
    if missing:
        raise SystemExit("\n[중단] 끝나지 않은 run 이 있다 — 같은 시드만 비교한다 (#59):\n  "
                         + "\n  ".join(missing))

    # 같은 세계인지. 다르면 비교가 성립하지 않는다 (#59)
    ev = kpis.loc[run_ids, "n_ev_charging"]
    if ev.nunique() > 1:
        raise SystemExit(f"\n[중단] 단계마다 충전 필요 EV 가 다르다: {ev.to_dict()}\n"
                         "  stage 는 세계를 바꾸지 않는다. 다르면 다른 수요를 비교하는 것이다.")

    with get_conn(db, readonly=True) as conn:
        stations = pd.read_sql(
            "SELECT station_id, name, offset_km FROM station WHERE corridor_id = ?",
            conn, params=(cfg.corridor_id,))

    con = duck_connect(db_path=db, run_ids=run_ids, runs_dir=RUNS_DIR)
    hourly = con.sql(load_sql("station_hourly_wait")).df()

    # 가로축을 **데이터 끝까지** 늘린다. 24 로 자르면 자정 넘은 칸이 사라지고,
    # 그러면 KPI 가 세는 칸 수와 그림에서 셀 수 있는 칸 수가 달라진다
    hours = int(hourly["hour"].max()) + 1
    if hours > 24:
        print(f"  가로축 {hours}시간 — 귀성 심야 도착이 자정을 넘는다 (익일은 점선 뒤)")

    panels, mismatch = [], []
    for stage, rid in zip(stages, run_ids, strict=True):
        grid = hourly[hourly["run_id"] == rid].rename(columns={"mean_wait_min": "value"})
        # ★ 그리는 격자에서 **다시 센다**. 기록된 KPI 와 같아야 "칸을 세 보라" 가 성립한다
        n_cells = int((grid["value"] >= BOTTLENECK_WAIT_MIN).sum())
        recorded = int(kpis.loc[rid, "bottleneck_slots"])
        if n_cells != recorded:
            mismatch.append(f"{stage}: 그림 {n_cells}칸 ≠ 기록 {recorded}칸")
        panels.append((stage_label(stage, kpis.loc[rid], n_cells), grid))

    if mismatch:
        raise SystemExit(
            "\n[중단] 그림에서 센 병목 칸과 run_kpi 가 다르다 — **그림을 저장하지 않는다**:\n  "
            + "\n  ".join(mismatch)
            + "\n\n  이 그림의 쓸모는 '숫자와 그림이 같다' 는 것이다. 어긋난 채로 내놓으면"
              "\n  심사에서 칸을 세는 순간 무너진다. 가로축 범위나 집계를 먼저 맞출 것.")

    out = Path(args.out) if args.out else RUNS_DIR / run_ids[0] / "ladder.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    plot_corridor_heatmap(
        panels, stations, out, hours=hours,
        title=f"엔진 사다리 — 정보가 늘면 병목이 어떻게 줄어드나  ({cfg.scenario_id}, 시드 {args.seed})",
        subtitle=(f"{cfg.demand_label}  ·  병목 기준 평균 대기 ≥ {BOTTLENECK_WAIT_MIN:.0f}분 "
                  f"(= '30–60' 밴드부터)  ·  충전 필요 EV {int(ev.iloc[0]):,}대 (네 칸 동일)"),
    )
    print(f"\n  저장: {out}")

    print("\n=== 칸마다 (셋을 같이 본다) ===")
    t = kpis.loc[run_ids, ["bottleneck_slots", "wait_mean_min", "wait_p95_min",
                           "n_escaped", "dwell_total_h"]].copy()
    t.index = stages
    t["총비용h"] = t["dwell_total_h"] + t["n_escaped"] * cfg.demand.escape_cost_min / 60.0
    print(t.to_string(float_format=lambda v: f"{v:,.1f}"))

    print("\n  ⚠ S0 칸이 S1 칸보다 밝으면 그것은 S0 가 좋아서가 아니다 — "
          f"이탈 {int(t.loc['S0', 'n_escaped']) if 'S0' in t.index else 0}대 대 "
          f"{int(t.loc['S1', 'n_escaped']) if 'S1' in t.index else 0}대를 같이 볼 것.\n"
          "  이탈 그림: python scripts/plot_escaped.py <run_id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
