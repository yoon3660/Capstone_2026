"""UE 선택지 만들기 — 차 한 대가 고를 수 있는 "충전 계획" 목록.

    (진입 시각·SoC·목적지·차종, 휴게소 위치)  →  TripDemand(plans=...)

선택의 단위가 휴게소가 아니라 **계획**(정차 휴게소들의 순서)인 이유
    설 하행에서 장거리 차의 약 1/5 이 두 번 이상 충전해야 한다. 첫 정차를 어디서
    하느냐가 두 번째 정차 위치와 시각을 바꾼다. 휴게소 하나씩 따로 고르게 하면
    "안성에서 조금만 넣고 칠곡에서 또 서는" 조합의 비용을 못 본다.

충전 규칙은 T-21 과 같다 (world/charge_decision.py 의 함수를 그대로 쓴다)
    - 목적지에 SoC 여유(low_soc_threshold)를 남기고 못 가면 충전해야 한다
    - 휴게소까지는 안전버퍼를 남기고 닿아야 한다
    - 휴게소에서는 목적지까지 필요한 만큼 + 여유, 상한 target_soc_cap 까지 넣는다
    - 목적지에 충전 없이 갈 수 있는 차는 UE 에 들어오지 않는다 (충전 수요가 아니다)

정차 수가 가장 적은 계획만 선택지로 둔다
    두 번이면 되는데 세 번 서는 사람은 없다. 이걸 막지 않으면 선택지가 휴게소 수의
    세제곱으로 늘고, 그중 대부분은 누구도 고르지 않는 계획이다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from evdt.world.charge_decision import (
    calculate_arrival_soc,
    calculate_target_soc,
    can_reach_destination,
    can_reach_station_with_buffer,
)
from evdt.world.charging import CurveSegment

#: SoC 비교 허용 오차 (charge_decision 과 같다)
SOC_EPS = 1e-9


@dataclass(frozen=True)
class PlannedStop:
    station_id: str
    offset_km: float
    soc_in: float
    soc_out: float


@dataclass(frozen=True)
class Plan:
    stops: tuple[PlannedStop, ...]

    @property
    def station_ids(self) -> tuple[str, ...]:
        return tuple(s.station_id for s in self.stops)


@dataclass(frozen=True)
class TripDemand:
    """충전이 필요한 차 한 대와 그 차의 선택지."""

    ev_id: str
    vclass_id: str
    entry_min: float
    entry_offset_km: float
    dest_offset_km: float
    battery_kwh: float
    vmax_kw: float
    curve: tuple[CurveSegment, ...]
    cold_factor: float
    plans: tuple[Plan, ...]


@dataclass(frozen=True)
class DemandBuild:
    trips: tuple[TripDemand, ...]
    n_ev: int                 # 진입한 전체 EV
    n_no_charge: int          # 충전 없이 목적지까지 가는 차
    n_infeasible: int         # max_stops 안에서 목적지까지 갈 계획이 없는 차 (결과에서 빠진다)


@dataclass(frozen=True)
class ChargeRule:
    """충전 판단에 필요한 값. 시나리오 config 에서 온다."""

    range_factor: float
    buffer_km: float
    reserve_soc: float        # 목적지 도착 시 남길 SoC (demand.low_soc_threshold)
    target_soc_cap: float
    max_stops: int


def enumerate_plans(
    stations: Sequence[Mapping],
    *,
    entry_offset_km: float,
    dest_offset_km: float,
    soc0: float,
    battery_kwh: float,
    consumption_kwh_km: float,
    rule: ChargeRule,
) -> tuple[Plan, ...] | None:
    """정차 수가 가장 적은 실행 가능한 계획 전부. 충전이 필요 없으면 None, 불가능하면 ()."""

    kw = dict(battery_kwh=battery_kwh, consumption_kwh_km=consumption_kwh_km, range_factor=rule.range_factor)

    def reaches_dest(soc: float, offset: float) -> bool:
        return can_reach_destination(
            target_soc=soc, distance_to_dest_km=dest_offset_km - offset,
            buffer_km=rule.buffer_km, arrival_reserve_soc=rule.reserve_soc, **kw,
        )

    if reaches_dest(soc0, entry_offset_km):
        return None

    on_route = sorted(
        (s for s in stations if entry_offset_km < float(s["offset_km"]) < dest_offset_km),
        key=lambda s: float(s["offset_km"]),
    )

    def extend(offset: float, soc: float, start: int, depth: int) -> list[tuple[PlannedStop, ...]]:
        """offset 에서 soc 로 출발해 depth 번 이내로 서서 목적지에 닿는 정차열."""

        found: list[tuple[PlannedStop, ...]] = []

        for j in range(start, len(on_route)):
            s = on_route[j]
            s_off = float(s["offset_km"])

            if not can_reach_station_with_buffer(
                soc=soc, distance_to_station_km=s_off - offset, buffer_km=rule.buffer_km, **kw,
            ):
                break  # 뒤쪽 휴게소는 더 멀다

            soc_in = calculate_arrival_soc(departure_soc=soc, distance_km=s_off - offset, **kw)
            soc_out = max(
                soc_in,
                calculate_target_soc(
                    distance_to_dest_km=dest_offset_km - s_off, buffer_km=rule.buffer_km,
                    target_soc_cap=rule.target_soc_cap, arrival_reserve_soc=rule.reserve_soc, **kw,
                ),
            )

            if soc_out <= soc_in + SOC_EPS:
                continue  # 넣을 것이 없는 곳은 정차가 아니다

            stop = PlannedStop(str(s["station_id"]), s_off, soc_in, soc_out)

            if depth == 1:
                if reaches_dest(soc_out, s_off):
                    found.append((stop,))
            else:
                found += [(stop, *rest) for rest in extend(s_off, soc_out, j + 1, depth - 1)]

        return found

    for depth in range(1, rule.max_stops + 1):
        # 정확히 depth 번 서는 계획만: depth-1 번으로 되는 차는 이미 위에서 끝났다
        plans = [p for p in extend(entry_offset_km, soc0, 0, depth) if len(p) == depth]

        if plans:
            return tuple(Plan(p) for p in plans)

    return ()


def build_trip_demands(
    evs: Iterable[Mapping],
    stations: Sequence[Mapping],
    vclasses: Mapping[str, Mapping],
    curves: Mapping[str, tuple[CurveSegment, ...]],
    *,
    rule: ChargeRule,
    charge_power_factor: float,
    entry_offset_km: float = 0.0,
) -> DemandBuild:
    """synthetic_ev 의 EV 행 → 충전이 필요한 차의 선택지.

    evs 행: ev_id, vclass_id, entry_time_min, initial_soc, dest_offset_km
    """

    trips: list[TripDemand] = []
    n_ev = n_no_charge = n_infeasible = 0

    for ev in evs:
        n_ev += 1
        v = vclasses[str(ev["vclass_id"])]
        plans = enumerate_plans(
            stations,
            entry_offset_km=entry_offset_km,
            dest_offset_km=float(ev["dest_offset_km"]),
            soc0=float(ev["initial_soc"]),
            battery_kwh=float(v["battery_kwh"]),
            consumption_kwh_km=float(v["consumption_kwh_km"]),
            rule=rule,
        )

        if plans is None:
            n_no_charge += 1
            continue

        if not plans:
            n_infeasible += 1
            continue

        trips.append(
            TripDemand(
                ev_id=str(ev["ev_id"]),
                vclass_id=str(ev["vclass_id"]),
                entry_min=float(ev["entry_time_min"]),
                entry_offset_km=entry_offset_km,
                dest_offset_km=float(ev["dest_offset_km"]),
                battery_kwh=float(v["battery_kwh"]),
                vmax_kw=float(v["vmax_kw"]),
                curve=tuple(curves[str(ev["vclass_id"])]),
                cold_factor=charge_power_factor,
                plans=plans,
            )
        )

    return DemandBuild(tuple(trips), n_ev, n_no_charge, n_infeasible)
