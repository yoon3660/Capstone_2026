"""#180 S0·UE 대응 비교 — 평시·설, 전체·환경부 범위 (#162·#167 형식).

    python scripts/compare_model_observed_180.py \\
        --seollal-sessions <runs/issue167_seollal_s0_history_source_scope_v3/observed_seollal_source_all_charging_history.csv>

입력
    runs/issue180_events/{quiet,seollal}/events*.parquet         `run_events_180.py` (S0·UE, MNL)
    평시 실측  data/processed/issue180_quiet202603/quiet_observed_station_day_hour.csv · issue178 원장
    설 실측    data/processed/issue167_seollal/seollal_observed_station_day_hour.csv · #167 원장(--seollal-sessions)

범위 (모델 쪽만 거른다. 실측은 처음부터 환경부·기후부 충전기만이다)
    ALL  모델 전체 충전기 (판정)
    ME   모델의 ME 슬롯 충전만 (보조 — 충전기 배정 규칙 MAX_POWER_IDLE 영향이 섞인다, #173-B)

공통 규칙 (#162 ALL_AT_OBSERVED_STATIONS 와 같다)
    - 그날 시작한 충전만 (0 ≤ t_start < 1440), 관측 휴게소 29곳만. 미관측 6곳은 N/A
    - 시드(1~3)마다 지표를 내고 시드 평균 ± 95% CI

지표
    총량 편향 %                   (모델 − 실측) / 실측 — 판정하지 않는다(모집단 다름)
    station_tvd_daily            날짜·방향별 휴게소 점유율 TVD, 실측 건수 가중 (#167·#168·#178 과 같다)
    station10_tvd / _wape        10일 합 휴게소 분포 (WAPE 는 건수 — 총량 편향이 섞인다)
    day_tvd / _wape              방향×날짜 분포
    hour_tvd / _wape             방향×시간(10일 합) 분포
    station_hour_tvd             휴게소×시간(10일 합) 분포
    peak_hour_abs_diff           방향×날짜 충전 시작 피크 시각 차이(시간). 실측 동률은 가장 가까운 시각
    peak_within_1h_share         그 차이가 1시간 이하인 비율
    max_concurrent_mae           휴게소별 10일 최대 동시 충전 세션의 절대 오차
    energy_kwh_mean/p50/p90      세션 충전량
    charge_min_mean/p50/p90      모델 충전시간(t_end − t_start) vs 실측 경과시간(종료 − 시작)
    occupied_min_total           세션-분 합 (판정하지 않는다)

출력 runs/issue180_compare/
    metrics_by_seed.csv · metrics.csv (기간 × 모델 × 범위 × 지표: 실측값, 모델 평균, CI)
    station10.csv (휴게소별 실측·모델 건수와 비중) · peaks.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_mnl_world import _ci  # noqa: E402
from sp3_64_validate_20day_base215 import norm  # noqa: E402

EVENTS = ROOT / "runs/issue180_events"
OBS = {
    "quiet": (ROOT / "data/processed/issue180_quiet202603/quiet_observed_station_day_hour.csv",
              ROOT / "data/processed/issue178_quiet202603/observed_sessions.csv"),
    "seollal": (ROOT / "data/processed/issue167_seollal/seollal_observed_station_day_hour.csv", None),
}


def tvd(o: pd.Series, m: pd.Series) -> float:
    idx = o.index.union(m.index)
    o, m = o.reindex(idx).fillna(0.0), m.reindex(idx).fillna(0.0)
    if o.sum() <= 0 or m.sum() <= 0:
        return 1.0
    return 0.5 * float(np.abs(o / o.sum() - m / m.sum()).sum())


def wape(o: pd.Series, m: pd.Series) -> float:
    idx = o.index.union(m.index)
    o, m = o.reindex(idx).fillna(0.0), m.reindex(idx).fillna(0.0)
    return float((m - o).abs().sum() / o.sum() * 100)


def pct(x: pd.Series, q: float) -> float:
    return float(np.percentile(x, q * 100)) if len(x) else float("nan")


def max_concurrent(starts: np.ndarray, ends: np.ndarray) -> int:
    ev = sorted([(s, 1) for s in starts] + [(e, -1) for e in ends], key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def load_obs(period: str, seollal_sessions: Path | None):
    sdh_path, ses_path = OBS[period]
    sdh = pd.read_csv(sdh_path, encoding="utf-8-sig")
    sdh["key"] = sdh["station_name"].map(norm)
    ses_path = ses_path if period == "quiet" else seollal_sessions
    ses = pd.read_csv(ses_path, encoding="utf-8-sig")
    if "in_start_window" in ses.columns:
        ses = ses[ses["in_start_window"].astype(str).str.lower().eq("true")]
    for c in ("start", "end"):
        ses[c] = pd.to_datetime(ses[c])
    ses["elapsed"] = (ses["end"] - ses["start"]).dt.total_seconds() / 60
    return sdh, ses


def peak_hours(g: pd.DataFrame, col: str) -> list[int]:
    mx = g[col].max()
    return sorted(g.loc[g[col] == mx, "hour"].astype(int).tolist()) if mx > 0 else []


def score(ev: pd.DataFrame, sdh: pd.DataFrame, ses: pd.DataFrame, obs_maxc: pd.Series,
          obs_peaks: dict) -> dict:
    sites = sdh.loc[sdh.groupby(["direction", "key"]).starts.transform("sum") > 0,
                    ["direction", "key"]].drop_duplicates()
    m = ev[(ev.t_start_min >= 0) & (ev.t_start_min < 1440)].merge(sites, on=["direction", "key"])
    m = m.assign(hour=(m.t_start_min // 60).astype(int))
    o = sdh.merge(sites, on=["direction", "key"])
    out = {"obs_total": int(o.starts.sum()), "model_total": len(m)}
    out["total_bias_pct"] = (len(m) - o.starts.sum()) / o.starts.sum() * 100
    for d in ("DOWN", "UP"):
        out[f"model_{d}"] = int((m.direction == d).sum())
        out[f"obs_{d}"] = int(o.loc[o.direction == d, "starts"].sum())

    num = den = 0.0
    for (d, day), g in o.groupby(["direction", "date"]):
        os_ = g.groupby("key").starts.sum()
        os_ = os_[os_ > 0]
        ms = m[(m.direction == d) & (m.date == day)].groupby("key").size()
        num += tvd(os_, ms.reindex(os_.index).fillna(0)) * os_.sum()
        den += os_.sum()
    out["station_tvd_daily"] = num / den

    def pair(keys):
        return o.groupby(keys).starts.sum(), m.groupby(keys).size()

    for name, keys in (("station10", ["direction", "key"]), ("day", ["direction", "date"]),
                       ("hour", ["direction", "hour"])):
        a, b = pair(keys)
        out[f"{name}_tvd"] = tvd(a, b)
        out[f"{name}_wape"] = wape(a, b)
    a, b = pair(["direction", "key", "hour"])
    out["station_hour_tvd"] = tvd(a[a > 0], b)

    diffs = []
    for (d, day), g in m.groupby(["direction", "date"]):
        mp = peak_hours(g.groupby("hour").size().rename("n").reset_index(), "n")
        op = obs_peaks.get((day, d), [])
        if mp and op:
            diffs.append(min(abs(x - y) for x in mp[:1] for y in op))
    out["peak_hour_abs_diff"] = float(np.mean(diffs)) if diffs else float("nan")
    out["peak_within_1h_share"] = float(np.mean([x <= 1 for x in diffs])) if diffs else float("nan")

    mc = {}
    for (d, k, _day), g in m.groupby(["direction", "key", "date"]):
        mc[(d, k)] = max(mc.get((d, k), 0), max_concurrent(g.t_start_min.to_numpy(), g.t_end_min.to_numpy()))
    mc = pd.Series(mc).reindex(obs_maxc.index).fillna(0)
    out["max_concurrent_mae"] = float((mc - obs_maxc).abs().mean())
    out["max_concurrent_model_max"] = int(mc.max())
    out["max_concurrent_model_where"] = " ".join(mc.idxmax()) if len(mc) else ""

    out["energy_kwh_mean"], out["energy_kwh_p50"], out["energy_kwh_p90"] = (
        float(m.energy_kwh.mean()), pct(m.energy_kwh, .5), pct(m.energy_kwh, .9))
    cm = m.t_end_min - m.t_start_min
    out["charge_min_mean"], out["charge_min_p50"], out["charge_min_p90"] = (
        float(cm.mean()), pct(cm, .5), pct(cm, .9))
    out["occupied_min_total"] = float(cm.sum())
    out["wait_mean_min"] = float(m.wait_min.mean())
    return out


def observed_values(sdh: pd.DataFrame, ses: pd.DataFrame, obs_maxc: pd.Series) -> dict:
    o = sdh[sdh.groupby(["direction", "key"]).starts.transform("sum") > 0]
    return {"obs_total": int(o.starts.sum()), "model_total": int(o.starts.sum()),
            "model_DOWN": int(o.loc[o.direction == "DOWN", "starts"].sum()),
            "model_UP": int(o.loc[o.direction == "UP", "starts"].sum()),
            "max_concurrent_model_max": int(obs_maxc.max()),
            "max_concurrent_model_where": " ".join(obs_maxc.idxmax()),
            "energy_kwh_mean": float(ses.energy_kwh.mean()), "energy_kwh_p50": pct(ses.energy_kwh, .5),
            "energy_kwh_p90": pct(ses.energy_kwh, .9), "charge_min_mean": float(ses.elapsed.mean()),
            "charge_min_p50": pct(ses.elapsed, .5), "charge_min_p90": pct(ses.elapsed, .9),
            "occupied_min_total": float(o.occupied_min.sum())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seollal-sessions", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "runs/issue180_compare")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows, obs_rows, st_rows, pk_rows = [], [], [], []
    for period in ("quiet", "seollal"):
        sdh, ses = load_obs(period, args.seollal_sessions)
        obs_maxc = sdh[sdh.groupby(["direction", "key"]).starts.transform("sum") > 0].groupby(
            ["direction", "key"]).max_concurrent_sessions_in_hour.max()
        obs_peaks = {}
        dh = sdh.groupby(["date", "direction", "hour"], as_index=False).starts.sum()
        for (day, d), g in dh.groupby(["date", "direction"]):
            obs_peaks[(day, d)] = peak_hours(g, "starts")
        obs_rows.append({"period": period, **observed_values(sdh, ses, obs_maxc)})
        ev = pd.concat([pd.read_parquet(f) for f in sorted((EVENTS / period).glob("events*.parquet"))],
                       ignore_index=True)
        for (model, seed), g in ev.groupby(["model", "seed"]):
            for scope in ("ALL", "ME"):
                s = g if scope == "ALL" else g[g.operator_bucket == "ME"]
                rows.append({"period": period, "model": model, "scope": scope, "seed": seed,
                             **score(s, sdh, ses, obs_maxc, obs_peaks)})
        # 휴게소별 표 (시드 평균, ALL·ME)
        o10 = sdh.groupby(["direction", "key"]).starts.sum()
        o10 = o10[o10 > 0]
        day_ev = ev[(ev.t_start_min >= 0) & (ev.t_start_min < 1440)]
        for model in sorted(ev.model.unique()):
            for scope in ("ALL", "ME"):
                s = day_ev[day_ev.model == model]
                s = s if scope == "ALL" else s[s.operator_bucket == "ME"]
                m10 = s.groupby(["direction", "key"]).size() / s.seed.nunique()
                m10 = m10.reindex(o10.index).fillna(0)
                for (d, k), v in o10.items():
                    st_rows.append({"period": period, "model": model, "scope": scope, "direction": d,
                                    "station": k, "obs_starts": int(v), "obs_share": v / o10.sum(),
                                    "model_starts": float(m10[(d, k)]),
                                    "model_share": float(m10[(d, k)] / m10.sum())})
        for (day, d), hrs in obs_peaks.items():
            pk_rows.append({"period": period, "date": day, "direction": d,
                            "obs_peak_hours": ",".join(f"{h:02d}" for h in hrs)})

    by_seed = pd.DataFrame(rows)
    by_seed.to_csv(args.out / "metrics_by_seed.csv", index=False, encoding="utf-8-sig")
    obs = pd.DataFrame(obs_rows).set_index("period")
    num = [c for c in by_seed.columns if c not in ("period", "model", "scope", "seed")
           and pd.api.types.is_numeric_dtype(by_seed[c])]
    long = []
    for (period, model, scope), g in by_seed.groupby(["period", "model", "scope"]):
        for c in num:
            long.append({"period": period, "model": model, "scope": scope, "metric": c,
                         "observed": obs.loc[period].get(c, np.nan), "model_mean": float(g[c].mean()),
                         "ci95": _ci(g[c]), "n_seeds": len(g)})
    metrics = pd.DataFrame(long)
    metrics.to_csv(args.out / "metrics.csv", index=False, encoding="utf-8-sig")
    obs.reset_index().to_csv(args.out / "observed_values.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(st_rows).to_csv(args.out / "station10.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(pk_rows).to_csv(args.out / "peaks.csv", index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 220)
    show = ["total_bias_pct", "station_tvd_daily", "station10_tvd", "day_tvd", "hour_tvd",
            "station_hour_tvd", "peak_hour_abs_diff", "peak_within_1h_share", "max_concurrent_mae",
            "energy_kwh_mean", "charge_min_mean"]
    piv = metrics[metrics.metric.isin(show)].pivot_table(
        index=["period", "model", "scope"], columns="metric", values="model_mean")[show]
    print(piv.to_string(float_format=lambda v: f"{v:.3f}"))
    print(obs.T.to_string())
    print(f"출력: {args.out}")
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
