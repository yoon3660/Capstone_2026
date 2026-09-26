"""두 단계를 나란히 놓는다 — KPI 신뢰구간 표 + 히트맵 (#59).

    python scripts/compare_stages.py --seeds 1-20
    python scripts/compare_stages.py --seeds 1-20 --base UE --other S0
    python scripts/compare_stages.py --seeds 1-20 --config config/scenario_seollal_up.yaml

두 단계를 각각 시드 20개로 돌린 결과가 있어야 한다 (없으면 어느 명령을 먼저 돌릴지 알려준다).

    python scripts/run_experiment.py --seeds 1-20
    python scripts/run_experiment.py --seeds 1-20 --stage S0

무엇이 나오나

    runs/compare/<scenario>__UE_vs_S0__s0001-0020/
        kpi_compare.csv · kpi_compare.md   지표별 평균 · 95% 신뢰구간 · 차이
        heatmap_wait.png                   같은 색 눈금으로 나란히

## 읽는 법 — **구간이 겹치면 "차이가 있다" 고 말하지 않는다**

시드마다 차량 집합이 달라서 KPI 가 흔들린다. 두 신뢰구간이 겹치면 그 흔들림으로
설명되는 차이다. 표의 `판정` 칸이 그걸 말해 준다.

## 히트맵을 **같은 색 눈금**으로 그리는 이유

각자 알아서 정규화하면 둘 다 "빨간 띠가 있는 그림" 이 되어 눈으로는 구별이 안 된다.
같은 눈금으로 그려야 "S0 쪽이 더 붉다" 가 그림에서 보인다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import RUNS_DIR  # noqa: E402
from evdt.runner import (  # noqa: E402
    QUEUE_BIN_LABELS,
    QUEUE_BINS,
    corridor_grid,
    corridor_stations,
    experiment_id,
    load_config,
    parse_seeds,
    with_stage,
)
from evdt.viz.plots import plot_corridor_heatmap  # noqa: E402

matplotlib.use("Agg")

#: 이 지표들은 "작을수록 좋다". 표의 화살표 방향에 쓴다
LOWER_IS_BETTER = {
    "wait_mean_min", "wait_p95_min", "wait_max_min", "dwell_total_h",
    "n_escaped", "n_escaped_balked", "n_escaped_stranded", "bottleneck_slots",
    "share_ratio_max", "wait_worst_station_min",
}

#: 표에 올릴 지표와 순서. 여기 없는 것은 csv 에만 남는다
HEADLINE = [
    "wait_mean_min", "wait_p95_min", "wait_max_min", "wait_worst_station_min",
    "dwell_total_h", "bottleneck_slots", "share_ratio_max",
    "n_escaped", "n_escaped_balked", "n_escaped_stranded", "n_charge_visits",
]

#: 두 단계가 **반드시 같아야** 하는 지표. 다르면 비교가 성립하지 않는다
MUST_MATCH = ["n_ev", "n_ev_charging", "n_ev_no_charge", "n_ev_infeasible",
              "departure_soc_mean"]


def _read(exp_dir: Path, stage: str) -> pd.DataFrame:
    path = exp_dir / "kpi_ci.csv"

    if not path.is_file():
        raise SystemExit(
            f"{stage} 결과가 없습니다: {path}\n"
            f"  먼저:  python scripts/run_experiment.py --seeds <시드> "
            f"{'--stage ' + stage if stage != 'UE' else ''}")

    return pd.read_csv(path).set_index("metric")


def _verdict(a: pd.Series, b: pd.Series) -> str:
    """신뢰구간이 겹치면 "시드 산포로 설명됨". 겹치지 않을 때만 방향을 말한다."""

    if a["ci_high"] >= b["ci_low"] and b["ci_high"] >= a["ci_low"]:
        return "구간 겹침"
    return "악화" if b["mean"] > a["mean"] else "개선"


def compare(base: pd.DataFrame, other: pd.DataFrame,
            base_name: str, other_name: str) -> pd.DataFrame:
    rows = []

    for metric in [m for m in HEADLINE if m in base.index and m in other.index]:
        a, b = base.loc[metric], other.loc[metric]
        change = (b["mean"] - a["mean"]) / a["mean"] * 100 if a["mean"] else float("nan")
        verdict = _verdict(a, b)

        if verdict != "구간 겹침" and metric not in LOWER_IS_BETTER:
            verdict = "증가" if b["mean"] > a["mean"] else "감소"

        rows.append({
            "metric": metric,
            "label": a.get("label", metric),
            f"{base_name}_mean": a["mean"],
            f"{base_name}_ci": f"[{a['ci_low']:,.1f}, {a['ci_high']:,.1f}]",
            f"{other_name}_mean": b["mean"],
            f"{other_name}_ci": f"[{b['ci_low']:,.1f}, {b['ci_high']:,.1f}]",
            "change_pct": change,
            "판정": verdict,
        })

    return pd.DataFrame(rows)


def check_same_world(base: pd.DataFrame, other: pd.DataFrame,
                     base_name: str, other_name: str) -> list[str]:
    """수요가 같은지 확인한다. **다르면 그 차이는 정책의 것이 아니다.**"""

    bad = []

    for metric in MUST_MATCH:
        if metric not in base.index or metric not in other.index:
            continue
        a, b = float(base.loc[metric, "mean"]), float(other.loc[metric, "mean"])
        if abs(a - b) > max(abs(a), 1.0) * 1e-6:
            bad.append(f"{metric}: {base_name} {a:,.4f} vs {other_name} {b:,.4f}")

    return bad


def markdown(table: pd.DataFrame, base_name: str, other_name: str,
             scenario: str, n_seeds: int) -> str:
    head = (f"# {base_name} vs {other_name} — {scenario} (시드 {n_seeds}개, 95% 신뢰구간)\n\n"
            f"**같은 세계, 같은 차.** 운전자가 보는 정보만 다르다.\n\n"
            f"| 지표 | {base_name} | {other_name} | 변화 | 판정 |\n|---|---:|---:|---:|---|\n")
    def change(r) -> str:
        # 기준이 0 이면 배수로 말할 수 없다. UE 의 `n_escaped_stranded` 가 그렇고,
        # 그 0 은 우연이 아니라 원리다 (docs/s0.md §3.1). "+nan%" 대신 절대 증가로 적는다.
        if pd.isna(r["change_pct"]):
            return f"{r[f'{other_name}_mean'] - r[f'{base_name}_mean']:+,.1f}대"
        return f"{r['change_pct']:+.1f}%"

    body = "".join(
        f"| {r['label']} | {r[f'{base_name}_mean']:,.1f} {r[f'{base_name}_ci']} "
        f"| {r[f'{other_name}_mean']:,.1f} {r[f'{other_name}_ci']} "
        f"| {change(r)} | {r['판정']} |\n"
        for _, r in table.iterrows()
    )
    return head + body + (
        "\n> **구간이 겹치면 차이를 주장하지 않는다.** 시드마다 차량 집합이 달라서 "
        "생기는 흔들림으로 설명되는 폭이다.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="두 단계 나란히 비교")
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--base", default="UE")
    ap.add_argument("--other", default="S0")
    ap.add_argument("--metric", default="wait", help="히트맵 지표 (wait | queue)")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    cfg = load_config(args.config)
    cfgs = {s: with_stage(cfg, s) for s in (args.base, args.other)}
    exp = {s: RUNS_DIR / "experiments" / experiment_id(c, seeds) for s, c in cfgs.items()}
    ci = {s: _read(exp[s], s) for s in cfgs}

    mismatch = check_same_world(ci[args.base], ci[args.other], args.base, args.other)
    if mismatch:
        raise SystemExit(
            "두 단계의 **수요가 다릅니다.** 정책 비교가 성립하지 않습니다:\n  "
            + "\n  ".join(mismatch)
            + "\n  같은 시드·같은 config 로 돌렸는지, 한쪽이 옛 코드로 돈 것은 아닌지 확인하세요"
              " (--fresh).")

    table = compare(ci[args.base], ci[args.other], args.base, args.other)
    out = RUNS_DIR / "compare" / f"{cfg.scenario_id}__{args.base}_vs_{args.other}__{len(seeds)}seeds"
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "kpi_compare.csv", index=False, encoding="utf-8")
    md = markdown(table, args.base, args.other, cfg.scenario_id, len(seeds))
    (out / "kpi_compare.md").write_text(md, encoding="utf-8")

    # --- 히트맵: 한 장에 나란히 -------------------------------------------
    # plot_corridor_heatmap 은 패널을 여러 개 받으면 **색 구간을 공유**한다.
    # 따로 그려서 붙이면 각자 정규화되어 "둘 다 빨간 그림" 이 된다.
    panels = [
        (s, corridor_grid(_run_ids(exp[s]), metric=args.metric))
        for s in (args.base, args.other)
    ]
    what = "실제로 기다린 평균 (분)" if args.metric == "wait" else "줄 선 차 수 (대)"
    kw = {} if args.metric == "wait" else {
        "bins": QUEUE_BINS, "bin_labels": QUEUE_BIN_LABELS,
        "value_label": "큐 길이 (대)", "x_label": "시각 (그 시각의 상태)"}

    plot_corridor_heatmap(
        panels, corridor_stations(cfg.corridor_id), out / f"heatmap_{args.metric}.png",
        title=f"{args.base} vs {args.other} — 휴게소 × 시간대 (시드 {len(seeds)}개 합산)",
        subtitle=f"{cfg.scenario_id} · {cfg.demand_label} · 색 = {what} · "
                 f"두 판의 색 구간은 같다",
        **kw,
    )

    print(md)
    print(f"결과 폴더: {out}")
    return 0


def _run_ids(exp_dir: Path) -> list[str]:
    import json
    return json.loads((exp_dir / "manifest.json").read_text(encoding="utf-8"))["run_ids"]


if __name__ == "__main__":
    raise SystemExit(main())
