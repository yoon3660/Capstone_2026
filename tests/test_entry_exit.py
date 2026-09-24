"""중간 진입·진출 추정 (#54).

교통량 증감에서 나온 진입·진출이 (1) 교통량 프로파일을 되살리는지 (2) 목적지가
진입 지점보다 하류인지 (3) 결측 구간을 숨기지 않는지를 본다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from evdt.io.entry_exit import (
    MAX_EXIT_SHARE,
    corridor_entry_hourly,
    entry_exit_profile,
    entry_points,
    sample_entry_offsets,
    sample_exit_offsets,
)

PERIOD = "seollal2026"
DIRECTION = "DOWN"


def _traffic(zones: list[tuple[float, float, float]], *, hours: int = 2,
             dates: tuple[str, ...] = ("2026-02-13",)) -> pd.DataFrame:
    """zones: [(start_km, end_km, 시간당 교통량), ...] — 하류 순서."""
    rows = []
    for date in dates:
        for hour in range(hours):
            for i, (start, end, volume) in enumerate(zones):
                rows.append({
                    "period": PERIOD, "date": pd.Timestamp(date), "hour": hour,
                    "direction": DIRECTION, "conzone_id": f"z{i}", "conzone_name": f"zone{i}",
                    "offset_km": (start + end) / 2,
                    "offset_km_start": start, "offset_km_end": end,
                    "volume_veh": volume, "missing_code": np.nan, "source": "test",
                })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 늘면 진입, 줄면 진출
# ---------------------------------------------------------------------------


def test_rising_volume_becomes_entry() -> None:
    profile = entry_exit_profile(
        _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 150.0)]),
        period=PERIOD, direction=DIRECTION,
    )
    first = profile[profile["hour"] == 0].iloc[0]

    assert first["offset_km"] == pytest.approx(10.0)
    assert first["entry_veh"] == pytest.approx(50.0)
    assert first["exit_veh"] == pytest.approx(0.0)


def test_falling_volume_becomes_exit() -> None:
    profile = entry_exit_profile(
        _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 60.0)]),
        period=PERIOD, direction=DIRECTION,
    )
    first = profile[profile["hour"] == 0].iloc[0]

    assert first["exit_veh"] == pytest.approx(40.0)
    assert first["exit_share"] == pytest.approx(0.4)
    assert first["entry_veh"] == pytest.approx(0.0)


def test_exit_share_is_capped() -> None:
    """교통량이 거의 0 으로 떨어져도 한 경계에서 전원이 빠지지는 않는다고 본다."""
    profile = entry_exit_profile(
        _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 1.0)]),
        period=PERIOD, direction=DIRECTION,
    )

    assert profile["exit_share"].max() == pytest.approx(MAX_EXIT_SHARE)


def test_entry_and_exit_reproduce_the_volume_profile() -> None:
    """진입·진출을 누적하면 원래 교통량이 나온다 (추정이 자료와 모순되지 않는다)."""
    zones = [(0.0, 10.0, 100.0), (10.0, 20.0, 150.0), (20.0, 30.0, 90.0), (30.0, 40.0, 120.0)]
    traffic = _traffic(zones)
    profile = entry_exit_profile(traffic, period=PERIOD, direction=DIRECTION)
    hour0 = profile[profile["hour"] == 0].sort_values("offset_km")

    volume = zones[0][2]
    for (_, row), expected in zip(hour0.iterrows(), zones[1:], strict=True):
        volume += row["entry_veh"] - row["exit_veh"]
        assert volume == pytest.approx(expected[2])


# ---------------------------------------------------------------------------
# 결측 구간을 숨기지 않는다
# ---------------------------------------------------------------------------


def test_gap_between_measured_zones_is_reported() -> None:
    """결측으로 건너뛴 거리를 남긴다. 그 사이의 진입·진출이 한 경계에 몰려 잡힌다."""
    profile = entry_exit_profile(
        _traffic([(0.0, 10.0, 100.0), (35.0, 45.0, 150.0)]),
        period=PERIOD, direction=DIRECTION,
    )

    assert profile["gap_km"].iloc[0] == pytest.approx(25.0)


def test_adjacent_zones_have_no_gap() -> None:
    profile = entry_exit_profile(
        _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 90.0)]),
        period=PERIOD, direction=DIRECTION,
    )

    assert profile["gap_km"].iloc[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 코리도 진입
# ---------------------------------------------------------------------------


def test_corridor_entry_is_the_first_zone() -> None:
    entry = corridor_entry_hourly(
        _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 150.0)], hours=3),
        period=PERIOD, direction=DIRECTION,
    )

    assert len(entry) == 24
    assert entry[entry["hour"] < 3]["entry_veh"].tolist() == [100.0, 100.0, 100.0]
    assert entry[entry["hour"] >= 3]["entry_veh"].sum() == 0.0


def test_entry_points_include_the_head_and_the_middle() -> None:
    traffic = _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 150.0), (20.0, 30.0, 90.0)])
    profile = entry_exit_profile(traffic, period=PERIOD, direction=DIRECTION)
    points = entry_points(profile, corridor_entry_hourly(traffic, period=PERIOD, direction=DIRECTION))
    hour0 = points[points["hour"] == 0]

    assert sorted(hour0["offset_km"]) == [0.0, 10.0]     # 20km 는 진출이라 안 들어간다


# ---------------------------------------------------------------------------
# 뽑기
# ---------------------------------------------------------------------------


def _points_and_profile():
    traffic = _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 300.0), (20.0, 30.0, 150.0)])
    profile = entry_exit_profile(traffic, period=PERIOD, direction=DIRECTION)
    points = entry_points(profile, corridor_entry_hourly(traffic, period=PERIOD, direction=DIRECTION))
    return points, profile


def test_entry_offsets_follow_the_estimated_weights() -> None:
    """기점 100 대 · 10km 진입 200 대 → 대략 1:2 로 뽑힌다."""
    points, _ = _points_and_profile()

    drawn = sample_entry_offsets(20_000, 0, points, np.random.default_rng(0))

    assert np.mean(drawn == 0.0) == pytest.approx(1 / 3, abs=0.02)


def test_destination_is_never_upstream_of_entry() -> None:
    """진입 지점보다 앞에서 내릴 수는 없다."""
    points, profile = _points_and_profile()
    rng = np.random.default_rng(1)

    entry = sample_entry_offsets(5_000, 0, points, rng)
    dest = sample_exit_offsets(entry, 0, profile, rng, corridor_end_km=30.0)

    assert (dest >= entry).all()


def test_cars_entering_late_cannot_exit_at_earlier_boundaries() -> None:
    """10km 에서 들어온 차는 10km 경계에서 빠지지 않는다 (이미 지나온 곳)."""
    _, profile = _points_and_profile()
    rng = np.random.default_rng(2)

    dest = sample_exit_offsets(np.full(2_000, 10.0), 0, profile, rng, corridor_end_km=30.0)

    assert (dest > 10.0).all()


def test_survivors_reach_the_corridor_end() -> None:
    """어느 경계에서도 안 빠진 차의 목적지는 코리도 끝이다."""
    traffic = _traffic([(0.0, 10.0, 100.0), (10.0, 20.0, 100.0)])   # 진출 0
    profile = entry_exit_profile(traffic, period=PERIOD, direction=DIRECTION)

    dest = sample_exit_offsets(np.zeros(100), 0, profile, np.random.default_rng(0),
                               corridor_end_km=20.0)

    assert (dest == 20.0).all()


def test_same_seed_same_draw() -> None:
    points, profile = _points_and_profile()

    def draw():
        rng = np.random.default_rng(7)
        entry = sample_entry_offsets(500, 0, points, rng)
        return entry, sample_exit_offsets(entry, 0, profile, rng, corridor_end_km=30.0)

    first_entry, first_dest = draw()
    second_entry, second_dest = draw()

    assert np.array_equal(first_entry, second_entry)
    assert np.array_equal(first_dest, second_dest)


def test_hour_without_entry_is_refused() -> None:
    points, _ = _points_and_profile()

    with pytest.raises(ValueError, match="진입 지점이 없다"):
        sample_entry_offsets(10, 5, points, np.random.default_rng(0))
