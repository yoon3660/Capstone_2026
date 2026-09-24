"""통행시간 테스트 (#56).

`ConstantSpeed` 와 `CellSpeedField` 가 같은 인터페이스를 쓰는지, 그리고 막힌 구간이
도착 시각을 실제로 늦추는지를 본다. 후자가 이 티켓의 요점이다 — 고정 80 km/h 로는
"막혀서 늦게 도착해 몰린다" 가 아예 나오지 않는다.
"""

from __future__ import annotations

import numpy as np
import pytest

from evdt.world.travel import MIN_SPEED_KMH, CellSpeedField, ConstantSpeed, speed_grid

EDGES = np.arange(0.0, 101.0, 1.0)      # 1 km 셀 100개
N_CELLS = EDGES.size - 1
BUCKETS = 288                            # 5분 × 288 = 하루


def uniform_field(speed_kmh: float = 100.0, bucket_min: float = 5.0) -> CellSpeedField:
    return CellSpeedField(EDGES, np.full((BUCKETS, N_CELLS), speed_kmh), bucket_min)


# ---------------------------------------------------------------------------
# 고정 속도
# ---------------------------------------------------------------------------


def test_constant_speed_matches_the_old_formula() -> None:
    """예전 drive_min 과 같은 값이어야 한다 (옛 실험 재현)."""
    assert ConstantSpeed(80.0).minutes(0.0, 120.0, depart_min=0.0) == pytest.approx(90.0)


def test_constant_speed_ignores_departure_time() -> None:
    fixed = ConstantSpeed(80.0)

    assert fixed.minutes(10.0, 50.0, 0.0) == fixed.minutes(10.0, 50.0, 900.0)


def test_backwards_leg_is_zero_not_negative() -> None:
    """목적지가 뒤에 있으면 0 이다. 음수 통행시간은 도착 시각을 거꾸로 돌린다."""
    assert ConstantSpeed(80.0).minutes(50.0, 10.0, 0.0) == 0.0
    assert uniform_field().minutes(50.0, 10.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# 균일한 속도 격자 = 고정 속도
# ---------------------------------------------------------------------------


def test_uniform_field_equals_constant_speed() -> None:
    """전 구간이 같은 속도면 고정 속도와 정확히 같아야 한다."""
    field = uniform_field(100.0)
    fixed = ConstantSpeed(100.0)

    for start, end in [(0.0, 100.0), (0.0, 37.0), (12.5, 88.5), (60.0, 61.0)]:
        assert field.minutes(start, end, 0.0) == pytest.approx(fixed.minutes(start, end), rel=1e-9)


def test_partial_cells_at_both_ends() -> None:
    """출발·도착이 둘 다 셀 한가운데여도 맞는다."""
    field = uniform_field(60.0)   # 1 km = 1분

    assert field.minutes(10.25, 20.75, 0.0) == pytest.approx(10.5, rel=1e-9)


def test_leg_inside_one_cell() -> None:
    field = uniform_field(60.0)

    assert field.minutes(10.25, 10.75, 0.0) == pytest.approx(0.5, rel=1e-9)


# ---------------------------------------------------------------------------
# 막힌 구간
# ---------------------------------------------------------------------------


def test_a_slow_stretch_delays_arrival() -> None:
    """30~40 km 가 20 km/h 면 그만큼 늦게 도착한다. 이게 이 티켓의 요점이다."""
    speeds = np.full((BUCKETS, N_CELLS), 100.0)
    speeds[:, 30:40] = 20.0
    field = CellSpeedField(EDGES, speeds)

    free = 100.0 / 100.0 * 60.0                      # 60분
    expected = 90.0 / 100.0 * 60.0 + 10.0 / 20.0 * 60.0   # 54 + 30

    assert field.minutes(0.0, 100.0, 0.0) == pytest.approx(expected, rel=1e-9)
    assert field.minutes(0.0, 100.0, 0.0) > free


def test_departure_time_changes_travel_time() -> None:
    """같은 구간도 막히는 시간대에 출발하면 오래 걸린다 (고정 속도로는 불가능)."""
    speeds = np.full((BUCKETS, N_CELLS), 100.0)
    speeds[120:, :] = 25.0                           # 10시(=600분)부터 막힌다
    field = CellSpeedField(EDGES, speeds)

    early = field.minutes(0.0, 50.0, depart_min=0.0)
    late = field.minutes(0.0, 50.0, depart_min=700.0)

    assert late > early * 3.5


def test_congestion_met_on_the_way_is_felt() -> None:
    """출발할 땐 뚫려 있어도, 도착할 즈음 막히면 그 속도를 겪는다."""
    speeds = np.full((BUCKETS, N_CELLS), 60.0)       # 1 km = 1분
    speeds[60:, 50:] = 6.0                           # 300분 뒤부터 뒤쪽 절반이 막힌다
    field = CellSpeedField(EDGES, speeds)

    # 295분에 출발하면 50 km 까지 50분, 그 뒤는 막힌 구간에서 10분/km
    travelled = field.minutes(0.0, 60.0, depart_min=295.0)

    assert travelled > 50.0 + 10.0 * 9   # 뚫려 있었다면 60분이면 끝났다


def test_zero_speed_is_floored_and_counted() -> None:
    """완전 정지는 무한이 아니라 하한으로 본다. 얼마나 걸렸는지 셀 수 있어야 한다."""
    speeds = np.full((BUCKETS, N_CELLS), 100.0)
    speeds[:, 50] = 0.0
    field = CellSpeedField(EDGES, speeds)

    travelled = field.minutes(0.0, 100.0, 0.0)

    assert np.isfinite(travelled)
    assert travelled == pytest.approx(99.0 / 100.0 * 60.0 + 1.0 / MIN_SPEED_KMH * 60.0, rel=1e-9)
    assert 0.0 < field.jammed_share < 1.0


# ---------------------------------------------------------------------------
# 캐시가 답을 바꾸지 않는다
# ---------------------------------------------------------------------------


def test_cache_returns_the_same_answer() -> None:
    """두 번째 물음은 캐시에서 나온다. 값이 달라지면 안 된다."""
    speeds = np.full((BUCKETS, N_CELLS), 100.0)
    speeds[:, 20:30] = 30.0
    field = CellSpeedField(EDGES, speeds)

    first = [field.minutes(a, b, t) for a, b, t in [(0, 100, 0), (5, 60, 30), (0, 100, 0)]]
    second = [field.minutes(a, b, t) for a, b, t in [(0, 100, 0), (5, 60, 30), (0, 100, 0)]]

    assert first == second
    assert first[0] == first[2]


def test_legs_add_up() -> None:
    """a→b 와 b→c 를 이어 붙이면 a→c 와 같다 (도착 시각을 그 자리에서 이어 쓴다)."""
    speeds = np.full((BUCKETS, N_CELLS), 100.0)
    speeds[:, 40:60] = 35.0
    field = CellSpeedField(EDGES, speeds)

    whole = field.minutes(0.0, 90.0, 0.0)
    first = field.minutes(0.0, 45.0, 0.0)
    second = field.minutes(45.0, 90.0, first)

    assert whole == pytest.approx(first + second, rel=1e-9)


# ---------------------------------------------------------------------------
# 만들 때 막는 것
# ---------------------------------------------------------------------------


def test_grid_with_the_wrong_number_of_cells_is_refused() -> None:
    with pytest.raises(ValueError, match="셀 수가 경계와 맞지 않는다"):
        CellSpeedField(EDGES, np.full((BUCKETS, N_CELLS - 3), 100.0))


def test_unsorted_edges_are_refused() -> None:
    with pytest.raises(ValueError, match="오름차순"):
        CellSpeedField(np.array([0.0, 2.0, 1.0]), np.full((1, 2), 100.0))


def test_speed_grid_stacks_rows() -> None:
    rows = [np.full(N_CELLS, 90.0), np.full(N_CELLS, 45.0)]

    grid = speed_grid(rows)

    assert grid.shape == (2, N_CELLS)
    assert grid[1, 0] == 45.0
