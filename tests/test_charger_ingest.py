"""T-05 / T-06 충전 인프라 적재 규칙. 네트워크 없이 돈다."""

from __future__ import annotations

import json
import math

import pytest

from evdt.io import charger_ingest as ci
from evdt.io.moe_codes import CHGER_TYPE


def _write_page(path, items):
    path.write_text(
        json.dumps({"items": {"item": items}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_deleted_chargers_are_dropped_by_default(tmp_path):
    _write_page(
        tmp_path / "page_0001.json",
        [
            {"statId": "A", "chgerId": "01", "delYn": "N"},
            {"statId": "A", "chgerId": "02", "delYn": "Y"},
            {"statId": "A", "chgerId": "03"},
        ],
    )

    kept = ci.load_raw_chargers(tmp_path)
    everything = ci.load_raw_chargers(tmp_path, include_deleted=True)

    assert [c["chgerId"] for c in kept] == ["01", "03"]
    assert len(everything) == 3


def test_charger_type_map_covers_guide_codes():
    assert set(CHGER_TYPE) <= set(ci.CHARGER_TYPE_MAP)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("김천(부산) 휴게소", ("김천휴게소", "DOWN")),
        ("김천(서울)휴게소", ("김천휴게소", "UP")),
        ("워터 칠곡휴게소 서울방향", ("칠곡휴게소", "UP")),
        ("안성휴게소(부산방향) 전기차충전소", ("안성휴게소", "DOWN")),
        ("기흥복합휴게소", ("기흥복합휴게소", None)),
    ],
)
def test_station_match_key(raw, expected):
    assert ci.station_match_key(raw) == expected


def _area(name, lat, lon):
    return {
        "unitName": name,
        "yValue": str(lat),
        "xValue": str(lon),
        "serviceAreaCode": "A0001",
    }


def test_same_name_far_away_is_rejected():
    areas = [_area("김천(부산)휴게소", 36.129, 128.165)]
    near = {"statNm": "김천(부산) 휴게소", "lat": "36.130", "lng": "128.164"}
    far = {"statNm": "김천(부산) 휴게소", "lat": "35.500", "lng": "127.000"}

    matched, _, _ = ci.filter_gyeongbu_chargers([near], areas)
    assert matched == [near]

    with pytest.raises(RuntimeError, match="좌표가"):
        ci.filter_gyeongbu_chargers([far], areas)


def _ic(lat, lon):
    return {"yValue": str(lat), "xValue": str(lon)}


def _arc_route(origin, n=40):
    """기점에서 북쪽으로 올라갔다가 기점을 중심으로 서쪽으로 도는 노선.

    경부선 영천~경산처럼 기점 직선거리가 거의 그대로이거나 줄어드는 구간이
    생긴다. 직선거리로 정렬하면 이 구간에서 순서가 뒤집힌다.
    """

    lat0, lon0 = origin
    points = [(lat0 + 0.07 * i, lon0) for i in range(1, 10)]   # 북쪽 약 70km

    for i in range(1, n + 1):
        theta = math.radians(90 + 100 * i / n)                  # 북 → 서남서
        radius = 0.63 - 0.05 * math.sin(math.pi * i / n)        # 가운데서 살짝 안쪽
        points.append(
            (
                lat0 + radius * math.sin(theta),
                lon0 + radius * math.cos(theta) / math.cos(math.radians(lat0)),
            )
        )

    return points


def test_offset_follows_route_not_chord():
    up_origin = (35.00, 129.00)
    route = _arc_route(up_origin)
    down_origin = route.pop()
    origins = {"UP": up_origin, "DOWN": down_origin}

    # 호 구간에서 기점 직선거리가 실제로 줄어드는지 (테스트 전제 확인)
    chords = [ci.haversine_km(*up_origin, *p) for p in route]
    assert any(b < a for a, b in zip(chords, chords[1:], strict=False))

    station_idx = [5, 15, 22, 30, 40]
    stations = [
        {"name": f"s{i}", "direction": "UP", "lat": route[i][0], "lon": route[i][1]}
        for i in station_idx
    ]
    stations.append(
        {"name": "s_down", "direction": "DOWN", "lat": route[22][0], "lon": route[22][1]}
    )
    waypoints = [
        _ic(lat, lon) for i, (lat, lon) in enumerate(route) if i not in station_idx
    ]
    waypoints.append(_ic(*route[22]))   # 휴게소와 같은 좌표
    waypoints.append(_ic(*route[3]))    # 중복 IC

    result = {s["name"]: s for s in ci.add_offset_km(stations, origins, waypoints)}

    true_milepost = [0.0]
    prev = up_origin
    for point in route:
        true_milepost.append(true_milepost[-1] + ci.haversine_km(*prev, *point))
        prev = point
    length = true_milepost[-1] + ci.haversine_km(*prev, *down_origin)

    for i in station_idx:
        assert result[f"s{i}"]["offset_km"] == pytest.approx(true_milepost[i + 1], abs=0.01)

    # 같은 지점이면 상행 + 하행 = 총연장
    same_point = result["s_down"]["offset_km"] + result["s22"]["offset_km"]
    assert same_point == pytest.approx(length, abs=0.01)


def test_off_route_waypoint_is_ignored():
    origins = {"UP": (35.0, 129.0), "DOWN": (36.0, 129.0)}
    stations = [{"name": "mid", "direction": "UP", "lat": 35.5, "lon": 129.0}]
    on_route = [_ic(35.25, 129.0), _ic(35.75, 129.0)]
    stray = [_ic(35.4, 128.5)]   # 노선에서 45km 떨어진 IC (좌표 오류)

    clean = ci.add_offset_km(stations, origins, on_route)
    noisy = ci.add_offset_km(stations, origins, on_route + stray)

    assert noisy[0]["offset_km"] == pytest.approx(clean[0]["offset_km"])


def test_official_direction_fills_names_without_direction():
    areas = [
        {**_area("옥천만남휴게소", 36.309, 127.571), "stdRestCd": "000404"},
        {**_area("김천(부산)휴게소", 36.129, 128.165), "stdRestCd": "000031"},
        {**_area("어딘가쉼터", 36.0, 128.0), "stdRestCd": "999999"},
    ]
    official = {"000404": "DOWN", "000031": "DOWN"}

    result = ci.apply_official_directions(areas, official)

    assert [a["direction"] for a in result] == ["DOWN", "DOWN", None]
    assert "direction" not in areas[0]   # 원본은 건드리지 않는다


def test_official_direction_conflict_stops():
    areas = [{**_area("김천(부산)휴게소", 36.129, 128.165), "stdRestCd": "000031"}]

    with pytest.raises(RuntimeError, match="공식 구분"):
        ci.apply_official_directions(areas, {"000031": "UP"})


def test_charger_without_direction_matches_only_unique_name():
    known = {
        ("옥천만남휴게소", "DOWN"),
        ("칠곡휴게소", "DOWN"),
        ("칠곡휴게소", "UP"),
        ("서울하이패스센터쉼터", "DOWN"),
    }

    assert ci.resolve_charger_key("옥천만남 휴게소", known) == ("옥천만남휴게소", "DOWN")
    assert ci.resolve_charger_key("서울하이패스센터(입구)", known) == (
        "서울하이패스센터쉼터",
        "DOWN",
    )
    # 상·하행이 모두 있는 이름은 어느 쪽인지 알 수 없다
    assert ci.resolve_charger_key("칠곡휴게소", known) is None
    # 이름에 방향이 있으면 정확히 같아야 한다
    assert ci.resolve_charger_key("옥천만남(서울)휴게소", known) is None


def test_charger_without_direction_is_loaded_end_to_end():
    areas = ci.apply_official_directions(
        [{**_area("옥천만남휴게소", 36.309, 127.571), "stdRestCd": "000404"}],
        {"000404": "DOWN"},
    )
    charger = {
        "statNm": "옥천만남 휴게소",
        "statId": "ME178016",
        "chgerId": "01",
        "lat": "36.3079",
        "lng": "127.5731",
        "output": "50",
        "chgerType": "06",
    }

    matched, unmatched, unknown = ci.filter_gyeongbu_chargers([charger], areas)
    stations = ci.build_station_candidates(matched, areas)
    groups = ci.build_charger_candidates(matched, stations)

    assert (unmatched, unknown) == ([], [])
    assert [(s["name"], s["direction"]) for s in stations] == [("옥천만남휴게소", "DOWN")]
    assert groups[0]["n_units"] == 1
