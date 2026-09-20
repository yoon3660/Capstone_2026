"""충전 필요성 판단 규칙 테스트 (T-21)."""
import pytest

from evdt.world.charge_decision import (
    calculate_arrival_soc,
    calculate_target_soc,
    can_reach_destination,
    can_reach_station_with_buffer,
    decide_next_stop,
    find_reachable_stations,
    needs_charging,
    plan_charging_stops,
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

def test_target_soc_preserves_20_percent_on_arrival():
    # 배터리 100kWh, 전비 0.2kWh/km
    # 목적지까지 100km → 배터리 20% 소모
    # 30km 버퍼 → 배터리 6%
    # 안전 여유 max(6%, 20%) = 20%
    # 목표 SoC = 20% + 20% = 40%
    target = calculate_target_soc(
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_to_dest_km=100.0,
        buffer_km=30.0,
        target_soc_cap=0.8,
        arrival_reserve_soc=0.2,
    )

    assert target == 0.4


def test_low_arrival_soc_forces_charging():
    common = {
        "charging_needed": True,
        "current_soc": 0.5,
        "target_soc": 0.8,
        "random_value": 0.99,
        "charge_prob": 0.0,
        "low_soc_threshold": 0.2,
    }

    # 충전 확률이 0%여도 도착 예상 SoC가 20% 미만이면 강제 충전
    assert should_charge(
        **common,
        arrival_soc_without_charging=0.19,
    ) is True

    # 정확히 20%면 강제 충전 조건에 해당하지 않음
    assert should_charge(
        **common,
        arrival_soc_without_charging=0.2,
    ) is False

    # 배터리가 부족하면 도착 예상 SoC가 음수가 될 수도 있음
    assert should_charge(
        **common,
        arrival_soc_without_charging=-0.1,
    ) is True


def test_destination_requires_20_percent_reserve():
    common = {
        "target_soc": 0.8,
        "battery_kwh": 100.0,
        "consumption_kwh_km": 0.2,
        "range_factor": 1.0,
        "distance_to_dest_km": 320.0,
        "buffer_km": 30.0,
    }

    # 목적지까지 64kWh 소모 → 도착 시 SoC 16%
    # 30km 버퍼(6kWh)는 확보할 수 있음
    assert can_reach_destination(
        **common,
        arrival_reserve_soc=0.0,
    ) is True

    # 하지만 도착 시 SoC 20%는 확보할 수 없음
    assert can_reach_destination(
        **common,
        arrival_reserve_soc=0.2,
    ) is False


def test_can_reach_intermediate_station_with_buffer():
    common = {
        "soc": 0.3,
        "battery_kwh": 100.0,
        "consumption_kwh_km": 0.2,
        "range_factor": 1.0,
        "buffer_km": 30.0,
    }

    # 현재 에너지: 30kWh
    # 충전소까지 100km + 버퍼 30km = 130km
    # 필요 에너지: 26kWh → 도달 가능
    assert can_reach_station_with_buffer(
        **common,
        distance_to_station_km=100.0,
    ) is True

    # 충전소까지 130km + 버퍼 30km = 160km
    # 필요 에너지: 32kWh → 도달 불가능
    assert can_reach_station_with_buffer(
        **common,
        distance_to_station_km=130.0,
    ) is False


def test_find_reachable_stations():
    stations = [
        {"station_id": "A", "offset_km": 107.436},
        {"station_id": "B", "offset_km": 128.332},
        {"station_id": "C", "offset_km": 157.781},
    ]

    reachable = find_reachable_stations(
        stations=stations,
        current_offset_km=100.0,
        soc=0.1,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        buffer_km=30.0,
    )

    assert len(reachable) == 1
    assert reachable[0]["station_id"] == "A"
    assert reachable[0]["distance_to_station_km"] == pytest.approx(7.436)


def test_decide_next_stop():
    stations = [
        {"station_id": "A", "offset_km": 130.0},
        {"station_id": "B", "offset_km": 160.0},
        {"station_id": "C", "offset_km": 190.0},
    ]

    common = {
        "stations": stations,
        "current_offset_km": 100.0,
        "destination_offset_km": 200.0,
        "battery_kwh": 100.0,
        "consumption_kwh_km": 0.2,
        "range_factor": 1.0,
        "buffer_km": 30.0,
        "arrival_reserve_soc": 0.2,
    }

    # 목적지까지 20% 소모 + 도착 시 20% 확보 → 40% 필요
    # 현재 SoC 50%이므로 직행 가능
    result = decide_next_stop(**common, soc=0.5)
    assert result["status"] == "destination"
    assert result["station"] is None

    # 현재 SoC 30% → 목적지 직행은 불가능
    # 중간 충전소는 모두 도달 가능하므로 가장 먼 C 선택
    result = decide_next_stop(**common, soc=0.3)
    assert result["status"] == "intermediate_station"
    assert result["station"]["station_id"] == "C"

    # 현재 SoC 10% → 30km 안전 버퍼를 고려하면
    # 가장 가까운 A 충전소에도 안전하게 도달할 수 없음
    result = decide_next_stop(**common, soc=0.1)
    assert result["status"] == "no_reachable_station"
    assert result["station"] is None


def test_multiple_intermediate_charging_stops():
    # 테스트용 가상 충전소 위치
    stations = [
        {"station_id": "A", "offset_km": 100.0},
        {"station_id": "B", "offset_km": 200.0},
        {"station_id": "C", "offset_km": 300.0},
    ]

    common = {
        "stations": stations,
        "destination_offset_km": 400.0,
        "battery_kwh": 49.0,
        "consumption_kwh_km": 0.194,
        "range_factor": 0.77,
        "buffer_km": 30.0,
        "arrival_reserve_soc": 0.2,
    }

    # 각 중간 충전소에서 80%까지 충전했다고 가정한다.
    # 한 번에 목적지까지 갈 수 없어 A → B → C 순서로 방문해야 한다.
    for current_offset, expected_station in [
        (0.0, "A"),
        (100.0, "B"),
        (200.0, "C"),
    ]:
        result = decide_next_stop(
            **common,
            current_offset_km=current_offset,
            soc=0.8,
        )

        assert result["status"] == "intermediate_station"
        assert result["station"]["station_id"] == expected_station

    # C 충전소에서는 남은 100km에 맞춰 목표 SoC를 계산한다.
    final_target_soc = calculate_target_soc(
        battery_kwh=49.0,
        consumption_kwh_km=0.194,
        range_factor=0.77,
        distance_to_dest_km=100.0,
        buffer_km=30.0,
        arrival_reserve_soc=0.2,
    )

    result = decide_next_stop(
        **common,
        current_offset_km=300.0,
        soc=final_target_soc,
    )

    assert result["status"] == "destination"
    assert result["station"] is None



def test_calculate_arrival_soc():
    # 배터리 100kWh, 출발 SoC 80%
    # 100km × 0.2kWh/km = 20kWh 소모
    # 도착 SoC = 80% - 20% = 60%
    arrival_soc = calculate_arrival_soc(
        departure_soc=0.8,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_km=100.0,
    )

    assert arrival_soc == pytest.approx(0.6)

    # 같은 조건에서 출발 SoC가 10%라면
    # 100km 주행에 필요한 에너지가 부족하므로 예상 SoC는 음수
    insufficient_soc = calculate_arrival_soc(
        departure_soc=0.1,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        distance_km=100.0,
    )

    assert insufficient_soc == pytest.approx(-0.1)


def test_multiple_stops_with_actual_soc_changes():
    stations = [
        {"station_id": "A", "offset_km": 100.0},
        {"station_id": "B", "offset_km": 200.0},
        {"station_id": "C", "offset_km": 300.0},
    ]

    current_offset = 0.0
    soc = 0.8
    destination_offset = 400.0
    visited = []

    for expected_station in ["A", "B", "C"]:
        result = decide_next_stop(
            stations=stations,
            current_offset_km=current_offset,
            destination_offset_km=destination_offset,
            soc=soc,
            battery_kwh=49.0,
            consumption_kwh_km=0.194,
            range_factor=0.77,
            buffer_km=30.0,
            arrival_reserve_soc=0.2,
        )

        assert result["status"] == "intermediate_station"

        station = result["station"]
        assert station["station_id"] == expected_station

        # 실제로 다음 충전소까지 주행하여 SoC 감소
        distance = station["offset_km"] - current_offset

        arrival_soc = calculate_arrival_soc(
            departure_soc=soc,
            battery_kwh=49.0,
            consumption_kwh_km=0.194,
            range_factor=0.77,
            distance_km=distance,
        )

        assert arrival_soc >= 0.0

        # 충전소 도착 후 목표 SoC 계산 및 충전
        target_soc = calculate_target_soc(
            battery_kwh=49.0,
            consumption_kwh_km=0.194,
            range_factor=0.77,
            distance_to_dest_km=destination_offset - station["offset_km"],
            buffer_km=30.0,
            arrival_reserve_soc=0.2,
        )

        soc = max(arrival_soc, target_soc)
        current_offset = station["offset_km"]
        visited.append(station["station_id"])

    # 세 번째 충전소에서 충전한 뒤에는 목적지로 직행
    result = decide_next_stop(
        stations=stations,
        current_offset_km=current_offset,
        destination_offset_km=destination_offset,
        soc=soc,
        battery_kwh=49.0,
        consumption_kwh_km=0.194,
        range_factor=0.77,
        buffer_km=30.0,
        arrival_reserve_soc=0.2,
    )

    assert visited == ["A", "B", "C"]
    assert result["status"] == "destination"

    final_soc = calculate_arrival_soc(
        departure_soc=soc,
        battery_kwh=49.0,
        consumption_kwh_km=0.194,
        range_factor=0.77,
        distance_km=destination_offset - current_offset,
    )

    assert final_soc >= 0.2 - 1e-9


def test_plan_charging_stops():
    stations = [
        {"station_id": "A", "offset_km": 100.0},
        {"station_id": "B", "offset_km": 200.0},
        {"station_id": "C", "offset_km": 300.0},
    ]

    result = plan_charging_stops(
        stations=stations,
        current_offset_km=0.0,
        destination_offset_km=400.0,
        initial_soc=0.8,
        battery_kwh=49.0,
        consumption_kwh_km=0.194,
        range_factor=0.77,
        buffer_km=30.0,
        target_soc_cap=0.8,
        arrival_reserve_soc=0.2,
    )

    assert result["status"] == "destination"

    # 함수가 중간 충전소 방문 목록을 자동으로 생성했는지 확인
    visited = [
        stop["station_id"]
        for stop in result["charging_stops"]
    ]
    assert visited == ["A", "B", "C"]

    # 각 충전소에서 실제 충전량이 증가했는지 확인
    for stop in result["charging_stops"]:
        assert stop["soc_out"] > stop["soc_in"]
        assert stop["soc_out"] <= 0.8

    # 최종 목적지 도착 시 SoC 20% 이상 확보
    assert result["final_soc"] >= 0.2 - 1e-9


def test_plan_charging_stops_when_no_station_reachable():
    stations = [
        {"station_id": "A", "offset_km": 100.0},
    ]

    result = plan_charging_stops(
        stations=stations,
        current_offset_km=0.0,
        destination_offset_km=400.0,
        initial_soc=0.1,
        battery_kwh=49.0,
        consumption_kwh_km=0.194,
        range_factor=0.77,
        buffer_km=30.0,
        target_soc_cap=0.8,
        arrival_reserve_soc=0.2,
    )

    assert result["status"] == "no_reachable_station"
    assert result["charging_stops"] == []
    assert result["final_soc"] is None

def test_plan_prefers_farther_reachable_station():
    stations = [
        {"station_id": "near", "offset_km": 60.0},
        {"station_id": "far", "offset_km": 110.0},
    ]

    result = plan_charging_stops(
        stations=stations,
        current_offset_km=0.0,
        destination_offset_km=400.0,
        initial_soc=0.3,
        battery_kwh=100.0,
        consumption_kwh_km=0.2,
        range_factor=1.0,
        buffer_km=30.0,
        target_soc_cap=0.8,
        arrival_reserve_soc=0.2,
    )

    assert result["status"] == "destination"
    assert [
        stop["station_id"]
        for stop in result["charging_stops"]
    ] == ["far"]
    assert result["final_soc"] == pytest.approx(0.2)