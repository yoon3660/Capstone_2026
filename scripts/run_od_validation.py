"""Run monthly OD validation, compare legacy demand and check feasibility."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd
from estimate_od_daily import candidate_matrix, solve_constraints

from evdt.io.demand_profile import entry_hourly_volume, through_share


def summarize_results(root, processed_dir):
    raw = pd.read_parquet(processed_dir / "tcs_od_gyeongbu.parquet")
    raw["start_node"] = "tcs:" + raw.start_office_code.astype(str)
    raw["end_node"] = "tcs:" + raw.end_office_code.astype(str)
    metrics, per_day, checks = ([], [], [])
    for month in ["202602", "202603"]:
        for direction in ["DOWN", "UP"]:
            folder = root / f"expanded_{month}_{direction}"
            summary = pd.read_csv(folder / "metrics.csv")
            summary["period"], summary["direction"] = (month, direction)
            metrics.append(summary)
            fit = pd.read_csv(folder / "hourly_fit.csv")
            for date, day in fit[fit.split.eq("validation") & fit.scoreable].groupby("date"):
                for model in ["fitted_veh", "uniform_profile_veh"]:
                    err = day[model] - day.volume_veh
                    positive = day.volume_veh > 0
                    per_day.append(
                        dict(
                            period=month,
                            direction=direction,
                            date=date,
                            model=model,
                            rows=len(day),
                            rmse=np.sqrt(np.mean(err**2)),
                            mape_pct=(err[positive].abs() / day.volume_veh[positive]).mean() * 100,
                        )
                    )
            hourly = pd.read_parquet(next(folder.glob("od_gyeongbu*.parquet")))
            hourly["date"] = hourly.date.astype("datetime64[ns]")
            measured = hourly[hourly.source.eq("measured_tcs_daily")]
            keys = ["date", "start_node", "end_node"]
            actual = measured.groupby(keys).volume_veh.sum()
            expected = (
                raw[raw.direction.eq(direction) & raw.date.isin(hourly.date.unique())]
                .groupby(keys)
                .volume_veh.sum()
            )
            error = actual - expected
            assert actual.index.equals(expected.index)
            assert error.notna().all() and error.abs().max() < 1e-07
            assert hourly.volume_veh.ge(0).all()
            checks.append(
                dict(
                    period=month,
                    direction=direction,
                    measured_pairs=len(actual),
                    max_sum_error=error.abs().max(),
                    hourly_rows=len(hourly),
                )
            )
    out = root / "expanded_validation"
    out.mkdir(exist_ok=True)
    combined = pd.concat(metrics, ignore_index=True)
    combined.to_csv(out / "metrics.csv", index=False)
    pd.DataFrame(per_day).to_csv(out / "daily_metrics.csv", index=False)
    pd.DataFrame(checks).to_csv(out / "conservation_checks.csv", index=False)
    print(combined[combined.split.eq("validation")].to_string(index=False))
    print("All measured TCS pair sums conserved; hourly volumes nonnegative.")


def compare_legacy(root, processed_dir):
    traffic = pd.read_parquet(processed_dir / "traffic_gyeongbu.parquet")
    tcs = pd.read_parquet(processed_dir / "tcs_od_gyeongbu.parquet")
    scores, distances = ([], [])
    for month in ["202602", "202603"]:
        for direction in ["DOWN", "UP"]:
            folder = root / f"expanded_{month}_{direction}"
            params = json.loads((folder / "parameters.json").read_text())
            train = pd.to_datetime(params["train_dates"])
            fitted = pd.read_csv(folder / "hourly_fit.csv", parse_dates=["date"])
            hourly = pd.read_parquet(next(folder.glob("od_gyeongbu*.parquet")))
            days = sorted(hourly.date.unique())
            training = traffic[traffic.date.isin(train) & traffic.direction.eq(direction)].copy()
            curves = [
                entry_hourly_volume(
                    training, period=month, direction=direction, date=d
                ).volume_veh.to_numpy()
                for d in train
            ]
            survivals = [
                through_share(training, period=month, direction=direction, date=d) for d in train
            ]
            if not all(np.isfinite(c).all() for c in curves):
                raise ValueError("Incomplete training entry profile")
            mean_curve = np.mean(curves, axis=0)
            survival = survivals[0].copy()
            if not all(s.offset_km.equals(survival.offset_km) for s in survivals):
                raise ValueError("Training survival positions differ")
            survival["share"] = np.mean([s.share.to_numpy() for s in survivals], axis=0)
            fixed = tcs[tcs.direction.eq(direction)]
            train_total = np.mean([fixed[fixed.date.eq(d)].volume_veh.sum() for d in train])
            positions = (
                traffic[traffic.period.eq(month) & traffic.direction.eq(direction)]
                .drop_duplicates("conzone_id")
                .set_index("conzone_id")
                .offset_km
            )
            total_hours = int(fitted.time_index.max()) + 1
            predictions = {}
            for zone in fitted.conzone_id.unique():
                position = float(positions[zone])
                flow = np.zeros(total_hours)
                share = float(
                    np.interp(
                        position,
                        survival.offset_km,
                        survival.share,
                        left=1.0,
                        right=survival.share.iloc[-1],
                    )
                )
                lag = position / params["speed_kmh"]
                whole, fraction = (int(np.floor(lag)), lag % 1)
                for date in days:
                    date = pd.Timestamp(date)
                    if date in train:
                        curve = curves[list(train).index(date)]
                        s = survivals[list(train).index(date)]
                        daily_share = float(
                            np.interp(
                                position, s.offset_km, s.share, left=1.0, right=s.share.iloc[-1]
                            )
                        )
                    else:
                        scale = fixed[fixed.date.eq(date)].volume_veh.sum() / train_total
                        curve, daily_share = (mean_curve * scale, share)
                    start = (date - pd.Timestamp(days[0])).days * 24
                    for hour, volume in enumerate(curve):
                        arrival = start + hour + whole
                        if arrival < total_hours:
                            flow[arrival] += volume * daily_share * (1 - fraction)
                        if arrival + 1 < total_hours:
                            flow[arrival + 1] += volume * daily_share * fraction
                predictions[zone] = flow
            fitted["legacy_train_only_veh"] = [
                predictions[z][h] for z, h in zip(fitted.conzone_id, fitted.time_index, strict=True)
            ]
            selected = fitted[fitted.split.eq("validation") & fitted.scoreable]
            for model in ["fitted_veh", "legacy_train_only_veh"]:
                error = selected[model] - selected.volume_veh
                positive = selected.volume_veh > 0
                scores.append(
                    dict(
                        period=month,
                        direction=direction,
                        model=model,
                        rows=len(selected),
                        rmse=np.sqrt(np.mean(error**2)),
                        mape_pct=(error[positive].abs() / selected.volume_veh[positive]).mean()
                        * 100,
                    )
                )
            for date, group in hourly[hourly.split.eq("validation")].groupby("date"):
                distances.append(
                    dict(
                        period=month,
                        direction=direction,
                        date=date,
                        new_long_100km_pct=100
                        * group.loc[group.distance_km.ge(100), "volume_veh"].sum()
                        / group.volume_veh.sum(),
                        legacy_long_100km_pct=100
                        * np.interp(
                            100.0,
                            survival.offset_km,
                            survival.share,
                            left=1.0,
                            right=survival.share.iloc[-1],
                        ),
                        new_total_trips=group.volume_veh.sum(),
                        legacy_total_trips=mean_curve.sum()
                        * fixed[fixed.date.eq(date)].volume_veh.sum()
                        / train_total,
                    )
                )
            fitted.to_csv(folder / "legacy_comparison_hourly.csv", index=False)
    out = root / "legacy_comparison"
    out.mkdir(exist_ok=True)
    pd.DataFrame(scores).to_csv(out / "metrics.csv", index=False)
    pd.DataFrame(distances).to_csv(out / "long_distance.csv", index=False)
    print(pd.DataFrame(scores).to_string(index=False))
    print(
        pd.DataFrame(distances)
        .groupby(["period", "direction"])[["new_long_100km_pct", "legacy_long_100km_pct"]]
        .mean()
        .to_string()
    )


def run_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--results-dir", type=Path, default=Path("data/processed/od_validation"))
    parser.add_argument(
        "--daily-constraint-mode", choices=["exact", "approximate"], default="exact"
    )
    parser.add_argument(
        "--reuse-results",
        action="store_true",
        help="Check and summarize existing runs without refitting",
    )
    parser.add_argument(
        "--export-dir", type=Path, help="Export checked parquet files with the #62 requested names"
    )
    parser.add_argument("--train-day-count", type=int, choices=[2, 4], default=2)
    parser.add_argument("--profile-mode", choices=["shared", "calendar"], default="shared")
    parser.add_argument("--external-scale-mode", choices=["shared", "calendar"], default="shared")
    parser.add_argument("--speed-kmh", type=float, default=90.0)
    args = parser.parse_args()
    if not np.isfinite(args.speed_kmh) or args.speed_kmh <= 0:
        parser.error("--speed-kmh must be finite and positive")
    scripts = Path(__file__).resolve().parent

    def run(name, *arguments):
        subprocess.run([sys.executable, str(scripts / name), *map(str, arguments)], check=True)

    for period, first in [("202602", "2026-02-13"), ("202603", "2026-03-06")]:
        from datetime import date, timedelta

        dates = [(date.fromisoformat(first) + timedelta(days=i)).isoformat() for i in range(10)]
        for direction in ["DOWN", "UP"]:
            folder = args.results_dir / f"expanded_{period}_{direction}"
            if not args.reuse_results:
                run(
                    "estimate_od_daily.py",
                    "hourly",
                    "--processed-dir",
                    args.processed_dir,
                    "--output-dir",
                    folder,
                    "--direction",
                    direction,
                    "--daily-constraint-mode",
                    args.daily_constraint_mode,
                    "--profile-mode",
                    args.profile_mode,
                    "--external-scale-mode",
                    args.external_scale_mode,
                    "--speed-kmh",
                    args.speed_kmh,
                    "--train-dates",
                    *dates[: args.train_day_count],
                    "--validation-dates",
                    *dates[args.train_day_count :],
                )
            params = json.loads((folder / "parameters.json").read_text(encoding="utf-8"))
            if params.get("daily_constraint_mode", "approximate") != args.daily_constraint_mode:
                raise ValueError(f"Stored constraint mode differs from requested mode: {folder}")
            if (
                params["train_dates"] != dates[: args.train_day_count]
                or params["validation_dates"] != dates[args.train_day_count :]
                or params["direction"] != direction
                or params.get("profile_mode", "shared") != args.profile_mode
                or params.get("external_scale_mode", "shared") != args.external_scale_mode
                or not np.isclose(params["speed_kmh"], args.speed_kmh, rtol=0, atol=1e-9)
            ):
                raise ValueError(f"Unexpected experiment split: {folder}")
    summarize_results(args.results_dir, args.processed_dir)
    compare_legacy(args.results_dir, args.processed_dir)
    if args.export_dir:
        copies = []
        for period in ["202602", "202603"]:
            for direction in ["DOWN", "UP"]:
                source = args.results_dir / f"expanded_{period}_{direction}"
                destination = args.export_dir / f"od_gyeongbu_{direction}_{period}.parquet"
                parquet = source / f"od_gyeongbu_{direction}_{period}_prototype.parquet"
                sidecar = destination.with_suffix(".parameters.json")
                if destination.exists() or sidecar.exists():
                    raise FileExistsError(f"Existing export will not be overwritten: {destination}")
                copies.append((parquet, destination, source / "parameters.json", sidecar))
        args.export_dir.mkdir(parents=True, exist_ok=True)
        for source, destination, params_path, sidecar in copies:
            shutil.copyfile(source, destination)
            params = json.loads(params_path.read_text(encoding="utf-8"))
            params["artifact_status"] = (
                "prototype; canonical filename does not imply validated OD truth"
            )
            params["parquet_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
            sidecar.write_text(json.dumps(params, indent=2), encoding="utf-8")
            print(f"Exported checked prototype: {destination}")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(processed, results, feasibility):
    nodes = pd.read_parquet(processed / "od_nodes_gyeongbu_supplemented.parquet")
    estimates = nodes[
        nodes.kind.eq("junction")
        & ~nodes.source.isin(["ic_raw_projection", "moct_link_attachment_projection"])
    ]
    moct = nodes[nodes.source.eq("moct_link_attachment_projection")]
    if not moct.empty:
        evidence = pd.read_csv(processed / "od_jc_coordinate_evidence.csv")
        selected_evidence = evidence[evidence.selected].copy()
        selected_evidence["node_id"] = "jc:moct:" + selected_evidence.moct_node_id.astype(str)
        evidence_mileposts = selected_evidence.set_index("node_id").milepost_km
        if (
            len(moct) != 4
            or len(selected_evidence) != 2
            or (not moct.node_id.isin(evidence_mileposts.index).all())
            or (not np.allclose(moct.milepost_km, moct.node_id.map(evidence_mileposts), atol=1e-07))
        ):
            raise ValueError("Completed JC coordinates do not match their raw-link evidence")
    coordinate_ok = estimates.empty
    feasibility = pd.read_csv(feasibility / "feasibility_summary.csv")
    expected_dates = pd.to_datetime(
        [*pd.date_range("2026-02-13", "2026-02-22"), *pd.date_range("2026-03-06", "2026-03-15")]
    )
    expected_conditions = {
        (d.strftime("%Y-%m-%d"), direction) for d in expected_dates for direction in ["DOWN", "UP"]
    }
    checked_conditions = set(zip(feasibility.date, feasibility.direction, strict=True))
    exact_ok = (
        checked_conditions == expected_conditions
        and len(feasibility) == 40
        and feasibility.exact_feasible.eq(True).all()
        and feasibility.min_max_abs_error_veh.le(1e-06).all()
    )
    tcs = pd.read_parquet(processed / "tcs_od_gyeongbu.parquet")
    tcs.date = pd.to_datetime(tcs.date).astype("datetime64[ns]")
    tcs["start_node"] = "tcs:" + tcs.start_office_code.astype(str)
    tcs["end_node"] = "tcs:" + tcs.end_office_code.astype(str)
    zones = pd.read_parquet(processed / "conzone_gyeongbu.parquet")
    observed = pd.read_csv(processed / "od_input_audit.csv", parse_dates=["date"])
    for stored in feasibility.itertuples():
        date = pd.Timestamp(stored.date)
        zone = zones[
            zones.in_corridor
            & zones.direction.eq(stored.direction)
            & zones.period.astype(str).eq(str(stored.period))
        ].sort_values("seq")
        day = observed[observed.date.eq(date) & observed.direction.eq(stored.direction)]
        day = day.set_index("conzone_id").reindex(zone.conzone_id)
        complete = day.missing_hours.eq(0) & day.vds_daily_veh.notna()
        zone = zone.loc[complete.to_numpy()]
        day = day.loc[complete]
        positions = (zone.offset_km_start.to_numpy() + zone.offset_km_end.to_numpy()) / 2
        matrix = candidate_matrix(nodes[nodes.direction.eq(stored.direction)], positions)
        fixed = np.zeros(len(positions))
        for pair in tcs[tcs.date.eq(date) & tcs.direction.eq(stored.direction)].itertuples():
            fixed += (
                (pair.start_offset_km <= positions) & (positions < pair.end_offset_km)
            ) * pair.volume_veh
        actual_feasible, minimum, _ = solve_constraints(
            matrix, day.vds_daily_veh.to_numpy() - fixed
        )
        if (
            actual_feasible != stored.exact_feasible
            or len(day) != stored.complete_zones
            or matrix.shape[1] != stored.candidate_pairs
            or (not np.isclose(minimum, stored.min_max_abs_error_veh, atol=1e-06, rtol=0))
        ):
            raise ValueError(
                f"Stored feasibility evidence is stale: {stored.date}/{stored.direction}"
            )
    artifacts, measurements = ([], [])
    validation_day_count = 0
    for period in ["202602", "202603"]:
        for direction in ["DOWN", "UP"]:
            path = processed / f"od_gyeongbu_{direction}_{period}.parquet"
            params_path = path.with_suffix(".parameters.json")
            params = json.loads(params_path.read_text(encoding="utf-8"))
            current_inputs = {
                name: digest(processed / name)
                for name in [
                    "od_nodes_gyeongbu_supplemented.parquet",
                    "conzone_gyeongbu.parquet",
                    "tcs_od_gyeongbu.parquet",
                    "traffic_gyeongbu.parquet",
                    "od_input_audit.csv",
                ]
            }
            if params.get("input_sha256") != current_inputs:
                raise ValueError(f"Export inputs do not match current model inputs: {path}")
            parquet_hash = digest(path)
            if params["parquet_sha256"] != parquet_hash:
                raise ValueError(f"Export fingerprint mismatch: {path}")
            hourly = pd.read_parquet(path)
            hourly.date = pd.to_datetime(hourly.date).astype("datetime64[ns]")
            if (
                not np.isfinite(hourly.volume_veh).all()
                or hourly.volume_veh.lt(0).any()
                or (not hourly.direction.eq(direction).all())
                or (not hourly.date.dt.strftime("%Y%m").eq(period).all())
                or (not hourly.departure_hour.between(0, 23).all())
            ):
                raise ValueError(f"Invalid hourly export: {path}")
            keys = ["date", "start_node", "end_node"]
            measured = hourly[hourly.source.eq("measured_tcs_daily")]
            actual = measured.groupby(keys).volume_veh.sum()
            expected = (
                tcs[tcs.direction.eq(direction) & tcs.date.isin(hourly.date.unique())]
                .groupby(keys)
                .volume_veh.sum()
            )
            if not actual.index.equals(expected.index) or not np.allclose(
                actual, expected, atol=1e-07, rtol=0
            ):
                raise ValueError(f"Measured TCS pair sums not preserved: {path}")
            train = set(params["train_dates"])
            validation = set(params["validation_dates"])
            if (
                train & validation
                or max(train) >= min(validation)
                or len(train) not in (2, 4)
                or len(train) + len(validation) != 10
            ):
                raise ValueError(f"Invalid calibration/validation split: {path}")
            validation_day_count += len(validation)
            fit = pd.read_csv(results / f"expanded_{period}_{direction}" / "hourly_fit.csv")
            day = fit[fit.split.eq("validation") & fit.scoreable]
            error = day.fitted_veh - day.volume_veh
            positive = day.volume_veh.gt(0)
            rmse = float(np.sqrt(np.mean(error**2)))
            mape = float((error[positive].abs() / day.volume_veh[positive]).mean() * 100)
            metrics = pd.read_csv(results / f"expanded_{period}_{direction}" / "metrics.csv")
            saved = metrics[metrics.split.eq("validation") & metrics.model.eq("fitted_veh")].iloc[0]
            if not np.isclose(rmse, saved.rmse) or not np.isclose(mape, saved.mape_pct):
                raise ValueError("Validation metrics do not match underlying rows")
            artifacts.append(
                dict(
                    path=str(path),
                    sha256=parquet_hash,
                    rows=len(hourly),
                    measured_pairs=len(actual),
                    max_tcs_sum_error=float((actual - expected).abs().max()),
                    daily_constraint_mode=params.get("daily_constraint_mode", "approximate"),
                )
            )
            measurements.append(dict(period=period, direction=direction, rmse=rmse, mape_pct=mape))
    legacy = pd.read_csv(results / "legacy_comparison" / "metrics.csv")
    distances = pd.read_csv(results / "legacy_comparison" / "long_distance.csv")
    comparison_ok = (
        len(legacy) == 8
        and set(legacy.model) == {"fitted_veh", "legacy_train_only_veh"}
        and (len(distances) == validation_day_count)
        and np.isfinite(legacy.rmse).all()
    )
    checks = [
        dict(
            condition="coordinate_based_nodes",
            passed=bool(coordinate_ok),
            detail=f"{len(estimates)} directional JC rows remain boundary estimates",
        ),
        dict(
            condition="exact_daily_constraints",
            passed=bool(exact_ok),
            detail=f"{int(feasibility.exact_feasible.eq(True).sum())}/{len(feasibility)} exact feasible conditions",
        ),
        dict(
            condition="hourly_delay_and_pair_conservation",
            passed=True,
            detail="Hourly implementation tested separately; exported TCS sums verified",
        ),
        dict(
            condition="requested_output_files",
            passed=True,
            detail="Four parquet files and fingerprints verified",
        ),
        dict(
            condition="separate_calibration_validation_and_metrics",
            passed=True,
            detail="Disjoint chronological calibration/validation dates; metrics recomputed",
        ),
        dict(
            condition="legacy_error_and_long_distance_comparison",
            passed=bool(comparison_ok),
            detail=f"Four period/direction comparisons and {validation_day_count} daily distance rows",
        ),
    ]
    return dict(
        issue_url="https://github.com/yoon3660/Capstone_2026/issues/62",
        data_conditions_passed=all(item["passed"] for item in checks),
        note="Four-node recovery and CI test execution are separately recorded in od_design.md. Gate never overrides failed data conditions based on passing tests.",
        checks=checks,
        artifacts=artifacts,
        validation_metrics=measurements,
        input_sha256={
            name: digest(processed / name)
            for name in [
                "od_nodes_gyeongbu_supplemented.parquet",
                "conzone_gyeongbu.parquet",
                "tcs_od_gyeongbu.parquet",
                "traffic_gyeongbu.parquet",
                "od_input_audit.csv",
            ]
        },
    )


def check_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--feasibility-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = check(args.processed_dir, args.results_dir, args.feasibility_dir)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    for item in result["checks"]:
        print(f"{('PASS' if item['passed'] else 'FAIL')} {item['condition']}: {item['detail']}")
    raise SystemExit(0 if result["data_conditions_passed"] else 1)


def feasibility_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.processed_dir
    nodes = pd.read_parquet(source / "od_nodes_gyeongbu_supplemented.parquet")
    zones = pd.read_parquet(source / "conzone_gyeongbu.parquet")
    tcs = pd.read_parquet(source / "tcs_od_gyeongbu.parquet")
    observations = pd.read_csv(source / "od_input_audit.csv", parse_dates=["date"])
    summaries, details = ([], [])
    for (date, direction), day in observations.groupby(["date", "direction"]):
        period = date.strftime("%Y%m")
        selected = zones[
            zones.in_corridor & zones.direction.eq(direction) & zones.period.astype(str).eq(period)
        ].sort_values("seq")
        day = day.set_index("conzone_id").reindex(selected.conzone_id)
        complete = day.missing_hours.eq(0) & day.vds_daily_veh.notna()
        selected = selected.loc[complete.to_numpy()].reset_index(drop=True)
        day = day.loc[complete].reset_index()
        positions = (selected.offset_km_start.to_numpy() + selected.offset_km_end.to_numpy()) / 2
        matrix = candidate_matrix(nodes[nodes.direction.eq(direction)], positions)
        fixed = np.zeros(len(positions))
        for row in tcs[tcs.date.eq(date) & tcs.direction.eq(direction)].itertuples():
            fixed += (
                (row.start_offset_km <= positions) & (positions < row.end_offset_km)
            ) * row.volume_veh
        target = day.vds_daily_veh.to_numpy(dtype=float)
        feasible, minimum, error = solve_constraints(matrix, target - fixed)
        summaries.append(
            dict(
                date=date,
                direction=direction,
                period=period,
                complete_zones=len(day),
                candidate_pairs=matrix.shape[1],
                exact_feasible=feasible,
                min_max_abs_error_veh=minimum,
            )
        )
        day["date"], day["direction"] = (date, direction)
        day["tcs_fixed_veh"] = fixed
        day["minimax_fitted_veh"] = target + error
        day["minimax_error_veh"] = error
        details.append(day)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(summaries)
    summary.to_csv(args.output_dir / "feasibility_summary.csv", index=False)
    pd.concat(details, ignore_index=True).to_csv(
        args.output_dir / "minimax_residuals.csv", index=False
    )
    print(summary.to_string(index=False))
    print(
        "Feasibility applies only to current daily midpoint observations and movement assumptions."
    )


def main():
    actions = {"run": run_main, "feasibility": feasibility_main, "check": check_main}
    command = sys.argv.pop(1) if len(sys.argv) > 1 and sys.argv[1] in actions else "run"
    actions[command]()


if __name__ == "__main__":
    main()
