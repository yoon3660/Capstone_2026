"""CTM 셀 분할 (T-17).

완료조건 네 가지를 실제 데이터 없이 검사한다. 예전에는 셋이 scripts/seed_cells.py 의
런타임 검사에만 있어서, 차로 프로파일(SHP 필요)이 있는 사람이 스크립트를 돌릴 때만
작동했다. CI 에서는 아무도 확인하지 않았다.
"""

from __future__ import annotations

import pandas as pd
import pytest
import yaml

from evdt.paths import CONFIG_DIR
from evdt.world.cell_split import (
    assign_lanes_to_cells,
    build_direction_cells,
    cfl_min_cell_km,
    lane_change_offsets,
    place_anchors,
    split_anchor_intervals,
    station_cell_index,
)

L = 1.0          # 셀 목표 길이
CFL = 0.327      # 98.1 km/h × 0.2분


def _segments(rows):
    return [
        {"offset_km_start": a, "offset_km_end": b, "lanes": n, "lanes_source": src}
        for a, b, n, src in rows
    ]


def _corridor(length_km=40.0):
    """휴게소 4곳, 차로수 변경 5곳. 그중 둘은 휴게소·서로에 가깝다."""

    stations = [0.559, 12.0, 23.4, 31.0]
    lanes = _segments(
        [
            (0.0, 5.0, 4, "measured"),
            (5.0, 5.6, 3, "measured"),      # 0.6 km — 양쪽 변경 지점이 서로 가깝다
            (5.6, 12.3, 4, "measured"),     # 12.3 은 휴게소 12.0 과 0.3 km
            (12.3, 20.0, 3, "measured"),
            (20.0, length_km, 4, "assumed"),
        ]
    )
    return length_km, stations, lanes


def _build(length_km, stations, lanes, *, cell_length_km=L, cfl_floor_km=CFL):
    return build_direction_cells(
        length_km, stations, lanes, cell_length_km=cell_length_km, cfl_floor_km=cfl_floor_km
    )


# ---------------------------------------------------------------------------
# 완료조건 네 가지
# ---------------------------------------------------------------------------


def test_every_cell_respects_cfl_and_short_cells_only_sit_between_protected_anchors():
    """셀 길이 ≥ CFL 하한. 목표 L 보다 짧은 셀은 휴게소를 경계에 두기 위한 것뿐이다."""

    length_km, stations, lanes = _corridor()
    cells, _ = _build(length_km, stations, lanes)
    protected = {0.0, length_km, *stations}

    for c in cells:
        assert c["length_km"] >= CFL - 1e-9

        if c["length_km"] < L - 1e-9:
            assert c["offset_km_start"] in protected
            assert c["offset_km_end"] in protected


def test_cells_are_contiguous():
    """seq 순서대로 인접 셀의 끝 == 다음 셀의 시작."""

    length_km, stations, lanes = _corridor()
    cells, _ = _build(length_km, stations, lanes)

    assert cells[0]["offset_km_start"] == 0.0
    assert cells[-1]["offset_km_end"] == length_km

    for previous, current in zip(cells, cells[1:], strict=False):
        assert previous["offset_km_end"] == current["offset_km_start"]


def test_cell_lengths_sum_to_the_corridor_within_one_metre():
    length_km, stations, lanes = _corridor()
    cells, _ = _build(length_km, stations, lanes)

    assert abs(sum(c["length_km"] for c in cells) - length_km) < 0.001


def test_every_station_sits_exactly_on_a_cell_boundary():
    """셀 한가운데 진출입로가 있으면 CTM 이 유입·유출을 표현하지 못한다."""

    length_km, stations, lanes = _corridor()
    cells, _ = _build(length_km, stations, lanes)
    boundaries = {c["offset_km_start"] for c in cells} | {c["offset_km_end"] for c in cells}

    for offset in stations:
        assert offset in boundaries


# ---------------------------------------------------------------------------
# 서울만남의광장 — 코리도 끝에 붙은 휴게소
# ---------------------------------------------------------------------------


def test_station_near_the_corridor_end_gets_one_short_cell():
    """하행 기점에서 0.559 km. "모든 셀 ≥ L" 을 고집하면 분할 자체가 안 된다."""

    cells, _ = _build(10.0, [0.559], _segments([(0.0, 10.0, 4, "measured")]))

    assert cells[0]["offset_km_start"] == 0.0
    assert cells[0]["offset_km_end"] == 0.559
    assert all(c["length_km"] >= L - 1e-9 for c in cells[1:])


def test_protected_anchors_closer_than_cfl_are_rejected():
    """CFL 하한보다 짧은 셀은 물리적으로 불가능하다. 여기서는 멈춘다."""

    with pytest.raises(ValueError, match="CFL 하한"):
        _build(10.0, [0.2], _segments([(0.0, 10.0, 4, "measured")]))


def test_target_length_below_cfl_is_rejected():
    with pytest.raises(ValueError, match="CFL 하한"):
        place_anchors(10.0, [5.0], [], cell_length_km=0.2, cfl_floor_km=CFL)


def test_station_outside_the_corridor_is_rejected():
    """이정축이 어긋난 데이터(docs/debug_lanes_log.md §3-①)가 여기서 걸린다."""

    with pytest.raises(ValueError, match="노선 밖"):
        place_anchors(10.0, [12.0], [], cell_length_km=L, cfl_floor_km=CFL)


# ---------------------------------------------------------------------------
# 차로수 변경 지점
# ---------------------------------------------------------------------------


def test_lane_changes_far_from_other_anchors_become_boundaries():
    anchors, dropped = place_anchors(
        20.0, [10.0], [4.0, 15.0], cell_length_km=L, cfl_floor_km=CFL
    )

    assert anchors == [0.0, 4.0, 10.0, 15.0, 20.0]
    assert dropped == []


def test_lane_change_too_close_to_a_station_is_dropped_and_the_station_stays():
    anchors, dropped = place_anchors(
        20.0, [10.0], [10.3], cell_length_km=L, cfl_floor_km=CFL
    )

    assert 10.0 in anchors
    assert dropped == [10.3]


def test_two_close_lane_changes_keep_the_first_and_drop_the_second():
    """예전에는 여기서 예외를 던졌다 (T-09b 의 0.501 km 간격이 실제로 있다).

    둘 중 어느 쪽을 버려도 그 셀의 차로수는 걸친 구간 중 적은 쪽이 된다.
    왼쪽부터 훑으므로 결과는 결정적이다.
    """

    anchors, dropped = place_anchors(
        20.0, [], [5.0, 5.501], cell_length_km=L, cfl_floor_km=CFL
    )

    assert 5.0 in anchors
    assert dropped == [5.501]


def test_dropped_boundary_gives_the_cell_the_smaller_lane_count():
    """용량을 크게 잡는 쪽으로 틀리면 실측 교통량이 잡아내지 못한다."""

    length_km, stations, lanes = _corridor()
    cells, dropped = _build(length_km, stations, lanes)

    assert 5.6 in dropped            # 5.0 과 0.6 km
    assert 12.3 in dropped           # 휴게소 12.0 과 0.3 km

    straddling = next(c for c in cells if c["offset_km_start"] < 5.3 < c["offset_km_end"])
    assert straddling["lanes"] == 3
    assert straddling["lanes_source"] == "assumed"


def test_lane_change_offsets_come_from_the_profile():
    lanes = _segments(
        [(0.0, 2.0, 4, "measured"), (2.0, 3.0, 4, "assumed"), (3.0, 5.0, 3, "measured")]
    )

    # 출처만 다른 경계는 차로수 변경이 아니다
    assert lane_change_offsets(lanes) == [3.0]


# ---------------------------------------------------------------------------
# 등분
# ---------------------------------------------------------------------------


def test_split_evenly():
    cells = split_anchor_intervals([0.0, 5.3], cell_length_km=L, cfl_floor_km=CFL)

    assert len(cells) == 5
    assert all(b - a >= L - 1e-9 for a, b in cells)
    assert cells[-1][1] == 5.3


def test_short_gap_becomes_one_cell():
    cells = split_anchor_intervals([0.0, 0.559, 3.0], cell_length_km=L, cfl_floor_km=CFL)

    assert cells[0] == (0.0, 0.559)


# ---------------------------------------------------------------------------
# 셀 수 — "크게 벗어나면 앵커를 의심한다"
# ---------------------------------------------------------------------------


def test_cell_count_follows_the_target_length_not_the_lane_merge_threshold():
    """0.5 km(차로 병합 임계값)로 쪼개면 셀이 두 배가 된다. 그 사고의 회귀 테스트.

    floor(g / L) 등분이라 셀은 [L, 2L) 에 들어가고, 개수는 length / (2L) 과
    length / L 사이다. 415 km 에 L = 1.0 이면 방향당 약 400 셀이다.
    """

    length_km = 415.058
    stations = [9.7 + 26.0 * i for i in range(16)]
    lanes = _segments([(0.0, length_km, 4, "measured")])

    at_target, _ = _build(length_km, stations, lanes, cell_length_km=1.0)
    at_merge_threshold, _ = _build(length_km, stations, lanes, cell_length_km=0.5)

    assert length_km / 2.0 < len(at_target) <= length_km / 1.0
    assert len(at_merge_threshold) > 1.9 * len(at_target)


# ---------------------------------------------------------------------------
# 차로수 배정 (팀원 원본 테스트 유지)
# ---------------------------------------------------------------------------


def test_assign_smaller_lane_count_when_boundary_removed():
    segments = _segments([(0.0, 1.0, 3, "measured"), (1.0, 3.0, 2, "measured")])
    result = assign_lanes_to_cells([(0.5, 1.193), (1.193, 1.8)], segments)

    assert result[0]["lanes"] == 2
    assert result[0]["lanes_source"] == "assumed"
    assert result[1]["lanes"] == 2
    assert result[1]["lanes_source"] == "measured"


def test_keep_assumed_source():
    result = assign_lanes_to_cells([(0.0, 1.0)], _segments([(0.0, 2.0, 3, "assumed")]))

    assert result[0]["lanes"] == 3
    assert result[0]["lanes_source"] == "assumed"


def test_lane_profile_must_cover_every_cell():
    with pytest.raises(ValueError, match="덮지 않습니다"):
        assign_lanes_to_cells([(0.0, 3.0)], _segments([(0.0, 2.0, 3, "measured")]))


# ---------------------------------------------------------------------------
# config — 하드코딩이 아니라 실제 설정값으로
# ---------------------------------------------------------------------------


def test_config_cell_length_is_separate_from_the_lane_merge_threshold():
    """같은 키 하나로 읽다가 셀이 전부 0.5 km 가 됐다. 두 값은 따로 있어야 한다."""

    defaults = yaml.safe_load((CONFIG_DIR / "flow_params.yaml").read_text(encoding="utf-8"))[
        "defaults"
    ]

    assert "cell_length_km" in defaults
    assert float(defaults["cell_length_km"]) > float(defaults["min_cell_length_km"])


@pytest.mark.parametrize("direction", ["UP", "DOWN"])
def test_config_cell_length_clears_the_cfl_floor(direction):
    config = yaml.safe_load((CONFIG_DIR / "flow_params.yaml").read_text(encoding="utf-8"))
    params = config[direction]

    floor = cfl_min_cell_km(
        float(params["v_free_kmh"]["value"]),
        float(params["w_back_kmh"]["value"]),
        float(config["dt_min"]),
    )

    assert float(config["defaults"]["cell_length_km"]) >= floor


# ---------------------------------------------------------------------------
# 가짜 휴게소가 진짜 코리도에 섞이면 멈춘다
# ---------------------------------------------------------------------------


def test_smoke_station_in_a_real_corridor_is_refused(seeded_db):
    """smoke_run.py 가 예전에 gyeongbu_down 에 넣던 가짜 휴게소. 셀 앵커가 됐었다."""

    from evdt.io.db import get_conn, upsert_df
    from evdt.io.stations import SMOKE_SOURCE, read_station_chargers

    with get_conn(seeded_db) as conn:
        upsert_df(
            conn,
            "station",
            pd.DataFrame(
                [
                    {
                        "station_id": "smoke_cheonan", "corridor_id": "gyeongbu_down",
                        "name": "천안휴게소(가짜)", "direction": "DOWN", "offset_km": 92.0,
                        "lat": 36.82, "lon": 127.14, "source": SMOKE_SOURCE,
                    }
                ]
            ),
        )

    with get_conn(seeded_db, readonly=True) as conn:
        with pytest.raises(RuntimeError, match="smoke_clean"):
            read_station_chargers(conn, corridor_id="gyeongbu_down")


def test_clean_real_corridor_reads_normally(seeded_db):
    from evdt.io.db import get_conn
    from evdt.io.stations import read_station_chargers

    with get_conn(seeded_db, readonly=True) as conn:
        stations, chargers = read_station_chargers(conn, corridor_id="gyeongbu_down")

    assert [s["station_id"] for s in stations] == ["st_anseong"]
    assert [c["n_units"] for c in chargers] == [4]


# ---------------------------------------------------------------------------
# 휴게소 → 셀 매핑 (station.cell_id)
# ---------------------------------------------------------------------------


def test_station_maps_to_the_cell_that_starts_at_it():
    """경계에 있는 휴게소는 하류 셀(합류하는 곳)에 붙는다."""

    cells, _ = _build(10.0, [4.0], _segments([(0.0, 10.0, 4, "measured")]))
    index = station_cell_index(cells, 4.0)

    assert cells[index]["offset_km_start"] == 4.0


def test_station_at_the_corridor_end_maps_to_the_last_cell():
    cells, _ = _build(10.0, [10.0], _segments([(0.0, 10.0, 4, "measured")]))

    assert station_cell_index(cells, 10.0) == len(cells) - 1


def test_station_off_any_boundary_is_rejected():
    cells, _ = _build(10.0, [4.0], _segments([(0.0, 10.0, 4, "measured")]))

    with pytest.raises(ValueError, match="경계"):
        station_cell_index(cells, 4.5)


def test_writing_cells_without_station_mapping_breaks_validate_master(seeded_db):
    """셀만 저장하고 station.cell_id 를 비워 두면 validate_master 가 실패한다.

    seed_vehicles.py 가 validate_master 를 부르므로, 셀을 처음 저장한 뒤부터
    차종 적재가 깨졌다. seed_cells.py 가 매핑까지 같이 쓰는 이유다.
    """

    from evdt.io.db import get_conn, upsert_df, validate_master
    from evdt.io.flow_params import q_per_lane

    length_km = 416.0
    cells, _ = _build(length_km, [62.0], _segments([(0.0, length_km, 4, "measured")]))
    q_lane = q_per_lane(100.0, 18.0, 144.0)
    rows = pd.DataFrame(
        [
            {
                "cell_id": f"gyeongbu_down_{i:04d}", "corridor_id": "gyeongbu_down", "seq": i,
                "offset_km_start": c["offset_km_start"], "offset_km_end": c["offset_km_end"],
                "length_km": c["length_km"], "lanes": c["lanes"], "lanes_source": c["lanes_source"],
                "v_free_kmh": 100.0, "w_back_kmh": 18.0, "k_jam_veh_km_lane": 144.0,
                "q_max_veh_h": c["lanes"] * q_lane,
                "lat_start": 36.0, "lon_start": 127.0, "lat_end": 36.0, "lon_end": 127.0,
            }
            for i, c in enumerate(cells)
        ]
    )

    with get_conn(seeded_db) as conn:
        upsert_df(conn, "cell", rows)
        before = validate_master(conn)

        index = station_cell_index(cells, 62.0)
        conn.execute(
            "UPDATE station SET cell_id = ? WHERE station_id = ?",
            (f"gyeongbu_down_{index:04d}", "st_anseong"),
        )
        after = validate_master(conn)

    assert any("st_anseong" in p and "매핑" in p for p in before)
    assert after == []
