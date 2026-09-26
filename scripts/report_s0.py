"""UE vs S0 를 표로 뜯어본다 — 어디가 왜 달라졌나 (#59).

    python scripts/report_s0.py --seeds 1-20
    python scripts/report_s0.py --seeds 1-20 --config config/scenario_seollal_up.yaml

`compare_stages.py` 가 **"얼마나 달라졌나"** 를 한 장으로 말한다면, 이쪽은
**"어디가 달라졌나"** 를 뜯어 놓는다. 표 네 개가 나온다.

    1. KPI          단계 전체 요약 (신뢰구간 포함)
    2. 휴게소별      km · 충전기 · 대수 · 평균 대기 · **도착 뭉침**
    3. 이탈 분해     줄이 길어서 / 정책이 몰아넣음 / 진짜로 닿는 곳이 없음
    4. 시간대별      언제 벌어지는가

## 도착 뭉침 지수 = 5분당 도착 대수의 분산 ÷ 평균

**1.0 이면 무작위 도착**(포아송)이고, 2 면 같은 대수가 두 배로 뭉쳐서 온다.
평균 대기는 "얼마나 나쁜가" 를 말하고, 뭉침은 **"왜 나쁜가"** 를 말한다.

쏠림을 몫(수요몫÷충전기몫)으로만 재면 근시안을 못 잡는다 — 같은 몫이 **한꺼번에**
오느냐 고르게 오느냐가 대기를 정하기 때문이다. 실제로 UE 와 S0 의 몰림비는 둘 다
2.1 인데 평균 대기는 40% 차이가 난다.

시드마다 따로 재서 평균 낸다. 20개를 한 통에 부어서 재면 서로 다른 날의 도착이
섞여 뭉침이 씻겨 나간다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.loaders import load_run_table  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    KPI_LABELS,
    experiment_id,
    load_config,
    parse_seeds,
    with_stage,
)

BIN_MIN = 5.0
#: 뭉침 지수를 재는 시간대. 새벽은 도착이 드물어 분산/평균 비가 불안정하다
LIVE = (300.0, 1300.0)


def _run_ids(cfg, stage: str, seeds) -> list[str]:
    import json
    exp = RUNS_DIR / "experiments" / experiment_id(with_stage(cfg, stage), seeds)
    path = exp / "manifest.json"
    if not path.is_file():
        raise SystemExit(
            f"{stage} 결과가 없습니다: {path}\n"
            f"  먼저:  python scripts/run_experiment.py --seeds <시드>"
            f"{' --stage ' + stage if stage != 'UE' else ''}")
    return json.loads(path.read_text(encoding="utf-8"))["run_ids"]


def clumping(arrive_min: pd.Series) -> float:
    """도착 뭉침 = 5분 격자 도착 대수의 분산 ÷ 평균 (1.0 = 무작위 도착)."""

    if arrive_min.empty:
        return float("nan")
    grid = np.arange(LIVE[0], LIVE[1] + BIN_MIN, BIN_MIN)
    counts = (pd.Series(np.floor(arrive_min / BIN_MIN) * BIN_MIN)
              .value_counts().reindex(grid, fill_value=0))
    mean = float(counts.mean())
    return float(counts.var() / mean) if mean > 0 else float("nan")


def per_station(run_ids: list[str]) -> pd.DataFrame:
    """휴게소별 대수 · 평균 대기 · 도착 뭉침. **뭉침은 시드마다 재서 평균 낸다.**"""

    rows = []
    for rid in run_ids:
        ce = load_run_table(rid, "charge_event", RUNS_DIR)
        if ce.empty:
            continue
        for sid, g in ce.groupby("station_id"):
            rows.append({"station_id": sid, "run": rid, "n": len(g),
                         "wait": g["wait_min"].mean(), "clump": clumping(g["t_arrive_min"])})
    per_run = pd.DataFrame(rows)
    return per_run.groupby("station_id").agg(
        n=("n", "mean"), wait=("wait", "mean"), clump=("clump", "mean"))


def escape_split(run_ids: list[str]) -> dict[str, float]:
    """이탈을 셋으로 가른다. **`no_plan` 안에도 정책의 몫이 숨어 있다** (#59)."""

    balked = stranded = no_plan = 0.0
    for rid in run_ids:
        esc = load_run_table(rid, "escape_event", RUNS_DIR)
        if esc.empty:
            continue
        ce = load_run_table(rid, "charge_event", RUNS_DIR)
        charged = set(ce["ev_id"]) if not ce.empty else set()
        balked += (esc["reason"] == "balked").sum()
        np_rows = esc[esc["reason"] == "no_plan"]
        hit = np_rows["ev_id"].isin(charged)
        stranded += int(hit.sum())
        no_plan += int((~hit).sum())
    n = len(run_ids)
    return {"balked": balked / n, "stranded": stranded / n, "no_plan": no_plan / n}


def hourly(run_ids: list[str]) -> pd.Series:
    rows = []
    for rid in run_ids:
        ce = load_run_table(rid, "charge_event", RUNS_DIR)
        if not ce.empty:
            rows.append(ce.assign(h=(ce["t_arrive_min"] // 60).astype(int).clip(0, 23))
                        .groupby("h")["wait_min"].mean())
    return pd.concat(rows, axis=1).mean(axis=1).reindex(range(24))


def stations(corridor_id: str) -> pd.DataFrame:
    with get_conn(default_db_path(), readonly=True) as conn:
        rows = conn.execute(
            "SELECT s.station_id, s.name, s.offset_km, COALESCE(SUM(c.n_units), 0) units"
            " FROM station s LEFT JOIN charger c"
            "   ON c.station_id = s.station_id AND c.is_active = 1"
            " WHERE s.corridor_id = ? GROUP BY s.station_id ORDER BY s.offset_km",
            (corridor_id,)).fetchall()
    st = pd.DataFrame(rows, columns=["station_id", "name", "offset_km", "units"])
    st["gap_km"] = st["offset_km"].diff().fillna(st["offset_km"])
    return st


def main() -> int:
    ap = argparse.ArgumentParser(description="UE vs S0 를 표로 뜯어본다")
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    seeds = parse_seeds(args.seeds)
    ids = {s: _run_ids(cfg, s, seeds) for s in ("UE", "S0")}
    ci = {
        s: pd.read_csv(RUNS_DIR / "experiments"
                       / experiment_id(with_stage(cfg, s), seeds) / "kpi_ci.csv").set_index("metric")
        for s in ids
    }

    st = stations(cfg.corridor_id)
    ps = {s: per_station(v) for s, v in ids.items()}
    esc = {s: escape_split(v) for s, v in ids.items()}
    hr = {s: hourly(v) for s, v in ids.items()}

    out = []
    out.append(f"# UE vs S0 상세 — {cfg.scenario_id} (시드 {len(seeds)}개)\n")
    out.append(f"{cfg.label}\n")

    # --- 1. KPI --------------------------------------------------------------
    out.append("\n## 1. KPI (95% 신뢰구간)\n")
    out.append("| 지표 | UE | S0 | 변화 |\n|---|---:|---:|---:|")
    for m in ("wait_mean_min", "wait_p95_min", "wait_max_min", "wait_worst_station_min",
              "dwell_total_h", "bottleneck_slots", "share_ratio_max", "n_escaped",
              "n_escaped_balked", "n_escaped_stranded", "n_charge_visits",
              "n_ev", "n_ev_charging"):
        if m not in ci["UE"].index or m not in ci["S0"].index:
            continue
        a, b = ci["UE"].loc[m], ci["S0"].loc[m]
        ch = (f"{(b['mean'] - a['mean']) / a['mean'] * 100:+.1f}%" if a["mean"]
              else f"{b['mean'] - a['mean']:+,.1f}")
        overlap = a["ci_high"] >= b["ci_low"] and b["ci_high"] >= a["ci_low"]
        out.append(f"| {KPI_LABELS.get(m, m)} | {a['mean']:,.1f} [{a['ci_low']:,.1f}, {a['ci_high']:,.1f}] "
                   f"| {b['mean']:,.1f} [{b['ci_low']:,.1f}, {b['ci_high']:,.1f}] "
                   f"| {ch}{' (구간 겹침)' if overlap else ''} |")

    # --- 2. 휴게소별 ----------------------------------------------------------
    out.append("\n## 2. 휴게소별 — 대수 · 평균 대기 · 도착 뭉침\n")
    out.append("뭉침 1.0 = 무작위 도착. 시드마다 재서 평균.\n")
    out.append("| km | 휴게소 | 앞과 | 기 | UE 대수 | S0 대수 | UE 대기 | S0 대기 | UE 뭉침 | S0 뭉침 |")
    out.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in st.iterrows():
        u = ps["UE"].reindex([r.station_id]).iloc[0]
        z = ps["S0"].reindex([r.station_id]).iloc[0]
        out.append(
            f"| {r.offset_km:,.0f} | {r['name']} | {r.gap_km:,.0f} | {int(r.units)} "
            f"| {u['n']:,.0f} | {z['n']:,.0f} | {u['wait']:,.1f} | {z['wait']:,.1f} "
            f"| {u['clump']:,.2f} | {z['clump']:,.2f} |")

    # --- 3. 이탈 분해 ---------------------------------------------------------
    out.append("\n## 3. 이탈 분해 — 어디까지가 배정의 몫인가\n")
    out.append("| 이유 | 고칠 수 있나 | UE | S0 | 변화 |\n|---|---|---:|---:|---:|")
    labels = [("balked", "줄이 길어서", "**엔진**"),
              ("stranded", "한 번 서고 나서 갇힘", "**엔진**"),
              ("no_plan", "처음부터 닿는 곳이 없음", "증설·SoC (#55)")]
    for key, what, who in labels:
        a, b = esc["UE"][key], esc["S0"][key]
        out.append(f"| {what} | {who} | {a:,.1f} | {b:,.1f} | {b - a:+,.1f} |")
    ta = esc["UE"]["balked"] + esc["UE"]["stranded"]
    tb = esc["S0"]["balked"] + esc["S0"]["stranded"]
    out.append(f"| **배정의 과녁 (위 둘)** | | **{ta:,.1f}** | **{tb:,.1f}** | **{tb - ta:+,.1f}** |")

    # --- 4. 시간대별 ----------------------------------------------------------
    out.append("\n## 4. 시간대별 평균 대기 (도착 시각 기준)\n")
    out.append("| 시 | UE | S0 | 차이 |\n|---:|---:|---:|---:|")
    for h in range(24):
        a, b = hr["UE"].get(h, float("nan")), hr["S0"].get(h, float("nan"))
        if pd.isna(a) and pd.isna(b):
            continue
        out.append(f"| {h} | {a:,.1f} | {b:,.1f} | {b - a:+,.1f} |")

    text = "\n".join(out) + "\n"
    path = args.out or (RUNS_DIR / "compare"
                        / f"{cfg.scenario_id}__UE_vs_S0__{len(seeds)}seeds" / "detail.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(text)
    print(f"저장 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
