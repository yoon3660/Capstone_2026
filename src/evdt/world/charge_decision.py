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
    arrival_reserve_soc: float = 0.0,
) -> float:
    """목적지까지 필요한 에너지와 안전 여유로 목표 SoC를 계산한다."""

    if battery_kwh <= 0:
        raise ValueError("배터리 용량은 0보다 커야 합니다.")

    if distance_to_dest_km < 0 or buffer_km < 0:
        raise ValueError("거리와 안전 버퍼는 음수일 수 없습니다.")

    if not 0 < target_soc_cap <= 1:
        raise ValueError("목표 SoC 상한은 0 초과 1 이하여야 합니다.")

    if not 0 <= arrival_reserve_soc <= 1:
        raise ValueError("도착 시 최소 SoC는 0~1이어야 합니다.")

    # 목적지까지 필요한 에너지
    required_kwh = energy_for_distance_kwh(
        distance_to_dest_km,
        consumption_kwh_km,
        range_factor,
    )

    # 안전 버퍼 30km에 해당하는 에너지
    buffer_kwh = energy_for_distance_kwh(
        buffer_km,
        consumption_kwh_km,
        range_factor,
    )

    required_soc = required_kwh / battery_kwh
    buffer_soc = buffer_kwh / battery_kwh

    # 30km 버퍼와 도착 시 최소 SoC 중 더 큰 안전 여유를 적용
    safety_reserve_soc = max(buffer_soc, arrival_reserve_soc)

    return min(required_soc + safety_reserve_soc, target_soc_cap)


def can_reach_destination(
    target_soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    distance_to_dest_km: float,
    buffer_km: float,
    arrival_reserve_soc: float = 0.0,
) -> bool:
    """목표 SoC로 목적지까지 이동한 뒤 안전 여유를 확보할 수 있는지 확인한다."""

    if not 0 <= target_soc <= 1:
        raise ValueError("목표 SoC는 0~1이어야 합니다.")

    if battery_kwh <= 0 or buffer_km < 0 or distance_to_dest_km < 0:
        raise ValueError("배터리 용량과 거리를 확인하세요.")

    if not 0 <= arrival_reserve_soc <= 1:
        raise ValueError("도착 시 최소 SoC는 0~1이어야 합니다.")

    # 목적지까지 주행하는 데 필요한 에너지
    driving_kwh = energy_for_distance_kwh(
        distance_to_dest_km,
        consumption_kwh_km,
        range_factor,
    )

    # 30km 버퍼에 필요한 에너지
    buffer_kwh = energy_for_distance_kwh(
        buffer_km,
        consumption_kwh_km,
        range_factor,
    )

    # 두 안전 여유 중 더 큰 값 적용
    reserve_kwh = max(
        buffer_kwh,
        arrival_reserve_soc * battery_kwh,
    )

    available_kwh = target_soc * battery_kwh

    return available_kwh + 1e-9 >= driving_kwh + reserve_kwh


def should_charge(  # noqa: PLR0913
    charging_needed: bool,
    current_soc: float,
    target_soc: float,
    random_value: float,
    charge_prob: float,
    low_soc_threshold: float,
    arrival_soc_without_charging: float | None = None,
) -> bool:
    """충전 필요성, 도착 예상 SoC, 목표 SoC를 이용해 충전 여부를 결정한다.

    ⚠ **지금 이 함수를 부르는 곳은 자기 테스트뿐이다** (#54). UE 는
    `engine.ue_demand.enumerate_plans` 에서 `can_reach_destination` 으로 직접
    판단하므로, `demand.charge_prob` 를 바꿔도 UE 결과는 안 바뀐다.

    지우지 않고 두는 이유: S0 이후 정책은 "지금 화면을 보고 결정" 하므로 이런
    시점 판단 함수가 필요하다 (#59). 그때 이 자리로 돌아온다.
    """

    if not 0.0 <= current_soc <= 1.0:
        raise ValueError("현재 SoC는 0~1이어야 합니다.")

    if not 0.0 <= target_soc <= 1.0:
        raise ValueError("목표 SoC는 0~1이어야 합니다.")

    if not 0.0 <= random_value < 1.0:
        raise ValueError("난수는 0 이상 1 미만이어야 합니다.")

    if not 0.0 <= charge_prob <= 1.0:
        raise ValueError("충전 확률은 0~1이어야 합니다.")

    if not 0.0 <= low_soc_threshold <= 1.0:
        raise ValueError("낮은 SoC 기준은 0~1이어야 합니다.")

    if arrival_soc_without_charging is not None:
        import math

        if not math.isfinite(arrival_soc_without_charging):
            raise ValueError("도착 예상 SoC는 유한한 숫자여야 합니다.")

    # 이미 목표 SoC 이상이면 충전을 생략한다.
    if current_soc >= target_soc:
        return False

    # 현재 SoC가 기준 미만이면 강제 충전한다.
    if current_soc < low_soc_threshold:
        return True

    # 충전하지 않으면 목적지 도착 SoC가 기준 미만인 경우 강제 충전한다.
    if (
        arrival_soc_without_charging is not None
        and arrival_soc_without_charging < low_soc_threshold
    ):
        return True

    # 그 외 충전 필요 차량에는 기존 확률을 적용한다.
    if not charging_needed:
        return False

    return random_value < charge_prob


def can_reach_station_with_buffer(
    soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    distance_to_station_km: float,
    buffer_km: float,
) -> bool:
    """현재 위치에서 충전소까지 이동하고 안전 버퍼를 남길 수 있는지 확인한다."""

    if not 0 <= soc <= 1:
        raise ValueError("SoC는 0~1이어야 합니다.")

    if battery_kwh <= 0:
        raise ValueError("배터리 용량은 0보다 커야 합니다.")

    if distance_to_station_km < 0 or buffer_km < 0:
        raise ValueError("거리와 안전 버퍼는 음수일 수 없습니다.")

    required_kwh = energy_for_distance_kwh(
        distance_to_station_km + buffer_km,
        consumption_kwh_km,
        range_factor,
    )

    available_kwh = soc * battery_kwh

    return available_kwh + 1e-9 >= required_kwh


def find_reachable_stations(
    stations: list[dict],
    current_offset_km: float,
    soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    buffer_km: float,
) -> list[dict]:
    """경로상 충전소 중 현재 배터리로 안전 버퍼를 남기고 도달 가능한 곳을 반환한다."""

    if current_offset_km < 0:
        raise ValueError("현재 offset은 음수일 수 없습니다.")

    reachable = []

    for station in stations:
        distance_km = station["offset_km"] - current_offset_km

        # 현재 위치 또는 뒤쪽에 있는 충전소 제외
        if distance_km <= 0:
            continue

        if can_reach_station_with_buffer(
            soc=soc,
            battery_kwh=battery_kwh,
            consumption_kwh_km=consumption_kwh_km,
            range_factor=range_factor,
            distance_to_station_km=distance_km,
            buffer_km=buffer_km,
        ):
            reachable.append(
                {
                    **station,
                    "distance_to_station_km": distance_km,
                }
            )

    return sorted(reachable, key=lambda s: s["offset_km"])


def select_next_station(reachable_stations: list[dict]) -> dict | None:
    """도달 가능한 충전소 중 가장 앞쪽에 있는 충전소를 선택한다."""

    if not reachable_stations:
        return None

    return max(
        reachable_stations,
        key=lambda station: station["offset_km"],
    )

def decide_next_stop(
    stations: list[dict],
    current_offset_km: float,
    destination_offset_km: float,
    soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    buffer_km: float,
    arrival_reserve_soc: float = 0.2,
) -> dict:
    """충전 후 목적지로 직행할지, 중간 충전소를 방문할지 결정한다."""

    if current_offset_km < 0:
        raise ValueError("현재 offset은 음수일 수 없습니다.")

    if destination_offset_km <= current_offset_km:
        raise ValueError("목적지는 현재 위치보다 앞에 있어야 합니다.")

    distance_to_dest_km = destination_offset_km - current_offset_km

    # 1. 목적지에 안전 여유를 남기고 도착할 수 있다면 직행
    if can_reach_destination(
        target_soc=soc,
        battery_kwh=battery_kwh,
        consumption_kwh_km=consumption_kwh_km,
        range_factor=range_factor,
        distance_to_dest_km=distance_to_dest_km,
        buffer_km=buffer_km,
        arrival_reserve_soc=arrival_reserve_soc,
    ):
        return {
            "status": "destination",
            "station": None,
        }

    # 2. 목적지에 바로 갈 수 없다면, 경로상 중간 충전소를 검사
    intermediate_stations = [
        station
        for station in stations
        if current_offset_km < station["offset_km"] < destination_offset_km
    ]

    reachable_stations = find_reachable_stations(
        stations=intermediate_stations,
        current_offset_km=current_offset_km,
        soc=soc,
        battery_kwh=battery_kwh,
        consumption_kwh_km=consumption_kwh_km,
        range_factor=range_factor,
        buffer_km=buffer_km,
    )

    next_station = select_next_station(reachable_stations)

    # 3. 안전하게 도달 가능한 충전소가 없는 경우
    if next_station is None:
        return {
            "status": "no_reachable_station",
            "station": None,
        }

    # 4. 도달 가능한 중간 충전소가 있다면 추가 방문
    return {
        "status": "intermediate_station",
        "station": next_station,
    }

def calculate_arrival_soc(
    departure_soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    distance_km: float,
) -> float:
    """주어진 거리를 주행했을 때의 도착 예상 SoC를 계산한다."""

    if not 0 <= departure_soc <= 1:
        raise ValueError("출발 SoC는 0~1이어야 합니다.")

    if battery_kwh <= 0:
        raise ValueError("배터리 용량은 0보다 커야 합니다.")

    if distance_km < 0:
        raise ValueError("주행 거리는 음수일 수 없습니다.")

    used_kwh = energy_for_distance_kwh(
        distance_km,
        consumption_kwh_km,
        range_factor,
    )

    return departure_soc - used_kwh / battery_kwh


def plan_charging_stops(
    stations: list[dict],
    current_offset_km: float,
    destination_offset_km: float,
    initial_soc: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    range_factor: float,
    buffer_km: float,
    target_soc_cap: float = 0.8,
    arrival_reserve_soc: float = 0.2,
) -> dict:
    """목적지까지 이동하면서 필요한 충전소 방문 목록을 계산한다.

    stations에는 같은 주행 방향의 충전소 목록을 전달해야 한다.
    충전소의 실제 이용 가능 여부와 대기시간은 고려하지 않는다.
    """

    current_offset = current_offset_km
    soc = initial_soc
    charging_stops = []

    # 각 반복에서 다음 충전소로 전진하므로,
    # 충전소 개수보다 많이 반복할 필요는 없다.
    for _ in range(len(stations) + 1):
        decision = decide_next_stop(
            stations=stations,
            current_offset_km=current_offset,
            destination_offset_km=destination_offset_km,
            soc=soc,
            battery_kwh=battery_kwh,
            consumption_kwh_km=consumption_kwh_km,
            range_factor=range_factor,
            buffer_km=buffer_km,
            arrival_reserve_soc=arrival_reserve_soc,
        )

        # 목적지까지 안전 조건을 만족하며 직행 가능
        if decision["status"] == "destination":
            final_soc = calculate_arrival_soc(
                departure_soc=soc,
                battery_kwh=battery_kwh,
                consumption_kwh_km=consumption_kwh_km,
                range_factor=range_factor,
                distance_km=destination_offset_km - current_offset,
            )

            return {
                "status": "destination",
                "charging_stops": charging_stops,
                "final_soc": final_soc,
            }

        # 다음 충전소를 찾을 수 없음
        if decision["status"] == "no_reachable_station":
            return {
                "status": "no_reachable_station",
                "charging_stops": charging_stops,
                "final_soc": None,
            }

        station = decision["station"]
        station_offset = station["offset_km"]

        # 다음 충전소까지 주행
        arrival_soc = calculate_arrival_soc(
            departure_soc=soc,
            battery_kwh=battery_kwh,
            consumption_kwh_km=consumption_kwh_km,
            range_factor=range_factor,
            distance_km=station_offset - current_offset,
        )

        if arrival_soc < -1e-9:
            return {
                "status": "no_reachable_station",
                "charging_stops": charging_stops,
                "final_soc": None,
            }

        # 해당 충전소에서의 목표 SoC 계산
        target_soc = calculate_target_soc(
            battery_kwh=battery_kwh,
            consumption_kwh_km=consumption_kwh_km,
            range_factor=range_factor,
            distance_to_dest_km=destination_offset_km - station_offset,
            buffer_km=buffer_km,
            target_soc_cap=target_soc_cap,
            arrival_reserve_soc=arrival_reserve_soc,
        )

        departure_soc = max(arrival_soc, target_soc)

        # 실제 충전량이 증가하는 경우만 충전 방문으로 기록
        if departure_soc > arrival_soc + 1e-9:
            charging_stops.append(
                {
                    "station_id": station["station_id"],
                    "offset_km": station_offset,
                    "soc_in": arrival_soc,
                    "soc_out": departure_soc,
                }
            )

        # 차량 상태를 갱신하고 다음 구간 결정
        current_offset = station_offset
        soc = departure_soc

    raise RuntimeError("충전 경로 계획이 예상 반복 횟수를 초과했습니다.")