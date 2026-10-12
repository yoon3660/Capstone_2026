#!/usr/bin/env python3
"""SP3 #64: 2026-02-13~22, 20 independent BASE215 UE days vs observed logs.

Read-only inputs; new output directory only. Compares *simulated ME-labelled*
charging sessions against environment-ministry-provided history, whose actual
operator coverage is NOT established. This is a diagnostic, not validation success.

Run at EVDT project root:
 python scripts/sp3_64_validate_20day_base215.py \
   --out-dir data/processed/sp3_64_validation/compare20_v1

Dependencies: pandas, numpy, PyYAML (standard project environment).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

DATES = [f"2026-02-{day:02d}" for day in range(13, 23)]
LABS = {
    "DOWN": Path("data/processed/sp3_64_operator_lab_down_s0007"),
    "UP": Path("data/processed/sp3_64_operator_lab_up_s0007_retry1"),
}
PEAK = {"DOWN": "2026-02-13", "UP": "2026-02-17"}
DAILY = Path("data/processed/sp3_64_validation/ue_daily_base215")
OBS_HOURLY = Path("data/processed/sp3_64_10day_utilization_hourly.csv")
OBS_SITE = Path("data/processed/sp3_64_10day_utilization_by_station.csv")
UNITS_TOTAL = 215


def need(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"필수 파일이 없음: {path}")
    return path


def sha256(path: Path) -> str:
    with path.open("rb") as fh:
        h = hashlib.sha256()
        for piece in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def norm(value: object) -> str:
    return "".join(str(value).split())


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(need(path), encoding="utf-8-sig", keep_default_na=False)


def number(frame: pd.DataFrame, column: str, *, allow_nan: bool = False) -> pd.Series:
    s = pd.to_numeric(frame[column].replace("", np.nan), errors="raise")
    if (not allow_nan and s.isna().any()) or (~np.isfinite(s.dropna())).any():
        raise ValueError(f"{column}: 결측 또는 무한대가 포함됨")
    return s


def ensure_unique(df: pd.DataFrame, cols: list[str], title: str) -> None:
    if df.duplicated(cols).any():
        raise ValueError(
            f"{title}: 중복 키 {cols}: {df.loc[df.duplicated(cols, keep=False), cols].head(3).to_dict('records')}"
        )


def as_flag(s: pd.Series) -> pd.Series:
    valid = s.astype(str).str.strip().str.lower()
    if not valid.isin(["1", "0", "1.0", "0.0", "true", "false"]).all():
        raise ValueError(f"실측 관측상태가 0/1이 아님: {valid.unique().tolist()}")
    return valid.isin(["1", "1.0", "true"])


def read_observed(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    site = read_csv(root / OBS_SITE)
    hourly = read_csv(root / OBS_HOURLY)
    require_s = {"direction", "station_name", "xlsx_observed_station", "starts_10day"}
    require_h = {
        "date",
        "direction",
        "station_name",
        "hour",
        "session_starts",
        "busy_minutes_clipped_to_hour",
        "xlsx_observed_station",
    }
    for df, needcols in [(site, require_s), (hourly, require_h)]:
        if missing := needcols - set(df.columns):
            raise ValueError(f"실측 CSV 필수 열 누락: {sorted(missing)}")
    for df in [site, hourly]:
        df["direction"] = df["direction"].astype(str).str.strip().str.upper()
        df["key"] = df["station_name"].map(norm)
        df["observed"] = as_flag(df["xlsx_observed_station"])
    ensure_unique(site, ["direction", "key"], "실측 35개소 요약")
    if len(site) != 35 or int(site["observed"].sum()) != 29:
        raise ValueError(f"35개소/관측29개소 불일치: {len(site)}/{site['observed'].sum()}")
    if site.groupby("direction").size().to_dict() != {"DOWN": 19, "UP": 16}:
        raise ValueError("실측 방향별 휴게소 개수 19/16 불일치")
    hourly["date"] = hourly["date"].astype(str).str[:10]
    hourly["hour"] = number(hourly, "hour").astype(int)
    if not hourly["hour"].between(0, 23).all() or not hourly["date"].isin(DATES).all():
        raise ValueError("실측 날짜 또는 시간 범위 이상")
    ensure_unique(hourly, ["direction", "key", "date", "hour"], "실측 10일 시간별")
    if len(hourly) != 35 * 10 * 24:
        raise ValueError(f"실측 행 수 8400이 아님: {len(hourly)}")
    # Preserve NOT_OBSERVED as null; never interpret an empty field as zero.
    hstatus = hourly.merge(
        site[["direction", "key", "observed"]].rename(columns={"observed": "site_observed"}),
        on=["direction", "key"],
        how="left",
        validate="many_to_one",
        indicator=True,
    )
    if (
        not hstatus["_merge"].eq("both").all()
        or not hstatus["observed"].eq(hstatus["site_observed"]).all()
    ):
        raise ValueError("실측 요약과 hourly의 매칭/관측상태 불일치")
    obs = hstatus.loc[hstatus["observed"]].copy()
    unobs = hstatus.loc[~hstatus["observed"]].copy()
    if (
        (unobs["session_starts"].astype(str).str.strip() != "")
        | (unobs["busy_minutes_clipped_to_hour"].astype(str).str.strip() != "")
    ).any():
        raise ValueError("미관측 휴게소를 숫자 0 등으로 채운 행 존재")
    obs["observed_starts"] = number(obs, "session_starts")
    obs["observed_busy_min"] = number(obs, "busy_minutes_clipped_to_hour")
    if (obs[["observed_starts", "observed_busy_min"]] < 0).any().any():
        raise ValueError("음수 실측값")
    if not np.allclose(obs["observed_starts"], obs["observed_starts"].round()):
        raise ValueError("실측 충전 시작 건수가 정수가 아님")
    totals = obs.groupby("direction")["observed_starts"].sum().round().astype(int).to_dict()
    if totals != {"DOWN": 4828, "UP": 6577}:
        raise ValueError(f"실측 10일 총합 불일치: {totals}")
    site_o = site.loc[site["observed"]].copy()
    site_o["starts_10day"] = number(site_o, "starts_10day")
    check = (
        obs.groupby(["direction", "key"])["observed_starts"]
        .sum()
        .reset_index()
        .merge(
            site_o[["direction", "key", "starts_10day"]],
            on=["direction", "key"],
            validate="one_to_one",
        )
    )
    if len(check) != 29 or not np.allclose(check["observed_starts"], check["starts_10day"]):
        raise ValueError("휴게소별 10일 시작 건수 합계 검산 실패")
    return site, hourly


def read_station_lookup(db: Path, direction: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{need(db).resolve().as_posix()}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query("SELECT station_id,name,direction FROM station", conn)
    df["direction"] = df["direction"].astype(str).str.upper()
    df = df[df["direction"].eq(direction)].copy()
    df["key"] = df["name"].map(norm)
    ensure_unique(df, ["key"], "모델 휴게소 이름")
    ensure_unique(df, ["station_id"], "모델 station_id")
    expected = 19 if direction == "DOWN" else 16
    if len(df) != expected:
        raise ValueError(f"{direction}: 모델 휴게소 {len(df)}개 != {expected}개")
    return df


def read_units(root: Path) -> dict:
    a = read_csv(root / LABS["DOWN"] / "model_unit_to_operator.csv")
    b = read_csv(root / LABS["UP"] / "model_unit_to_operator.csv")
    required = {"station_id", "charger_id", "operator_bucket", "operator", "mapping_status"}
    if (required - set(a.columns)) or (required - set(b.columns)):
        raise ValueError("운영사별 가상 충전기 매핑 열 누락")
    ensure_unique(a, ["charger_id"], "가상 충전기 단위")
    if len(a) != UNITS_TOTAL or a["operator_bucket"].value_counts().to_dict() != {
        "NONME": 120,
        "ME": 95,
    }:
        raise ValueError("215슬롯 공급 ME95 + NONME120 불일치")
    if a.sort_values("charger_id").reset_index(drop=True).astype(str).to_dict(
        "records"
    ) != b.sort_values("charger_id").reset_index(drop=True).astype(str).to_dict("records"):
        raise ValueError("DOWN과 UP 가상충전기 매핑표 상이")
    if not a["mapping_status"].astype(str).eq("SIMULATED_SLOT_NOT_FEB_PHYSICAL_ID").all():
        raise ValueError("9월 가상 슬롯과 2월 물리 신원이 혼동됨")
    return dict(
        zip(
            a["charger_id"],
            zip(a["station_id"], a["operator_bucket"], strict=True),
            strict=True,
        )
    )


def get_run(root: Path, direction: str, day: str, unit_lookup: dict) -> tuple[pd.DataFrame, dict]:
    is_peak = day == PEAK[direction]
    if is_peak:
        base = root / LABS[direction]
        db = base / "isolated.db"
        summary_file = base / "run_summary.json"
        event_file = base / "ue_charge_events_by_operator.csv"
        date_provenance = "PROFILE_DATE_INFERRED_IN_PRIOR_VALIDATION"
    else:
        base = root / DAILY / direction.lower()
        db = base / "isolated.db"
        summary_file = base / "days" / day / "run_summary.json"
        event_file = base / "days" / day / "ue_charge_events_by_operator.csv"
        date_provenance = "EXPLICIT_DAY_INPUT"
    summary = json.loads(need(summary_file).read_text(encoding="utf-8-sig"))
    if summary.get("direction") != direction:
        raise ValueError(f"{direction} {day}: run_summary 방향 불일치")
    if not is_peak and summary.get("input_date") != day:
        raise ValueError(f"{direction} {day}: run_summary 입력날짜 불일치")
    rid = summary.get("run_id")
    if not rid or "sp364-op-base215" not in rid or "__UE__p100__s0007" not in rid:
        raise ValueError(f"{direction} {day}: BASE215/UE/seed7 run_id 불일치")
    if (not is_peak) and f"day-{day}" not in rid:
        raise ValueError(f"{direction} {day}: 일자 run_id 불일치")
    cfg_path = need(base / "runs" / rid / "config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if cfg.get("direction") != direction or cfg.get("policy", {}).get("stage") != "UE":
        raise ValueError(f"{direction} {day}: run config 방향/UE 오류")
    if float(cfg["demand"]["ev_share"]) != 0.05 or float(cfg["demand"]["demand_multiplier"]) != 1:
        raise ValueError(f"{direction} {day}: 기존 ev_share/dm 변경됨")
    if (
        cfg["vehicles"].get("departure_soc") != "holiday"
        or float(cfg["vehicles"].get("habit_soc", -1)) != 0
    ):
        raise ValueError(f"{direction} {day}: 이전 BASE215 기준 SoC 설정이 아님")
    od = cfg["demand"].get("od_profile")
    if od not in (None, "", "null"):
        raise ValueError(f"{direction} {day}: 이번 10일 검증 기준은 OD 미적용이어야 함: {od}")
    if not is_peak:
        sha = summary.get("input_profiles_sha256") or {}
        for key, suffix in [
            ("demand.volume_profile", "volume.csv"),
            ("demand.through_profile", "through.csv"),
            ("demand.entry_exit_profile", "entry_exit.csv"),
        ]:
            val = cfg["demand"][key.split(".")[1]]
            expected = (
                root
                / "data/processed/sp3_64_validation/daily_inputs"
                / direction.lower()
                / day
                / suffix
            )
            if Path(val).as_posix() != expected.relative_to(root).as_posix():
                raise ValueError(f"{direction} {day}: input profile 경로 불일치: {key}={val}")
            if sha.get(key) != sha256(need(expected)):
                raise ValueError(f"{direction} {day}: 입력 파일이 UE 실행 후 수정됨: {expected}")
    events = read_csv(event_file)
    needed = {"station_id", "charger_id", "operator_bucket", "t_start_min", "t_end_min"}
    if missing := needed - set(events.columns):
        raise ValueError(f"{direction} {day}: 이벤트 필드 누락: {missing}")
    if len(events) != int(summary.get("charge_events_total", -1)):
        raise ValueError(f"{direction} {day}: 이벤트 건수 vs summary 불일치")
    if (
        events["operator_bucket"].value_counts().to_dict()
        != summary["charge_events_by_operator_group"]
    ):
        raise ValueError(f"{direction} {day}: 이벤트 운영사 건수 vs summary 불일치")
    for fld in ["t_start_min", "t_end_min"]:
        events[fld] = number(events, fld)
    if (events["t_start_min"] < 0).any() or (events["t_end_min"] < events["t_start_min"]).any():
        raise ValueError(f"{direction} {day}: 이벤트 시간 역전/음수")
    if not events["operator_bucket"].isin(["ME", "NONME"]).all():
        raise ValueError(f"{direction} {day}: 운영사 그룹 알 수 없음")
    mapped = events["charger_id"].map(unit_lookup)
    if mapped.isna().any() or any(
        u != (s, b)
        for u, s, b in zip(mapped, events["station_id"], events["operator_bucket"], strict=True)
    ):
        raise ValueError(f"{direction} {day}: 가상충전기 매핑 불일치")
    lookup = read_station_lookup(db, direction)
    events = events.merge(
        lookup[["station_id", "key"]], on="station_id", how="left", validate="many_to_one"
    )
    if events["key"].isna().any():
        raise ValueError(f"{direction} {day}: station_id 지도에 없음")
    # Optionally read run_kpi, if available. No reliance on the KPIs for correctness.
    kpis = {}
    with sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True) as conn:
        status = conn.execute("SELECT status FROM run WHERE run_id=?", (rid,)).fetchone()
        if status is None or status[0] != "DONE":
            raise ValueError(f"{direction} {day}: run.status DONE 아님: {status}")
        for metric, val in conn.execute("SELECT metric,value FROM run_kpi WHERE run_id=?", (rid,)):
            kpis[metric] = val
    meta = {
        "direction": direction,
        "date": day,
        "run_id": rid,
        "source_type": "PEAK_REUSED" if is_peak else "DAILY",
        "date_evidence": date_provenance,
        "event_total_all_times": len(events),
        "me_events_all_times": int((events["operator_bucket"] == "ME").sum()),
        "nonme_events_all_times": int((events["operator_bucket"] == "NONME").sum()),
        "n_ev": kpis.get("n_ev"),
        "n_ev_charging": kpis.get("n_ev_charging"),
        "n_ev_infeasible": kpis.get("n_ev_infeasible"),
        "start_spill_total": int((events["t_start_min"] >= 1440).sum()),
        "start_spill_me": int(
            ((events["t_start_min"] >= 1440) & events["operator_bucket"].eq("ME")).sum()
        ),
        "config_od_profile": str(od),
        "config_habit_soc": cfg["vehicles"]["habit_soc"],
    }
    return events, meta


def model_by_hour(events: pd.DataFrame, direction: str, day: str) -> pd.DataFrame:
    ev = events.copy()
    ev["hour"] = np.floor(ev["t_start_min"] / 60).astype(int)
    within = ev.loc[(ev["t_start_min"] >= 0) & (ev["t_start_min"] < 1440)]
    counts = (
        within.groupby(["key", "hour", "operator_bucket"])
        .size()
        .unstack("operator_bucket", fill_value=0)
    )
    starts = (
        counts.reindex(columns=["ME", "NONME"], fill_value=0)
        .rename(columns={"ME": "model_me_starts", "NONME": "model_nonme_starts"})
        .reset_index()
    )
    occupied = []
    # Occupancy must clip each session to the day and to each clock-hour (not start-only).
    for h in range(24):
        overlap = np.maximum(
            0,
            np.minimum(ev["t_end_min"].to_numpy(), (h + 1) * 60)
            - np.maximum(ev["t_start_min"].to_numpy(), h * 60),
        )
        positive = overlap > 0
        if positive.any():
            pieces = ev.loc[positive, ["key", "operator_bucket"]].copy()
            pieces["hour"] = h
            pieces["busy_min"] = overlap[positive]
            occupied.append(pieces)
    if occupied:
        oc = (
            pd.concat(occupied, ignore_index=True)
            .groupby(["key", "hour", "operator_bucket"])["busy_min"]
            .sum()
            .unstack("operator_bucket", fill_value=0)
            .reindex(columns=["ME", "NONME"], fill_value=0)
            .rename(columns={"ME": "model_me_busy_min", "NONME": "model_nonme_busy_min"})
            .reset_index()
        )
    else:
        oc = pd.DataFrame(columns=["key", "hour", "model_me_busy_min", "model_nonme_busy_min"])
    result = starts.merge(oc, on=["key", "hour"], how="outer")
    result[
        ["model_me_starts", "model_nonme_starts", "model_me_busy_min", "model_nonme_busy_min"]
    ] = result[
        ["model_me_starts", "model_nonme_starts", "model_me_busy_min", "model_nonme_busy_min"]
    ].fillna(0)
    result["direction"] = direction
    result["date"] = day
    return result


def metric(frame: pd.DataFrame, actual: str, predicted: str, name: str) -> dict:
    v = frame[[actual, predicted]].dropna()
    a = v[actual].to_numpy(float)
    m = v[predicted].to_numpy(float)
    if not len(a):
        return {
            f"{name}_{x}": None
            for x in (
                "observed_total",
                "model_total",
                "bias",
                "bias_pct",
                "mae",
                "rmse",
                "wape_pct",
                "mape_pct",
                "mape_n",
                "zero_observed_n",
            )
        }
    diff = m - a
    nz = a > 0
    return {
        f"{name}_observed_total": float(a.sum()),
        f"{name}_model_total": float(m.sum()),
        f"{name}_bias": float(diff.sum()),
        f"{name}_bias_pct": float(100 * diff.sum() / a.sum()) if a.sum() > 0 else None,
        f"{name}_mae": float(np.abs(diff).mean()),
        f"{name}_rmse": float(np.sqrt(np.square(diff).mean())),
        f"{name}_wape_pct": float(100 * np.abs(diff).sum() / a.sum()) if a.sum() > 0 else None,
        f"{name}_mape_pct": float(100 * np.mean(np.abs(diff[nz]) / a[nz])) if nz.any() else None,
        f"{name}_mape_n": int(nz.sum()),
        f"{name}_zero_observed_n": int((~nz).sum()),
    }


def make_summary(df: pd.DataFrame, group: list[str], level: str) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(group, dropna=False, sort=True):
        if not isinstance(key, tuple):
            key = (key,)
        item = dict(zip(group, key, strict=True))
        item.update({"level": level, "n_comparable_cells": len(g)})
        item.update(metric(g, "observed_starts", "model_me_starts", "starts_me"))
        item.update(metric(g, "observed_busy_min", "model_me_busy_min", "busy_me"))
        item.update(metric(g, "observed_starts", "model_all_starts", "starts_all_DIAGNOSTIC_ONLY"))
        rows.append(item)
    return pd.DataFrame(rows)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--project-root", type=Path, default=Path.cwd())
    p.add_argument("--out-dir", type=Path, required=True)
    args = p.parse_args()
    root = args.project_root.resolve()
    out = args.out_dir if args.out_dir.is_absolute() else root / args.out_dir
    if out.exists():
        raise FileExistsError(f"기존 결과 덮어쓰기 금지: {out}")
    if not (root / "src/evdt/runner.py").exists():
        raise ValueError("EVDT 프로젝트 루트에서 실행해야 함")
    site, hourly = read_observed(root)
    units = read_units(root)
    df_site_names = site[["direction", "key", "station_name", "observed"]].copy()
    if len(df_site_names) != 35:
        raise ValueError("실측 목록 35개소 아님")
    frames = []
    source = []
    for direction in ("DOWN", "UP"):
        labdb = root / LABS[direction] / "isolated.db"
        mapdb = read_station_lookup(labdb, direction)
        observed_keys = set(df_site_names.loc[df_site_names.direction == direction, "key"])
        model_keys = set(mapdb["key"])
        if observed_keys != model_keys:
            raise ValueError(
                f"{direction}: 실측과 DB 이름/방향 키 불일치, 자동 연결 금지. 실측만 {sorted(observed_keys - model_keys)}, 모델만 {sorted(model_keys - observed_keys)}"
            )
        for day in DATES:
            events, meta = get_run(root, direction, day, units)
            source.append(meta)
            frames.append(model_by_hour(events, direction, day))
            print(
                f"[OK] {direction} {day}: {meta['event_total_all_times']} events; ME {meta['me_events_all_times']}",
                flush=True,
            )
    predicted = pd.concat(frames, ignore_index=True)
    # 35*10*24 table, including not-observed stations, zero filling MODEL only.
    predicted = hourly[["direction", "key", "station_name", "date", "hour", "observed"]].merge(
        predicted, on=["direction", "key", "date", "hour"], how="left", validate="one_to_one"
    )
    mcols = ["model_me_starts", "model_nonme_starts", "model_me_busy_min", "model_nonme_busy_min"]
    predicted[mcols] = predicted[mcols].fillna(0)
    predicted["model_all_starts"] = predicted["model_me_starts"] + predicted["model_nonme_starts"]
    predicted["model_all_busy_min"] = (
        predicted["model_me_busy_min"] + predicted["model_nonme_busy_min"]
    )
    pred = predicted.merge(
        hourly[
            ["direction", "key", "date", "hour", "session_starts", "busy_minutes_clipped_to_hour"]
        ],
        on=["direction", "key", "date", "hour"],
        validate="one_to_one",
    )
    for c in ["observed_starts", "observed_busy_min"]:
        sourcecol = "session_starts" if c == "observed_starts" else "busy_minutes_clipped_to_hour"
        pred[c] = pd.to_numeric(pred[sourcecol].replace("", np.nan), errors="raise")
        if (
            pred.loc[pred["observed"], c].isna().any()
            or pred.loc[~pred["observed"], c].notna().any()
        ):
            raise ValueError(f"실측 결측 상태 오류: {c}")
    pred["observation_status"] = np.where(pred["observed"], "OBSERVED", "NOT_OBSERVED")
    pred["start_error_me"] = np.where(
        pred["observed"], pred["model_me_starts"] - pred["observed_starts"], np.nan
    )
    pred["abs_start_error_me"] = pred["start_error_me"].abs()
    pred["hour_start_APE_pct"] = np.where(
        (pred["observed"]) & (pred["observed_starts"] > 0),
        100 * pred["abs_start_error_me"] / pred["observed_starts"],
        np.nan,
    )
    pred["busy_error_me_min"] = np.where(
        pred["observed"], pred["model_me_busy_min"] - pred["observed_busy_min"], np.nan
    )
    if len(pred) != 35 * 10 * 24 or pred["observed"].sum() != 29 * 10 * 24:
        raise AssertionError("전체 8400/관측 6960 cell 누락")
    obs = pred.loc[pred["observed"]].copy()
    # Station-day rows include zero starts and cannot calculate APE when obs is zero.
    station_day = obs.groupby(["direction", "date", "key", "station_name"], as_index=False).agg(
        observed_starts=("observed_starts", "sum"),
        model_me_starts=("model_me_starts", "sum"),
        model_nonme_starts=("model_nonme_starts", "sum"),
        model_all_starts=("model_all_starts", "sum"),
        observed_busy_min=("observed_busy_min", "sum"),
        model_me_busy_min=("model_me_busy_min", "sum"),
        model_nonme_busy_min=("model_nonme_busy_min", "sum"),
        model_all_busy_min=("model_all_busy_min", "sum"),
        compared_hours=("hour", "nunique"),
    )
    if len(station_day) != 290 or not station_day["compared_hours"].eq(24).all():
        raise AssertionError("station_day 관측 29개소 × 10일 누락")
    station_day["diff_me"] = station_day["model_me_starts"] - station_day["observed_starts"]
    station_day["ape_me_pct"] = np.where(
        station_day["observed_starts"] > 0,
        100 * station_day["diff_me"].abs() / station_day["observed_starts"],
        np.nan,
    )
    station_day["diff_all_DIAGNOSTIC_ONLY"] = (
        station_day["model_all_starts"] - station_day["observed_starts"]
    )
    station_day["busy_diff_me_min"] = (
        station_day["model_me_busy_min"] - station_day["observed_busy_min"]
    )
    sd = make_summary(station_day, ["direction", "date"], "STATION_DAY")
    sh = make_summary(obs, ["direction", "date"], "STATION_HOUR")
    sd = sd.drop(
        columns=[
            "starts_all_DIAGNOSTIC_ONLY_" + suffix
            for suffix in (
                "observed_total",
                "model_total",
                "bias",
                "bias_pct",
                "mae",
                "rmse",
                "wape_pct",
                "mape_pct",
                "mape_n",
                "zero_observed_n",
            )
        ]
    )
    sh = sh.drop(
        columns=[
            "starts_all_DIAGNOSTIC_ONLY_" + suffix
            for suffix in (
                "observed_total",
                "model_total",
                "bias",
                "bias_pct",
                "mae",
                "rmse",
                "wape_pct",
                "mape_pct",
                "mape_n",
                "zero_observed_n",
            )
        ]
    )
    sd = sd.rename(columns={k: k + "_day" for k in sd if k not in ("direction", "date", "level")})
    sh = sh.rename(columns={k: k + "_hour" for k in sh if k not in ("direction", "date", "level")})
    daily = sd.drop(columns="level").merge(
        sh.drop(columns="level"), on=["direction", "date"], validate="one_to_one"
    )
    daily = daily.merge(pd.DataFrame(source), on=["direction", "date"], validate="one_to_one")
    site_ten = make_summary(
        station_day, ["direction", "key", "station_name"], "SITE_10DAY_STATION_DAY"
    )
    direction_ten = make_summary(station_day, ["direction"], "DIRECTION_10DAY_STATION_DAY")
    total_ten = make_summary(
        station_day.assign(period="ALL"), ["period"], "TOTAL_10DAY_STATION_DAY"
    )
    direction_ten_hours = make_summary(obs, ["direction"], "DIRECTION_10DAY_STATION_HOUR")
    total_ten_hours = make_summary(obs.assign(period="ALL"), ["period"], "TOTAL_10DAY_STATION_HOUR")
    byhour = obs.groupby(["direction", "date", "hour"], as_index=False)[
        [
            "observed_starts",
            "model_me_starts",
            "model_nonme_starts",
            "model_all_starts",
            "observed_busy_min",
            "model_me_busy_min",
            "model_nonme_busy_min",
        ]
    ].sum()
    peak = []
    for (direction, day), g in byhour.groupby(["direction", "date"]):
        actual = (
            int(g.loc[g["observed_starts"].idxmax(), "hour"])
            if g["observed_starts"].sum() > 0
            else None
        )
        predicted_h = (
            int(g.loc[g["model_me_starts"].idxmax(), "hour"])
            if g["model_me_starts"].sum() > 0
            else None
        )
        peak.append(
            {
                "direction": direction,
                "date": day,
                "measured_peak_hour": actual,
                "model_me_peak_hour": predicted_h,
                "peak_hour_shift": (predicted_h - actual)
                if actual is not None and predicted_h is not None
                else None,
                "tie_rule": "earliest hour on ties",
            }
        )
    peak = pd.DataFrame(peak)
    daily = daily.merge(peak, on=["direction", "date"], validate="one_to_one")
    # Reconcile source event totals across the 20 independent days.
    source_df = pd.DataFrame(source)
    qa = {
        "label": "SP3_64_BASE215_ME_LABELLED_DIAGNOSTIC",
        "run_count": len(source_df),
        "days_per_direction": source_df.groupby("direction").size().to_dict(),
        "reused_peak_days": source_df.loc[
            source_df["source_type"].eq("PEAK_REUSED"), ["direction", "date"]
        ].to_dict("records"),
        "observed_sites": int(site["observed"].sum()),
        "unobserved_sites": int((~site["observed"]).sum()),
        "station_hour_rows_all_35sites": len(pred),
        "station_hour_rows_observed": len(obs),
        "station_day_rows_observed": len(station_day),
        "measured_10day_starts": obs.groupby("direction")["observed_starts"]
        .sum()
        .astype(int)
        .to_dict(),
        "model_me_starts_20days_observed_sites": obs.groupby("direction")["model_me_starts"]
        .sum()
        .astype(int)
        .to_dict(),
        "model_nonme_starts_20days_observed_sites": obs.groupby("direction")["model_nonme_starts"]
        .sum()
        .astype(int)
        .to_dict(),
        "model_me_start_spills_outside_daily_window": source_df.groupby("direction")[
            "start_spill_me"
        ]
        .sum()
        .astype(int)
        .to_dict(),
        "source_model_event_count_all_times": int(source_df["event_total_all_times"].sum()),
        "model_me_slots": 95,
        "model_nonme_slots": 120,
        "operator_scope": "UNVERIFIED: Environment-ministry-provided XLSX is not proof of ME-operator-only history",
        "equipment_identity": "UNVERIFIED: 2026-09 proxy slots are not confirmed 2026-02 physical operating chargers",
        "od": "NOT_APPLIED",
        "habit_soc": 0.0,
        "day_independence": "No overnight queue/session state carryover; session overlap clipped to [0,1440] for every model day",
        "mape_rule": "Only observed > 0 is in MAPE denominator, including zero actual rows in WAPE numerator; missing observation excluded",
        "mape_coverage": "Consult mape_n and zero_observed_n fields",
        "conclusion": "DIAGNOSTIC_ONLY_NOT_EXTERNAL_VALIDATION_PASS",
    }
    # Write AFTER all source validation; never overwrite any pre-existing output.
    out.mkdir(parents=True, exist_ok=False)

    def save(name, frame):
        frame.to_csv(out / name, index=False, encoding="utf-8-sig", float_format="%.6f", na_rep="")
        print(f"[저장] {out / name}", flush=True)

    save("01_station_hour_ALL35_with_NA.csv", pred)
    save("02_station_day_observed29.csv", station_day)
    save("03_direction_day_metrics.csv", daily)
    save("04_station_10day_metrics.csv", site_ten)
    save("05_direction_10day_station_day_metrics.csv", direction_ten)
    save("06_direction_10day_station_hour_metrics.csv", direction_ten_hours)
    save("07_10day_overall_site_day_metrics.csv", total_ten)
    save("08_10day_overall_site_hour_metrics.csv", total_ten_hours)
    save("09_direction_hourly_totals.csv", byhour)
    save("10_run_input_provenance.csv", source_df)
    (out / "validation_checks.json").write_text(
        json.dumps(qa, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    notes = [
        "# #64 BASE215 2026 설 10일 × 양방향 UE ↔ 환경부 제공 충전이력 (조건부 비교)",
        "",
        "**결과: 기술적 오차 진단만 가능; 실제 충전 이용 재현의 통과/실패 판정 보류.**",
        "",
        "## 실측 범위 · 실행 조건",
        "- 기간: 2026-02-13~22. 각 날짜 00:00~24:00 개별 UE 실행(연속 240시간 재현 아님).",
        "- 35개소 중 29개소만 실측 관측; 미관측 6개소는 결측/NA이지 충전 0건 아님.",
        "- 실측 시작 총합 DOWN 4,828 · UP 6,577 · 전체 11,405건.",
        "- 공급: ME 95 + 비ME 120 = BASE215 가상 충전 서비스 슬롯; 2026년 2월 가동 대수 확정 아님.",
        "- 모델은 OD 미적용(`od_profile: null`), `habit_soc: 0`, 진입 SoC holiday, EV 비중 5%, seed7.",
        "- 실측 환경부 제공 기록의 개별 운영사(ME busiId) 범위는 아직 확정되지 않음.",
        "- 따라서 `model_me`는 비교 가능성 향상을 위한 대리 그룹, `model_all`은 범위 불일치 진단용.",
        "",
        "## 일자별 휴게소×일 시작 건수 ME 오차",
        "",
        "| 방향 | 날짜 | 실측 관측 29개소 범위 | 모델 ME | 차이 | WAPE(휴게소×일) | MAPE(실측 양수 휴게소) | WAPE(휴게소×시간) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]

    def v(x, d=2):
        return "N/A" if pd.isna(x) else f"{x:,.{d}f}"

    for _, r in daily.sort_values(["direction", "date"]).iterrows():
        notes.append(
            f"| {r.direction} | {r.date} | {v(r['starts_me_observed_total_day'], 0)} | {v(r['starts_me_model_total_day'], 0)} | {v(r['starts_me_bias_day'], 0)} | {v(r['starts_me_wape_pct_day'])}% | {v(r['starts_me_mape_pct_day'])}% (n={int(r['starts_me_mape_n_day'])}) | {v(r['starts_me_wape_pct_hour'])}% |"
        )
    notes.extend(["", "## 10일 합산 요약 (휴게소×일 / 휴게소×시간은 다른 정의)", ""])
    for direction in ("DOWN", "UP"):
        d = direction_ten[direction_ten.direction == direction].iloc[0]
        h = direction_ten_hours[direction_ten_hours.direction == direction].iloc[0]
        notes.append(
            f"- {direction}: 실측 {v(d['starts_me_observed_total'], 0)} / ME {v(d['starts_me_model_total'], 0)}; 휴게소×일 WAPE {v(d['starts_me_wape_pct'])}%, MAPE {v(d['starts_me_mape_pct'])}% (양수 실측 행 n={int(d['starts_me_mape_n'])}, 0건 행={int(d['starts_me_zero_observed_n'])}); 휴게소×시간 WAPE {v(h['starts_me_wape_pct'])}%, MAPE {v(h['starts_me_mape_pct'])}% (양수 셀 n={int(h['starts_me_mape_n'])}); 점유시간 WAPE {v(h['busy_me_wape_pct'])}%."
        )
    notes.extend(
        [
            "",
            "## 지표 정의",
            "- 시작건수 절대오차: `|모델_ME - 실측|` (같은 방향/날짜/휴게소/시간 기준).",
            "- WAPE(%) = `100 × SUM(|모델 - 실측|) / SUM(실측)`. 실측=0인 관측 셀도 분자에 포함.",
            "- MAPE(%) = `100 × AVERAGE(|모델 - 실측| / 실측)` **실측 > 0인 관측 셀만**. 제외 셀 수 함께 보고.",
            "- Bias(%) = `100 × (SUM(모델)-SUM(실측)) / SUM(실측)`. WAPE와 다르게 과대/과소 방향 보존.",
            "- MAE = 관측 가능한 셀별 절대오차 평균, RMSE = 평균제곱오차의 제곱근.",
            "- Occupancy: 각 충전 세션의 시작~종료 겹침을 시간대별 60분으로 절단해서 합산. 24시 이후는 당일에서 절단.",
            "- 휴게소×일 WAPE는 하루 총건수를 휴게소별로 먼저 합산하므로, 시간대 배분 오차가 일부 상쇄됨.",
            "- 휴게소×시간 WAPE는 24시간 분포가 틀리면 하루 총합이 맞아도 커질 수 있음.",
            "- 10일 총 WAPE는 일자별 WAPE의 단순 평균이 아니라, 모든 같은 수준 관측셀에서 분자/분모를 다시 합산함.",
            "",
            "## 검증을 주장하면 안 되는 이유",
            "1. 환경부 제공 이력의 개별 운영사·장비 신원이 2026-09 API `ME`와 일대일 검증되지 않음.",
            "2. BASE215는 검증된 물리 충전기 수가 아닌 9월 기반 공급 대리값.",
            "3. 각 날짜는 독립 24시간 실행; 전날의 점유/대기열 이월이 없음.",
            "4. 실측에는 개별 EV의 목적지·SoC·실제 대기시작시각이 없음.",
            "5. 실측 OD 목적지 모델(#99) / #112의 습관 목표 충전 SoC는 이번 BASE215 실험에 적용되지 않음.",
            "",
            "## 역사적 수치와의 비교",
            "- 과거 DOWN 87.67%, UP 54.19%는 ME 휴게소×일 WAPE, 일부 하행 이름 불일치 제외 전 기준. **이번 수치와 동일 조건 아님**.",
            "- 과거 ALL-operator 시간대 WAPE DOWN 235.33%, UP 168.28%는 ME 전용 WAPE와 혼합 비교 금지.",
            "- #99의 통행거리 MAPE 53.0→8.1%(DOWN)는 **통행거리** 성능. 이 보고서의 충전 시작 MAPE와 다른 목표변수.",
            "",
            "## 산출물",
            "- `01_station_hour_ALL35_with_NA.csv`: 관측/미관측 명시한 35개소 × 10일 × 24시간.",
            "- `02_station_day_observed29.csv`: 실측 29개소 × 10일 일자별 절대오차·APE.",
            "- `03_direction_day_metrics.csv`: 방향별 20일 지표.",
            "- `04_station_10day_metrics.csv`: 휴게소별 10일 수준 WAPE·MAPE.",
            "- `05~08`: 방향별·전체 WAPE/MAPE의 합산 수준 비교.",
            "- `09_direction_hourly_totals.csv`: 방향별 날짜별 24시간 충전 피크 비교.",
            "- `10_run_input_provenance.csv`: 실행 ID·입력 기준·일자·익일 이월 건수.",
            "- `validation_checks.json`: 관측 범위, 충전기 수, 모집단 한계 QA.",
        ]
    )
    (out / "VALIDATION_REPORT.md").write_text("\n".join(notes) + "\n", encoding="utf-8")
    print("\n[완료] 측정 29개소 10일 × 24시간, 20개 BASE215 실행 조건부 비교")
    print("지표 보고서:", out / "VALIDATION_REPORT.md")
    for _, r in daily.iterrows():
        print(
            f"{r.direction} {r.date}: 실측={int(r['starts_me_observed_total_day'])}, ME={int(r['starts_me_model_total_day'])}, site WAPE={r['starts_me_wape_pct_day']:.2f}% / hour WAPE={r['starts_me_wape_pct_hour']:.2f}%"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[중단] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
