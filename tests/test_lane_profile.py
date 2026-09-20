"""#24 debug/lanes — 차로 프로파일 규칙.

차로수는 CTM 용량에 그대로 곱해진다. 1차로로 잘못 잡힌 구간 하나가 없던 병목을
만들고, 그 병목이 충전 수요 쏠림을 만든다. 그래서 규칙을 테스트로 박아둔다.
"""

from __future__ import annotations

import pandas as pd
import pytest
import yaml

from evdt.io import flow_params as fp
from evdt.io.lane_profile import (
    check_lanes_against_observed,
    covered_ranges,
    lane_change_points,
    max_dt_min,
    merge_adjacent_lane_segments,
    merge_short_segments,
    select_mainline_candidates,
    uncovered_ranges,
    validate_lane_profile,
)
from evdt.paths import CONFIG_DIR


def _profile(rows: list[tuple[float, float, int, str]], direction: str = "UP") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "offset_km_start": a,
                "offset_km_end": b,
                "lanes": lanes,
                "direction": direction,
                "lanes_source": source,
            }
            for a, b, lanes, source in rows
        ]
    )


# ---------------------------------------------------------------------------
# 구간 합치기 (램프 판정의 기반)
# ---------------------------------------------------------------------------


def test_covered_and_uncovered_ranges():
    intervals = [(0.0, 2.0), (1.5, 3.0), (5.0, 6.0)]

    assert covered_ranges(intervals) == [(0.0, 3.0), (5.0, 6.0)]
    assert uncovered_ranges(intervals, (0.0, 8.0)) == [(3.0, 5.0), (6.0, 8.0)]
    assert uncovered_ranges(intervals, (0.0, 3.0)) == []


# ---------------------------------------------------------------------------
# 1. 연결로(램프) 제외
# ---------------------------------------------------------------------------


def _links(rows: list[tuple[str, float, float, str, int]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"link_id": link_id, "m_start": a, "m_end": b, "connect": connect, "lanes": lanes}
            for link_id, a, b, connect, lanes in rows
        ]
    )


def test_ramps_are_dropped_when_mainline_covers_the_same_stretch():
    """본선이 덮는 구간의 램프(1차로)는 후보에서 빠진다."""

    candidates = _links(
        [
            ("main_a", 0.0, 5.0, "0", 4),
            ("main_b", 5.0, 10.0, "0", 3),
            ("ramp_x", 2.0, 2.4, "1", 1),   # 본선 위에 겹치는 램프
        ]
    )

    selected, ramp_fallback = select_mainline_candidates(candidates)

    assert sorted(selected["link_id"]) == ["main_a", "main_b"]
    assert ramp_fallback.empty
    assert set(selected["lanes_source"]) == {"measured"}


def test_ramp_is_kept_only_where_mainline_has_a_gap():
    """본선이 끊긴 구간은 램프로 메우되 lanes_source='assumed' 로 표시한다."""

    candidates = _links(
        [
            ("main_a", 0.0, 5.0, "0", 4),
            ("ramp_gap", 5.0, 6.0, "1", 2),   # 본선이 없는 구간
            ("main_b", 6.0, 10.0, "0", 4),
            ("ramp_dup", 1.0, 2.0, "1", 1),   # 본선이 이미 덮는 구간
        ]
    )

    selected, ramp_fallback = select_mainline_candidates(candidates)

    assert sorted(selected["link_id"]) == ["main_a", "main_b", "ramp_gap"]
    assert list(ramp_fallback["link_id"]) == ["ramp_gap"]

    by_id = selected.set_index("link_id")["lanes_source"]
    assert by_id["main_a"] == "measured"
    assert by_id["ramp_gap"] == "assumed"


# ---------------------------------------------------------------------------
# 2. 관측 교통량 교차검증
# ---------------------------------------------------------------------------


def _traffic(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"direction": direction, "offset_km": offset, "volume_veh": volume}
            for direction, offset, volume in rows
        ]
    )


def test_observed_flow_refutes_impossible_lane_count():
    """한 차로가 2,200대/h 를 넘길 수 없다. 5,000대가 관측된 구간은 1차로일 수 없다."""

    profile = _profile([(0.0, 10.0, 1, "measured"), (10.0, 20.0, 4, "measured")])
    traffic = _traffic([("UP", 5.0, 5000.0), ("UP", 15.0, 5000.0)])

    violations = check_lanes_against_observed(profile, traffic, 2200.0)

    assert len(violations) == 1
    row = violations.iloc[0]
    assert row["offset_km_start"] == 0.0
    assert row["lanes"] == 1
    assert row["lanes_required"] == 3       # ceil(5000 / 2200)


def test_observed_flow_accepts_consistent_lane_count():
    profile = _profile([(0.0, 10.0, 3, "measured")])
    traffic = _traffic([("UP", 5.0, 5000.0), ("UP", 6.0, 1200.0)])

    assert check_lanes_against_observed(profile, traffic, 2200.0).empty


def test_observed_flow_ignores_other_direction_and_missing_values():
    profile = _profile([(0.0, 10.0, 2, "measured")], direction="UP")
    traffic = pd.DataFrame(
        [
            {"direction": "DOWN", "offset_km": 5.0, "volume_veh": 9000.0},
            {"direction": "UP", "offset_km": 5.0, "volume_veh": None},
        ]
    )

    assert check_lanes_against_observed(profile, traffic, 2200.0).empty


# ---------------------------------------------------------------------------
# 3. 최소 셀 길이와 CFL
# ---------------------------------------------------------------------------


def test_short_segment_is_absorbed_by_the_smaller_neighbour():
    """24.7m 짜리 구간은 사라지고, 용량을 크게 잡지 않도록 적은 차로 쪽에 붙는다."""

    profile = _profile(
        [
            (0.0, 3.0, 4, "measured"),
            (3.0, 3.0247, 1, "measured"),   # 24.7 m
            (3.0247, 8.0, 3, "measured"),
        ]
    )

    merged = merge_short_segments(profile, min_length_km=0.5)

    assert len(merged) == 2
    assert merged["offset_km_start"].min() == 0.0
    assert merged["offset_km_end"].max() == 8.0
    # 짧은 구간은 차로수가 적은 오른쪽(3차로)에 흡수된다
    assert merged.iloc[1]["lanes"] == 3
    assert merged.iloc[1]["offset_km_start"] == 3.0
    assert merged.iloc[1]["lanes_source"] == "assumed"


def test_merge_short_segments_keeps_total_span():
    profile = _profile(
        [(0.0, 0.1, 4, "measured"), (0.1, 0.2, 2, "measured"), (0.2, 4.0, 4, "measured")]
    )

    merged = merge_short_segments(profile, min_length_km=1.0)

    assert merged["offset_km_start"].min() == 0.0
    assert merged["offset_km_end"].max() == 4.0
    assert (merged["offset_km_end"] > merged["offset_km_start"]).all()


def test_long_enough_segments_are_left_alone():
    profile = _profile([(0.0, 2.0, 4, "measured"), (2.0, 5.0, 3, "measured")])

    merged = merge_short_segments(profile, min_length_km=0.5)

    pd.testing.assert_frame_equal(
        merged.reset_index(drop=True), profile.reset_index(drop=True), check_dtype=False
    )


def test_cfl_limit():
    """100 km/h 에서 0.5 km 셀이면 dt 는 0.3분(18초)을 넘을 수 없다."""

    assert max_dt_min(100.0, 0.5) == pytest.approx(0.3)

    with pytest.raises(ValueError):
        max_dt_min(0.0, 0.5)


def test_config_dt_satisfies_cfl():
    """config/flow_params.yaml 의 dt_min 이 CFL 조건을 지키는지 (하드코딩 아님)."""

    config = yaml.safe_load((CONFIG_DIR / "flow_params.yaml").read_text(encoding="utf-8"))
    defaults = config["defaults"]

    limit = max_dt_min(
        float(defaults["v_free_kmh"]),
        float(defaults["min_cell_length_km"]),
    )

    assert float(config["dt_min"]) <= limit


def test_config_q_max_matches_anchor():
    """config 값으로 계산한 차로당 q_max 가 기준 앵커 근처인지."""

    config = yaml.safe_load((CONFIG_DIR / "flow_params.yaml").read_text(encoding="utf-8"))
    defaults = config["defaults"]

    q = fp.q_per_lane(
        v_free_kmh=float(defaults["v_free_kmh"]),
        w_back_kmh=float(defaults["w_back_kmh"]),
        k_jam_veh_km_lane=float(defaults["k_jam_veh_km_lane"]),
    )

    assert q == pytest.approx(float(defaults["q_max_anchor_veh_h_lane"]), rel=0.05)


# ---------------------------------------------------------------------------
# 프로파일 정리·검증
# ---------------------------------------------------------------------------


def test_adjacent_same_lane_segments_merge():
    profile = _profile(
        [
            (0.0, 1.0, 4, "measured"),
            (1.0, 2.0, 4, "measured"),
            (2.0, 3.0, 4, "assumed"),    # 출처가 다르면 합치지 않는다
            (3.0, 4.0, 3, "assumed"),
        ]
    )

    merged = merge_adjacent_lane_segments(profile)

    assert len(merged) == 3
    assert merged.iloc[0]["offset_km_end"] == 2.0


def test_lane_change_points():
    profile = _profile(
        [(0.0, 1.0, 4, "measured"), (1.0, 2.0, 3, "measured"), (2.0, 3.0, 4, "measured")]
    )

    changes = lane_change_points(profile)

    assert list(changes["offset_km"]) == [1.0, 2.0]
    assert list(changes["lanes_from"]) == [4, 3]
    assert list(changes["lanes_to"]) == [3, 4]


def test_validate_accepts_a_clean_profile():
    validate_lane_profile(_profile([(0.0, 1.0, 4, "measured"), (1.0, 2.0, 3, "measured")]))


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([(0.0, 1.0, 4, "measured"), (1.5, 2.0, 3, "measured")], "gap"),
        ([(0.0, 1.5, 4, "measured"), (1.0, 2.0, 3, "measured")], "overlap"),
        ([(0.0, 1.0, 1, "measured")], "본선 차로수"),
        ([(0.0, 1.0, 7, "measured")], "본선 차로수"),
        ([(0.0, 1.0, 4, "guessed")], "lanes_source"),
    ],
)
def test_validate_rejects_broken_profiles(rows, message):
    with pytest.raises(RuntimeError, match=message):
        validate_lane_profile(_profile(rows))


def test_validate_rejects_wrong_columns():
    profile = _profile([(0.0, 1.0, 4, "measured")]).drop(columns="lanes_source")

    with pytest.raises(RuntimeError, match="컬럼"):
        validate_lane_profile(profile)
