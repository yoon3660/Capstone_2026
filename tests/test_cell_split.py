import pytest

from evdt.world.cell_split import (
    assign_lanes_to_cells,
    resolve_short_anchor_gaps,
    split_anchor_intervals,
)


def test_split_evenly():
    cells = split_anchor_intervals([0.0, 5.3], 0.5)

    assert len(cells) == 10
    assert all(
        end - start >= 0.5 - 1e-9
        for start, end in cells
    )


def test_preserve_anchors():
    cells = split_anchor_intervals([0.0, 2.0, 5.3], 0.5)

    assert cells[0][0] == pytest.approx(0.0)
    assert cells[-1][1] == pytest.approx(5.3)
    assert any(end == pytest.approx(2.0) for _, end in cells)


def test_cells_are_continuous():
    cells = split_anchor_intervals([0.0, 2.0, 5.3], 0.5)

    for previous, current in zip(cells, cells[1:], strict=False):
        assert previous[1] == pytest.approx(current[0])


def test_reject_short_anchor_gap():
    with pytest.raises(ValueError, match="너무 짧습니다"):
        split_anchor_intervals([0.0, 0.193, 2.0], 0.5)


def test_keep_station_and_remove_nearby_lane_change():
    anchors, removed = resolve_short_anchor_gaps(
        length_km=3.0,
        station_offsets=[1.193],
        lane_change_offsets=[1.0],
        min_cell_length_km=0.5,
    )

    assert 1.193 in anchors
    assert 1.0 not in anchors
    assert removed == [1.0]
    assert all(
        b - a >= 0.5 - 1e-9
        for a, b in zip(anchors, anchors[1:], strict=False)
    )


def test_reject_two_close_stations():
    with pytest.raises(ValueError, match="보호된 앵커"):
        resolve_short_anchor_gaps(
            length_km=3.0,
            station_offsets=[1.0, 1.2],
            lane_change_offsets=[],
            min_cell_length_km=0.5,
        )


def test_assign_smaller_lane_count_when_boundary_removed():
    segments = [
        {
            "offset_km_start": 0.0,
            "offset_km_end": 1.0,
            "lanes": 3,
            "lanes_source": "measured",
        },
        {
            "offset_km_start": 1.0,
            "offset_km_end": 3.0,
            "lanes": 2,
            "lanes_source": "measured",
        },
    ]

    cells = [
        (0.5, 1.193),
        (1.193, 1.8),
    ]

    result = assign_lanes_to_cells(cells, segments)

    # 첫 번째 셀은 3차로와 2차로에 걸치므로 2차로 적용
    assert result[0]["lanes"] == 2
    assert result[0]["lanes_source"] == "assumed"

    # 두 번째 셀은 원본 2차로 구간 안에만 있음
    assert result[1]["lanes"] == 2
    assert result[1]["lanes_source"] == "measured"


def test_keep_assumed_source():
    segments = [
        {
            "offset_km_start": 0.0,
            "offset_km_end": 2.0,
            "lanes": 3,
            "lanes_source": "assumed",
        },
    ]

    result = assign_lanes_to_cells([(0.0, 1.0)], segments)

    assert result[0]["lanes"] == 3
    assert result[0]["lanes_source"] == "assumed"