"""장거리 통행 섞기 (#54).

핵심은 하나다: **얼마를 섞든 합치면 실측 구간 교통량이 그대로 나와야 한다.**
그러지 않으면 자료와 모순되는 수요를 만드는 것이다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from evdt.io.long_distance import (
    SYNTHETIC_SOURCE,
    InfeasibleShare,
    max_feasible_share,
    split,
)

END_KM = 400.0


def _volume(sections: dict[float, float], *, hours: int = 24) -> pd.DataFrame:
    """구간마다 시간당 같은 교통량. sections: {offset_km_start: 시간당 대수}."""
    return pd.DataFrame([
        {"hour": h, "offset_km_start": km, "volume_veh": veh}
        for h in range(hours)
        for km, veh in sections.items()
    ])


FLAT = {0.0: 1000.0, 100.0: 1000.0, 200.0: 1000.0, 300.0: 1000.0}


# ---------------------------------------------------------------------------
# 상한 — 자료가 담을 수 있는 최대치
# ---------------------------------------------------------------------------


def test_upper_bound_is_the_thinnest_section() -> None:
    """완주 통행은 모든 구간을 지나므로 가장 얇은 구간이 상한이다."""
    volume = _volume({0.0: 1000.0, 100.0: 300.0, 200.0: 800.0, 300.0: 900.0})

    assert max_feasible_share(volume, min_trip_km=END_KM, corridor_end_km=END_KM) == pytest.approx(0.3)


def test_shorter_trips_have_a_looser_bound() -> None:
    """짧은 통행은 얇은 구간을 안 지날 수도 있으니 상한이 느슨하다."""
    volume = _volume({0.0: 1000.0, 100.0: 300.0, 200.0: 800.0, 300.0: 900.0})

    short = max_feasible_share(volume, min_trip_km=100.0, corridor_end_km=END_KM)
    whole = max_feasible_share(volume, min_trip_km=END_KM, corridor_end_km=END_KM)

    assert short > whole


# ---------------------------------------------------------------------------
# 합치면 실측이 나온다
# ---------------------------------------------------------------------------


def test_long_plus_residual_equals_the_measurement() -> None:
    """이 모듈의 존재 이유. 장거리를 얼마나 섞든 실측과 모순되면 안 된다."""
    volume = _volume(FLAT)
    result = split(volume, share=0.1, min_trip_km=100.0, corridor_end_km=END_KM)

    measured = volume["volume_veh"].sum()
    residual = result.residual_volume["volume_veh"].sum()

    # 장거리 통행이 실제로 얹은 양 = 실측 − 잔차
    assert measured - residual > 0
    assert result.n_long_veh > 0


def test_residual_never_goes_negative() -> None:
    volume = _volume(FLAT)

    result = split(volume, share=0.2, min_trip_km=100.0, corridor_end_km=END_KM)

    assert (result.residual_volume["volume_veh"] >= 0).all()


def test_zero_share_leaves_the_measurement_alone() -> None:
    volume = _volume(FLAT)

    result = split(volume, share=0.0, min_trip_km=100.0, corridor_end_km=END_KM)

    assert result.long_trips.empty
    assert result.residual_volume["volume_veh"].sum() == pytest.approx(volume["volume_veh"].sum())


# ---------------------------------------------------------------------------
# 담을 수 없으면 거부하고, 얼마까지 되는지 알려준다
# ---------------------------------------------------------------------------


def test_too_much_long_distance_is_refused_with_the_limit() -> None:
    """조용히 잘라내면 실측과 다른 수요가 된다. 멈추고 최대치를 말한다."""
    volume = _volume({0.0: 1000.0, 100.0: 100.0, 200.0: 100.0, 300.0: 100.0})

    with pytest.raises(InfeasibleShare, match="최대 비중"):
        split(volume, share=0.9, min_trip_km=300.0, corridor_end_km=END_KM)


def test_share_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError, match="0 과 1 사이"):
        split(_volume(FLAT), share=1.5, min_trip_km=100.0, corridor_end_km=END_KM)


# ---------------------------------------------------------------------------
# 만든 통행은 만든 것이라고 표시한다
# ---------------------------------------------------------------------------


def test_generated_trips_are_labelled() -> None:
    """실측과 절대 섞이지 않게 표시를 붙인다 (docs/fake_data_audit.md)."""
    result = split(_volume(FLAT), share=0.1, min_trip_km=100.0, corridor_end_km=END_KM)

    assert (result.long_trips["source"] == SYNTHETIC_SOURCE).all()


def test_generated_trips_are_long_enough() -> None:
    result = split(_volume(FLAT), share=0.1, min_trip_km=200.0, corridor_end_km=END_KM)
    trips = result.long_trips

    assert (trips["dest_offset_km"] - trips["entry_offset_km"] >= 200.0).all()


# ---------------------------------------------------------------------------
# 통행시간 지연
# ---------------------------------------------------------------------------


def test_far_sections_are_charged_later_than_departure() -> None:
    """8시에 출발한 차는 먼 구간에 8시가 아니라 나중에 나타난다."""
    volume = _volume(FLAT, hours=24)

    slow = split(volume, share=0.05, min_trip_km=300.0, corridor_end_km=END_KM, cruise_kmh=50.0)
    fast = split(volume, share=0.05, min_trip_km=300.0, corridor_end_km=END_KM, cruise_kmh=200.0)

    far_slow = slow.residual_volume.query("offset_km_start == 300.0").set_index("hour")["volume_veh"]
    far_fast = fast.residual_volume.query("offset_km_start == 300.0").set_index("hour")["volume_veh"]

    # 느릴수록 늦게 도착하므로 이른 시간대의 잔차가 더 많이 남는다
    assert far_slow.loc[1] > far_fast.loc[1]
