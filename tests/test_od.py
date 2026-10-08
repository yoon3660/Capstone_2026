"""Corridor OD regression tests: daily recovery, hourly conservation and coordinates."""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy.sparse import csr_matrix

# OD scripts are executable files, so this test owns their import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_od_nodes import replace_coordinates, select_attachments  # noqa: E402
from estimate_od_daily import (  # noqa: E402
    InfeasibleDailyConstraints,
    allocate,
    apply_rules,
    fit,
    learn_profiles,
    nearest_prior_exact,
    passage_operator,
    solve_constraints,
)


def four_nodes():
    return pd.DataFrame(
        [
            dict(
                node_id=f"n{i}",
                name=f"node{i}",
                kind="endpoint" if i in (0, 3) else "junction",
                direction="UP",
                offset_km=float(i * 10),
            )
            for i in range(4)
        ]
    )


def empty_tcs():
    return pd.DataFrame(
        columns=[
            "start_office_code",
            "end_office_code",
            "start_offset_km",
            "end_offset_km",
            "distance_km",
            "volume_veh",
        ]
    )


def zones():
    return pd.DataFrame({"offset_km_start": [0.0, 10.0, 20.0], "offset_km_end": [10.0, 20.0, 30.0]})


@pytest.mark.parametrize(
    "truth", [[120.0, 80.0, 160.0], [30.0, 250.0, 90.0], [100.0, 100.0, 100.0]]
)
def test_unknown_daily_od_recovered_on_identifiable_four_node_corridor(truth):
    paths = {("n0", "n1"), ("n1", "n2"), ("n2", "n3")}
    incidence = np.eye(3)
    assert np.linalg.matrix_rank(incidence) == 3
    observations = pd.DataFrame({"vds_daily_veh": incidence @ np.asarray(truth)})
    recovered, diagnostics = fit(
        four_nodes(), zones(), empty_tcs(), observations, strength=1e-08, allowed_pairs=paths
    )
    values = recovered.set_index(["start_node", "end_node"]).volume_veh
    assert set(values.index) == paths
    np.testing.assert_allclose([values[p] for p in sorted(paths)], truth, rtol=0.0001, atol=0.01)
    np.testing.assert_allclose(
        diagnostics.fitted_daily_veh, observations.vds_daily_veh, rtol=0.0001, atol=0.01
    )
    assert recovered.volume_veh.ge(0).all()


def test_all_six_paths_are_not_identifiable_from_three_section_counts():
    incidence = np.array([[1, 1, 1, 0, 0, 0], [0, 1, 1, 1, 1, 0], [0, 0, 1, 0, 1, 1]], dtype=float)
    first = np.array([100.0, 20.0, 10.0, 30.0, 20.0, 70.0])
    second = first + np.array([10.0, -10.0, 0.0, 10.0, 0.0, 0.0])
    assert (first >= 0).all() and (second >= 0).all()
    assert not np.array_equal(first, second)
    assert np.linalg.matrix_rank(incidence) == 3 < incidence.shape[1]
    np.testing.assert_array_equal(incidence @ first, incidence @ second)
    observations = pd.DataFrame({"vds_daily_veh": incidence @ first})
    recovered, diagnostic = fit(four_nodes(), zones(), empty_tcs(), observations, 1e-08)
    assert len(recovered) == 6
    np.testing.assert_allclose(
        diagnostic.fitted_daily_veh, observations.vds_daily_veh, rtol=0.0001, atol=0.01
    )
    values = recovered.set_index(["start_node", "end_node"]).volume_veh
    pair_order = [
        ("n0", "n1"),
        ("n0", "n2"),
        ("n0", "n3"),
        ("n1", "n2"),
        ("n1", "n3"),
        ("n2", "n3"),
    ]
    assert not np.allclose([values[p] for p in pair_order], first)


def test_exact_unknown_nonadjacent_od_recovery_on_four_nodes():
    matrix = csr_matrix([[1, 1, 0], [1, 1, 1], [0, 1, 0]])
    truth = np.array([80.0, 40.0, 60.0])
    recovered = nearest_prior_exact(matrix, matrix @ truth, [50, 50, 50])
    np.testing.assert_allclose(recovered, truth, atol=1e-06)


def test_underdetermined_solution_is_nearest_prior_not_arbitrary_lp_vertex():
    recovered = nearest_prior_exact(csr_matrix([[1, 1]]), [40], [10, 20])
    np.testing.assert_allclose(recovered, [12, 28], atol=1e-05)


def test_dependent_constraints_and_nonnegative_active_bound():
    matrix = csr_matrix([[1, 1], [2, 2]])
    recovered = nearest_prior_exact(matrix, [0, 0], [10, 20])
    np.testing.assert_allclose(recovered, 0, atol=1e-06)


def test_infeasible_equal_path_observations_report_minimum_error():
    with pytest.raises(InfeasibleDailyConstraints) as error:
        nearest_prior_exact(csr_matrix([[1], [1]]), [100, 60], [80])
    assert abs(error.value.minimum_error - 20) < 1e-06


def test_exact_mode_of_production_fit_recovers_nonadjacent_four_node_paths():
    import pandas as pd
    from estimate_od_daily import fit

    nodes = pd.DataFrame(
        [
            dict(
                node_id=f"n{i}",
                name=f"node{i}",
                kind="endpoint" if i in (0, 3) else "junction",
                direction="UP",
                offset_km=float(i * 10),
            )
            for i in range(4)
        ]
    )
    zones = pd.DataFrame(
        {"offset_km_start": [0.0, 10.0, 20.0], "offset_km_end": [10.0, 20.0, 30.0]}
    )
    tcs = pd.DataFrame(
        columns=[
            "start_office_code",
            "end_office_code",
            "start_offset_km",
            "end_offset_km",
            "distance_km",
            "volume_veh",
        ]
    )
    paths = {("n0", "n2"), ("n0", "n3"), ("n1", "n2")}
    observations = pd.DataFrame({"vds_daily_veh": [120.0, 180.0, 40.0]})
    result, diagnostics = fit(
        nodes, zones, tcs, observations, 0.001, allowed_pairs=paths, constraint_mode="exact"
    )
    volumes = result.set_index(["start_node", "end_node"]).volume_veh
    np.testing.assert_allclose([volumes[p] for p in sorted(paths)], [80, 40, 60], atol=1e-06)
    np.testing.assert_allclose(diagnostics.error_veh, 0, atol=1e-06)


def test_identifiable_exact_counts():
    feasible, minimum, error = solve_constraints(csr_matrix(np.eye(3)), [100, 80, 120])
    assert feasible and minimum == 0
    np.testing.assert_allclose(error, 0, atol=1e-06)


def test_identical_path_rows_require_equal_residual_counts():
    feasible, minimum, error = solve_constraints(csr_matrix([[1], [1]]), [100, 60])
    assert not feasible
    assert abs(minimum - 20) < 1e-06
    np.testing.assert_allclose(error, [-20, 20], atol=1e-06)


def test_fixed_tcs_exceeding_observations_cannot_be_cancelled():
    feasible, minimum, error = solve_constraints(csr_matrix([[1]]), [-15])
    assert not feasible
    assert abs(minimum - 15) < 1e-06
    np.testing.assert_allclose(error, [15], atol=1e-06)


def test_no_candidate_paths():
    feasible, minimum, error = solve_constraints(csr_matrix((2, 0)), [0, 10])
    assert not feasible
    assert abs(minimum - 10) < 1e-06
    np.testing.assert_allclose(error, [0, -10], atol=1e-06)


class MovementTests(unittest.TestCase):
    def nodes(self, direction):
        return pd.DataFrame(
            [
                dict(
                    node_id="end:0",
                    name="origin",
                    kind="endpoint",
                    offset_km=0,
                    direction=direction,
                ),
                dict(
                    node_id="tcs:1", name="상서", kind="office", offset_km=10, direction=direction
                ),
                dict(
                    node_id="tcs:2",
                    name="금강휴게소",
                    kind="office",
                    offset_km=20,
                    direction=direction,
                ),
                dict(
                    node_id="jc:3",
                    name="example JC",
                    kind="junction",
                    offset_km=30,
                    direction=direction,
                ),
                dict(
                    node_id="end:40",
                    name="destination",
                    kind="endpoint",
                    offset_km=40,
                    direction=direction,
                ),
            ]
        )

    def test_direction_restriction_and_special_extension(self):
        down = apply_rules(self.nodes("DOWN"))
        up = apply_rules(self.nodes("UP"))
        self.assertFalse(down.iloc[1]["allow_estimated_entry"])
        self.assertFalse(down.iloc[1]["allow_estimated_exit"])
        self.assertTrue(up.iloc[1]["allow_estimated_entry"])
        self.assertFalse(up.iloc[2]["allow_estimated_entry"])
        self.assertFalse(up.iloc[2]["allow_estimated_exit"])
        self.assertIn("assumed", up.iloc[3]["movement_status"])

    def test_fit_preserves_special_measurement_and_forbids_extensions(self):
        tcs = pd.DataFrame(
            [
                dict(
                    start_office_code="1",
                    end_office_code="2",
                    start_offset_km=10,
                    end_offset_km=20,
                    distance_km=10,
                    volume_veh=7,
                )
            ]
        )
        zones = pd.DataFrame(
            {"offset_km_start": [0, 10, 20, 30], "offset_km_end": [10, 20, 30, 40]}
        )
        obs = pd.DataFrame({"vds_daily_veh": [100, 100, 100, 100]})
        result, diagnostic = fit(self.nodes("DOWN"), zones, tcs, obs, 0.001)
        measured = result[result.source == "measured_tcs_daily"]
        self.assertEqual(measured.volume_veh.tolist(), [7])
        extra = result[result.source == "estimated_external_daily"]
        self.assertFalse(extra.start_node.isin(["tcs:1", "tcs:2"]).any())
        self.assertFalse(extra.end_node.isin(["tcs:1", "tcs:2"]).any())
        self.assertTrue(extra.volume_veh.ge(0).all())
        np.testing.assert_allclose(diagnostic.tcs_fixed_veh, [0, 7, 0, 0])


class HourlyODTest(unittest.TestCase):
    def test_midnight_fractional_delay_and_daily_conservation(self):
        daily = pd.DataFrame(
            [
                dict(
                    date="2026-02-14",
                    start_node="a",
                    end_node="b",
                    start_offset_km=0.0,
                    end_offset_km=100.0,
                    distance_km=100.0,
                    volume_veh=80.0,
                    source="measured_tcs_daily",
                )
            ]
        )
        profile = np.zeros((1, 24))
        profile[0, 23] = 1
        matrix, hours = passage_operator(daily, [50.0], ["a"], "2026-02-14", 1, 100.0)
        predicted = matrix @ profile.ravel()
        self.assertAlmostEqual(predicted[23], 40.0)
        self.assertAlmostEqual(predicted[24], 40.0)
        self.assertEqual(hours, 26)
        self.assertAlmostEqual(predicted.sum(), 80.0)
        self.assertAlmostEqual(allocate(daily, ["a"], profile).volume_veh.sum(), 80.0)

    def test_known_od_on_four_nodes_with_identifiable_adjacent_paths(self):
        daily = pd.DataFrame(
            [
                dict(
                    date="2026-02-14",
                    start_node=f"n{i}",
                    end_node=f"n{i + 1}",
                    start_offset_km=i * 10.0,
                    end_offset_km=(i + 1) * 10.0,
                    distance_km=10.0,
                    volume_veh=100.0 + i * 20.0,
                )
                for i in range(3)
            ]
        )
        origins = ["n0", "n1", "n2"]
        matrix, hours = passage_operator(daily, [5.0, 15.0, 25.0], origins, "2026-02-14", 1, 10.0)
        truth = np.full((3, 24), 0.01)
        for i in range(3):
            truth[i, 6 + i] = 0.77
        target = matrix @ truth.ravel()
        learned = learn_profiles(
            matrix,
            target,
            np.ones(len(target), dtype=bool),
            np.full((3, 24), 1 / 24),
            strength=1e-08,
            solver_gtol=1e-10,
            solver_ftol=1e-14,
        )
        self.assertLess(np.max(np.abs(matrix @ learned.ravel() - target)), 0.05)
        for i in range(3):
            self.assertEqual(int(learned[i].argmax()), 6 + i)
        np.testing.assert_allclose(learned.sum(axis=1), 1.0)
        self.assertEqual(hours, 26)

    def test_heldout_targets_do_not_change_training(self):
        daily = pd.DataFrame(
            [
                dict(
                    date="2026-02-14",
                    start_node="a",
                    end_node="b",
                    start_offset_km=0.0,
                    end_offset_km=10.0,
                    distance_km=10.0,
                    volume_veh=24.0,
                )
            ]
        )
        matrix, _ = passage_operator(daily, [5.0], ["a"], "2026-02-14", 1, 10.0)
        target = np.ones(matrix.shape[0])
        mask = np.arange(len(target)) < 12
        prior = np.full((1, 24), 1 / 24)
        first = learn_profiles(matrix, target, mask, prior)
        target[~mask] = 999999.0
        second = learn_profiles(matrix, target, mask, prior)
        np.testing.assert_allclose(first, second)


class Identity:
    def transform(self, x, y):
        return (x, y)


class Route:
    length_km = 100

    def project(self, lat, lon):
        return (lat, abs(lon))

    def to_direction(self, milepost, direction):
        return milepost if direction == "UP" else self.length_km - milepost


def link(identifier, road, first, last, point):
    return dict(
        LINK_ID=identifier, ROAD_NAME=road, F_NODE=first, T_NODE=last, start_xy=point, end_xy=point
    )


def connections():
    return [
        link("a", "경부고속도로", "e", "e2", (0.01, 40)),
        link("b", "울산고속도로", "e", "r", (0.01, 40)),
        link("c", "경부고속도로", "o", "o2", (0.02, 80)),
        link("d", "옥산오창고속도로", "o", "r2", (0.02, 80)),
    ]


def test_actual_shared_road_node_coordinates_replace_estimates():
    evidence = select_attachments(connections(), Route(), Identity())
    assert set(evidence[evidence.selected].name) == {"언양JC", "옥산JC"}
    nodes = pd.DataFrame(
        [
            dict(
                node_id=f"jc:estimated:{name}",
                direction=direction,
                milepost_km=0.0,
                offset_km=0.0,
                source="conzone_boundary_estimate",
            )
            for name in ["언양JC", "옥산JC"]
            for direction in ["DOWN", "UP"]
        ]
    )
    actual = replace_coordinates(nodes, evidence, Route())
    assert actual.source.eq("moct_link_attachment_projection").all()
    assert not actual.node_id.str.contains("estimated").any()
    assert actual.groupby("node_id").offset_km.sum().eq(100).all()


def test_nearby_unconnected_road_is_not_a_junction_coordinate():
    rows = connections()
    rows[-1]["F_NODE"] = "different_node"
    with pytest.raises(ValueError, match="Both junctions"):
        select_attachments(rows, Route(), Identity())


def test_conflicting_raw_coordinates_for_same_node_are_rejected():
    rows = connections()
    rows[1]["start_xy"] = (999, 999)
    with pytest.raises(ValueError, match="Inconsistent coordinates"):
        select_attachments(rows, Route(), Identity())


def test_calendar_labels_holiday_precedes_weekend_and_unseen_type_falls_back():
    from estimate_od_daily import day_type, profile_keys

    assert day_type("2026-02-16") == "holiday"
    assert day_type("2026-02-14") == "weekend"
    assert day_type("2026-03-09") == "weekday"
    rows = pd.DataFrame(
        {"start_node": ["a", "a"], "date": pd.to_datetime(["2026-02-13", "2026-02-16"])}
    )
    keyed, _, _ = profile_keys(rows, ["2026-02-13"])
    assert keyed.start_node.tolist() == ["a|weekday", "a|shared"]


def test_calendar_hourly_allocation_preserves_daily_pairs_and_changes_peaks():
    from estimate_od_daily import allocate_by_calendar

    od = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-03-06", "2026-03-07"]),
            "start_node": ["a", "a"],
            "end_node": ["b", "b"],
            "volume_veh": [100.0, 200.0],
        }
    )
    profiles = np.full((3, 24), 1 / 24)
    profiles[1] = 0
    profiles[1, 8] = 1
    profiles[2] = 0
    profiles[2, 18] = 1
    result = allocate_by_calendar(od, ["a"], profiles, ["shared", "weekday", "weekend"], od.date)
    np.testing.assert_allclose(result.groupby("date").volume_veh.sum(), [100, 200])
    assert result.loc[result.volume_veh.gt(0), "departure_hour"].tolist() == [8, 18]
    assert set(result.start_node) == {"a"}


def test_profile_learning_cannot_see_validation_targets():
    from scipy.sparse import eye

    prior = np.full((2, 24), 1 / 24)
    matrix = eye(48, format="csr") * 2400
    target = np.full(48, 100.0)
    target[8] = 500
    mask = np.arange(48) < 24
    first = learn_profiles(matrix, target, mask, prior)
    target[~mask] = 1000000
    second = learn_profiles(matrix, target, mask, prior)
    np.testing.assert_allclose(first, second, atol=1e-10)


def test_calendar_passage_uses_departure_day_through_midnight():
    from estimate_od_daily import profile_keys

    od = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-03-06"]),
            "start_node": ["a"],
            "end_node": ["b"],
            "start_offset_km": [0.0],
            "end_offset_km": [200.0],
            "distance_km": [200.0],
            "volume_veh": [100.0],
        }
    )
    keyed, keys, _ = profile_keys(od, od.date)
    matrix, hours = passage_operator(keyed, [100.0], keys, od.date.iloc[0], 1, 100.0)
    profiles = np.zeros((len(keys), 24))
    profiles[:, 23] = 1
    flow = (matrix @ profiles.ravel()).reshape(1, hours)
    assert flow[0, 24] == 100
    assert flow.sum() == 100


def external_training_example(volume):
    return pd.DataFrame(
        {
            "start_node": ["a"],
            "end_node": ["b"],
            "start_offset_km": [0.0],
            "end_offset_km": [10.0],
            "distance_km": [10.0],
            "volume_veh": [volume],
        }
    )


def test_external_scale_uses_matching_training_date_type():
    from estimate_od_daily import predict_external

    frames = [external_training_example(100), external_training_example(400)]
    dates = ["2026-03-06", "2026-03-07"]
    weekday = predict_external(frames, [50, 100], dates, "2026-03-10", 100, "calendar")
    weekend = predict_external(frames, [50, 100], dates, "2026-03-14", 100, "calendar")
    assert weekday.volume_veh.sum() == 200
    assert weekend.volume_veh.sum() == 400
    shared = predict_external(frames, [50, 100], dates, "2026-03-10", 100, "shared")
    assert shared.volume_veh.sum() == pytest.approx(500 * 100 / 150)


def test_external_scale_unseen_holiday_falls_back_and_zero_is_nonnegative():
    from estimate_od_daily import predict_external

    frames = [external_training_example(100), external_training_example(400)]
    dates = ["2026-02-13", "2026-02-14"]
    result = predict_external(frames, [50, 100], dates, "2026-02-17", 100, "calendar")
    assert result.volume_veh.sum() == pytest.approx(500 * 100 / 150)
    zero = predict_external(frames, [50, 100], dates, "2026-02-17", 0, "calendar")
    assert zero.volume_veh.sum() == 0


def test_external_scale_rejects_invalid_training_totals():
    from estimate_od_daily import predict_external

    with pytest.raises(ValueError, match="Positive training"):
        predict_external(
            [external_training_example(100)], [0], ["2026-03-06"], "2026-03-10", 100, "calendar"
        )


@pytest.mark.parametrize("requested_speed", [90, 110])
def test_validation_reuse_requires_same_travel_speed(tmp_path, monkeypatch, requested_speed):
    import json
    import sys

    import run_od_validation

    for period, first in [("202602", "2026-02-13"), ("202603", "2026-03-06")]:
        dates = pd.date_range(first, periods=10).strftime("%Y-%m-%d").tolist()
        for direction in ["DOWN", "UP"]:
            folder = tmp_path / f"expanded_{period}_{direction}"
            folder.mkdir()
            (folder / "parameters.json").write_text(
                json.dumps(
                    dict(
                        daily_constraint_mode="exact",
                        train_dates=dates[:2],
                        validation_dates=dates[2:],
                        direction=direction,
                        speed_kmh=90,
                    )
                ),
                encoding="utf-8",
            )
    calls = []
    monkeypatch.setattr(
        run_od_validation, "summarize_results", lambda *args: calls.append("summary")
    )
    monkeypatch.setattr(run_od_validation, "compare_legacy", lambda *args: calls.append("legacy"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_od_validation.py",
            "--results-dir",
            str(tmp_path),
            "--reuse-results",
            "--speed-kmh",
            str(requested_speed),
        ],
    )
    if requested_speed == 90:
        run_od_validation.run_main()
        assert calls == ["summary", "legacy"]
    else:
        with pytest.raises(ValueError, match="Unexpected experiment split"):
            run_od_validation.run_main()
        assert not calls
