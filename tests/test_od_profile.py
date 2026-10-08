"""실측 OD 로 목적지를 뽑는다 (#99).

여기서 지키는 것은 **"기점을 모르면 조용히 넘어가지 않는다"** 하나다. 엉뚱한 기점에
붙은 차는 **다른 사람의 목적지 분포**를 쓰고, 그 결과는 통행거리 평균 한 줄로만
보이기 때문에 알아채기 어렵다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from evdt.io.od_profile import MAX_SNAP_KM, load_od_profile, od_subset_for


def _od(rows: list[tuple[float, float, int]], *, period="holiday", direction="DOWN"):
    """(기점, 종점, 대수) 목록 → OD DataFrame."""
    return pd.DataFrame({
        "period": [period] * len(rows),
        "direction": [direction] * len(rows),
        "start_offset_km": [r[0] for r in rows],
        "end_offset_km": [r[1] for r in rows],
        "volume_veh": [r[2] for r in rows],
    })


def _write(tmp_path, df) -> str:
    p = tmp_path / "od.parquet"
    df.to_parquet(p, index=False)
    return str(p)


# -- 비율대로 뽑히는가 ------------------------------------------------------


def test_destinations_follow_the_measured_mix(tmp_path):
    """기점 하나에 목적지 둘, 3:1 이면 3:1 로 나와야 한다."""
    path = _write(tmp_path, _od([(10.0, 50.0, 300), (10.0, 200.0, 100)]))
    od = load_od_profile(path, "holiday", "DOWN")

    rng = np.random.default_rng(1)
    _, dest = od.sample(np.full(8000, 10.0), rng)

    near = float((dest == 50.0).mean())
    assert near == pytest.approx(0.75, abs=0.02), f"3:1 이 아니다: {near:.3f}"


def test_each_origin_keeps_its_own_mix(tmp_path):
    """**이것이 #99 의 전부다.** 기점마다 목적지 분포가 달라야 한다.

    옛 위험률 모델은 전원에게 같은 이탈 확률 곡선을 적용했다 — 실측으로 재 보니
    서울 진입차의 p50 이 16.8 km 인데 모델은 63.1 km 였다 (3.8배).
    """
    path = _write(tmp_path, _od([
        (10.0, 30.0, 1000),    # 가까운 기점은 짧게 간다
        (100.0, 400.0, 1000),  # 먼 기점은 길게 간다
    ]))
    od = load_od_profile(path, "holiday", "DOWN")
    rng = np.random.default_rng(2)

    _, near = od.sample(np.full(500, 10.0), rng)
    _, far = od.sample(np.full(500, 100.0), rng)

    assert set(near) == {30.0}
    assert set(far) == {400.0}


# -- 기점을 맞추는 것과 거부하는 것 ------------------------------------------


def test_entry_is_snapped_to_the_od_origin(tmp_path):
    """진입 위치를 기점으로 맞춘 값을 돌려준다 — 그래야 통행거리가 같은 자로 재진다.

    코리도 0 km(양재)와 서울TG(12.941)는 같은 진입을 다르게 부르는 것이다.
    """
    path = _write(tmp_path, _od([(12.941, 60.0, 100)]))
    od = load_od_profile(path, "holiday", "DOWN")

    entry, dest = od.sample(np.array([0.0]), np.random.default_rng(3))

    assert entry[0] == pytest.approx(12.941)
    assert dest[0] == pytest.approx(60.0)


def test_an_entry_too_far_from_any_origin_is_refused(tmp_path):
    """조용히 가까운 기점에 붙이면 **다른 기점의 분포**를 쓰게 된다."""
    path = _write(tmp_path, _od([(10.0, 60.0, 100)]))
    od = load_od_profile(path, "holiday", "DOWN")

    with pytest.raises(ValueError, match="맞출 수 없다"):
        od.sample(np.array([10.0 + MAX_SNAP_KM + 1.0]), np.random.default_rng(4))


def test_snap_report_uses_the_distance_before_snapping(tmp_path):
    """맞춘 값으로 거리를 재면 0 이 나와서 보고가 거짓 안심이 된다."""
    path = _write(tmp_path, _od([(12.941, 60.0, 100)]))
    od = load_od_profile(path, "holiday", "DOWN")

    assert "12.9" in od.describe_snap(np.array([0.0]))


# -- 기간·방향 -------------------------------------------------------------


def test_the_wrong_period_is_refused(tmp_path):
    path = _write(tmp_path, _od([(10.0, 60.0, 100)], period="holiday"))

    with pytest.raises(ValueError, match="normal"):
        load_od_profile(path, "normal", "DOWN")


def test_direction_is_filtered(tmp_path):
    """상행·하행을 섞으면 반대 방향의 목적지가 나온다."""
    path = _write(tmp_path, pd.concat([
        _od([(10.0, 60.0, 100)], direction="DOWN"),
        _od([(10.0, 400.0, 100)], direction="UP"),
    ], ignore_index=True))

    od = load_od_profile(path, "holiday", "UP")
    _, dest = od.sample(np.full(50, 10.0), np.random.default_rng(5))
    assert set(dest) == {400.0}


def test_missing_columns_are_named(tmp_path):
    bad = _od([(10.0, 60.0, 100)]).drop(columns=["volume_veh"])
    with pytest.raises(ValueError, match="volume_veh"):
        load_od_profile(_write(tmp_path, bad), "holiday", "DOWN")


# -- 시나리오 → OD 부분집합 --------------------------------------------------


class _Cfg:
    def __init__(self, scenario_id, corridor_id, period):
        self.scenario_id = scenario_id
        self.corridor_id = corridor_id
        self.demand = type("D", (), {"period": period})()


def test_subset_ignores_the_scenario_name():
    """**이름으로 짐작하면 틀린다.**

    `seollal_2026_down_base` 에는 "base" 가 들어 있다. 이름으로 기간을 고르면
    **설 연휴 시나리오가 평시 OD 를 쓴다** — #97 이 바로 그 실패였다.
    """
    cfg = _Cfg("seollal_2026_down_base", "gyeongbu_down", "seollal2026")
    assert od_subset_for(cfg) == ("holiday", "DOWN")


def test_subset_survives_a_variant_tag():
    """variant() 는 scenario_id 에 태그를 붙인다. period·corridor_id 는 안 바뀐다."""
    cfg = _Cfg("seollal_2026_down_base__od-up-test", "gyeongbu_down", "seollal2026")
    assert od_subset_for(cfg) == ("holiday", "DOWN")


def test_subset_reads_direction_from_the_corridor():
    cfg = _Cfg("anything", "gyeongbu_up", "base202603")
    assert od_subset_for(cfg) == ("normal", "UP")


def test_an_unknown_period_is_refused():
    """기간을 새로 추가했으면 PERIOD_ALIAS 에 같이 넣어야 한다."""
    cfg = _Cfg("chuseok", "gyeongbu_up", "chuseok2026")
    with pytest.raises(ValueError, match="chuseok2026"):
        od_subset_for(cfg)


# -- 조용한 축소를 막는다 (#62 hotfix) ---------------------------------------


def test_a_richer_od_is_refused_unless_the_caller_says_so(tmp_path):
    """**이것이 #62 hotfix 의 전부다.**

    #62 산출물은 `departure_hour` 와 `source` 를 들고 온다. 이 함수는 날짜·시간을
    전부 합쳐 일별 측정 OD 처럼 다루므로, 그냥 넣으면 시간 정보가 사라지고 추정
    통행이 섞인다 — **에러 없이.** 실제로 평균 통행거리가 40.50 → 39.52 km 로
    조용히 바뀌었다.
    """
    df = _od([(10.0, 60.0, 100)])
    df["departure_hour"] = 8
    df["source"] = "measured_tcs_daily"
    path = _write(tmp_path, df)

    with pytest.raises(ValueError, match="departure_hour"):
        load_od_profile(path, "holiday", "DOWN")

    od = load_od_profile(path, "holiday", "DOWN", accept_subset=True)
    assert len(od.origins) == 1


def test_rows_with_a_blank_period_are_refused(tmp_path):
    """NaN 은 `== want` 에서 조용히 떨어진다.

    #62 의 첫 산출물은 period 가 **보정일에서만** 비어 있어서, 이 필터가
    "실제로 적합한 날은 버리고 예측한 날만 남기는" 결과를 냈다.
    """
    df = pd.concat([
        _od([(10.0, 60.0, 100)]),
        _od([(10.0, 200.0, 900)]).assign(period=None),
    ], ignore_index=True)
    path = _write(tmp_path, df)

    with pytest.raises(ValueError, match="비어 있는"):
        load_od_profile(path, "holiday", "DOWN")


def test_a_plain_measured_od_still_loads_without_ceremony(tmp_path):
    """기존 파일은 그대로 돌아야 한다 — 가드가 일을 막으면 안 된다."""
    path = _write(tmp_path, _od([(10.0, 60.0, 100), (10.0, 200.0, 50)]))
    assert len(load_od_profile(path, "holiday", "DOWN").origins) == 1
