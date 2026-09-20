"""충전 대기열 규칙 — 시뮬레이터와 예약 원장이 공유하는 단 하나의 모듈 (T-15).

    (도착 목록, 충전기 목록) → 충전기 배정 · 시작/종료 시각 · 대기시간

이 모듈을 호출하는 곳은 **정확히 두 곳**이다.

    1. 시뮬레이터   world/sim.py        (T-16, SimPy DES)
    2. 예약 원장    engine/ledger.py    (예약 = FIFO 큐의 예측)

세 번째가 생기면 안 된다. 규칙이 두 곳에 있으면 한쪽만 고치는 일이 **반드시**
벌어지고, 그 순간 시뮬레이터가 계산한 대기시간과 원장이 약속한 대기시간이
달라진다. 그러면 최적화 엔진의 한계 외부비용이 전부 오염된다 (설계 규칙 1).
이 제약은 주석으로만 두지 않고 tests/test_queue_rule.py 가 검사한다.

왜 DES 보다 먼저 만드는가
    DES 를 먼저 만들면 큐 규칙이 SimPy 프로세스 안에 녹아든다. 그다음 원장에서
    같은 규칙이 필요해지면 복사하게 된다. 순서를 뒤집으면 복사할 것이 없다.

규칙 (설계문서 §7.2 규칙 4)
    1. 도착 순서: FIFO. 같은 시각이면 ev_id 사전순 (임의지만 **결정적**이어야 한다).
    2. 배정: 도착 시점에 비어 있는 충전기 중 **최고출력**.
    3. 전부 사용 중이면: **가장 빨리 비는** 충전기를 기다린다. 동률이면 고출력.
    4. 대기시간 = 시작 시각 − 도착 시각.

    규칙 3 은 "가장 빨리 비는" 쪽이지 "가장 빨리 끝나는" 쪽이 아니다. 고출력
    충전기가 조금 늦게 비어도 충전이 빨라 총 소요가 짧을 수 있지만, 그건 다른
    규칙이다. 설계문서에 적힌 규칙을 그대로 구현한다.

순수 함수만 둔다
    모듈 수준 상태가 없고, 입력을 변형하지 않으며, 파일·DB·시각을 읽지 않는다.
    SimPy 를 임포트하지 않는다 — DES 에 종속되면 원장이 이 모듈을 못 쓴다.
    점유시간은 world/charging.py 의 charge_time_min 을 그대로 쓴다. 두 호출자가
    같은 답을 내야 하므로 점유시간도 한 곳에서만 계산돼야 한다 (T-14).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from evdt.world.charging import CurveSegment, charge_time_min


@dataclass(frozen=True)
class Charger:
    """충전기 한 대. available_from_min 은 이미 물려 있는 차가 끝나는 시각(분)."""

    charger_id: str
    power_kw: float
    available_from_min: float = 0.0


@dataclass(frozen=True)
class Arrival:
    """충전소에 도착한 차 한 대와 그 차의 충전 요구.

    점유시간이 충전기 출력에 따라 달라지므로(50kW 와 200kW 는 다른 시간이다)
    배정 전에는 확정할 수 없다. 그래서 '소요시간' 이 아니라 '요구' 를 받는다.
    """

    ev_id: str
    arrival_min: float
    soc_from: float
    soc_to: float
    battery_kwh: float
    vmax_kw: float
    curve: Sequence[CurveSegment]
    charge_power_factor: float = 1.0


@dataclass(frozen=True)
class Assignment:
    """한 대의 배정 결과. wait_min = start_min − arrival_min."""

    ev_id: str
    charger_id: str
    arrival_min: float
    start_min: float
    end_min: float
    wait_min: float


def _check_finite(value: float, what: str) -> float:
    number = float(value)

    if not math.isfinite(number):
        raise ValueError(f"{what} 가 유한한 값이 아닙니다: {value}")

    return number


def validate_chargers(chargers: Sequence[Charger]) -> tuple[Charger, ...]:
    """충전기 목록을 검사한다. 충전기가 없으면 여기서 멈춘다.

    충전기 0대를 허용하면 대기시간이 무한이 되고, 그 무한이 KPI 평균까지
    조용히 오염시킨다. 무한 대기 대신 예외를 던진다 — 충전기가 없는 충전소는
    입력이 잘못된 것이지 대기가 긴 것이 아니다.
    """

    if not chargers:
        raise ValueError("충전기가 0대입니다. 대기시간을 정의할 수 없습니다.")

    seen: set[str] = set()

    for charger in chargers:
        if charger.charger_id in seen:
            raise ValueError(f"충전기 ID 가 중복됩니다: {charger.charger_id}")

        seen.add(charger.charger_id)

        if _check_finite(charger.power_kw, "충전기 출력") <= 0:
            raise ValueError(
                f"충전기 출력은 0보다 커야 합니다: {charger.charger_id} "
                f"{charger.power_kw} kW"
            )

        if _check_finite(charger.available_from_min, "충전기 해제 시각") < 0:
            raise ValueError(
                f"충전기 해제 시각이 음수입니다: {charger.charger_id} "
                f"{charger.available_from_min} 분"
            )

    return tuple(chargers)


def arrival_order(arrivals: Sequence[Arrival]) -> tuple[Arrival, ...]:
    """규칙 1. FIFO 도착순으로 정렬한다.

    같은 시각에 도착한 차들의 순서는 물리적으로 정해지지 않는다. 그래도 **결정적**
    이어야 한다 — 입력 순서에 따라 답이 달라지면 시뮬레이터와 원장이 같은 상태를
    다른 순서로 담았을 때 다른 답을 낸다. 그래서 ev_id 사전순으로 고정한다.
    """

    seen: set[str] = set()

    for arrival in arrivals:
        if arrival.ev_id in seen:
            raise ValueError(f"EV ID 가 중복됩니다: {arrival.ev_id}")

        seen.add(arrival.ev_id)

        if _check_finite(arrival.arrival_min, "도착 시각") < 0:
            raise ValueError(
                f"도착 시각이 음수입니다: {arrival.ev_id} {arrival.arrival_min} 분"
            )

    return tuple(sorted(arrivals, key=lambda a: (a.arrival_min, a.ev_id)))


def choose_charger(
    chargers: Sequence[Charger],
    free_at_min: dict[str, float],
    arrival_min: float,
) -> str:
    """규칙 2·3. 이 시각에 도착한 차가 쓸 충전기 ID.

    free_at_min: 충전기별로 다음에 비는 시각(분).

    비어 있는 충전기가 있으면 그중 최고출력을, 전부 사용 중이면 가장 빨리 비는
    충전기를 고른다. 동률은 고출력 → charger_id 순으로 깬다 (결정성).
    """

    idle = [c for c in chargers if free_at_min[c.charger_id] <= arrival_min]

    if idle:
        return min(idle, key=lambda c: (-c.power_kw, c.charger_id)).charger_id

    return min(
        chargers,
        key=lambda c: (free_at_min[c.charger_id], -c.power_kw, c.charger_id),
    ).charger_id


def service_minutes(arrival: Arrival, charger: Charger) -> float:
    """이 차를 이 충전기에 물렸을 때의 점유시간(분).

    T-14 의 charge_time_min 을 그대로 부른다. 여기서 따로 계산하면 점유시간이
    두 곳에 생겨 이 모듈을 만든 이유가 없어진다.
    """

    return charge_time_min(
        soc_from=arrival.soc_from,
        soc_to=arrival.soc_to,
        battery_kwh=arrival.battery_kwh,
        vmax_kw=arrival.vmax_kw,
        charger_kw=charger.power_kw,
        curve=arrival.curve,
        charge_power_factor=arrival.charge_power_factor,
    )


def assign(
    arrivals: Sequence[Arrival],
    chargers: Sequence[Charger],
) -> tuple[Assignment, ...]:
    """규칙 전체를 적용한다. 반환은 FIFO 도착순.

    같은 입력이면 항상 같은 출력이다. 입력 객체는 변형하지 않는다.
    """

    fleet = validate_chargers(chargers)
    queue = arrival_order(arrivals)

    by_id = {c.charger_id: c for c in fleet}
    free_at_min = {c.charger_id: float(c.available_from_min) for c in fleet}

    assignments: list[Assignment] = []

    for arrival in queue:
        charger_id = choose_charger(fleet, free_at_min, arrival.arrival_min)
        charger = by_id[charger_id]

        start_min = max(float(arrival.arrival_min), free_at_min[charger_id])
        end_min = start_min + service_minutes(arrival, charger)

        free_at_min[charger_id] = end_min

        assignments.append(
            Assignment(
                ev_id=arrival.ev_id,
                charger_id=charger_id,
                arrival_min=float(arrival.arrival_min),
                start_min=start_min,
                end_min=end_min,
                wait_min=start_min - float(arrival.arrival_min),
            )
        )

    return tuple(assignments)


def chargers_after(
    chargers: Sequence[Charger],
    assignments: Sequence[Assignment],
) -> tuple[Charger, ...]:
    """배정 결과를 반영한 충전기 상태. 원장이 다음 Δt 로 넘길 때 쓴다.

    입력 Charger 를 바꾸지 않고 새 객체를 돌려준다.
    """

    free_at_min = {c.charger_id: float(c.available_from_min) for c in chargers}

    for assignment in assignments:
        if assignment.charger_id not in free_at_min:
            raise ValueError(f"모르는 충전기입니다: {assignment.charger_id}")

        free_at_min[assignment.charger_id] = max(
            free_at_min[assignment.charger_id], assignment.end_min
        )

    return tuple(
        replace(c, available_from_min=free_at_min[c.charger_id]) for c in chargers
    )
