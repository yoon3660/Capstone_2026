"""Corridor OD regression tests: daily recovery, hourly conservation and coordinates."""

import unittest

import numpy as np
import pandas as pd
import pytest
from build_od_nodes import replace_coordinates, select_attachments
from estimate_od_daily import (
    InfeasibleDailyConstraints,
    allocate,
    apply_rules,
    fit,
    learn_profiles,
    nearest_prior_exact,
    passage_operator,
    solve_constraints,
)
from scipy.sparse import csr_matrix


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
