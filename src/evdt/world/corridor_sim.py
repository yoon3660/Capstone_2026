"""Δt 루프 — 코리도를 달리면서 정책에게 물어보는 시뮬레이터 (#59).

    (충전이 필요한 차들, 휴게소, Policy) → charge_event · escape_event · 스냅샷

UE(engine/ue.py)와 무엇이 다른가
    UE 는 **하루를 미리 다 풀어 놓고** 균형 배정을 찾는다. 운전자가 "도착했을 때의
    대기" 를 안다는 뜻이다. 여기서는 아무도 미래를 모른다. 차는 결정 시점에 그때
    화면에 떠 있던 숫자만 보고 고르고, 100 km 를 달려가 그 결과를 맞는다.

    그래서 이 모듈이 **쏠림(큐 진동)이 실제로 생기는 자리**다. 같은 화면을 본 차들이
    한꺼번에 같은 곳으로 간다 → 그 휴게소가 막힌다 → 화면이 빨개진다 → 다음 무리가
    딴 데로 간다 → 다시 빈다. 이 진동은 UE 에서는 원리상 나오지 않는다.

시뮬레이터는 어느 단계가 도는지 모른다 (설계 규칙 2)
    아는 것은 `Policy` 하나뿐이다. S0 든 S3 든 사후 최적해든 **이 파일은 안 바뀐다.**
    `decide` 가 휴게소 ID 를 주면 그리로 보내고, `ESCAPE_*` 면 코리도 밖으로 내보내고,
    `NO_CHARGE` 면 목적지까지 그냥 달린다. 충전이 필요한지, 줄이 너무 긴지 같은
    **판단은 전부 정책의 몫**이다 — 여기서 하면 스테이지 비교가 의미를 잃는다.

    반대로 **얼마나 채우는가는 정책의 몫이 아니다.** 설계문서 §2.3 이 목표 SoC 를
    결정변수에서 뺐다 (상한 0.8). 그래서 충전량은 여기서 UE 와 **같은 함수**로
    계산한다 (`ChargeAmount`). 이게 정책마다 다르면 "같은 차가 같은 만큼 충전했다"
    가 깨져서 KPI 비교가 성립하지 않는다.

정보의 나이 — 이 모델의 핵심 (그리고 유일한 근사)
    화면은 Δt 마다 갱신된다. 시각 t 의 화면은 t 까지 도착한 차만 반영한다.
    결정 시점이 [t, t+Δt) 에 있는 차는 **t 의 화면**을 본다 — 즉 최대 Δt 만큼 낡은
    정보다. 이게 실제 앱이고, 쏠림을 만드는 것도 이 낡음이다.

    반면 **물리는 근사하지 않는다.** 주행시간은 차가 실제로 결정한 시각과 위치에서
    UE 와 **같은 TravelTime 객체**로 잰다. Δt 를 줄이면 정보가 신선해질 뿐,
    도착 시각이 정확해지는 것이 아니다.

큐는 world/sim.py 의 StationQueue 가 돌린다
    DES 와 같은 객체다. "UE 와 S0 가 같은 세계에서 돌았다" 가 여기에 달려 있다.

하루가 끝나도 멈추지 않는다
    23:50 에 진입한 차도 끝까지 따라간다 (UE 는 애초에 시계를 안 본다). horizon 은
    **스냅샷을 찍는 구간**일 뿐이다. 여기서 자르면 늦게 들어온 차가 통째로 사라져
    S0 만 KPI 가 좋아 보인다.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evdt.interfaces import (
    ESCAPE_REASON,
    NO_CHARGE,
    EVState,
    Policy,
    StationView,
    WorldView,
    read_answer,
)
from evdt.world.charge_decision import calculate_arrival_soc, calculate_target_soc
from evdt.world.charging import CurveSegment

# ⚠ queue_rule 을 직접 임포트하지 않는다. world 쪽에서 큐 규칙을 부르는 자리는
#   world/sim.py 하나뿐이다 (tests/test_queue_rule.py::test_queue_rule_has_at_most_two_callers).
#   Arrival 도 거기를 거쳐서 받는다.
from evdt.world.sim import (
    DEFAULT_SNAPSHOT_EVERY_MIN,
    Arrival,
    StationQueue,
    StationSpec,
    station_snapshot_rows,
)
from evdt.world.travel import TravelTime

#: 차 한 대가 설 수 있는 횟수의 하드 리밋. 정책이 max_stops 를 어겨도 무한 루프는 안 돈다
HARD_STOP_LIMIT = 10

#: SoC 비교 허용 오차 (charge_decision · ue_demand 와 같다)
SOC_EPS = 1e-9


@dataclass(frozen=True)
class ChargeAmount:
    """휴게소에서 얼마나 채우는가. **모든 스테이지가 같은 값을 쓴다** (설계문서 §2.3).

    UE 의 `ue_demand.enumerate_plans` 와 같은 계산이다. 여기가 갈라지면 "정책만
    다르고 나머지는 같다" 가 깨진다.
    """

    buffer_km: float
    reserve_soc: float
    target_soc_cap: float

    def soc_out(self, *, soc_in: float, offset_km: float, dest_offset_km: float,
                battery_kwh: float, consumption_kwh_km: float, range_factor: float,
                opportunity: bool) -> float:
        need = calculate_target_soc(
            battery_kwh=battery_kwh, consumption_kwh_km=consumption_kwh_km,
            range_factor=range_factor,
            distance_to_dest_km=max(dest_offset_km - offset_km, 0.0),
            buffer_km=self.buffer_km, target_soc_cap=self.target_soc_cap,
            arrival_reserve_soc=self.reserve_soc,
        )
        # 기회 충전은 "가야 할 거리" 가 아니라 상한까지 채운다 — 필요해서가 아니라
        # 들른 김에 꽂는 것이므로 (ue_demand.enumerate_plans 와 같다)
        return max(soc_in, need, self.target_soc_cap if opportunity else 0.0)


@dataclass(frozen=True)
class SimEV:
    """Δt 루프에 들어가는 차 한 대.

    **UE 의 TripDemand 와 같은 모집단**이어야 한다 (충전이 필요하다고 판정된 차들).
    여기서 다시 뽑거나 거르면 UE 와 S0 가 서로 다른 수요를 상대하게 되고,
    그때 나온 차이는 정책의 차이가 아니다.
    """

    ev_id: str
    vclass_id: str
    entry_min: float
    entry_offset_km: float
    dest_offset_km: float
    soc0: float
    battery_kwh: float
    consumption_kwh_km: float
    vmax_kw: float
    curve: tuple[CurveSegment, ...]
    cold_factor: float = 1.0
    wants_opportunity_charge: bool = False


@dataclass(frozen=True)
class CorridorResult:
    """writers.py 스키마 그대로. 호출자가 Parquet 로 쓴다 (world 는 io 를 모른다)."""

    charge_events: tuple[dict, ...]
    snapshots: tuple[dict, ...]
    escape_events: tuple[dict, ...]
    n_decide_calls: int         # decide() 를 부른 Δt 수
    n_answers: int              # decide() 가 실제로 답을 준 횟수 (차 수가 아니다)
    n_arrived: int              # 코리도 안에서 해결하고 목적지까지 간 차
    n_stranded: int             # 정책이 못 닿는 곳을 골라 길에서 멈춘 차 — 0 이어야 한다

    @property
    def n_escaped(self) -> int:
        return len(self.escape_events)

    @property
    def n_balked(self) -> int:
        return sum(1 for r in self.escape_events if r["reason"] == "balked")


@dataclass
class _Car:
    ev: SimEV
    offset_km: float
    soc: float
    t_min: float                    # 이 위치·SoC 가 정확한 시각
    stops_done: int = 0
    target: str | None = None       # 가고 있는 휴게소
    done: bool = False


def _station_view(sid: str, q: StationQueue, spec: StationSpec, offset_km: float,
                  t_min: float) -> StationView:
    charging, waiting = q.counts(t_min)
    return StationView(
        station_id=sid,
        offset_km=offset_km,
        lat=spec.lat,
        lon=spec.lon,
        charger_powers_kw=q.powers_kw,
        n_charging=charging,
        n_waiting=waiting,
        next_free_min=q.next_free_min,
        # 앱 화면의 숫자. S0 가 보는 유일한 혼잡 정보다
        wait_min=q.wait_min(t_min),
    )


def _escape_row(car: _Car, reason: str, escape_cost_min: float, best: str) -> dict:
    """UE 의 `escape_rows` 와 **같은 컬럼**. 다르면 두 스테이지를 한 표로 못 본다."""

    return {
        "ev_id": car.ev.ev_id,
        "vclass_id": car.ev.vclass_id,
        "entry_time_min": car.ev.entry_min,
        "entry_offset_km": car.ev.entry_offset_km,
        "dest_offset_km": car.ev.dest_offset_km,
        "escape_cost_min": float(escape_cost_min),
        "reason": reason,
        "best_station_id": best,
    }


def run_corridor(  # noqa: PLR0912, PLR0915 — 한 스텝의 순서가 곧 모델이라 나누면 읽기 어렵다
    stations: Sequence[StationSpec],
    station_offsets: Mapping[str, float],
    evs: Sequence[SimEV],
    policy: Policy,
    travel: TravelTime,
    amount: ChargeAmount,
    *,
    corridor_id: str,
    dt_min: float,
    horizon_min: float,
    temp_c: float,
    cold_factor: float,
    range_factor: float,
    cruise_speed_kmh: float,
    escape_cost_min: float = 0.0,
    snapshot_every_min: float = DEFAULT_SNAPSHOT_EVERY_MIN,
) -> CorridorResult:
    """Δt 루프를 돌린다. 같은 입력·같은 정책이면 같은 결과다 (난수를 쓰지 않는다).

    한 스텝에서 하는 일 — **순서가 곧 정보의 나이다**

        1. t 까지의 스냅샷을 찍는다 (horizon 안이면)
        2. 결정 시점이 [t, t+Δt) 인 차를 모은다
        3. 시각 t 의 화면을 만들어 보여주고 물어본다
        4. 대답대로 보낸다 — 도착 시각은 **차의 실제 결정 시각**에서 잰다
        5. [t, t+Δt) 에 휴게소에 닿은 차를 큐에 넣는다

    이 순서는 `StationQueue.counts` 가 **시각을 되돌려 물으면 안 되기** 때문이기도
    하다: 스냅샷(≤t) → 화면(t) → 도착(≥t) 이라 항상 시각이 커진다.

    남은 차가 없을 때까지 돈다. horizon 은 스냅샷 구간일 뿐 종료 조건이 아니다.
    """

    if dt_min <= 0:
        raise ValueError(f"Δt 는 0보다 커야 합니다: {dt_min}")
    if snapshot_every_min <= 0:
        raise ValueError(f"스냅샷 간격은 0보다 커야 합니다: {snapshot_every_min}")

    specs = {s.station_id: s for s in stations}
    unknown = sorted(set(station_offsets) - set(specs))
    if unknown:
        raise ValueError(f"충전기가 없는 휴게소의 기점거리가 들어왔습니다: {unknown}")
    no_offset = sorted(set(specs) - set(station_offsets))
    if no_offset:
        raise ValueError(f"기점거리를 모르는 휴게소가 있습니다: {no_offset}")

    queues = {sid: StationQueue(sid, tuple(s.chargers)) for sid, s in specs.items()}
    cars = {ev.ev_id: _Car(ev, ev.entry_offset_km, ev.soc0, ev.entry_min) for ev in evs}

    # 결정을 기다리는 차: (결정 시각, ev_id). 진입할 때와 충전이 끝날 때 들어온다
    decisions: list[tuple[float, str]] = [(ev.entry_min, ev.ev_id) for ev in evs]
    heapq.heapify(decisions)
    arrivals: list[tuple[float, str, str]] = []      # (도착 시각, ev_id, 휴게소)

    charge_events: list[dict] = []
    escape_events: list[dict] = []
    snapshots: list[dict] = []
    n_decide_calls = n_answers = n_arrived = n_stranded = 0
    step = 0
    next_snapshot = 0.0

    def snapshot_until(t: float) -> None:
        """t 까지의 스냅샷을 찍는다. **스텝 맨 앞**에서 부른다 — counts() 를 과거
        시각으로 물으면 안 되기 때문이다 (StationQueue.counts 주석)."""

        nonlocal next_snapshot
        while next_snapshot <= t + 1e-9 and next_snapshot <= horizon_min:
            for sid in sorted(queues):
                q = queues[sid]
                charging, waiting = q.counts(next_snapshot)
                snapshots.extend(station_snapshot_rows(
                    sid, specs[sid].lat, specs[sid].lon, next_snapshot,
                    wait_min=q.wait_min(next_snapshot), queue_len=waiting,
                    chargers_busy=charging, chargers_total=len(q.chargers),
                ))
            next_snapshot += snapshot_every_min

    while decisions or arrivals:
        t = step * dt_min
        step += 1

        # --- 1. 스냅샷 (t 까지) -------------------------------------------------
        snapshot_until(t)

        # --- 2. 이번 Δt 에 결정하는 차 ----------------------------------------
        asking: list[_Car] = []
        while decisions and decisions[0][0] < t + dt_min:
            car = cars[heapq.heappop(decisions)[1]]
            if not car.done:
                asking.append(car)

        # --- 3~4. 시각 t 의 화면을 보여주고, 대답대로 보낸다 ---------------------
        if asking:
            n_decide_calls += 1
            world = WorldView(
                t_min=t,
                stations={sid: _station_view(sid, q, specs[sid], station_offsets[sid], t)
                          for sid, q in queues.items()},
                temp_c=temp_c,
                cold_factor=cold_factor,
                range_factor=range_factor,
            )
            answers = policy.decide(
                [
                    EVState(
                        ev_id=c.ev.ev_id, vclass_id=c.ev.vclass_id, corridor_id=corridor_id,
                        offset_km=c.offset_km, speed_kmh=cruise_speed_kmh,
                        soc=c.soc, soc_target=amount.target_soc_cap,
                        battery_kwh=c.ev.battery_kwh, vmax_kw=c.ev.vmax_kw,
                        dest_offset_km=c.ev.dest_offset_km, is_participant=True,
                        consumption_kwh_km=c.ev.consumption_kwh_km,
                        wants_opportunity_charge=c.ev.wants_opportunity_charge,
                        stops_done=c.stops_done, curve=c.ev.curve,
                    )
                    for c in asking
                ],
                world,
            )

            for car in asking:
                raw = answers.get(car.ev.ev_id)

                if raw is None:
                    # "이번 Δt 에는 못 정했다" — 다음 스텝에 다시 물어본다
                    heapq.heappush(decisions, (t + dt_min, car.ev.ev_id))
                    continue

                n_answers += 1
                kind, best = read_answer(str(raw))

                if kind == NO_CHARGE:
                    car.done = True
                    n_arrived += 1
                    continue

                if kind in ESCAPE_REASON:
                    car.done = True
                    escape_events.append(
                        _escape_row(car, ESCAPE_REASON[kind], escape_cost_min, best))
                    continue

                if kind not in queues:
                    raise ValueError(
                        f"{policy.stage} 가 모르는 답을 줬다: {car.ev.ev_id} → {raw!r}")

                if car.stops_done >= HARD_STOP_LIMIT:
                    raise RuntimeError(
                        f"{car.ev.ev_id} 가 {HARD_STOP_LIMIT}번 넘게 섰다. "
                        f"{policy.stage} 가 목적지로 못 보내고 있다 (max_stops 확인)")

                off = station_offsets[kind]
                if off <= car.offset_km + 1e-9:
                    # **앞으로만 간다.** 되돌아가기가 없다는 뜻이기도 하지만, 서 있는
                    # 자리를 다시 고르면 (도착 → 넣을 것 없음 → 다시 물음 → 같은 답)
                    # 루프가 영원히 돈다. 조용히 넘기면 러너가 멈춘 것처럼 보인다.
                    raise ValueError(
                        f"{policy.stage} 가 앞에 있지 않은 휴게소를 골랐다: {car.ev.ev_id} "
                        f"{car.offset_km:.1f} km → {kind} {off:.1f} km (되돌아가기는 없다)")

                car.target = kind
                heapq.heappush(
                    arrivals,
                    (car.t_min + travel.minutes(car.offset_km, off, car.t_min),
                     car.ev.ev_id, kind),
                )

        # --- 5. 휴게소 도착 ----------------------------------------------------
        # 같은 스텝·같은 휴게소의 도착은 한 묶음으로 넣는다. 그래야 (도착시각, ev_id)
        # 타이브레이크가 DES 와 같아진다 (world/sim.py::_admit 과 같은 이유).
        batch: dict[str, list[tuple[float, _Car]]] = {}
        while arrivals and arrivals[0][0] < t + dt_min:
            t_arrive, ev_id, sid = heapq.heappop(arrivals)
            car = cars[ev_id]
            if not car.done and car.target == sid:
                batch.setdefault(sid, []).append((t_arrive, car))

        for sid in sorted(batch):
            off = station_offsets[sid]
            group = sorted(batch[sid], key=lambda g: (g[0], g[1].ev.ev_id))
            arrs: list[Arrival] = []
            state: dict[str, tuple[float, float, _Car]] = {}   # ev_id → (soc_in, soc_out, car)

            for t_arrive, car in group:
                soc_in = calculate_arrival_soc(
                    departure_soc=car.soc, battery_kwh=car.ev.battery_kwh,
                    consumption_kwh_km=car.ev.consumption_kwh_km,
                    range_factor=range_factor,
                    distance_km=max(off - car.offset_km, 0.0),
                )

                if soc_in < 0:
                    # 정책이 못 닿는 곳을 골랐다. 현실에서는 길에서 멈추는 일인데 그
                    # 모델은 없다. 조용히 넘기면 정책의 버그가 KPI 개선으로 보인다.
                    n_stranded += 1
                    car.done = True
                    continue

                soc_out = amount.soc_out(
                    soc_in=soc_in, offset_km=off, dest_offset_km=car.ev.dest_offset_km,
                    battery_kwh=car.ev.battery_kwh,
                    consumption_kwh_km=car.ev.consumption_kwh_km,
                    range_factor=range_factor,
                    opportunity=car.ev.wants_opportunity_charge,
                )

                if soc_out <= soc_in + SOC_EPS:
                    # 넣을 것이 없다 = 정차가 아니다. 충전기를 잡지 않고 그 자리에서 다시 묻는다
                    car.offset_km, car.soc, car.t_min, car.target = off, soc_in, t_arrive, None
                    heapq.heappush(decisions, (t_arrive, car.ev.ev_id))
                    continue

                arrs.append(Arrival(
                    ev_id=car.ev.ev_id, arrival_min=t_arrive, soc_from=soc_in, soc_to=soc_out,
                    battery_kwh=car.ev.battery_kwh, vmax_kw=car.ev.vmax_kw,
                    curve=car.ev.curve, charge_power_factor=car.ev.cold_factor,
                ))
                state[car.ev.ev_id] = (soc_in, soc_out, car)

            if not arrs:
                continue

            power = {c.charger_id: c.power_kw for c in queues[sid].chargers}

            for a in queues[sid].admit(arrs):
                soc_in, soc_out, car = state[a.ev_id]
                charge_min = a.end_min - a.start_min
                car.stops_done += 1
                charge_events.append({
                    "ev_id": car.ev.ev_id,
                    "stop_seq": car.stops_done,
                    "vclass_id": car.ev.vclass_id,
                    "station_id": sid,
                    "charger_id": a.charger_id,
                    "power_kw": power[a.charger_id],
                    "t_arrive_min": a.arrival_min,
                    "t_start_min": a.start_min,
                    "t_end_min": a.end_min,
                    "wait_min": a.wait_min,
                    "charge_min": charge_min,
                    "dwell_min": a.wait_min + charge_min,
                    "soc_in": soc_in,
                    "soc_out": soc_out,
                    "soc_target": soc_out,
                    "energy_kwh": (soc_out - soc_in) * car.ev.battery_kwh,
                    "cold_factor": car.ev.cold_factor,
                    "was_reassigned": False,   # 재배정은 S3 의 개념이다
                })
                # 충전이 끝나면 그 자리에서 다시 묻는다 (또 서야 할 수도 있다)
                car.offset_km, car.soc, car.t_min, car.target = off, soc_out, a.end_min, None
                heapq.heappush(decisions, (a.end_min, car.ev.ev_id))

    # 다 끝난 뒤에도 horizon 까지는 찍는다 (차가 일찍 끝나면 루프가 먼저 멈춘다)
    snapshot_until(horizon_min)

    charge_events.sort(key=lambda r: (r["t_end_min"], r["ev_id"]))
    escape_events.sort(key=lambda r: (r["entry_time_min"], r["ev_id"]))
    snapshots.sort(key=lambda r: (r["t_min"], r["entity_id"], r["state"]))

    return CorridorResult(
        charge_events=tuple(charge_events),
        snapshots=tuple(snapshots),
        escape_events=tuple(escape_events),
        n_decide_calls=n_decide_calls,
        n_answers=n_answers,
        n_arrived=n_arrived,
        n_stranded=n_stranded,
    )
