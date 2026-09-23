"""T-05 / T-06 충전 인프라 적재 규칙. 네트워크 없이 돈다."""

from __future__ import annotations

import json

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
