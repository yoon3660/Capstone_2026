"""Daily and hourly OD estimation, input audit and movement constraints."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd
from scipy.linalg import qr
from scipy.optimize import LinearConstraint, linprog, minimize
from scipy.sparse import coo_matrix, csr_matrix, hstack, vstack
from scipy.special import softmax

from evdt.paths import DATA_PROCESSED_DIR

#: 실측 TCS 행이 갖고 있는 신원 열. 추정 행에는 해당하는 값이 없으므로 비어 있다.
#: **보정일과 검증일이 같은 열을 갖도록** 양쪽 경로에서 똑같이 들고 간다 (#62 hotfix).
TCS_IDENTITY = [
    "period",
    "start_office_code",
    "start_office",
    "end_office_code",
    "end_office",
    "start_milepost_km",
    "end_milepost_km",
]


def apply_rules(nodes: pd.DataFrame) -> pd.DataFrame:
    result = nodes.copy()
    result["allow_estimated_entry"] = True
    result["allow_estimated_exit"] = True
    result["movement_status"] = "assumed_bidirectional_not_network_verified"
    result["movement_basis"] = "prototype_default_requires_review"
    for index, row in result.iterrows():
        name = str(row.get("name", ""))
        if row["kind"] == "endpoint":
            same_direction = result[result["direction"] == row["direction"]]
            endpoints = same_direction[same_direction["kind"] == "endpoint"]
            result.at[index, "allow_estimated_entry"] = (
                row["offset_km"] == endpoints["offset_km"].min()
            )
            result.at[index, "allow_estimated_exit"] = (
                row["offset_km"] == endpoints["offset_km"].max()
            )
            result.at[index, "movement_status"] = "corridor_boundary"
            result.at[index, "movement_basis"] = "directional_offset_order"
        elif row["kind"] == "office" and name == "상서":
            allowed = row["direction"] == "UP"
            result.at[index, "allow_estimated_entry"] = allowed
            result.at[index, "allow_estimated_exit"] = allowed
            result.at[index, "movement_status"] = "project_direction_restriction"
            result.at[index, "movement_basis"] = "T61_check_tcs_od_pairs_DIRECTION_LIMITED_OFFICES"
        elif row["kind"] == "office" and "금강휴게소" in name:
            result.at[index, "allow_estimated_entry"] = False
            result.at[index, "allow_estimated_exit"] = False
            result.at[index, "movement_status"] = "special_tcs_no_external_extension"
            result.at[index, "movement_basis"] = "T61_SPECIAL_OD_MARKERS_measured_records_preserved"
    return result


class InfeasibleDailyConstraints(ValueError):
    def __init__(self, minimum_error):
        self.minimum_error = float(minimum_error)
        super().__init__(
            f"Exact daily section constraints are infeasible; minimum possible maximum absolute error is {self.minimum_error:.6f} veh/day"
        )


def nearest_prior_exact(matrix, residual, prior):
    """Minimize mean squared relative prior change under A x = residual, x >= 0."""
    matrix = csr_matrix(matrix)
    residual = np.asarray(residual, dtype=float)
    prior = np.asarray(prior, dtype=float)
    if (
        matrix.shape != (len(residual), len(prior))
        or not len(prior)
        or (not len(residual))
        or (not np.isfinite(residual).all())
        or (not np.isfinite(prior).all())
        or (prior <= 0).any()
    ):
        raise ValueError("Finite observations and positive compatible prior required")
    feasible = linprog(
        np.zeros(len(prior)), A_eq=matrix, b_eq=residual, bounds=(0, None), method="highs"
    )
    if feasible.status == 2:
        minus_one = csr_matrix(-np.ones((len(residual), 1)))
        constraints = vstack([hstack([matrix, minus_one]), hstack([-matrix, minus_one])])
        closest = linprog(
            np.r_[np.zeros(len(prior)), 1],
            A_ub=constraints,
            b_ub=np.r_[residual, -residual],
            bounds=(0, None),
            method="highs",
        )
        if not closest.success:
            raise RuntimeError(f"Minimum residual solver failed: {closest.message}")
        raise InfeasibleDailyConstraints(closest.fun)
    if not feasible.success:
        raise RuntimeError(f"Exact feasibility solver failed: {feasible.message}")
    scaled = matrix.toarray() * prior
    _, triangular, indices = qr(scaled.T, pivoting=True, mode="economic")
    tolerance = np.finfo(float).eps * max(scaled.shape) * np.linalg.norm(scaled, 2)
    rank = int((np.abs(np.diag(triangular)) > tolerance).sum())
    selected = indices[:rank]

    def objective(z):
        return (np.mean((z - 1) ** 2), 2 * (z - 1) / len(z))

    constraints = []
    if rank:
        row_scale = np.maximum(np.abs(residual[selected]), 1.0)
        constraints = [
            LinearConstraint(
                scaled[selected] / row_scale[:, None],
                residual[selected] / row_scale,
                residual[selected] / row_scale,
            )
        ]
    solved = minimize(
        objective,
        feasible.x / prior,
        jac=True,
        method="SLSQP",
        bounds=[(0, None)] * len(prior),
        constraints=constraints,
        options={"ftol": 1e-12, "maxiter": 2000},
    )
    if not solved.success:
        raise RuntimeError(f"Nearest-prior solver failed: {solved.message}")
    values = prior * solved.x
    if (values < -1e-08).any() or np.max(np.abs(matrix @ values - residual)) > 1e-06:
        raise RuntimeError("Exact constrained result failed independent validation")
    return values


def audit(traffic, conzones, od):
    keys = ["direction", "conzone_id"]
    spatial = ["offset_km_start", "offset_km_end"]
    inside = conzones.loc[conzones["in_corridor"]].copy()
    changes = inside.groupby(keys)[spatial].nunique(dropna=False)
    if (changes > 1).any().any():
        raise ValueError("Conzone positions differ between periods")
    inside = inside.drop_duplicates(keys)
    if inside[spatial].isna().any().any():
        raise ValueError("Missing internal conzone positions")
    if (inside["offset_km_end"] <= inside["offset_km_start"]).any():
        raise ValueError("Invalid conzone boundary order")
    t = traffic.merge(inside[keys], on=keys, how="inner", validate="many_to_one")
    t["date"] = pd.to_datetime(t["date"]).dt.normalize()
    od = od.copy()
    od["date"] = pd.to_datetime(od["date"]).dt.normalize()
    if t.duplicated(["date", "hour", *keys]).any():
        raise ValueError("Duplicate VDS date/hour/conzone")
    if not t["hour"].isin(range(24)).all():
        raise ValueError("Invalid hour")
    if t["volume_veh"].lt(0).any() or od["volume_veh"].lt(0).any():
        raise ValueError("Negative volume")
    dates = sorted(od["date"].unique())
    rows = []
    for date in dates:
        for direction in ("UP", "DOWN"):
            daily_t = t[(t["date"] == date) & (t["direction"] == direction)]
            daily_od = od[(od["date"] == date) & (od["direction"] == direction)]
            if daily_od.empty:
                raise ValueError(f"Missing TCS date/direction: {date}, {direction}")
            for zone in inside.loc[inside["direction"] == direction].to_dict("records"):
                observed = daily_t[daily_t["conzone_id"] == zone["conzone_id"]]
                valid = observed["volume_veh"].notna()
                hours = int(observed.loc[valid, "hour"].nunique())
                vds = float(observed["volume_veh"].sum()) if hours == 24 else np.nan
                midpoint = (zone["offset_km_start"] + zone["offset_km_end"]) / 2
                crossing = daily_od[
                    (daily_od["start_offset_km"] <= midpoint)
                    & (daily_od["end_offset_km"] > midpoint)
                ]["volume_veh"].sum()
                rows.append(
                    {
                        "date": date,
                        "direction": direction,
                        "conzone_id": zone["conzone_id"],
                        "conzone_name": zone["name"],
                        "valid_hours": hours,
                        "missing_hours": 24 - hours,
                        "vds_daily_veh": vds,
                        "tcs_midpoint_crossing_veh": int(crossing),
                        "tcs_excess_veh": max(float(crossing) - vds, 0) if hours == 24 else np.nan,
                        "comparison_basis": "same_day_midpoint_screen_no_travel_time_shift",
                    }
                )
    return pd.DataFrame(rows)


def audit_main():
    result = audit(
        pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet"),
        pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet"),
        pd.read_parquet(DATA_PROCESSED_DIR / "tcs_od_gyeongbu.parquet"),
    )
    path = DATA_PROCESSED_DIR / "od_input_audit.csv"
    result.to_csv(path, index=False)
    print("Internal corridor completeness:")
    print(
        result.groupby("direction")
        .agg(
            zone_days=("valid_hours", "size"),
            missing_hours=("missing_hours", "sum"),
            complete_zone_days=("valid_hours", lambda s: s.eq(24).sum()),
        )
        .to_string()
    )
    excess = result[result["tcs_excess_veh"] > 0].sort_values("tcs_excess_veh", ascending=False)
    print(f"\nTCS midpoint crossings above complete-day VDS: {len(excess)}")
    print(excess.head(20).to_string(index=False))
    print(f"\nSaved: {path}")
    print("Screen only: daily boundary timing, station offsets and VDS semantics need review.")


def candidate_matrix(nodes, positions):
    nodes = apply_rules(nodes).sort_values(["offset_km", "node_id"])
    records = nodes.to_dict("records")
    columns = []
    for i, start in enumerate(records):
        for end in records[i + 1 :]:
            if not start["allow_estimated_entry"] or not end["allow_estimated_exit"]:
                continue
            if end["offset_km"] <= start["offset_km"]:
                continue
            if start["kind"] == "office" and end["kind"] == "office":
                continue
            crossed = (start["offset_km"] <= positions) & (positions < end["offset_km"])
            if crossed.any():
                columns.append(crossed.astype(float))
    return csr_matrix(np.column_stack(columns)) if columns else csr_matrix((len(positions), 0))


def solve_constraints(matrix, residual):
    """Return exact status plus a minimum-L-infinity nonnegative approximation."""
    residual = np.asarray(residual, dtype=float)
    if not len(residual) or not np.isfinite(residual).all():
        raise ValueError("Finite nonempty observations required")
    exact = (
        linprog(
            np.zeros(matrix.shape[1]), A_eq=matrix, b_eq=residual, bounds=(0, None), method="highs"
        )
        if matrix.shape[1]
        else None
    )
    if exact is not None and exact.status not in (0, 2):
        raise RuntimeError(f"Exact feasibility solver failed: {exact.message}")
    if exact is not None and exact.success:
        error = np.asarray(matrix @ exact.x).ravel() - residual
        if np.max(np.abs(error)) > 1e-06:
            raise RuntimeError("Exact solution failed independent residual check")
        return (True, 0.0, error)
    minus_one = csr_matrix(-np.ones((len(residual), 1)))
    inequalities = vstack([hstack([matrix, minus_one]), hstack([-matrix, minus_one])])
    objective = np.r_[np.zeros(matrix.shape[1]), 1.0]
    closest = linprog(
        objective,
        A_ub=inequalities,
        b_ub=np.r_[residual, -residual],
        bounds=(0, None),
        method="highs",
    )
    if not closest.success:
        raise RuntimeError(f"Minimum residual solver failed: {closest.message}")
    error = np.asarray(matrix @ closest.x[:-1]).ravel() - residual
    if np.max(np.abs(error)) > closest.x[-1] + 1e-06:
        raise RuntimeError("Minimum residual solution failed independent check")
    feasible = np.max(np.abs(error)) <= 1e-06
    return (feasible, float(closest.x[-1]), error)


def fit(
    nodes, zones, tcs, observations, strength, *, allowed_pairs=None, constraint_mode="approximate"
):
    """Fit daily volumes; optional known paths further restrict movement candidates."""
    if constraint_mode not in {"exact", "approximate"}:
        raise ValueError("Unknown daily constraint mode")
    if strength <= 0:
        raise ValueError("Prior strength must be positive")
    nodes = apply_rules(nodes).sort_values(["offset_km", "node_id"]).reset_index(drop=True)
    mids = (zones["offset_km_start"].to_numpy() + zones["offset_km_end"].to_numpy()) / 2
    target = observations["vds_daily_veh"].to_numpy(dtype=float)
    fixed = np.zeros(len(zones))
    for row in tcs.to_dict("records"):
        fixed += ((row["start_offset_km"] <= mids) & (mids < row["end_offset_km"])) * row[
            "volume_veh"
        ]
    residual = target - fixed
    if constraint_mode == "approximate" and (residual < -1e-06).any():
        raise ValueError("Fixed TCS exceeds complete-day VDS; review constraints")
    pairs, rr, cc = ([], [], [])
    records = nodes.to_dict("records")
    for i, start in enumerate(records):
        for end in records[i + 1 :]:
            if not start["allow_estimated_entry"] or not end["allow_estimated_exit"]:
                continue
            if (
                allowed_pairs is not None
                and (start["node_id"], end["node_id"]) not in allowed_pairs
            ):
                continue
            distance = end["offset_km"] - start["offset_km"]
            if distance <= 0 or (start["kind"] == "office" and end["kind"] == "office"):
                continue
            crossed = np.flatnonzero((start["offset_km"] <= mids) & (mids < end["offset_km"]))
            if not len(crossed):
                continue
            col = len(pairs)
            rr.extend(crossed.tolist())
            cc.extend([col] * len(crossed))
            pairs.append(
                {
                    "start_node": start["node_id"],
                    "end_node": end["node_id"],
                    "start_offset_km": start["offset_km"],
                    "end_offset_km": end["offset_km"],
                    "distance_km": distance,
                }
            )
    if not pairs:
        raise ValueError("No observable external OD candidates")
    matrix = csr_matrix((np.ones(len(rr)), (rr, cc)), shape=(len(zones), len(pairs)))
    distances = np.array([p["distance_km"] for p in pairs])
    gravity = (distances + 10.0) ** (-1.5)
    denominator = matrix @ gravity
    if ((denominator <= 0) & (residual > 0)).any():
        raise ValueError("Residual VDS flow has no candidate paths")
    scale = np.median(residual[denominator > 0] / denominator[denominator > 0])
    prior = gravity * max(float(scale), 1e-06)
    weights = 1.0 / np.maximum(target, 100.0)

    def objective(z):
        error = (matrix @ (prior * z) - residual) * weights
        deviation = z - 1
        value = np.mean(error**2) + strength * np.mean(deviation**2)
        grad = 2 * prior * (matrix.T @ (error * weights)) / len(target)
        grad += 2 * strength * deviation / len(z)
        return (value, grad)

    if constraint_mode == "exact":
        values = nearest_prior_exact(matrix, residual, prior)
    else:
        solved = minimize(
            objective,
            np.ones(len(prior)),
            jac=True,
            method="L-BFGS-B",
            bounds=[(0, None)] * len(prior),
            options={"maxiter": 2000, "ftol": 1e-12, "gtol": 1e-08},
        )
        if not solved.success:
            raise RuntimeError(f"OD solver did not converge: {solved.message}")
        values = prior * solved.x
    estimated = pd.DataFrame(pairs)
    estimated["volume_veh"] = values
    estimated["prior_volume_veh"] = prior
    estimated["source"] = "estimated_external_daily"
    fixed_od = tcs.copy()
    fixed_od["start_node"] = "tcs:" + fixed_od["start_office_code"].astype(str)
    fixed_od["end_node"] = "tcs:" + fixed_od["end_office_code"].astype(str)
    fixed_od["prior_volume_veh"] = fixed_od["volume_veh"]
    fixed_od["source"] = "measured_tcs_daily"
    columns = [
        "start_node",
        "end_node",
        "start_offset_km",
        "end_offset_km",
        "distance_km",
        "volume_veh",
        "prior_volume_veh",
        "source",
    ]
    # TCS 행의 신원 열은 같이 들고 간다. 보정일은 이 함수를 거치고 검증일은
    # measured_od() 를 거치는데, 여기서 떨어뜨리면 **보정일에서만 비는** 열이 생긴다.
    # 그 결과 period 로 거르는 쪽에서 "적합한 날은 버리고 예측한 날만 남는" 일이
    # 벌어진다 — 에러 없이.
    result = (
        estimated.reindex(columns=columns + TCS_IDENTITY).copy()
        if fixed_od.empty
        else pd.concat(
            [
                fixed_od.reindex(columns=columns + TCS_IDENTITY),
                estimated.reindex(columns=columns + TCS_IDENTITY),
            ],
            ignore_index=True,
        )
    )
    diagnostics = observations.copy()
    diagnostics["tcs_fixed_veh"] = fixed
    diagnostics["fitted_daily_veh"] = fixed + matrix @ estimated["volume_veh"].to_numpy()
    diagnostics["error_veh"] = diagnostics["fitted_daily_veh"] - target
    diagnostics["error_pct"] = np.where(
        target > 0, diagnostics["error_veh"] / np.maximum(target, 1) * 100, np.nan
    )
    return (result, diagnostics)


def fit_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--direction", choices=["UP", "DOWN"], required=True)
    parser.add_argument("--prior-strength", type=float, default=0.001)
    parser.add_argument("--constraint-mode", choices=["exact", "approximate"], default="exact")
    parser.add_argument(
        "--nodes-file", type=Path, default=DATA_PROCESSED_DIR / "od_nodes_gyeongbu.parquet"
    )
    args = parser.parse_args()
    date = pd.Timestamp(args.date).normalize()
    nodes = pd.read_parquet(args.nodes_file)
    zones = pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet")
    tcs = pd.read_parquet(DATA_PROCESSED_DIR / "tcs_od_gyeongbu.parquet")
    obs = pd.read_csv(DATA_PROCESSED_DIR / "od_input_audit.csv", parse_dates=["date"])
    obs = obs[
        (obs["date"] == date) & (obs["direction"] == args.direction) & (obs["valid_hours"] == 24)
    ]
    if obs.empty:
        raise ValueError("No complete-day VDS observations")
    zones = zones[
        zones["in_corridor"]
        & (zones["direction"] == args.direction)
        & zones["period"].astype(str).eq(date.strftime("%Y%m"))
    ].drop_duplicates("conzone_id")
    zones = obs[["conzone_id"]].merge(zones, on="conzone_id", validate="one_to_one")
    tcs = tcs[(tcs["date"] == date) & (tcs["direction"] == args.direction)]
    if tcs.empty:
        raise ValueError("Missing TCS observations")
    result, diagnostics = fit(
        nodes[nodes["direction"] == args.direction],
        zones,
        tcs,
        obs,
        args.prior_strength,
        constraint_mode=args.constraint_mode,
    )
    result["date"] = date
    result["direction"] = args.direction
    stem = f"od_daily_prototype_{args.direction}_{date:%Y%m%d}"
    apply_rules(nodes[nodes["direction"] == args.direction]).to_csv(
        DATA_PROCESSED_DIR / f"{stem}_movement_rules.csv", index=False
    )
    result.to_parquet(DATA_PROCESSED_DIR / f"{stem}.parquet", index=False)
    diagnostics.to_csv(DATA_PROCESSED_DIR / f"{stem}_fit.csv", index=False)
    metadata = {
        "date": args.date,
        "direction": args.direction,
        "prior_strength": args.prior_strength,
        "nodes_file": str(args.nodes_file),
        "constraint_mode": args.constraint_mode,
        "gravity_distance_shift_km": 10,
        "gravity_exponent": 1.5,
        "prior_status": "assumption_not_empirically_calibrated",
        "constraints": "nonnegative external OD, fixed measured TCS",
        "limitations": [
            "midpoint path approximation",
            "no midnight travel-time shift",
            "only T61 project restrictions applied; other IC/JC movements assumed",
            "special measured TCS records preserved; not independently interpretable",
            "fit error is not holdout validation",
        ],
    }
    (DATA_PROCESSED_DIR / f"{stem}_params.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    print(result.groupby("source")["volume_veh"].agg(["size", "sum"]).to_string())
    print("Complete VDS zone-days:", len(obs))
    print("Fit RMSE (veh/day):", float(np.sqrt(np.mean(diagnostics["error_veh"] ** 2))))
    print("Fit MAPE (%):", float(diagnostics["error_pct"].abs().mean()))
    print("Prototype only; hourly estimation and holdout validation remain.")


def passage_operator(daily_od, positions, origins, start_date, n_days, speed_kmh):
    """Map origin x departure-hour probabilities to zone x calendar-hour flow.

    A uniform within-hour departure rate is split across adjacent arrival bins.
    Rows include a tail after the last departure day; no circular midnight wrap.
    """
    if not np.isfinite(speed_kmh) or speed_kmh <= 0:
        raise ValueError("Speed must be finite and positive")
    positions = np.asarray(positions, dtype=float)
    if not np.isfinite(positions).all():
        raise ValueError("Invalid observation positions")
    lookup = {name: i for i, name in enumerate(origins)}
    max_distance = float(daily_od.distance_km.max())
    tail = int(np.ceil(max_distance / speed_kmh)) + 1
    hours = n_days * 24 + tail
    rr, cc, values = ([], [], [])
    for row in daily_od.to_dict("records"):
        day = (pd.Timestamp(row["date"]) - pd.Timestamp(start_date)).days
        if not 0 <= day < n_days:
            raise ValueError("Departure date outside window")
        if row["end_offset_km"] <= row["start_offset_km"] or row["volume_veh"] < 0:
            raise ValueError("Invalid OD path or volume")
        crossed = np.flatnonzero(
            (row["start_offset_km"] <= positions) & (positions < row["end_offset_km"])
        )
        origin = lookup[row["start_node"]]
        for zone in crossed:
            lag = (positions[zone] - row["start_offset_km"]) / speed_kmh
            whole = int(np.floor(lag))
            fraction = lag - whole
            for hour in range(24):
                arrival = day * 24 + hour + whole
                rr.append(zone * hours + arrival)
                cc.append(origin * 24 + hour)
                values.append(row["volume_veh"] * (1 - fraction))
                if fraction > 0:
                    rr.append(zone * hours + arrival + 1)
                    cc.append(origin * 24 + hour)
                    values.append(row["volume_veh"] * fraction)
    return (
        coo_matrix((values, (rr, cc)), shape=(len(positions) * hours, len(origins) * 24)).tocsr(),
        hours,
    )


def learn_profiles(
    matrix, target, training_mask, prior, strength=0.01, solver_gtol=1e-08, solver_ftol=1e-12
):
    target = np.asarray(target, dtype=float)
    mask = np.asarray(training_mask, dtype=bool) & np.isfinite(target)
    if not mask.any() or strength <= 0:
        raise ValueError("Missing training observations or invalid prior strength")
    prior = np.asarray(prior, dtype=float)
    if prior.ndim != 2 or prior.shape[1] != 24 or (prior <= 0).any():
        raise ValueError("Strictly positive origin x 24 prior required")
    prior = prior / prior.sum(axis=1, keepdims=True)
    observed = matrix[mask]
    y = target[mask]
    weights = 1 / np.maximum(y, 100.0)

    def objective(logits):
        probs = softmax(logits.reshape(prior.shape), axis=1)
        error = (observed @ probs.ravel() - y) * weights
        difference = probs - prior
        loss = np.mean(error**2) + strength * np.mean((difference * 24) ** 2)
        grad_p = (2 * (observed.T @ (error * weights)) / len(y)).reshape(prior.shape)
        grad_p += 2 * strength * difference * 24**2 / prior.size
        grad = probs * (grad_p - (grad_p * probs).sum(axis=1, keepdims=True))
        return (loss, grad.ravel())

    solved = minimize(
        objective,
        np.log(prior).ravel(),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 3000, "ftol": solver_ftol, "gtol": solver_gtol},
    )
    if not solved.success:
        raise RuntimeError(f"Hourly solver failed: {solved.message}")
    return softmax(solved.x.reshape(prior.shape), axis=1)


def allocate(daily_od, origins, profiles):
    lookup = {name: i for i, name in enumerate(origins)}
    if (profiles < 0).any() or not np.allclose(profiles.sum(axis=1), 1):
        raise ValueError("Departure profiles must be nonnegative and sum to one")
    result = daily_od.loc[daily_od.index.repeat(24)].reset_index(drop=True)
    result["departure_hour"] = np.tile(np.arange(24), len(daily_od))
    probabilities = np.vstack([profiles[lookup[name]] for name in daily_od.start_node])
    result["daily_volume_veh"] = result.volume_veh
    result["volume_veh"] = np.repeat(daily_od.volume_veh.to_numpy(), 24) * probabilities.ravel()
    return result


def measured_od(tcs):
    result = tcs.copy()
    result["start_node"] = "tcs:" + result.start_office_code.astype(str)
    result["end_node"] = "tcs:" + result.end_office_code.astype(str)
    result["source"] = "measured_tcs_daily"
    return result


def day_type(date):
    """Calendar covariate known before prediction; Seollal 2026 is Feb 16–18."""
    date = pd.Timestamp(date).normalize()
    if date in pd.date_range("2026-02-16", "2026-02-18"):
        return "holiday"
    return "weekend" if date.dayofweek >= 5 else "weekday"


def profile_keys(daily_od, train_dates):
    """Use unseen date types' shared fallback, without inspecting validation VDS."""
    trained = sorted({day_type(date) for date in train_dates})
    kinds = ["shared", *trained]
    origins = sorted(daily_od.start_node.unique())
    keyed = daily_od.copy()
    keyed["profile_type"] = [
        day_type(d) if day_type(d) in trained else "shared" for d in keyed.date
    ]
    keyed["start_node"] = keyed.start_node + "|" + keyed.profile_type
    keys = [origin + "|" + kind for kind in kinds for origin in origins]
    return keyed, keys, kinds


def allocate_by_calendar(daily_od, origins, profiles, kinds, train_dates):
    """Retain original node IDs and each measured daily pair sum."""
    trained = {day_type(date) for date in train_dates}
    frames = []
    for date, rows in daily_od.groupby("date", sort=True):
        kind = day_type(date) if day_type(date) in trained else "shared"
        block = kinds.index(kind) * len(origins)
        frames.append(allocate(rows, origins, profiles[block : block + len(origins)]))
    return pd.concat(frames, ignore_index=True)


def predict_external(
    training_frames,
    training_tcs_totals,
    train_dates,
    prediction_date,
    prediction_tcs_total,
    mode="shared",
):
    """Estimate external OD with training-only daily OD/TCS ratios by date type."""
    if mode not in {"shared", "calendar"}:
        raise ValueError("Unknown external scale mode")
    if (
        not training_frames
        or len(training_frames) != len(train_dates)
        or len(training_frames) != len(training_tcs_totals)
    ):
        raise ValueError("Aligned nonempty calibration inputs required")
    totals = np.asarray(training_tcs_totals, dtype=float)
    if not np.isfinite(totals).all() or (totals <= 0).any():
        raise ValueError("Positive training TCS totals required")
    if not np.isfinite(prediction_tcs_total) or prediction_tcs_total < 0:
        raise ValueError("Finite nonnegative prediction TCS total required")
    selected = list(range(len(train_dates)))
    if mode == "calendar":
        matches = [
            i for i, date in enumerate(train_dates) if day_type(date) == day_type(prediction_date)
        ]
        if matches:
            selected = matches
    keys = ["start_node", "end_node", "start_offset_km", "end_offset_km", "distance_km"]
    predicted = (
        pd.concat([training_frames[i] for i in selected])
        .groupby(keys, dropna=False)
        .volume_veh.sum()
        .reset_index()
    )
    predicted.volume_veh *= float(prediction_tcs_total) / totals[selected].sum()
    if not np.isfinite(predicted.volume_veh).all() or predicted.volume_veh.lt(0).any():
        raise ValueError("Invalid external OD estimate")
    predicted["source"] = "predicted_external_daily_from_training"
    return predicted


def hourly_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--direction", choices=["UP", "DOWN"], required=True)
    parser.add_argument("--train-dates", nargs="+", default=["2026-02-13", "2026-02-14"])
    parser.add_argument("--validation-dates", nargs="+", default=["2026-02-15"])
    parser.add_argument("--speed-kmh", type=float, default=90.0)
    parser.add_argument("--daily-prior-strength", type=float, default=0.001)
    parser.add_argument(
        "--daily-constraint-mode", choices=["exact", "approximate"], default="exact"
    )
    parser.add_argument("--hourly-prior-strength", type=float, default=0.01)
    parser.add_argument("--profile-mode", choices=["shared", "calendar"], default="shared")
    parser.add_argument("--external-scale-mode", choices=["shared", "calendar"], default="shared")
    args = parser.parse_args()
    train = sorted(pd.Timestamp(x).normalize() for x in args.train_dates)
    validation = sorted(pd.Timestamp(x).normalize() for x in args.validation_dates)
    if set(train) & set(validation) or max(train) >= min(validation):
        raise ValueError("Validation dates must be disjoint and after training dates")
    days = pd.date_range(min(train), max(validation), freq="D")
    if set(days) != set(train + validation):
        raise ValueError("Contiguous train/validation dates required for midnight carry")
    source = args.processed_dir
    nodes = pd.read_parquet(source / "od_nodes_gyeongbu_supplemented.parquet")
    nodes = nodes[nodes.direction.eq(args.direction)]
    zones = pd.read_parquet(source / "conzone_gyeongbu.parquet")
    month = f"{days[0]:%Y%m}"
    zones = (
        zones[
            zones.in_corridor
            & zones.direction.eq(args.direction)
            & zones.period.astype(str).eq(month)
        ]
        .sort_values("seq")
        .drop_duplicates("conzone_id")
    )
    if zones.empty or any(f"{d:%Y%m}" != month for d in days):
        raise ValueError("One available period required")
    tcs = pd.read_parquet(source / "tcs_od_gyeongbu.parquet")
    tcs = tcs[tcs.direction.eq(args.direction)].copy()
    tcs.date = pd.to_datetime(tcs.date).dt.normalize()
    audit = pd.read_csv(source / "od_input_audit.csv", parse_dates=["date"])
    audit = audit[audit.direction.eq(args.direction)]
    traffic = pd.read_parquet(source / "traffic_gyeongbu.parquet")
    traffic = traffic[traffic.direction.eq(args.direction) & traffic.date.isin(days)]
    daily_frames = []
    external_training = []
    training_tcs_total = []
    for date in train:
        fixed = tcs[tcs.date.eq(date)]
        obs = audit[audit.date.eq(date) & audit.valid_hours.eq(24)].copy()
        if fixed.empty or obs.empty:
            raise ValueError(f"Missing training inputs: {date}")
        pos = obs[["conzone_id"]].merge(zones, on="conzone_id", validate="one_to_one")
        daily, _ = fit(
            nodes,
            pos,
            fixed,
            obs,
            args.daily_prior_strength,
            constraint_mode=args.daily_constraint_mode,
        )
        daily["date"] = date
        daily_frames.append(daily)
        external_training.append(daily[daily.source.eq("estimated_external_daily")])
        training_tcs_total.append(float(fixed.volume_veh.sum()))
    for date in validation:
        fixed = tcs[tcs.date.eq(date)]
        if fixed.empty:
            raise ValueError(f"Missing validation TCS: {date}")
        predicted = predict_external(
            external_training,
            training_tcs_total,
            train,
            date,
            float(fixed.volume_veh.sum()),
            args.external_scale_mode,
        )
        daily = pd.concat([measured_od(fixed), predicted], ignore_index=True)
        daily["date"] = date
        daily_frames.append(daily)
    daily_od = pd.concat(daily_frames, ignore_index=True)
    origins = sorted(daily_od.start_node.unique())
    positions = (zones.offset_km_start.to_numpy() + zones.offset_km_end.to_numpy()) / 2
    matrix, total_hours = passage_operator(
        daily_od, positions, origins, days[0], len(days), args.speed_kmh
    )
    calendar_keys = calendar_kinds = None
    if args.profile_mode == "calendar":
        keyed, calendar_keys, calendar_kinds = profile_keys(daily_od, train)
        calendar_matrix, _ = passage_operator(
            keyed, positions, calendar_keys, days[0], len(days), args.speed_kmh
        )
    index = pd.MultiIndex.from_product(
        [zones.conzone_id, range(total_hours)], names=["conzone_id", "time_index"]
    )
    diagnostic = index.to_frame(index=False)
    diagnostic["date"] = days[0] + pd.to_timedelta(diagnostic.time_index // 24, unit="D")
    diagnostic["hour"] = diagnostic.time_index % 24
    diagnostic = diagnostic.merge(
        traffic[["conzone_id", "date", "hour", "volume_veh"]],
        on=["conzone_id", "date", "hour"],
        how="left",
        validate="one_to_one",
    )
    warmup_hours = int(np.ceil(nodes.offset_km.max() / args.speed_kmh))
    diagnostic["scoreable"] = diagnostic.time_index.ge(warmup_hours) & diagnostic.volume_veh.notna()
    diagnostic["split"] = np.where(
        diagnostic.date.isin(train),
        "train",
        np.where(diagnostic.date.isin(validation), "validation", "tail"),
    )
    training_mask = diagnostic.split.eq("train") & diagnostic.scoreable
    training_traffic = traffic[traffic.date.isin(train)]
    global_curve = training_traffic.groupby("hour").volume_veh.mean().reindex(range(24))
    if global_curve.isna().any() or global_curve.sum() <= 0:
        raise ValueError("Missing training hour profile")
    prior_rows = []
    for origin in origins:
        start = float(daily_od[daily_od.start_node.eq(origin)].start_offset_km.iloc[0])
        downstream = np.flatnonzero(positions >= start)
        nearest = downstream[np.argmin(positions[downstream] - start)] if len(downstream) else None
        curve = (
            training_traffic[training_traffic.conzone_id.eq(zones.conzone_id.iloc[nearest])]
            .groupby("hour")
            .volume_veh.mean()
            .reindex(range(24))
            if nearest is not None
            else global_curve
        )
        if curve.isna().any() or curve.sum() <= 0:
            curve = global_curve
        values = np.maximum(curve.to_numpy(dtype=float), 1.0)
        prior_rows.append(values / values.sum())
    prior = np.asarray(prior_rows)
    profiles = learn_profiles(
        matrix,
        diagnostic.volume_veh.to_numpy(),
        training_mask.to_numpy(),
        prior,
        args.hourly_prior_strength,
        solver_gtol=1e-07,
        solver_ftol=1e-10,
    )
    if args.profile_mode == "calendar":
        # The shared solution is a shrinkage prior and an unseen-type fallback.
        grouped_prior = np.tile(np.maximum(profiles, 1e-12), (len(calendar_kinds), 1))
        profiles = learn_profiles(
            calendar_matrix,
            diagnostic.volume_veh.to_numpy(),
            training_mask.to_numpy(),
            grouped_prior,
            args.hourly_prior_strength,
            solver_gtol=1e-07,
            solver_ftol=1e-10,
        )
        profiles[: len(origins)] = grouped_prior[: len(origins)]
        diagnostic["fitted_veh"] = calendar_matrix @ profiles.ravel()
    else:
        diagnostic["fitted_veh"] = matrix @ profiles.ravel()
    diagnostic["uniform_profile_veh"] = matrix @ np.full(len(origins) * 24, 1 / 24)
    metrics = []
    for split in ["train", "validation"]:
        selected = diagnostic[diagnostic.split.eq(split) & diagnostic.scoreable]
        for model in ["fitted_veh", "uniform_profile_veh"]:
            error = selected[model] - selected.volume_veh
            positive = selected.volume_veh > 0
            metrics.append(
                dict(
                    split=split,
                    model=model,
                    rows=len(selected),
                    rmse=float(np.sqrt(np.mean(error**2))),
                    mape_pct=float(
                        (error[positive].abs() / selected.volume_veh[positive]).mean() * 100
                    ),
                    zero_target_rows=int((~positive).sum()),
                )
            )
    hourly = (
        allocate_by_calendar(daily_od, origins, profiles, calendar_kinds, train)
        if args.profile_mode == "calendar"
        else allocate(daily_od, origins, profiles)
    )
    hourly["direction"] = args.direction
    hourly["split"] = np.where(hourly.date.isin(train), "train", "validation")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    hourly.to_parquet(
        args.output_dir / f"od_gyeongbu_{args.direction}_{month}_prototype.parquet", index=False
    )
    diagnostic.to_csv(args.output_dir / "hourly_fit.csv", index=False)
    pd.DataFrame(metrics).to_csv(args.output_dir / "metrics.csv", index=False)
    pd.DataFrame(profiles, index=calendar_keys if calendar_keys else origins).rename_axis(
        "start_node"
    ).to_csv(args.output_dir / "entry_profiles.csv")
    metadata = dict(
        direction=args.direction,
        profile_mode=args.profile_mode,
        external_scale_mode=args.external_scale_mode,
        holiday_dates=["2026-02-16", "2026-02-17", "2026-02-18"],
        profile_fallback="shared training profile for unobserved date type",
        input_sha256={
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in [
                "od_nodes_gyeongbu_supplemented.parquet",
                "conzone_gyeongbu.parquet",
                "tcs_od_gyeongbu.parquet",
                "traffic_gyeongbu.parquet",
                "od_input_audit.csv",
            ]
        },
        solver_gtol=1e-07,
        solver_ftol=1e-10,
        train_dates=args.train_dates,
        validation_dates=args.validation_dates,
        speed_kmh=args.speed_kmh,
        daily_prior_strength=args.daily_prior_strength,
        daily_constraint_mode=args.daily_constraint_mode,
        hourly_prior_strength=args.hourly_prior_strength,
        warmup_hours=warmup_hours,
        validation_external_scale="training external OD / training TCS, within date type if calendar; unseen type uses all training days",
        limitations=[
            "prototype",
            "constant speed assumption",
            "midpoint observations",
            "VDS lane aggregation unresolved",
            "daily stage lacks midnight correction",
            (
                "origin profiles stationary within calendar category; holiday phase not distinguished"
                if args.profile_mode == "calendar"
                else "shared origin profile stationary across dates"
            ),
            "uniform profile baseline is not legacy single-origin model",
            "unverified IC/JC movements remain",
        ],
    )
    (args.output_dir / "parameters.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    print(pd.DataFrame(metrics).to_string(index=False))
    print("Hourly TCS departure sums preserved; validation VDS excluded from calibration.")


def main():
    actions = {"audit": audit_main, "daily": fit_main, "fit": fit_main, "hourly": hourly_main}
    command = sys.argv.pop(1) if len(sys.argv) > 1 and sys.argv[1] in actions else "daily"
    actions[command]()


if __name__ == "__main__":
    main()
