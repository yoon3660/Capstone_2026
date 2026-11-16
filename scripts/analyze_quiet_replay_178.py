"""#178 세 기간 KPI 표 — 평시·설(같은 세계·같은 채점)과 추석(#162 참고값).

    python scripts/analyze_quiet_replay_178.py --runs runs/issue178_replay \\
        --inputs data/processed/issue178_quiet202603 --out runs/issue178_replay/summary

입력
    <runs>/quiet_measured/grid.csv   평시, 실측 기온
    <runs>/quiet_seollal/grid.csv    평시, 기온만 설과 같게(−5 °C) — 기온 효과 분리
    <runs>/seollal/grid.csv          설 20개 시나리오, 같은 채점
    <inputs>/traffic_comparison.csv  평시·설·추석 교통량 (VDS)
    <inputs>/observed_direction_day.csv · seollal 실측(#167)   실측 시작 건수

지표는 날짜·방향 값을 실측 시작 건수로 가중한다(#167·#168 과 같다). 시드별로 가중 평균을
내고 시드 평균 ± 95% CI 로 적는다. 평시 − 설 차이는 시드 대응이다(같은 시드 = 같은 SoC·
차종 난수 흐름, 수요는 기간마다 다르다).

추석은 #162 결과(UE · 공급 266 · 시드 7 · 4일 합산 TVD)라 설정이 달라 **참고로만** 둔다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import _ci  # noqa: E402

METRICS = ("station_tvd", "hour_tvd", "station_hour_tvd")
OTHER = ("wait_mean_min", "wait_p95_min", "n_escaped", "n_exhausted", "congested_station_hours",
         "share_wait_gt0", "n_ev_total", "n_ev_charging", "model_starts_all")
CHUSEOK_162 = ROOT / "docs/data_validation/issue162_20261108/passenger266/comparison_metrics.csv"


def seed_weighted(g: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    w = g.assign(**{f"{m}_w": g[m] * g["obs_starts"] for m in METRICS})
    agg = w.groupby(keys + ["seed"], as_index=False).agg(
        **{f"{m}_w": (f"{m}_w", "sum") for m in METRICS}, obs=("obs_starts", "sum"),
        **{o: (o, "sum") if o in ("n_escaped", "n_exhausted", "congested_station_hours",
                                  "n_ev_total", "n_ev_charging", "model_starts_all")
           else (o, "mean") for o in OTHER})
    for m in METRICS:
        agg[m] = agg[f"{m}_w"] / agg["obs"]
    return agg.drop(columns=[f"{m}_w" for m in METRICS])


def table(s: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    cols = list(METRICS) + list(OTHER) + ["obs"]
    out = s.groupby(keys)[cols].agg(["mean", _ci])
    out.columns = [f"{a}" if b == "mean" else f"{a}_ci" for a, b in out.columns]
    return out.reset_index()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=Path, default=ROOT / "runs/issue178_replay")
    ap.add_argument("--inputs", type=Path, default=ROOT / "data/processed/issue178_quiet202603")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--chuseok-metrics", type=Path, default=CHUSEOK_162,
                    help="#162 passenger266/comparison_metrics.csv (git 에 없는 로컬 산출물)")
    args = ap.parse_args()
    out = args.out or args.runs / "summary"
    out.mkdir(parents=True, exist_ok=True)

    frames = []
    for name, sub in (("평시(실측 기온)", "quiet_measured"), ("평시(설 기온 −5°C)", "quiet_seollal"),
                      ("설", "seollal")):
        p = args.runs / sub / "grid.csv"
        if p.is_file():
            frames.append(pd.read_csv(p).assign(run=name))
    g = pd.concat(frames, ignore_index=True)
    g["holiday"] = g["date"].isin(["2026-03-02"])

    s_all = seed_weighted(g, ["run", "model"])
    s_dir = seed_weighted(g, ["run", "model", "direction"])
    kpi = table(s_all, ["run", "model"])
    kpi_dir = table(s_dir, ["run", "model", "direction"])
    kpi.to_csv(out / "kpi_by_period.csv", index=False, encoding="utf-8-sig")
    kpi_dir.to_csv(out / "kpi_by_period_direction.csv", index=False, encoding="utf-8-sig")

    # 날짜별 (시드 평균) — 3/2 대체공휴일 표시
    day = (g.groupby(["run", "model", "direction", "date", "holiday"], as_index=False)
           [list(METRICS) + ["obs_starts", "wait_mean_min", "n_escaped", "temp_c"]].mean())
    day.to_csv(out / "by_date.csv", index=False, encoding="utf-8-sig")

    # 시드 대응 차이: 평시 − 설, 평시(실측 기온) − 평시(설 기온)
    pairs = []
    for model in sorted(set(g["model"])):
        for a, b in (("평시(실측 기온)", "설"), ("평시(설 기온 −5°C)", "설"),
                     ("평시(실측 기온)", "평시(설 기온 −5°C)")):
            x = s_all[(s_all.run == a) & (s_all.model == model)].set_index("seed")
            y = s_all[(s_all.run == b) & (s_all.model == model)].set_index("seed")
            if x.empty or y.empty:
                continue
            for m in list(METRICS) + ["wait_mean_min", "n_escaped", "n_ev_charging"]:
                d = (x[m] - y[m]).dropna()
                pairs.append({"model": model, "a": a, "b": b, "metric": m,
                              "diff": float(d.mean()), "ci95": _ci(d)})
    pd.DataFrame(pairs).to_csv(out / "paired_differences.csv", index=False, encoding="utf-8-sig")

    traffic = pd.read_csv(args.inputs / "traffic_comparison.csv")
    tr = (traffic.groupby(["period", "direction"])[["head_traffic", "total_entries", "mean_section_veh"]]
          .mean().round(0).reset_index())
    tr.to_csv(out / "traffic_by_period.csv", index=False, encoding="utf-8-sig")

    ref = {}
    if args.chuseok_metrics.is_file():
        c = pd.read_csv(args.chuseok_metrics)
        c = c[(c.scope == "ALL_AT_OBSERVED_STATIONS") & (c.metric == "station_tvd")]
        ref = {r.direction: float(r.value) for r in c.itertuples()}
    (out / "chuseok_reference.json").write_text(json.dumps({
        "source": "docs/data_validation/issue162_20261108/passenger266/comparison_metrics.csv", "model": "UE", "supply": "9/18 승용 266",
        "seed": 7, "days": "2026-09-24~27 합산", "station_tvd_all_at_observed": ref,
        "note": "설정·채점 방식이 달라(4일 합산, 공급 266) 평시·설과 같은 표로 비교하지 않는다"},
        ensure_ascii=False, indent=2), encoding="utf-8")

    pd.set_option("display.width", 220)
    show = ["run", "model"] + [c for m in METRICS for c in (m, f"{m}_ci")] + [
        "wait_mean_min", "n_escaped", "congested_station_hours", "n_ev_charging"]
    print(kpi[show].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(pd.DataFrame(pairs).to_string(index=False, float_format=lambda v: f"{v:+.4f}"))
    print(tr.to_string(index=False))
    print("추석 #162 참고 station TVD:", ref)
    print(f"출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
