#!/usr/bin/env python3
"""#162: fixed-arrivals FIFO charging-duration sensitivity experiment.

Uses project evdt.world.queue_rule.choose_charger (the actual FIFO charger choice).
Read-only: does not modify repo DB, runs, or issue. Writes only --out directory.

Inputs:
  --events  One UE charge_event.csv/parquet from ONE run (one date/direction).
  --db      SQLite MASTER DB with exactly the SAME charger inventory as that UE run.
  --observed Standardized observation CSV:
              station_id,reported_charge_min,elapsed_min[,direction]
  --me-unit-ids CSV containing column charger_id with model UNIT ids to receive
                ME-observed service duration. If omitted with --scope me, ABORT.

Output: baseline_check.csv, event_comparison.csv, station_summary.csv, report.json.
The observed series are *charging time proxies*, not measured waits.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from evdt.world.queue_rule import Charger, choose_charger
from evdt.world.sim import expand_chargers


def load_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def require(df: pd.DataFrame, columns: list[str], label: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{label}: 필수 열 {missing} 누락. 실제 열: {list(df.columns)}")


def load_inventory(dbpath: Path, station_ids: set[str]) -> dict[str, list[Charger]]:
    # Only read the original inventory: model charger counts must not be inferred
    # from event logs; unused installed chargers would disappear from the queue.
    if not dbpath.is_file():
        raise FileNotFoundError(dbpath)
    conn = sqlite3.connect(f"file:{dbpath.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT charger_id, station_id, power_kw, n_units, is_active FROM charger"
        ).fetchall()
    finally:
        conn.close()
    by_station = defaultdict(list)
    for r in rows:
        sid = str(r["station_id"])
        if sid in station_ids:
            by_station[sid].append(dict(r))
    out = {sid: list(expand_chargers(by_station[sid])) for sid in station_ids}
    missing = sorted(sid for sid, fleet in out.items() if not fleet)
    if missing:
        raise ValueError(
            f"충전기 공급 자료가 없는 휴게소: {missing}; 실행 당시에 사용한 DB를 지정해야 함"
        )
    return out


def load_observed(path: Path, kind: str) -> dict[str, list[float]]:
    df = load_table(path)
    require(df, ["station_id", kind], "관측 자료")
    vals = defaultdict(list)
    for _, r in df.iterrows():
        try:
            n = float(r[kind])
        except (TypeError, ValueError):
            continue
        if math.isfinite(n) and n >= 0:
            vals[str(r["station_id"])].append(n)
    return dict(vals)


def reconstruct(
    events: pd.DataFrame,
    fleet: dict[str, list[Charger]],
    *,
    duration_override: dict[str, list[float]] | None = None,
    eligible_units: set[str] | None = None,
    seed: int = 7,
    min_samples: int = 5,
) -> pd.DataFrame:
    rnd = random.Random(seed)
    rows = []
    for sid, group in events.groupby("station_id", sort=True):
        sid = str(sid)
        all_ch = fleet[sid]
        powers = {c.charger_id: c.power_kw for c in all_ch}
        free = {c.charger_id: float(c.available_from_min) for c in all_ch}
        samples = (duration_override or {}).get(sid, [])
        for r in group.sort_values(["t_arrive_min", "event_id"], kind="stable").to_dict("records"):
            arr = float(r["t_arrive_min"])
            unit = choose_charger(all_ch, free, arr)
            use_obs = (
                duration_override is not None
                and (eligible_units is None or unit in eligible_units)
                and len(samples) >= min_samples
            )
            service = rnd.choice(samples) if use_obs else float(r["charge_min"])
            if not math.isfinite(service) or service < 0:
                raise ValueError(f"유효하지 않은 충전시간 {r['event_id']} = {service}")
            start = max(arr, free[unit])
            end = start + service
            free[unit] = end
            rows.append(
                {
                    "event_id": r["event_id"],
                    "station_id": sid,
                    "t_arrive_min": arr,
                    "charger_id": unit,
                    "t_start_min": start,
                    "t_end_min": end,
                    "wait_min": start - arr,
                    "charge_min": service,
                    "used_observed_duration": bool(use_obs),
                    "power_kw": powers[unit],
                }
            )
    return pd.DataFrame(rows)


def summary_table(df: pd.DataFrame, scenario: str) -> pd.DataFrame:
    out = (
        df.groupby("station_id")
        .agg(
            visits=("event_id", "size"),
            mean_wait_min=("wait_min", "mean"),
            p95_wait_min=("wait_min", lambda x: x.quantile(0.95)),
            max_wait_min=("wait_min", "max"),
            any_wait_count=("wait_min", lambda x: (x > 1e-7).sum()),
            mean_charge_min=("charge_min", "mean"),
            observed_duration_count=("used_observed_duration", "sum"),
        )
        .reset_index()
    )
    out["scenario"] = scenario
    out["any_wait_rate"] = out.any_wait_count / out.visits
    return out


def snapshot_table(
    df: pd.DataFrame, scenario: str, *, horizon_min: int, interval_min: int = 5
) -> pd.DataFrame:
    """Model queue/occupancy at fixed 5-min grid, derived from event intervals.

    Event interval interpretation: queue [arrival,start), charging [start,end).
    These counts are simulation outputs, NOT measured physical queues.
    """
    out = []
    ticks = np.arange(0, horizon_min, interval_min, dtype=float)
    for sid, group in df.groupby("station_id", sort=True):
        arrivals = np.sort(group["t_arrive_min"].to_numpy(dtype=float))
        starts = np.sort(group["t_start_min"].to_numpy(dtype=float))
        ends = np.sort(group["t_end_min"].to_numpy(dtype=float))
        queue_count = np.searchsorted(arrivals, ticks, side="right") - np.searchsorted(
            starts, ticks, side="right"
        )
        busy_count = np.searchsorted(starts, ticks, side="right") - np.searchsorted(
            ends, ticks, side="right"
        )
        for t, q, busy in zip(ticks, queue_count, busy_count, strict=False):
            out.append(
                {
                    "station_id": str(sid),
                    "t_min": float(t),
                    "queue_len": int(q),
                    "chargers_busy": int(busy),
                    "scenario": scenario,
                }
            )
    return pd.DataFrame(out)


def main() -> None:
    p = argparse.ArgumentParser(description="FIFO observed-duration sensitivity for #162")
    p.add_argument(
        "--events", type=Path, required=True, help="One UE charge_event.csv/parquet, one run only"
    )
    p.add_argument(
        "--observed",
        type=Path,
        required=True,
        help="Standardized obs CSV: station_id,reported_charge_min,elapsed_min",
    )
    p.add_argument("--db", type=Path, required=True, help="SQLite inventory used for that UE run")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--duration", choices=["reported_charge_min", "elapsed_min"], default="reported_charge_min"
    )
    p.add_argument(
        "--scope",
        choices=["me", "all"],
        default="me",
        help="ME only requires --me-unit-ids; all is only a hypothetical sensitivity",
    )
    p.add_argument(
        "--me-unit-ids", type=Path, help="CSV with charger_id column of model unit IDs (e.g. foo#1)"
    )
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--min-samples", type=int, default=5)
    p.add_argument(
        "--horizon-min", type=int, default=1440, help="One-run snapshot horizon (default 24h)"
    )
    p.add_argument(
        "--allow-baseline-mismatch",
        action="store_true",
        help="Only for diagnostics. Baseline mismatch invalidates causal interpretation.",
    )
    args = p.parse_args()
    if args.horizon_min <= 0:
        raise ValueError("--horizon-min must be > 0")

    original = load_table(args.events).copy()
    require(
        original,
        [
            "station_id",
            "ev_id",
            "t_arrive_min",
            "charge_min",
            "charger_id",
            "wait_min",
            "t_start_min",
        ],
        "UE charge_event",
    )
    if original.empty:
        raise ValueError("UE 원장에 이벤트가 없음")
    original["station_id"] = original.station_id.astype(str)
    original["event_id"] = (
        original.ev_id.astype(str)
        + ":"
        + (original["stop_seq"].astype(str) if "stop_seq" in original else "1")
    )
    if original.event_id.duplicated().any():
        raise ValueError("event_id 중복: 한 run이 아닌 여러 run을 합쳤거나 세션 식별이 불완전함")
    for col in ("t_arrive_min", "charge_min", "wait_min", "t_start_min"):
        original[col] = pd.to_numeric(original[col], errors="raise")
    if (original.t_arrive_min < 0).any() or (original.charge_min < 0).any():
        raise ValueError("음수 도착/충전시간 발견")
    fleet = load_inventory(args.db, set(original.station_id))
    all_units = {c.charger_id for chs in fleet.values() for c in chs}
    unknown = sorted(set(original.charger_id.astype(str)) - all_units)
    if unknown:
        raise ValueError(f"원장 충전기 ID가 현재 DB에 없음: {unknown[:10]} ; 공급 스냅샷 불일치")

    obs = load_observed(args.observed, args.duration)
    if args.scope == "me":
        if not args.me_unit_ids:
            raise ValueError("ME 전용 실험은 --me-unit-ids 가 필수. 운영사를 임의 추정하지 않음")
        ids = load_table(args.me_unit_ids)
        require(ids, ["charger_id"], "ME 충전기 ID 파일")
        requested_me = set(ids.charger_id.astype(str))

        # 전체 DB에서 실제 등록된 ME 충전기인지 먼저 확인한다.
        with sqlite3.connect(args.db) as conn:
            rows = conn.execute("SELECT charger_id, n_units, is_active FROM charger").fetchall()

        db_units = {
            f"{charger_id}#{i}"
            for charger_id, n_units, active in rows
            if int(active)
            for i in range(1, int(n_units) + 1)
        }

        unknown_me = requested_me - db_units
        if unknown_me:
            raise ValueError(f"전체 공급 DB에도 없는 ME 충전기: {sorted(unknown_me)[:10]}")

        # 현재 날짜·방향에서 이벤트가 발생한 휴게소만 사용
        eligible = requested_me & all_units

        print(f"ME 전체: {len(requested_me)}기")
        print(f"현재 실행 대상 ME: {len(eligible)}기")
    else:
        eligible = None

    base = reconstruct(original, fleet, seed=args.seed)
    check = original[["event_id", "station_id", "charger_id", "t_start_min", "wait_min"]].merge(
        base[["event_id", "charger_id", "t_start_min", "wait_min"]],
        on="event_id",
        suffixes=("_original", "_reconstructed"),
        validate="one_to_one",
    )
    check["id_matches"] = check.charger_id_original.astype(str) == check.charger_id_reconstructed
    check["start_delta_min"] = check.t_start_min_reconstructed - check.t_start_min_original
    check["wait_delta_min"] = check.wait_min_reconstructed - check.wait_min_original
    mismatch = (
        ~check.id_matches
        | check.start_delta_min.abs().gt(0.001)
        | check.wait_delta_min.abs().gt(0.001)
    ).sum()
    if mismatch and not args.allow_baseline_mismatch:
        raise ValueError(
            f"기준선 재현 실패: {mismatch}/{len(check)}건. "
            "DB가 런 공급과 동일한지, 원장 단위가 하루/방향별인지 확인. "
            "--allow-baseline-mismatch로 진단만 가능"
        )

    sensitivity = reconstruct(
        original,
        fleet,
        duration_override=obs,
        eligible_units=eligible,
        seed=args.seed,
        min_samples=args.min_samples,
    )
    cmp = base.merge(
        sensitivity, on="event_id", suffixes=("_base", "_sensitivity"), validate="one_to_one"
    )
    cmp["wait_delta_min"] = cmp.wait_min_sensitivity - cmp.wait_min_base
    sensitivity_name = f"observed_{args.duration}_{args.scope}"
    summarized = pd.concat(
        [summary_table(base, "ue_baseline"), summary_table(sensitivity, sensitivity_name)],
        ignore_index=True,
    )
    snapshots = pd.concat(
        [
            snapshot_table(base, "ue_baseline", horizon_min=args.horizon_min),
            snapshot_table(sensitivity, sensitivity_name, horizon_min=args.horizon_min),
        ],
        ignore_index=True,
    )
    qstats = (
        snapshots.groupby(["station_id", "scenario"])
        .agg(
            max_queue_len=("queue_len", "max"),
            mean_queue_len=("queue_len", "mean"),
            queue_positive_slots=("queue_len", lambda x: (x > 0).sum()),
            max_chargers_busy=("chargers_busy", "max"),
        )
        .reset_index()
    )
    summarized = summarized.merge(qstats, on=["station_id", "scenario"], validate="one_to_one")
    hourly_queue = (
        snapshots.assign(hour=(snapshots.t_min // 60).astype(int))
        .groupby(["station_id", "scenario", "hour"], as_index=False)
        .agg(
            mean_queue_len=("queue_len", "mean"),
            max_queue_len=("queue_len", "max"),
            mean_chargers_busy=("chargers_busy", "mean"),
        )
    )
    hourly_wait = pd.concat(
        [base.assign(scenario="ue_baseline"), sensitivity.assign(scenario=sensitivity_name)],
        ignore_index=True,
    )
    hourly_wait = (
        hourly_wait.assign(hour=(hourly_wait.t_arrive_min // 60).astype(int))
        .groupby(["station_id", "scenario", "hour"], as_index=False)
        .agg(
            n_arrivals=("event_id", "size"),
            mean_wait_min=("wait_min", "mean"),
            p95_wait_min=("wait_min", lambda x: x.quantile(0.95)),
        )
    )
    args.out.mkdir(parents=True, exist_ok=True)
    check.to_csv(args.out / "baseline_check.csv", index=False, encoding="utf-8-sig")
    cmp.to_csv(args.out / "event_comparison.csv", index=False, encoding="utf-8-sig")
    summarized.to_csv(args.out / "station_summary.csv", index=False, encoding="utf-8-sig")
    snapshots.to_csv(args.out / "snapshots_5min.csv", index=False, encoding="utf-8-sig")
    hourly_queue.to_csv(args.out / "hourly_queue.csv", index=False, encoding="utf-8-sig")
    hourly_wait.to_csv(args.out / "hourly_wait.csv", index=False, encoding="utf-8-sig")
    report = {
        "classification": "COUNTERFACTUAL_SENSITIVITY_NOT_EXTERNAL_WAIT_VALIDATION",
        "events": len(original),
        "station_count": len(fleet),
        "baseline_mismatch_events": int(mismatch),
        "observed_groups": {k: len(v) for k, v in sorted(obs.items())},
        "observed_duration_assigned_events": int(sensitivity.used_observed_duration.sum()),
        "mean_wait_baseline_min": float(base.wait_min.mean()),
        "mean_wait_sensitivity_min": float(sensitivity.wait_min.mean()),
        "mean_wait_change_min": float(sensitivity.wait_min.mean() - base.wait_min.mean()),
        "max_queue_len_baseline": int(
            snapshots.loc[snapshots.scenario.eq("ue_baseline"), "queue_len"].max()
        ),
        "max_queue_len_sensitivity": int(
            snapshots.loc[snapshots.scenario.eq(sensitivity_name), "queue_len"].max()
        ),
        "seed": args.seed,
        "duration_field": args.duration,
        "scope": args.scope,
        "notes": [
            "Fixed UE station choices and arrival times: no UE re-optimization.",
            "ME record distribution is not valid NONME ground truth.",
            "Observation is recorded charge time or session elapsed; not real queue or verified plug occupancy.",
            "Scenario draws empirical nonnegative durations by station (>=min_samples); preserves 0; no unsupported global imputation.",
            "Uses same FIFO charger-selection rule, but replaces service time; physics curve not recalibrated.",
            "No claims about real-world waiting time MAE/MAPE/accuracy.",
            "Raw CDR session arrivals into parking queue are unavailable.",
        ],
    }
    (args.out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
