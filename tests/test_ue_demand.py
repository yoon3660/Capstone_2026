"""UE 수요 입력 — 충전 계획 선택지, 목적지 분포, 설정, 그래프."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest
import yaml

from evdt.config import ConfigError, ScenarioConfig
from evdt.engine.ue_demand import ChargeRule, build_trip_demands, enumerate_plans
from evdt.io.demand_profile import (
    entry_hourly_volume,
    entry_zone,
    peak_date,
    sample_dest_offsets,
    through_share,
)
from evdt.viz.plots import plot_ue_gap

RULE = ChargeRule(range_factor=1.0, buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8, max_stops=3)
# 배터리 100 kWh, 전비 0.2 kWh/km → SoC 0.1 = 50 km
CAR = dict(battery_kwh=100.0, consumption_kwh_km=0.2)
STATIONS = [{"station_id": s, "offset_km": km} for s, km in
            [("s50", 50.0), ("s150", 150.0), ("s250", 250.0), ("s350", 350.0)]]


def _plans(soc0: float, dest: float, stations=STATIONS, rule=RULE):
    return enumerate_plans(stations, entry_offset_km=0.0, dest_offset_km=dest, soc0=soc0, rule=rule, **CAR)


# ---------------------------------------------------------------------------
# 충전 계획
# ---------------------------------------------------------------------------


def test_car_that_reaches_destination_does_not_enter_ue():
    # 100 km + 여유 SoC 0.2 → 필요 SoC 0.4
    assert _plans(0.45, 100.0) is None


def test_plans_have_the_fewest_stops_possible():
    """두 번이면 되는 차에게 세 번 서는 계획을 주지 않는다.

    SoC 0.3 → s50 까지만 닿는다. s50 에서 0.8 로 채워도 340 km + 여유 0.2 = 0.88 이 필요 → 2회.
    """
    plans = _plans(0.3, 390.0)

    assert plans
    assert {len(p.stops) for p in plans} == {2}


def test_every_leg_keeps_the_buffer_and_soc_rises_at_each_stop():
    for p in _plans(0.3, 390.0):
        prev_km, soc = 0.0, 0.3
        for s in p.stops:
            used = (s.offset_km - prev_km) * 0.2 / 100.0
            assert s.soc_in == pytest.approx(soc - used)
            assert soc - (s.offset_km - prev_km + 30.0) * 0.2 / 100.0 >= -1e-9   # 버퍼를 남기고 닿는다
            assert s.soc_in < s.soc_out <= 0.8
            prev_km, soc = s.offset_km, s.soc_out


def test_low_battery_car_is_captive_to_the_first_station():
    """SoC 0.25 로는 50 km 휴게소 (+30 km 버퍼 = SoC 0.16) 까지만 닿는다 → 쏠림의 씨앗."""
    plans = _plans(0.25, 200.0)

    assert [p.station_ids[0] for p in plans] == ["s50"]


def test_impossible_trip_returns_empty():
    assert _plans(0.25, 390.0, rule=ChargeRule(1.0, 30.0, 0.2, 0.8, max_stops=1)) == ()


def test_build_counts_every_car_once():
    evs = [
        {"ev_id": "a", "vclass_id": "v", "entry_time_min": 0.0, "initial_soc": 0.45, "dest_offset_km": 100.0},
        {"ev_id": "b", "vclass_id": "v", "entry_time_min": 1.0, "initial_soc": 0.5, "dest_offset_km": 390.0},
        {"ev_id": "c", "vclass_id": "v", "entry_time_min": 2.0, "initial_soc": 0.1, "dest_offset_km": 390.0},
    ]
    built = build_trip_demands(
        evs, STATIONS, {"v": {"battery_kwh": 100.0, "vmax_kw": 150.0, "consumption_kwh_km": 0.2}},
        {"v": ((0.0, 1.0, 150.0),)}, rule=ChargeRule(1.0, 30.0, 0.2, 0.8, max_stops=2), charge_power_factor=0.6,
    )

    assert (built.n_ev, built.n_no_charge, built.n_infeasible) == (3, 1, 1)
    assert [t.ev_id for t in built.trips] == ["b"]
    assert built.trips[0].cold_factor == 0.6


# ---------------------------------------------------------------------------
# 수요 프로파일
# ---------------------------------------------------------------------------


def _traffic() -> pd.DataFrame:
    rows = []
    zones = [("z0", 3.0, 1000.0), ("z1", 50.0, 600.0), ("z2", 80.0, 700.0), ("z3", 150.0, 300.0), ("zx", 120.0, None)]
    for day, scale in (("2026-02-13", 1.0), ("2026-02-14", 1.2)):
        for zid, km, daily in zones:
            for h in range(24):
                rows.append({
                    "period": "p", "direction": "DOWN", "date": pd.Timestamp(day), "hour": h,
                    "conzone_id": zid, "offset_km": km,
                    "volume_veh": None if daily is None else daily * scale / 24,
                })
    return pd.DataFrame(rows)


def test_entry_zone_skips_missing_and_peak_day_is_busiest():
    t = _traffic()
    assert entry_zone(t, period="p", direction="DOWN") == "z0"
    assert peak_date(t, period="p", direction="DOWN") == pd.Timestamp("2026-02-14")


def test_entry_volume_has_24_hours():
    v = entry_hourly_volume(_traffic(), period="p", direction="DOWN")
    assert v["hour"].tolist() == list(range(24))
    assert v["volume_veh"].sum() == pytest.approx(1200.0)


def test_through_share_never_rises():
    """도시 근처에서 교통량이 다시 늘어도(z2 700 > z1 600) 지나가는 비율은 늘지 않는다."""
    s = through_share(_traffic(), period="p", direction="DOWN")

    assert s["share"].tolist() == pytest.approx([1.0, 0.6, 0.6, 0.3])
    assert 120.0 not in s["offset_km"].tolist()     # 결측 콘존은 뺀다


def test_sampled_destinations_follow_the_share():
    s = through_share(_traffic(), period="p", direction="DOWN")
    dest = sample_dest_offsets(20_000, s, np.random.default_rng(0), corridor_end_km=400.0)

    assert (dest > 50.0).mean() == pytest.approx(0.6, abs=0.02)
    assert (dest >= 400.0).mean() == pytest.approx(0.3, abs=0.02)
    assert dest.min() >= 3.0


# ---------------------------------------------------------------------------
# 설정 · 그래프
# ---------------------------------------------------------------------------


def test_ue_settings_come_from_config(cfg: ScenarioConfig):
    assert cfg.policy.ue.max_iter == 50
    # 3% 는 첫 sweep 에 통과해 "균형" 이라 부를 수 없었다 → 0.8% 로 조였다 (#54)
    assert cfg.policy.ue.gap_tol == 0.008
    assert cfg.demand.through_profile.endswith("through_gyeongbu_down_seollal.csv")


def test_unknown_ue_key_is_rejected(cfg: ScenarioConfig, tmp_path):
    data = yaml.safe_load(cfg.raw_yaml)
    bad = copy.deepcopy(data)
    bad["policy"]["ue"]["gap_tolerance"] = 0.1
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError, match="policy.ue.gap_tolerance"):
        ScenarioConfig.from_yaml(path)


def test_gap_plot_is_written(tmp_path):
    log = pd.DataFrame({"iteration": [0, 1, 2], "rel_gap": [0.8, 0.02, 0.0], "mip_gap": [None] * 3})
    out = plot_ue_gap(log, tmp_path / "gap.png", gap_tol=0.03)

    assert out.is_file() and out.stat().st_size > 1000


# ---------------------------------------------------------------------------
# 출발 SoC 실험 스위치 (vehicles.departure_soc / soc_profiles) · variant
# ---------------------------------------------------------------------------


def _mean(beta) -> float:
    return beta.lo + beta.a / (beta.a + beta.b) * (beta.hi - beta.lo)


def test_departure_soc_profiles_are_selectable(cfg: ScenarioConfig):
    assert cfg.vehicles.departure_soc == "low"
    assert {p.name for p in cfg.vehicles.soc_profiles} == {"low", "high"}
    assert _mean(cfg.vehicles.soc_beta) == pytest.approx(0.343, abs=0.001)

    high = cfg.variant("soc-high", {"vehicles.departure_soc": "high"})
    assert _mean(high.vehicles.soc_beta) == pytest.approx(0.764, abs=0.001)


def test_variant_gets_its_own_scenario_and_run_id(cfg: ScenarioConfig):
    """이름이 같으면 SoC 높음/낮음 실행이 같은 run_id 로 서로를 덮어쓴다."""
    v = cfg.variant("soc-high__dm2", {"vehicles.departure_soc": "high", "demand.demand_multiplier": 2.0})

    assert v.scenario_id == f"{cfg.scenario_id}__soc-high__dm2"
    assert v.demand.demand_multiplier == 2.0
    assert v.config_hash != cfg.config_hash
    assert "departure_soc: high" in v.raw_yaml        # 실제로 쓴 설정이 기록에 남는다
    assert cfg.vehicles.departure_soc == "low"         # 원본은 그대로


def test_variant_rejects_unknown_key_and_profile(cfg: ScenarioConfig):
    with pytest.raises(ConfigError, match="바꿀 키가 없다"):
        cfg.variant("x", {"vehicles.departure_sco": "high"})
    with pytest.raises(ConfigError, match="vehicles.departure_soc"):
        cfg.variant("x", {"vehicles.departure_soc": "medium"})


def test_soc_beta_and_profiles_cannot_both_be_given(cfg: ScenarioConfig, tmp_path):
    data = yaml.safe_load(cfg.raw_yaml)
    data["vehicles"]["soc_beta"] = {"a": 2.0, "b": 5.0, "lo": 0.1, "hi": 0.95}
    path = tmp_path / "both.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ConfigError, match="하나만"):
        ScenarioConfig.from_yaml(path)


def test_wait_heatmap_is_written(tmp_path):
    from evdt.viz.plots import plot_wait_heatmap

    stations = pd.DataFrame({"station_id": ["a", "b"], "name": ["가휴게소", "나휴게소"], "offset_km": [10.0, 50.0]})
    hourly = pd.DataFrame({
        "run_id": ["r1", "r1", "r2"], "station_id": ["a", "b", "a"], "hour": [8, 9, 8],
        "n_ev": [3, 2, 1], "mean_wait_min": [45.0, 5.0, 0.0],
    })
    out = plot_wait_heatmap(hourly, stations, [("r1", "UE"), ("r2", "엔진")], tmp_path / "h.png")

    assert out.is_file() and out.stat().st_size > 1000


def test_entry_offset_is_read_per_ev():
    """진입 지점이 차마다 다르다 (#54). 늦게 탄 차는 앞쪽 휴게소를 고를 수 없다."""
    stations = [
        {"station_id": "A", "offset_km": 50.0, "name": "A"},
        {"station_id": "B", "offset_km": 150.0, "name": "B"},
    ]
    vclasses = {"vc": {"vclass_id": "vc", "battery_kwh": 60.0, "consumption_kwh_km": 0.25,
                       "vmax_kw": 200.0}}
    curves = {"vc": ((0.0, 1.0, 200.0),)}
    rule = ChargeRule(range_factor=1.0, buffer_km=10.0, reserve_soc=0.1,
                      target_soc_cap=0.8, max_stops=3)

    evs = [
        {"ev_id": "early", "vclass_id": "vc", "entry_time_min": 0.0, "initial_soc": 0.5,
         "dest_offset_km": 250.0, "entry_offset_km": 0.0},
        {"ev_id": "late", "vclass_id": "vc", "entry_time_min": 0.0, "initial_soc": 0.5,
         "dest_offset_km": 250.0, "entry_offset_km": 100.0},
    ]

    built = build_trip_demands(evs, stations, vclasses, curves,
                               rule=rule, charge_power_factor=1.0)
    by_id = {t.ev_id: t for t in built.trips}

    assert by_id["early"].entry_offset_km == 0.0
    assert by_id["late"].entry_offset_km == 100.0

    # 100 km 에서 탄 차의 어떤 계획에도 50 km 휴게소는 없다
    late_stations = {s for plan in by_id["late"].plans for s in plan.station_ids}
    assert "A" not in late_stations


def test_missing_entry_offset_falls_back_to_the_argument():
    """예전 호출(진입 지점 하나)이 그대로 돈다."""
    stations = [{"station_id": "A", "offset_km": 50.0, "name": "A"}]
    vclasses = {"vc": {"vclass_id": "vc", "battery_kwh": 60.0, "consumption_kwh_km": 0.25,
                       "vmax_kw": 200.0}}
    curves = {"vc": ((0.0, 1.0, 200.0),)}
    rule = ChargeRule(range_factor=1.0, buffer_km=10.0, reserve_soc=0.1,
                      target_soc_cap=0.8, max_stops=3)
    ev = {"ev_id": "e", "vclass_id": "vc", "entry_time_min": 0.0, "initial_soc": 0.3,
          "dest_offset_km": 200.0}

    built = build_trip_demands([ev], stations, vclasses, curves, rule=rule,
                               charge_power_factor=1.0, entry_offset_km=20.0)

    assert built.trips[0].entry_offset_km == 20.0


# ---------------------------------------------------------------------------
# 기회 충전 (#54)
# ---------------------------------------------------------------------------


def _opp_setup(**rule_kw):
    stations = [
        {"station_id": "A", "offset_km": 30.0, "name": "A"},
        {"station_id": "B", "offset_km": 60.0, "name": "B"},
    ]
    vclasses = {
        "big": {"vclass_id": "big", "battery_kwh": 100.0, "consumption_kwh_km": 0.2, "vmax_kw": 200.0},
        "small": {"vclass_id": "small", "battery_kwh": 30.0, "consumption_kwh_km": 0.2, "vmax_kw": 100.0},
    }
    curves = {k: ((0.0, 1.0, 200.0),) for k in vclasses}
    rule = ChargeRule(range_factor=1.0, buffer_km=10.0, reserve_soc=0.1,
                      target_soc_cap=0.8, max_stops=3, **rule_kw)
    return stations, vclasses, curves, rule


def _ev(ev_id: str, vclass: str, soc: float, dest: float = 90.0) -> dict:
    return {"ev_id": ev_id, "vclass_id": vclass, "entry_time_min": 0.0,
            "initial_soc": soc, "dest_offset_km": dest, "entry_offset_km": 0.0}


def test_without_the_layer_nobody_charges_opportunistically():
    """기본값은 0 이다. 레이어를 안 켜면 수요가 몰래 늘지 않는다."""
    stations, vclasses, curves, rule = _opp_setup()
    evs = [_ev(f"e{i}", "big", 0.9) for i in range(50)]

    built = build_trip_demands(evs, stations, vclasses, curves,
                               rule=rule, charge_power_factor=1.0)

    assert built.n_opportunity == 0
    assert built.n_no_charge == 50


def test_opportunity_charging_creates_demand_that_was_not_there():
    """목적지까지 갈 수 있는 차도 들른 김에 충전한다 — 이게 이 레이어의 전부다."""
    stations, vclasses, curves, rule = _opp_setup(opportunity_prob=1.0, opportunity_soc_margin=1.0)
    evs = [_ev(f"e{i}", "big", 0.9) for i in range(50)]

    built = build_trip_demands(evs, stations, vclasses, curves, rule=rule,
                               charge_power_factor=1.0, rng=np.random.default_rng(0))

    assert built.n_opportunity == 50
    assert len(built.trips) == 50
    assert built.n_no_charge == 0


def test_opportunity_stop_actually_charges():
    """0분짜리 정차면 충전기를 점유하지도 대기를 만들지도 않는다 — 있으나 마나 하다."""
    stations, vclasses, curves, rule = _opp_setup(opportunity_prob=1.0, opportunity_soc_margin=1.0)

    built = build_trip_demands([_ev("e", "big", 0.9)], stations, vclasses, curves,
                               rule=rule, charge_power_factor=1.0, rng=np.random.default_rng(0))
    stop = built.trips[0].plans[0].stops[0]

    assert stop.soc_out > stop.soc_in
    assert stop.soc_out == pytest.approx(rule.target_soc_cap)


def test_thick_margin_does_not_trigger_it():
    """도착 SoC 여유가 두꺼우면 들러도 안 꽂는다."""
    stations, vclasses, curves, rule = _opp_setup(opportunity_prob=1.0, opportunity_soc_margin=0.2)
    evs = [_ev(f"e{i}", "big", 0.95, dest=40.0) for i in range(30)]   # 거의 안 쓰고 도착

    built = build_trip_demands(evs, stations, vclasses, curves, rule=rule,
                               charge_power_factor=1.0, rng=np.random.default_rng(0))

    assert built.n_opportunity == 0


def test_small_batteries_are_caught_more_often_without_naming_them():
    """경차를 따로 지정하지 않아도 여유가 얇아 **자연히** 더 걸린다.

    차종별로 충전 횟수를 강제하면 이미 맞게 도는 물리를 덮어쓰게 된다.
    """
    stations, vclasses, curves, rule = _opp_setup(opportunity_prob=1.0, opportunity_soc_margin=0.5)

    big = build_trip_demands([_ev(f"b{i}", "big", 0.8) for i in range(40)], stations,
                             vclasses, curves, rule=rule, charge_power_factor=1.0,
                             rng=np.random.default_rng(0))
    small = build_trip_demands([_ev(f"s{i}", "small", 0.8) for i in range(40)], stations,
                               vclasses, curves, rule=rule, charge_power_factor=1.0,
                               rng=np.random.default_rng(0))

    assert small.n_opportunity > big.n_opportunity


def test_probability_scales_the_number_of_opportunity_stops():
    stations, vclasses, curves, _ = _opp_setup()
    evs = [_ev(f"e{i}", "big", 0.9) for i in range(2_000)]

    counts = []
    for prob in (0.2, 0.6):
        rule = ChargeRule(range_factor=1.0, buffer_km=10.0, reserve_soc=0.1,
                          target_soc_cap=0.8, max_stops=3,
                          opportunity_prob=prob, opportunity_soc_margin=1.0)
        built = build_trip_demands(evs, stations, vclasses, curves, rule=rule,
                                   charge_power_factor=1.0, rng=np.random.default_rng(1))
        counts.append(built.n_opportunity / len(evs))

    assert counts[0] == pytest.approx(0.2, abs=0.03)
    assert counts[1] == pytest.approx(0.6, abs=0.03)


def test_cars_that_really_need_a_charge_are_unaffected():
    """기회 충전을 꺼도 필요한 차는 그대로 충전한다."""
    stations, vclasses, curves, rule = _opp_setup()
    built = build_trip_demands([_ev("need", "small", 0.3)], stations, vclasses, curves,
                               rule=rule, charge_power_factor=1.0)

    assert len(built.trips) == 1
    assert built.n_opportunity == 0


def test_opportunity_car_with_no_station_in_range_just_does_not_stop():
    """기회 충전 차는 원래 충전 없이도 간다. 들를 곳이 없으면 '불가능' 이 아니다.

    () 로 돌려주면 그 차가 결과에서 통째로 빠져나가 진입 EV 가 조용히 줄어든다.
    """
    stations = [{"station_id": "far", "offset_km": 5.0, "name": "far"}]   # 진입 지점보다 상류
    vclasses = {"big": {"vclass_id": "big", "battery_kwh": 100.0,
                        "consumption_kwh_km": 0.2, "vmax_kw": 200.0}}
    curves = {"big": ((0.0, 1.0, 200.0),)}
    rule = ChargeRule(range_factor=1.0, buffer_km=10.0, reserve_soc=0.1, target_soc_cap=0.8,
                      max_stops=3, opportunity_prob=1.0, opportunity_soc_margin=1.0)
    ev = {"ev_id": "e", "vclass_id": "big", "entry_time_min": 0.0, "initial_soc": 0.9,
          "dest_offset_km": 50.0, "entry_offset_km": 20.0}

    built = build_trip_demands([ev], stations, vclasses, curves, rule=rule,
                               charge_power_factor=1.0, rng=np.random.default_rng(0))

    assert built.n_infeasible == 0
    assert built.n_no_charge == 1


def test_a_car_that_truly_cannot_make_it_is_still_infeasible():
    """진짜 못 가는 차는 그대로 불가능으로 남는다 (위 수정이 이걸 가리면 안 된다)."""
    stations = [{"station_id": "far", "offset_km": 300.0, "name": "far"}]
    vclasses = {"small": {"vclass_id": "small", "battery_kwh": 20.0,
                          "consumption_kwh_km": 0.25, "vmax_kw": 100.0}}
    curves = {"small": ((0.0, 1.0, 100.0),)}
    rule = ChargeRule(range_factor=1.0, buffer_km=10.0, reserve_soc=0.1,
                      target_soc_cap=0.8, max_stops=3)
    ev = {"ev_id": "e", "vclass_id": "small", "entry_time_min": 0.0, "initial_soc": 0.15,
          "dest_offset_km": 400.0, "entry_offset_km": 0.0}

    built = build_trip_demands([ev], stations, vclasses, curves,
                               rule=rule, charge_power_factor=1.0)

    assert built.n_infeasible == 1
