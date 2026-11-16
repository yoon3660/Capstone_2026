"""#180 평시(3/2~3/11) 충전이력 실측 기준값 — #167 설과 같은 산출물.

    python scripts/issue180_observed_reference.py

입력
    data/processed/issue178_quiet202603/observed_sessions.csv
        `issue178_observed_march.py` 원장. 기간과 겹치는 세션 전부(이월 포함, in_start_window 로 구분)

#167(`issue167_seollal_observed_reference_report.py`)과 같은 정의다.
    starts                   그 시간에 시작한 세션 수 (기간 내 시작만)
    occupied_min             세션 [시작, 종료) 와 시간 칸이 겹치는 분의 합 (세션-분, 이월 포함)
    max_concurrent_sessions  동시에 진행 중인 세션의 최대 수 (충전기 대수·대기 차량 수가 아니다)
    분포(충전량·경과시간·기록 충전시간)는 기간 내 시작 세션만

#167 의 집계 함수 중 설 숫자가 박히지 않은 것(`audit_quality`·`agg_percentiles`·
`direction_day_peaks`·`station_day_peaks`)은 그대로 가져다 쓴다. 시간표 집계
(`hourly_from_sessions`)는 설 건수(11,405)를 코드에서 검사하므로 같은 규칙으로 여기에 둔다.
기존 파일은 고치지 않는다.

출력 data/processed/issue180_quiet202603/ (#167 과 같은 9종 + 분포·대표 사례·요약)
    quiet_observed_direction_day.csv · _station_10day.csv · _station_day_hour.csv
    quiet_observed_direction_day_peaks.csv · _station_day_peaks.csv
    quiet_observed_carryover_sessions.csv · _carryover_hourly_impact.csv
    quiet_observed_quality_overlap_pairs.csv · _quality_long_sessions.csv
    quiet_observed_distribution_overall.csv   전체·방향별 분포
    quiet_observed_representative.csv         대표 사례
    quiet_observed_summary.json
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from issue167_seollal_observed_reference_report import (  # noqa: E402
    agg_percentiles,
    audit_quality,
    direction_day_peaks,
    station_day_peaks,
)

SRC = ROOT / "data/processed/issue178_quiet202603/observed_sessions.csv"
OUT = ROOT / "data/processed/issue180_quiet202603"
START = pd.Timestamp("2026-03-02")
END = pd.Timestamp("2026-03-12")
DAYS = pd.date_range(START, END - pd.Timedelta(days=1))
#: #178·#179 에서 확인한 값 — 어긋나면 멈춘다
STARTS_EXPECTED = {"DOWN": 4299, "UP": 6674}
SITES_EXPECTED = 29
CARRY_EXPECTED = 13


def hourly_from_sessions(sessions: pd.DataFrame, sites: pd.DataFrame,
                         start_sessions: pd.DataFrame | None = None) -> pd.DataFrame:
    """#167 `hourly_from_sessions` 와 같은 계산. 기간·기대 건수만 이 기간 것."""

    events = defaultdict(list)
    for row in sessions.itertuples(index=False):
        a, b = max(row.start, START), min(row.end, END)
        if b <= a:
            continue
        cursor = a.floor("h")
        while cursor < b:
            lo, hi = max(a, cursor), min(b, cursor + timedelta(hours=1))
            if lo < hi:
                key = (row.direction, row.station_name, cursor.strftime("%Y-%m-%d"), cursor.hour)
                events[key].append((lo, 1))
                events[key].append((hi, -1))
            cursor += timedelta(hours=1)
    rows = []
    for key, timeline in events.items():
        busy, concurrent, peak, prev = 0.0, 0, 0, None
        for clock, delta in sorted(timeline, key=lambda x: (x[0], x[1])):   # 같은 시각이면 종료 먼저
            if prev is not None:
                busy += concurrent * (clock - prev).total_seconds() / 60
            concurrent += delta
            peak = max(peak, concurrent)
            prev = clock
        if concurrent != 0:
            raise AssertionError(f"동시 세션 이벤트 종결 불일치: {key}")
        rows.append((*key, busy, peak))
    occ = pd.DataFrame(rows, columns=["direction", "station_name", "date", "hour",
                                      "occupied_min", "max_concurrent_sessions_in_hour"])
    starts = ((start_sessions if start_sessions is not None else sessions)
              .groupby(["direction", "station_name", "date", "hour"]).size()
              .rename("starts").reset_index())
    grid = sites[["direction", "station_name"]].merge(
        pd.MultiIndex.from_product([DAYS.strftime("%Y-%m-%d"), range(24)],
                                   names=["date", "hour"]).to_frame(index=False), how="cross")
    keys = ["direction", "station_name", "date", "hour"]
    grid = grid.merge(starts, on=keys, how="left", validate="one_to_one")
    grid = grid.merge(occ, on=keys, how="left", validate="one_to_one")
    for c in ("starts", "occupied_min", "max_concurrent_sessions_in_hour"):
        grid[c] = grid[c].fillna(0)
    grid["starts"] = grid["starts"].astype(int)
    grid["max_concurrent_sessions_in_hour"] = grid["max_concurrent_sessions_in_hour"].astype(int)
    if len(grid) != SITES_EXPECTED * len(DAYS) * 24:
        raise AssertionError("시간별 집계 격자 크기 불일치")
    return grid


def read_sessions() -> tuple[pd.DataFrame, pd.DataFrame]:
    o = pd.read_csv(SRC, encoding="utf-8-sig", dtype={"observed_charger_id": str, "source_row": str})
    for c in ("start", "end"):
        o[c] = pd.to_datetime(o[c])
    o["in_start_window"] = o["in_start_window"].astype(str).str.lower().eq("true")
    o["elapsed_from_ts_min"] = (o["end"] - o["start"]).dt.total_seconds() / 60
    df = o[o["in_start_window"]].copy()
    carry = o[~o["in_start_window"] & (o["start"] < START)].copy()
    counts = df["direction"].value_counts().to_dict()
    if counts != STARTS_EXPECTED:
        raise SystemExit(f"[중단] 방향별 시작 건수 불일치: {counts}")
    if len(carry) != CARRY_EXPECTED:
        raise SystemExit(f"[중단] 이월 세션 수 불일치: {len(carry)}")
    if (df["end"] <= df["start"]).any() or df[["start", "end"]].isna().any().any():
        raise SystemExit("[중단] 시작·종료 시각 이상")
    if df["source_row"].duplicated().any():
        raise SystemExit("[중단] source_row 중복")
    df["date"] = df["start"].dt.strftime("%Y-%m-%d")
    df["hour"] = df["start"].dt.hour
    carry["occupied_min_in_window"] = ((carry["end"].clip(upper=END) - START)
                                       .dt.total_seconds() / 60).clip(lower=0)
    return df, carry


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df, carry = read_sessions()
    exact, overlaps, long_sessions, quality = audit_quality(df)
    sites = df[["direction", "station_name"]].drop_duplicates().sort_values(["direction", "station_name"])
    if len(sites) != SITES_EXPECTED:
        raise SystemExit(f"[중단] 관측 휴게소 수 불일치: {len(sites)}")

    start_only = hourly_from_sessions(df, sites)
    hourly = hourly_from_sessions(pd.concat([df, carry[["direction", "station_name", "start", "end"]]],
                                            ignore_index=True), sites, start_sessions=df)
    hourly["occupied_min_start_sessions"] = start_only["occupied_min"]
    hourly["occupied_min_carry_in"] = hourly["occupied_min"] - hourly["occupied_min_start_sessions"]
    hourly.loc[hourly["occupied_min_carry_in"].abs() < 1e-8, "occupied_min_carry_in"] = 0.0
    hourly["max_concurrent_without_carry_in"] = start_only["max_concurrent_sessions_in_hour"]
    hourly["max_concurrent_delta_from_carry_in"] = (hourly["max_concurrent_sessions_in_hour"]
                                                    - hourly["max_concurrent_without_carry_in"])
    affected = hourly.loc[hourly["occupied_min_carry_in"] > 1e-6].copy()

    started_busy = float(start_only["occupied_min"].sum())
    carry_busy = float(carry["occupied_min_in_window"].sum())
    full_busy = float(hourly["occupied_min"].sum())
    if abs(full_busy - started_busy - carry_busy) > 1e-6:
        raise AssertionError("점유시간 합산 검산 실패")
    if int(hourly["starts"].sum()) != len(df):
        raise AssertionError("이월 세션이 시작 건수에 포함됨")

    station = agg_percentiles(df, ["direction", "station_name"]).merge(
        hourly.groupby(["direction", "station_name"], as_index=False).agg(
            occupied_min=("occupied_min", "sum"),
            max_concurrent_sessions=("max_concurrent_sessions_in_hour", "max")),
        on=["direction", "station_name"], validate="one_to_one")
    peak_rows = []
    for (d, s), g in hourly.groupby(["direction", "station_name"]):
        mx = g["max_concurrent_sessions_in_hour"].max()
        hit = g[g["max_concurrent_sessions_in_hour"] == mx].sort_values(["date", "hour"])
        peak_rows.append({"direction": d, "station_name": s, "max_concurrent_peak_times": ", ".join(
            f"{r.date} {int(r.hour):02d}시" for r in hit.head(10).itertuples())})
    station = station.merge(pd.DataFrame(peak_rows), on=["direction", "station_name"], how="left")
    station["share"] = station["starts"] / station["starts"].sum()
    station = station.sort_values("starts", ascending=False)

    direction_day = hourly.groupby(["date", "direction"], as_index=False).agg(
        starts=("starts", "sum"), occupied_min=("occupied_min", "sum"))
    dir_peaks = direction_day_peaks(hourly)
    st_peaks = station_day_peaks(hourly)
    overall = pd.concat([agg_percentiles(df.assign(scope="BOTH"), ["scope"]),
                         agg_percentiles(df, ["direction"]).rename(columns={"direction": "scope"})],
                        ignore_index=True)

    top_dd = direction_day.sort_values("starts", ascending=False).iloc[0]
    top_st = station.iloc[0]
    top_cc = station.sort_values(["max_concurrent_sessions", "starts"], ascending=False).iloc[0]
    top_occ = hourly.sort_values("occupied_min", ascending=False).iloc[0]
    rep = pd.DataFrame([
        {"case": "시작 최다 날짜×방향", "where": f"{top_dd.date} {top_dd.direction}",
         "value": int(top_dd.starts), "unit": "건"},
        {"case": "시작 최다 휴게소", "where": f"{top_st.direction} {top_st.station_name}",
         "value": int(top_st.starts), "unit": "건"},
        {"case": "최대 동시 충전 휴게소", "where": f"{top_cc.direction} {top_cc.station_name}",
         "value": int(top_cc.max_concurrent_sessions), "unit": "세션"},
        {"case": "점유 최다 휴게소×시간", "where": f"{top_occ.direction} {top_occ.station_name} "
         f"{top_occ.date} {int(top_occ.hour):02d}시", "value": round(float(top_occ.occupied_min), 2),
         "unit": "세션-분"}])

    carry_export = carry[["source_row", "direction", "station_name", "source_station_name",
                          "observed_charger_id", "start", "end", "occupied_min_in_window",
                          "energy_kwh"]].assign(source_kind="carry_in_before_start")
    long_export = long_sessions[["source_row", "direction", "station_name", "observed_charger_id",
                                 "start", "end", "elapsed_from_ts_min", "energy_kwh"]]
    outputs = {
        "quiet_observed_direction_day.csv": direction_day,
        "quiet_observed_station_10day.csv": station,
        "quiet_observed_station_day_hour.csv": hourly,
        "quiet_observed_direction_day_peaks.csv": dir_peaks,
        "quiet_observed_station_day_peaks.csv": st_peaks,
        "quiet_observed_carryover_sessions.csv": carry_export,
        "quiet_observed_carryover_hourly_impact.csv": affected,
        "quiet_observed_quality_overlap_pairs.csv": overlaps,
        "quiet_observed_quality_long_sessions.csv": long_export,
        "quiet_observed_distribution_overall.csv": overall,
        "quiet_observed_representative.csv": rep,
    }
    for name, frame in outputs.items():
        frame.to_csv(OUT / name, index=False, encoding="utf-8-sig")

    ov = overall[overall["scope"] == "BOTH"].iloc[0]
    summary = {
        "issue": 180, "window": [str(START), str(END)], "source": SRC.relative_to(ROOT).as_posix(),
        "starts": len(df), "starts_by_direction": STARTS_EXPECTED, "observed_sites": len(sites),
        "occupied_min_start_sessions": round(started_busy, 2),
        "occupied_min_carry_in": round(carry_busy, 2), "carry_in_sessions": len(carry),
        "occupied_min_total": round(full_busy, 2), "occupied_hours_total": round(full_busy / 60, 2),
        "carry_affected_site_hours": len(affected),
        "energy_kwh_mean_p50_p90": [round(float(ov[f"energy_kwh_{k}"]), 3) for k in ("mean", "p50", "p90")],
        "elapsed_min_mean_p50_p90": [round(float(ov[f"elapsed_min_{k}"]), 3) for k in ("mean", "p50", "p90")],
        "reported_charge_min_mean_p50_p90": [round(float(ov[f"reported_charge_min_{k}"]), 3)
                                             for k in ("mean", "p50", "p90")],
        "charger_id_blank": int(df["observed_charger_id"].fillna("").astype(str).str.strip().eq("").sum()),
        "max_concurrent": {"where": f"{top_cc.direction} {top_cc.station_name}",
                           "sessions": int(top_cc.max_concurrent_sessions)},
        "quality": quality,
    }
    (OUT / "quiet_observed_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                                default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k != "quality"}, ensure_ascii=False, indent=1))
    print(json.dumps(quality, ensure_ascii=False))
    print(rep.to_string(index=False))
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
