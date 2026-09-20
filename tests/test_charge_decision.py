"""충전 필요성 판단 규칙 테스트 (T-21)."""
import pytest

from evdt.world.charge_decision import (
    needs_charging,
    calculate_target_soc,
    can_reach_destination,
    should_charge,
)
def test_cold_weather_increases_charging_need():
    # 모델Y급 차량: 현재 배터리 50%, 목적지까지 130km
    common = {
        "soc": 0.5,
        "battery_kwh": 84.8,
        "consumption_kwh_km": 0.171,
        "distance_to_next_km": 90.0,
        "distance_to_dest_km": 130.0,
        "buffer_km": 20.0,
    }

    # 20°C: 충전이 필요하지 않음
    assert needs_charging(**common, range_factor=1.0) is False

    # -10°C: 같은 거리라도 전력 소모가 늘어나 충전이 필요함
    assert needs_charging(**common, range_factor=0.77) is True


def test_arrival_soc_threshold():
    common = {
        "battery_kwh": 100.0,
        "consumption_kwh_km": 1.0,
        "range_factor": 1.0,
        "distance_to_next_km": 5.0,
        "distance_to_dest_km": 20.0,
        "buffer_km": 0.0,
    }

    # 목적지 도착 예상 SoC가 정확히 20% → 충전 필요 없음
    assert needs_charging(soc=0.4, **common) is False

    # 목적지 도착 예상 SoC가 20% 미만 → 충전 필요
    assert needs_charging(soc=0.39, **common) is True


def test_safety_buffer_changes_decision():
    common = {
        "soc": 0.3,
        "battery_kwh": 100.0,
        "consumption_kwh_km": 0.2,
        "range_factor": 1.0,
        "distance_to_next_km": 140.0,
        "distance_to_dest_km": 150.0,
        "low_soc_threshold": 0.0,
    }

    # 현재 에너지: 30kWh
    # 다음 충전소까지 140km → 28kWh 필요
    # 버퍼가 없으면 충전 불필요
    assert needs_charging(buffer_km=0.0, **common) is False

    # 버퍼 20km 추가 → 총 160km → 32kWh 필요
    # 현재 에너지 30kWh로는 부족하므로 충전 필요
    assert needs_charging(buffer_km=20.0, **common) is True

def test_target_soc_below_limit():
    # 목적지 200km + 안전 버퍼 25km
    # 필요 에너지: 225 × 0.2 = 45kWh
    # 배터리 100kWh이므로 목표 SoC는 0.45
    target = calculate_target_soc(
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=200.0,
        buffer_km=25.0,
    )

    assert target == 0.45


def test_target_soc_capped_at_80_percent():
    # 필요 에너지: (450 + 25) × 0.2 = 95kWh
    # 목표 SoC는 0.95이지만 상한 0.8 적용
    target = calculate_target_soc(
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=450.0,
        buffer_km=25.0,
    )

    assert target == 0.8


def test_can_reach_destination():
    # 목적지 200km + 버퍼 25km
    # 필요 에너지: 225 × 0.2 = 45kWh
    # 목표 SoC 45%면 도달 가능
    assert can_reach_destination(
        target_soc=0.45,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=200.0,
        buffer_km=25.0,
    ) is True


def test_cannot_reach_destination_with_80_percent():
    # 목적지 450km + 버퍼 25km
    # 필요 에너지: 475 × 0.2 = 95kWh
    # 80% 충전 시 80kWh만 있으므로 도달 불가능
    assert can_reach_destination(
        target_soc=0.8,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=450.0,
        buffer_km=25.0,
    ) is False


def test_charging_probability_boundaries():
    # 충전 확률 0% → 충전하지 않음
    assert should_charge(
        charging_needed=True,
        current_soc=0.5,
        target_soc=0.8,
        random_value=0.0,
        charge_prob=0.0,
        low_soc_threshold=0.2,
    ) is False

    # 충전 확률 100% → 충전
    assert should_charge(
        charging_needed=True,
        current_soc=0.5,
        target_soc=0.8,
        random_value=0.99,
        charge_prob=1.0,
        low_soc_threshold=0.2,
    ) is True


def test_charging_need_changes_probability():
    common = {
        "current_soc": 0.5,
        "random_value": 0.5,
        "charge_prob": 0.95,
        "low_soc_threshold": 0.2,
    }

    # 충전 필요 조건 충족 → 95% 확률 적용
    assert should_charge(charging_needed=True, target_soc=0.8, **common) is True

    # 충전 필요 조건 미충족 → 충전하지 않음
    assert should_charge(charging_needed=False, target_soc=0.8, **common) is False


def test_low_soc_forces_charging():
    # 현재 SoC가 20% 미만이면 확률이 0%여도 무조건 충전
    assert should_charge(
        charging_needed=False,
        current_soc=0.19,
        target_soc=0.8,
        random_value=0.99,
        charge_prob=0.0,
        low_soc_threshold=0.2,
    ) is True

    # 정확히 20%는 '20% 미만'이 아니므로 강제 충전 대상이 아님
    assert should_charge(
        charging_needed=False,
        current_soc=0.2,
        target_soc=0.8,
        random_value=0.99,
        charge_prob=0.0,
        low_soc_threshold=0.2,
    ) is False


def test_target_soc_uses_configured_cap():
    # 필요 에너지: (350 + 25) × 0.2 = 75kWh
    # 배터리 100kWh → 필요한 SoC는 75%
    # 하지만 상한을 70%로 설정했으므로 목표는 70%
    target = calculate_target_soc(
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=350.0,
        buffer_km=25.0,
        target_soc_cap=0.7,
    )

    assert target == 0.7


def test_charging_need_uses_configured_threshold():
    common = {
        "soc": 0.5,
        "battery_kwh": 100.0,
        "consumption_kwh_km": 0.2,
        "range_factor": 1.0,
        "distance_to_next_km": 10.0,
        "distance_to_dest_km": 100.0,
        "buffer_km": 0.0,
    }

    # 현재 에너지: 50kWh
    # 목적지까지 필요한 에너지: 20kWh
    # 도착 예상 SoC: (50 - 20) / 100 = 0.30

    # 기준이 20%라면 충전 필요 없음
    assert needs_charging(
        **common,
        low_soc_threshold=0.2,
    ) is False

    # 기준을 40%로 높이면 충전 필요
    assert needs_charging(
        **common,
        low_soc_threshold=0.4,
    ) is True

def test_ignore_charger_beyond_destination():
    # 목적지는 20km 앞, 다음 충전소는 300km 앞
    # 현재 에너지: 50kWh
    # 목적지 도착 예상 SoC: (50 - 4) / 100 = 46%
    # 목적지까지 충분하므로 충전이 필요하지 않아야 함
    assert needs_charging(
        soc=0.5,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_next_km=300.0,
        distance_to_dest_km=20.0,
        buffer_km=30.0,
    ) is False

def test_low_arrival_soc_even_if_charger_beyond_destination():
    # 현재 SoC 23%, 목적지까지 20km 주행 시 4% 소모
    # 도착 예상 SoC 19% → 충전 필요성은 높음
    # 단, 충전소는 목적지보다 뒤에 있으므로 실제 방문은 별도 처리
    assert needs_charging(
        soc=0.23,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_next_km=300.0,
        distance_to_dest_km=20.0,
        buffer_km=30.0,
    ) is True


def test_charging_decision_with_scenario_config():
    from evdt.config import ScenarioConfig
    from evdt.io.vehicles import (
        read_temp_efficiency,
        read_vehicle_classes,
        temp_table,
    )
    from evdt.world.charging import temp_factors

    # 실제 설날 하행 시나리오와 차량 설정을 읽는다.
    cfg = ScenarioConfig.from_yaml("config/scenario_seollal_down.yaml")
    classes, _ = read_vehicle_classes()
    model_y = next(
        row for row in classes
        if row["vclass_id"] == "import_mid_suv"
    )

    # 시나리오 온도(-5°C)에 해당하는 전비 계수를 계산한다.
    table = temp_table(read_temp_efficiency())
    cold_factor, _ = temp_factors(cfg.environment.temp_c, table)
    warm_factor, _ = temp_factors(20.0, table)

    common = {
        "soc": 0.5,
        "battery_kwh": model_y["battery_kwh"],
        "consumption_kwh_km": model_y["consumption_kwh_km"],
        "distance_to_next_km": 90.0,
        "distance_to_dest_km": 130.0,
        "buffer_km": cfg.demand.safety_buffer_km,
        "low_soc_threshold": cfg.demand.low_soc_threshold,
    }

    # 같은 차량이라도 상온에서는 불필요하고, 설날 저온에서는 필요하다.
    assert needs_charging(**common, range_factor=warm_factor) is False

    charging_needed = needs_charging(
        **common,
        range_factor=cold_factor,
    )
    assert charging_needed is True

    # 충전 조건 충족 시 시나리오에 정의된 95% 확률을 적용한다.
    assert should_charge(
        charging_needed=charging_needed,
        current_soc=common["soc"],
        target_soc=0.8,
        random_value=0.5,
        charge_prob=cfg.demand.charge_prob,
        low_soc_threshold=cfg.demand.low_soc_threshold,
    ) is True

def test_target_soc_includes_distance_buffer():
    # 목적지 100km + 안전 버퍼 30km
    # 필요 에너지: 130 × 0.2 = 26kWh
    # 배터리 100kWh → 목표 SoC 26%
    target = calculate_target_soc(
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=100.0,
        buffer_km=30.0,
    )

    assert target == 0.26

def test_cold_weather_increases_charging_fraction():
    from evdt.config import ScenarioConfig
    from evdt.io.vehicles import (
        read_temp_efficiency,
        read_vehicle_classes,
        temp_table,
    )
    from evdt.world.charging import temp_factors

    cfg = ScenarioConfig.from_yaml("config/scenario_seollal_down.yaml")
    classes, _ = read_vehicle_classes()
    table = temp_table(read_temp_efficiency())

    warm_factor, _ = temp_factors(20.0, table)
    cold_factor, _ = temp_factors(cfg.environment.temp_c, table)

    def count_charging(range_factor):
        count = 0

        # 차종 5개 × 차량 20대 = 총 100대
        for vehicle in classes:
            for i in range(20):
                soc = 0.10 + 0.85 * i / 19

                needed = needs_charging(
                    soc=soc,
                    battery_kwh=vehicle["battery_kwh"],
                    consumption_kwh_km=vehicle["consumption_kwh_km"],
                    range_factor=range_factor,
                    distance_to_next_km=90.0,
                    distance_to_dest_km=130.0,
                    buffer_km=cfg.demand.safety_buffer_km,
                    low_soc_threshold=cfg.demand.low_soc_threshold,
                )

                if should_charge(
                    charging_needed=needed,
                    current_soc=soc,
                    target_soc=0.8,
                    random_value=0.5,
                    charge_prob=cfg.demand.charge_prob,
                    low_soc_threshold=cfg.demand.low_soc_threshold,
                ):
                    count += 1

        return count

    warm_count = count_charging(warm_factor)
    cold_count = count_charging(cold_factor)

    assert cold_count > warm_count


def test_negative_distance_raises_error():
    common = {
        "soc": 0.5,
        "battery_kwh": 100.0,
        "consumption_kwh_km": 0.2,
        "range_factor": 1.0,
        "buffer_km": 30.0,
    }

    # 다음 충전소까지의 거리가 음수인 경우
    with pytest.raises(ValueError):
        needs_charging(
            **common,
            distance_to_next_km=-5.0,
            distance_to_dest_km=100.0,
        )

    # 목적지까지의 거리가 음수인 경우
    with pytest.raises(ValueError):
        needs_charging(
            **common,
            distance_to_next_km=50.0,
            distance_to_dest_km=-5.0,
        )

def test_skip_charging_when_target_already_reached():
    common = {
        "charging_needed": True,
        "random_value": 0.0,
        "charge_prob": 1.0,
        "low_soc_threshold": 0.2,
    }

    # 현재 50%, 목표 35% → 이미 목표를 넘었으므로 충전 생략
    assert should_charge(
        current_soc=0.5,
        target_soc=0.35,
        **common,
    ) is False

    # 현재 50%, 목표 50% → 추가로 충전할 필요 없음
    assert should_charge(
        current_soc=0.5,
        target_soc=0.5,
        **common,
    ) is False

    # 현재 50%, 목표 70% → 충전 필요, 확률 100%이므로 충전
    assert should_charge(
        current_soc=0.5,
        target_soc=0.7,
        **common,
    ) is True