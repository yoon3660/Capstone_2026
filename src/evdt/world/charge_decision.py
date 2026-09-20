"""EV 충전 필요성 판단 규칙 (T-21)."""

from evdt.world.charging import energy_for_distance_kwh


def needs_charging(
    soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    distance_to_next_km: float,
    distance_to_dest_km: float,
    buffer_km: float,
    low_soc_threshold: float = 0.2,
) -> bool:
    """다음 충전소 도달 가능성과 목적지 도착 예상 SoC로 충전 필요성을 판단한다."""

    if not 0 <= soc <= 1:
        raise ValueError("SoC는 0~1이어야 합니다.")

    if battery_kwh <= 0 or buffer_km < 0:
        raise ValueError("배터리 용량과 안전 버퍼를 확인하세요.")

    if not 0 <= low_soc_threshold <= 1:
        raise ValueError("낮은 SoC 기준은 0~1이어야 합니다.")

    if distance_to_next_km < 0 or distance_to_dest_km < 0:
        raise ValueError("다음 충전소 및 목적지까지의 거리는 음수일 수 없습니다.")

    remaining_kwh = soc * battery_kwh

    energy_to_next = energy_for_distance_kwh(
        distance_to_next_km + buffer_km,
        consumption_kwh_km,
        range_factor,
    )

    energy_to_dest = energy_for_distance_kwh(
        distance_to_dest_km,
        consumption_kwh_km,
        range_factor,
    )

    arrival_soc = (remaining_kwh - energy_to_dest) / battery_kwh

    # 목적지보다 앞에 있는 충전소만 고려한다.
    next_charger_on_route = distance_to_next_km < distance_to_dest_km

    return (
            (next_charger_on_route and energy_to_next > remaining_kwh)
            or arrival_soc < low_soc_threshold
    )

def calculate_target_soc(
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    distance_to_dest_km: float,
    buffer_km: float,
    target_soc_cap: float = 0.8,
) -> float:
    """충전소에서 목적지까지 필요한 에너지로 목표 SoC를 계산한다."""

    if battery_kwh <= 0:
        raise ValueError("배터리 용량은 0보다 커야 합니다.")

    if distance_to_dest_km < 0 or buffer_km < 0:
        raise ValueError("거리와 안전 버퍼는 음수일 수 없습니다.")

    if not 0 < target_soc_cap <= 1:
        raise ValueError("목표 SoC 상한은 0 초과 1 이하여야 합니다.")

    required_kwh = energy_for_distance_kwh(
        distance_to_dest_km + buffer_km,
        consumption_kwh_km,
        range_factor,
    )

    required_soc = required_kwh / battery_kwh

    return min(required_soc, target_soc_cap)


def can_reach_destination(
    target_soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    distance_to_dest_km: float,
    buffer_km: float,
) -> bool:
    """목표 SoC로 목적지까지 안전 버퍼를 포함해 도달할 수 있는지 확인한다."""

    if not 0 <= target_soc <= 1:
        raise ValueError("목표 SoC는 0~1이어야 합니다.")

    if battery_kwh <= 0 or buffer_km < 0:
        raise ValueError("배터리 용량과 안전 버퍼를 확인하세요.")

    required_kwh = energy_for_distance_kwh(
        distance_to_dest_km + buffer_km,
        consumption_kwh_km,
        range_factor,
    )

    available_kwh = target_soc * battery_kwh

    return available_kwh >= required_kwh


def should_charge(
    charging_needed: bool,
    current_soc: float,
    random_value: float,
    charge_prob: float,
    low_soc_threshold: float,
) -> bool:
    """현재 SoC와 충전 필요성에 따라 실제 충전 여부를 결정한다."""

    if not 0.0 <= current_soc <= 1.0:
        raise ValueError("현재 SoC는 0~1이어야 합니다.")

    if not 0.0 <= random_value < 1.0:
        raise ValueError("난수는 0 이상 1 미만이어야 합니다.")

    if not 0.0 <= charge_prob <= 1.0:
        raise ValueError("충전 확률은 0~1이어야 합니다.")

    if not 0.0 <= low_soc_threshold <= 1.0:
        raise ValueError("낮은 SoC 기준은 0~1이어야 합니다.")

    # 현재 SoC가 기준 미만이면 확률과 관계없이 충전
    if current_soc < low_soc_threshold:
        return True

    # 충전 필요 조건을 만족하지 않으면 충전하지 않음
    if not charging_needed:
        return False

    # 충전 필요 조건을 만족하면 설정된 확률 적용
    return random_value < charge_prob