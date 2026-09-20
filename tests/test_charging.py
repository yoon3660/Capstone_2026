"""T-10 / T-11 / T-14 — 차종 파라미터, 온도 계수, 충전 점유시간 계산기."""

from __future__ import annotations

import pytest
import yaml

from evdt.io.vehicles import (
    VEHICLES_PATH,
    curve_segments,
    read_temp_efficiency,
    read_vehicle_classes,
    temp_table,
)
from evdt.world.charging import charge_time_min, energy_for_distance_kwh, temp_factors

# 테스트용 단순 곡선: 어느 SoC 에서나 차량 최대출력을 받는다 (테이퍼 없음)
FLAT_CURVE = [(0.0, 1.0, 350.0)]

# 논문(Table 3) 모양을 350kW 차량에 적용한 곡선
TAPER_CURVE = [
    (0.0, 0.2, 0.65 * 350),
    (0.2, 0.6, 0.95 * 350),
    (0.6, 0.8, 0.70 * 350),
    (0.8, 1.0, 0.20 * 350),
]


# ---------------------------------------------------------------------------
# T-14 충전 점유시간 계산기
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("charger_kw", "expected_kw"), [(200.0, 200.0), (50.0, 50.0)])
def test_bottleneck_is_the_smaller_of_charger_and_vehicle(charger_kw, expected_kw):
    """200kW 충전기 + 350kW 차량 → 200kW, 50kW 충전기면 50kW 가 상한."""

    minutes = charge_time_min(0.2, 0.8, 100.0, 350.0, charger_kw, FLAT_CURVE)

    # 0.6 × 100kWh 를 expected_kw 로 채운 시간
    assert minutes == pytest.approx(0.6 * 100.0 / expected_kw * 60.0)


def test_vehicle_limit_applies_when_charger_is_faster():
    """350kW 충전기 + 150kW 차량 → 차량이 병목."""

    minutes = charge_time_min(0.2, 0.8, 100.0, 150.0, 350.0, FLAT_CURVE)

    assert minutes == pytest.approx(0.6 * 100.0 / 150.0 * 60.0)


def test_last_20_percent_costs_more_than_the_rest():
    """목표 SoC 를 0.8 → 1.0 으로 올리면 시간이 2배 이상 는다 (곡선 테이퍼)."""

    common = (100.0, 350.0, 350.0, TAPER_CURVE)
    to_80 = charge_time_min(0.2, 0.8, *common)
    to_100 = charge_time_min(0.2, 1.0, *common)

    assert to_100 > 2 * to_80


def test_cold_takes_longer():
    """-10 °C 가 20 °C 보다 오래 걸린다."""

    table = temp_table(read_temp_efficiency())
    _, cold = temp_factors(-10.0, table)
    _, warm = temp_factors(20.0, table)

    common = (0.2, 0.8, 80.0, 235.0, 200.0, TAPER_CURVE)
    assert charge_time_min(*common, charge_power_factor=cold) > charge_time_min(
        *common, charge_power_factor=warm
    )


def test_same_soc_takes_no_time():
    assert charge_time_min(0.5, 0.5, 100.0, 350.0, 200.0, FLAT_CURVE) == 0.0


def test_time_is_additive_over_segments():
    """구간을 쪼개서 더한 시간 == 한 번에 계산한 시간 (적분이 맞는지)."""

    common = (100.0, 350.0, 350.0, TAPER_CURVE)
    whole = charge_time_min(0.1, 0.9, *common)
    parts = (
        charge_time_min(0.1, 0.35, *common)
        + charge_time_min(0.35, 0.6, *common)
        + charge_time_min(0.6, 0.9, *common)
    )

    assert whole == pytest.approx(parts)


@pytest.mark.parametrize(
    ("soc_from", "soc_to"),
    [(0.8, 0.5), (-0.1, 0.5), (0.5, 1.5)],
)
def test_bad_soc_is_rejected(soc_from, soc_to):
    with pytest.raises(ValueError):
        charge_time_min(soc_from, soc_to, 100.0, 350.0, 200.0, FLAT_CURVE)


def test_curve_gap_is_rejected():
    """곡선이 요청 구간을 못 덮으면 조용히 짧은 시간을 주지 않고 멈춘다."""

    with pytest.raises(ValueError, match="덮지 못"):
        charge_time_min(0.0, 0.9, 100.0, 350.0, 200.0, [(0.0, 0.5, 150.0)])


# ---------------------------------------------------------------------------
# T-11 온도-효율
# ---------------------------------------------------------------------------


def test_20c_is_the_baseline():
    table = temp_table(read_temp_efficiency())

    assert temp_factors(20.0, table) == (1.0, 1.0)


@pytest.mark.parametrize("temp_c", [-20.0, -10.0, -6.7, 0.0, 10.0])
def test_colder_means_both_factors_drop(temp_c):
    table = temp_table(read_temp_efficiency())
    range_factor, power_factor = temp_factors(temp_c, table)
    warmer_range, warmer_power = temp_factors(temp_c + 5.0, table)

    assert 0 < range_factor <= warmer_range <= 1.0
    assert 0 < power_factor <= warmer_power <= 1.0


def test_required_temperature_points_exist():
    temps = {row["temp_c"] for row in read_temp_efficiency()}

    assert {-10.0, 0.0, 20.0} <= temps


def test_every_point_has_a_source():
    assert all(row["source"].strip() for row in read_temp_efficiency())


def test_cold_needs_more_energy_for_the_same_distance():
    """같은 200km 를 가는 데 -10 °C 가 20 °C 보다 전력을 더 쓴다."""

    table = temp_table(read_temp_efficiency())
    cold, _ = temp_factors(-10.0, table)
    warm, _ = temp_factors(20.0, table)

    cold_kwh = energy_for_distance_kwh(200.0, 0.2, cold)
    warm_kwh = energy_for_distance_kwh(200.0, 0.2, warm)

    assert cold_kwh > warm_kwh
    assert warm_kwh == pytest.approx(40.0)


def test_interpolation_between_points():
    table = [(0.0, 0.90, 0.60), (20.0, 1.00, 1.00)]

    assert temp_factors(10.0, table) == pytest.approx((0.95, 0.80))
    # 표 밖은 양 끝 값으로 자른다 (외삽하지 않는다)
    assert temp_factors(-40.0, table) == (0.90, 0.60)
    assert temp_factors(50.0, table) == (1.00, 1.00)


# ---------------------------------------------------------------------------
# T-10 차종 파라미터
# ---------------------------------------------------------------------------


def test_shares_sum_to_one():
    class_rows, _ = read_vehicle_classes()

    assert sum(row["share"] for row in class_rows) == pytest.approx(1.0)
    assert len(class_rows) == 5


def test_every_class_has_a_source():
    class_rows, _ = read_vehicle_classes()

    assert all(row["source"].strip() for row in class_rows)


def test_charge_curve_covers_full_soc_range():
    _, curve_rows = read_vehicle_classes()
    vclass_ids = {row["vclass_id"] for row in curve_rows}

    for vclass_id in vclass_ids:
        segments = curve_segments(curve_rows, vclass_id)
        assert segments[0][0] == 0.0
        assert segments[-1][1] == 1.0
        for (_, prev_to, _), (next_from, _, _) in zip(segments, segments[1:], strict=False):
            assert prev_to == next_from


def test_10_to_80_matches_published_times():
    """차종별 10→80 % 충전시간이 공개 실측치의 ±15 % 안에 든다 (T-10 완료 조건).

    350kW 충전기 기준이다 — 공개값이 그 조건에서 측정된 것이라서.
    """

    class_rows, curve_rows = read_vehicle_classes()
    reference = {
        entry["vclass_id"]: entry["reference_10_80_min"]
        for entry in yaml.safe_load(VEHICLES_PATH.read_text(encoding="utf-8"))["vehicle_classes"]
    }

    for row in class_rows:
        minutes = charge_time_min(
            0.1,
            0.8,
            row["battery_kwh"],
            row["vmax_kw"],
            350.0,
            curve_segments(curve_rows, row["vclass_id"]),
        )
        expected = reference[row["vclass_id"]]

        assert minutes == pytest.approx(expected, rel=0.15), (
            f"{row['vclass_id']}: 계산 {minutes:.1f}분 vs 공개 {expected}분"
        )


def test_slow_charger_makes_every_class_slower():
    """50kW 충전기에서는 모든 차종이 350kW 때보다 오래 걸린다."""

    class_rows, curve_rows = read_vehicle_classes()

    for row in class_rows:
        curve = curve_segments(curve_rows, row["vclass_id"])
        fast = charge_time_min(0.2, 0.8, row["battery_kwh"], row["vmax_kw"], 350.0, curve)
        slow = charge_time_min(0.2, 0.8, row["battery_kwh"], row["vmax_kw"], 50.0, curve)

        assert slow > fast


# ---------------------------------------------------------------------------
# 시나리오 config ↔ 온도 계수 연결 (하드코딩이 아니라 config 로 지정된다)
# ---------------------------------------------------------------------------


def test_scenario_temperature_drives_the_factors():
    from evdt.config import load_scenario
    from evdt.paths import CONFIG_DIR

    table = temp_table(read_temp_efficiency())

    seollal = load_scenario(CONFIG_DIR / "scenario_seollal_down.yaml").environment.temp_c
    weekend = load_scenario(CONFIG_DIR / "scenario_weekend_base.yaml").environment.temp_c

    assert seollal < weekend                      # 설 시나리오가 더 춥다
    cold_range, cold_power = temp_factors(seollal, table)
    warm_range, warm_power = temp_factors(weekend, table)

    assert cold_range < warm_range < 1.0 or cold_range < warm_range == 1.0
    assert cold_power < warm_power

    # 같은 차로 같은 구간을 충전할 때 설 시나리오가 더 오래 걸린다
    class_rows, curve_rows = read_vehicle_classes()
    row = class_rows[0]
    curve = curve_segments(curve_rows, row["vclass_id"])
    args = (0.2, 0.8, row["battery_kwh"], row["vmax_kw"], 200.0, curve)

    assert charge_time_min(*args, charge_power_factor=cold_power) > charge_time_min(
        *args, charge_power_factor=warm_power
    )


def test_seeded_db_round_trip(db_path):
    import pandas as pd

    from evdt.io.db import get_conn, upsert_df, validate_master
    from evdt.io.vehicles import load_from_db

    class_rows, curve_rows = read_vehicle_classes()
    temp_rows = read_temp_efficiency()

    with get_conn(db_path) as conn:
        upsert_df(conn, "vehicle_class", pd.DataFrame(class_rows))
        upsert_df(conn, "charge_curve", pd.DataFrame(curve_rows))
        upsert_df(conn, "temp_efficiency", pd.DataFrame(temp_rows))

        assert validate_master(conn) == []

        db_classes, db_curves, db_temps = load_from_db(conn)

    assert len(db_classes) == len(class_rows)
    assert len(db_curves) == len(curve_rows)
    assert len(db_temps) == len(temp_rows)

    # DB 에서 읽은 값으로도 같은 충전시간이 나온다
    row = db_classes[0]
    minutes = charge_time_min(
        0.2, 0.8, row["battery_kwh"], row["vmax_kw"], 200.0,
        curve_segments(db_curves, row["vclass_id"]),
    )
    assert minutes > 0
