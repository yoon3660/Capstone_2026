"""UE — 모두가 각자 가장 빠른 충전 계획을 고른 균형 (Wardrop 제1원칙).

    TripDemand(선택지) × 휴게소 충전기  →  균형 배정 + 반복별 gap

정의
    균형 = **누구도 혼자 계획을 바꿔서 체류시간(대기 + 충전)을 줄일 수 없는 상태.**
    운전자는 자기가 **도착했을 때의 대기**를 안다고 본다 — 명절마다 같은 길을 다니며
    "안성은 9시에 막힌다" 를 경험으로 아는 운전자다.

    S0 와 다르다. S0 는 **출발할 때 화면에 뜬 대기**만 보고 고른다. 100 km 를 달리는
    동안 같은 화면을 본 차들이 같은 곳으로 몰린다. UE 는 그 결과까지 알고 고른다.

쏠림은 어디서 오는가 — 균형인데도
    UE 는 각자에게 최선이지만 **모두에게 최선은 아니다.** 내가 안성에 들어가면 뒤차
    대기가 늘어나는데, 그 비용은 내 선택에 안 들어간다 (혼잡 외부효과).
    특히 배터리가 적어서 **안성까지밖에 못 가는 차**가 있으면, 다른 곳에도 갈 수 있는
    차가 안성을 고를 때마다 그 차들이 대신 기다린다. 그 손실이 SO 와의 차이다.

비용은 queue_rule 이 정한다 (설계 규칙 1)
    한 차가 휴게소 s 에 시각 t 에 들어가면 얼마나 머무는가 = FIFO 에서 **t 이전에 온
    차들만으로** 정해진다. 그래서 "나 혼자 바꾸면 어떻게 되나" 가 근사가 아니라
    정확히 계산된다: 그 휴게소의 실제 도착열을 t 직전까지 쌓고 나를 한 대 넣어본다.
    이 일은 예약 원장(engine/ledger.py)이 한다 — queue_rule 의 호출자는 시뮬레이터와
    원장 둘뿐이고, UE 는 원장을 통해서만 큐를 본다. 큐 계산을 여기서 따로 하지 않는다.

반복 — 도착 순서대로 다시 고르기 (Gauss-Seidel 최적반응)
    반복 0. 빈 휴게소 기준으로 각자 가장 빠른 계획 = "대기를 전혀 모르는 운전자".
            충전기 출력만 보고 고르니 고출력 휴게소로 몰린다. gap 그래프의 첫 점이다.
    반복 k. 진입 순서대로 한 대씩, **나머지 차들의 현재 계획을 보고** 가장 빠른 계획으로
            바꾼다 (min_gain_min 이상 줄어들 때만). 바꾸면 바로 반영하고 다음 차로.
            한 바퀴 돈 뒤 전원을 다시 굴려서 정확한 gap 을 잰다.
    gap ≤ gap_tol 이거나 더 바꿀 차가 없으면 균형. max_iter 안에 못 가면 UENotConverged.

    2회 이상 서는 차가 있으면 gap 이 0 까지 가지 않고 몇 % 에서 오르내린다. 균형에서는
    대체 가능한 휴게소들의 비용이 같아지므로 1~2분 차이로 거의 동률인 차가 많고, 추월
    한 번이 그 동률을 연쇄로 뒤집는다 (설 수요 ×2 실측: 2.2~3.2%). 감쇠(바꾸고 싶은 차를
    확률 1/k 로만 바꾸기)를 넣어 봤지만 4% 로 오히려 나빠졌다. 그래서 gap_tol 기본값을
    3% 로 둔다 — 평균 체류 30분이면 1분이 안 되는 차이다 (docs/UE_equilibrium.md §4).

    왜 도착 순서인가: FIFO 에서 내 대기는 **나보다 먼저 온 차들만** 정한다. 먼저 온 차들이
    이번 바퀴에서 이미 골랐으니, 한 번 서는 차는 한 바퀴 만에 정확한 최적반응이 된다.
    남는 gap 은 2회 이상 서는 차 때문이다 — 내가 첫 휴게소에서 충전하는 동안 뒤에
    출발한 차가 나를 앞질러 다음 휴게소에 먼저 들어갈 수 있다.

    처음에는 MSA (개선 가능한 차 중 1/k 비율을 무작위로 옮기는 표준 방법) 로 만들었다.
    한 대만 옮겨도 그 휴게소 뒷차 전부가 밀리는 연쇄가 생겨서, 휴게소 두 곳 예제에서도
    gap 이 5% 근처에서 오르내리며 멈췄다 (docs/UE_equilibrium.md §4).

    상대 gap = Σ(내 체류 − 혼자 바꿨을 때 최선) / Σ 내 체류
    0 이면 정확한 균형. "모두가 평균 몇 % 손해를 보고 있나" 로 읽는다.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from evdt.engine.ledger import Arrival, Charger, StationLedger
from evdt.engine.ue_demand import Plan, TripDemand
from evdt.world.sim import EVArrival

#: 이보다 작은 개선은 동률로 본다 (부동소수 오차로 계획을 바꾸지 않는다)
GAIN_EPS_MIN = 1e-9


class UENotConverged(RuntimeError):
    """max_iter 안에 gap_tol 에 못 미쳤다. result 에 마지막 상태와 gap 이력이 있다."""

    def __init__(self, message: str, result: UEResult) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class UESettings:
    max_iter: int = 50
    gap_tol: float = 0.03
    min_gain_min: float = 1.0
    speed_kmh: float = 80.0


@dataclass(frozen=True)
class IterationStat:
    iteration: int
    rel_gap: float
    total_dwell_min: float
    n_improvable: int         # 혼자 바꾸면 min_gain 이상 줄어드는 차
    n_switched: int           # 이 반복 뒤 바퀴에서 실제로 계획을 바꾼 차


@dataclass(frozen=True)
class Visit:
    """한 차의 한 정차 — DES 입력 한 줄."""

    ev_id: str
    stop_seq: int
    station_id: str
    t_arrive_min: float
    soc_in: float
    soc_out: float
    dwell_min: float


@dataclass(frozen=True)
class UEResult:
    choice: dict[str, int]                    # ev_id → plans 의 인덱스
    history: tuple[IterationStat, ...]
    converged: bool
    visits: tuple[Visit, ...]                 # 마지막 배정의 실제 정차 (시각 순)
    dwell_by_ev: dict[str, float] = field(default_factory=dict)

    @property
    def final_gap(self) -> float:
        return self.history[-1].rel_gap

    def station_of(self, trips: Sequence[TripDemand]) -> dict[str, tuple[str, ...]]:
        return {t.ev_id: t.plans[self.choice[t.ev_id]].station_ids for t in trips}


# ---------------------------------------------------------------------------
# 한 번 굴리기 — 전원이 계획대로 움직인 결과
# ---------------------------------------------------------------------------


def drive_min(from_km: float, to_km: float, speed_kmh: float) -> float:
    return (to_km - from_km) / speed_kmh * 60.0


def _first_arrival_min(trip: TripDemand, plan: Plan, speed_kmh: float) -> float:
    return trip.entry_min + drive_min(trip.entry_offset_km, plan.stops[0].offset_km, speed_kmh)


def _arrival(trip: TripDemand, seq: int, t: float, plan: Plan) -> Arrival:
    stop = plan.stops[seq]
    return Arrival(
        ev_id=trip.ev_id,
        arrival_min=t,
        soc_from=stop.soc_in,
        soc_to=stop.soc_out,
        battery_kwh=trip.battery_kwh,
        vmax_kw=trip.vmax_kw,
        curve=trip.curve,
        charge_power_factor=trip.cold_factor,
    )


Ledgers = dict[str, StationLedger]


def _ledgers(chargers: Mapping[str, tuple[Charger, ...]]) -> Ledgers:
    return {sid: StationLedger(c) for sid, c in chargers.items()}


def _roll(
    trips: Sequence[TripDemand],
    choice: Mapping[str, int],
    chargers: Mapping[str, tuple[Charger, ...]],
    speed_kmh: float,
) -> tuple[Ledgers, list[Visit]]:
    """전원이 고른 계획대로 하루를 굴린다. 원장과 실제 정차 목록을 돌려준다.

    도착을 전체 시각 순 (t, ev_id) 으로 하나씩 원장에 넣는다. FIFO 에서 한 도착의 결과는
    그보다 먼저 온 차들로만 정해지므로 이 순서가 인과 순서다. 두 번째 정차의 도착
    시각은 첫 정차가 끝난 시각 + 주행시간이라, 처리하면서 힙에 넣는다.
    DES(world/sim.py)에 같은 도착을 넣으면 같은 결과가 나온다 (테스트로 고정).
    """

    ledgers = _ledgers(chargers)
    by_id = {t.ev_id: t for t in trips}
    heap = [
        (_first_arrival_min(t, t.plans[choice[t.ev_id]], speed_kmh), t.ev_id, 0) for t in trips
    ]
    heapq.heapify(heap)
    visits: list[Visit] = []

    while heap:
        t, ev_id, seq = heapq.heappop(heap)
        trip = by_id[ev_id]
        plan = trip.plans[choice[ev_id]]
        stop = plan.stops[seq]
        arr = _arrival(trip, seq, t, plan)
        ledger = ledgers[stop.station_id]

        dwell = ledger.evaluate(arr)   # 먼저 온 차들이 모두 들어가 있다
        ledger.commit(arr)
        visits.append(Visit(ev_id, seq + 1, stop.station_id, t, stop.soc_in, stop.soc_out, dwell))

        if seq + 1 < len(plan.stops):
            nxt = plan.stops[seq + 1]
            heapq.heappush(heap, (t + dwell + drive_min(stop.offset_km, nxt.offset_km, speed_kmh), ev_id, seq + 1))

    return ledgers, visits


def _plan_cost(
    trip: TripDemand,
    plan: Plan,
    ledgers: Ledgers,
    speed_kmh: float,
    *,
    exclude_self: bool,
) -> tuple[float, list[Arrival]]:
    """이 차가 plan 으로 가면 총 체류시간과 각 정차의 도착. 원장은 바꾸지 않는다.

    앞 정차의 체류가 뒤 정차의 도착을 민다. exclude_self 면 원장에 있는 자기 자신의
    기존 정차를 빼고 계산한다 ("나 혼자 계획을 바꾸면?").
    """

    t = _first_arrival_min(trip, plan, speed_kmh)
    total = 0.0
    arrivals: list[Arrival] = []
    exclude = trip.ev_id if exclude_self else None

    for seq, stop in enumerate(plan.stops):
        arr = _arrival(trip, seq, t, plan)
        dwell = ledgers[stop.station_id].evaluate(arr, exclude=exclude)
        arrivals.append(arr)
        total += dwell

        if seq + 1 < len(plan.stops):
            t += dwell + drive_min(stop.offset_km, plan.stops[seq + 1].offset_km, speed_kmh)

    return total, arrivals


# ---------------------------------------------------------------------------
# gap — 이 배정이 균형에서 얼마나 먼가
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    """한 배정 상태에서 모두의 실제 비용과 혼자 바꿨을 때 최선."""

    current: dict[str, float]
    best_cost: dict[str, float]
    best_plan: dict[str, int]
    visits: tuple[Visit, ...]

    @property
    def rel_gap(self) -> float:
        total = sum(self.current.values())
        excess = sum(max(0.0, self.current[e] - self.best_cost[e]) for e in self.current)
        return excess / total if total > 0 else 0.0


def evaluate(
    trips: Sequence[TripDemand],
    choice: Mapping[str, int],
    chargers: Mapping[str, tuple[Charger, ...]],
    speed_kmh: float,
) -> Evaluation:
    ledgers, visits = _roll(trips, choice, chargers, speed_kmh)
    current: dict[str, float] = {}

    for v in visits:
        current[v.ev_id] = current.get(v.ev_id, 0.0) + v.dwell_min

    best_cost: dict[str, float] = {}
    best_plan: dict[str, int] = {}

    for trip in trips:
        mine = choice[trip.ev_id]
        best_plan[trip.ev_id], best_cost[trip.ev_id] = mine, current[trip.ev_id]

        for k, plan in enumerate(trip.plans):
            if k == mine:
                continue

            cost, _ = _plan_cost(trip, plan, ledgers, speed_kmh, exclude_self=True)

            if cost < best_cost[trip.ev_id] - GAIN_EPS_MIN:
                best_plan[trip.ev_id], best_cost[trip.ev_id] = k, cost

    return Evaluation(current, best_cost, best_plan, tuple(visits))


# ---------------------------------------------------------------------------
# 균형 찾기
# ---------------------------------------------------------------------------


def _best(costs: Sequence[float]) -> int:
    return min(range(len(costs)), key=lambda k: (costs[k], k))


def free_flow_choice(
    trips: Sequence[TripDemand],
    chargers: Mapping[str, tuple[Charger, ...]],
    speed_kmh: float,
) -> dict[str, int]:
    """빈 휴게소 기준 각자 가장 빠른 계획 — 대기를 모르는 운전자. 반복 0."""

    empty = _ledgers(chargers)
    return {
        trip.ev_id: _best([_plan_cost(trip, p, empty, speed_kmh, exclude_self=False)[0] for p in trip.plans])
        for trip in trips
    }


def _sweep(
    trips: Sequence[TripDemand],
    choice: dict[str, int],
    chargers: Mapping[str, tuple[Charger, ...]],
    current: Evaluation,
    settings: UESettings,
) -> int:
    """진입 순서대로 한 대씩 최적반응으로 바꾼다. 바꾼 대수를 돌려준다."""

    by_id = {t.ev_id: t for t in trips}
    ledgers = _ledgers(chargers)
    own: dict[str, list[str]] = {}

    for v in current.visits:
        trip = by_id[v.ev_id]
        ledgers[v.station_id].commit(_arrival(trip, v.stop_seq - 1, v.t_arrive_min, trip.plans[choice[v.ev_id]]))
        own.setdefault(v.ev_id, []).append(v.station_id)

    switched = 0

    for trip in sorted(trips, key=lambda t: (t.entry_min, t.ev_id)):
        for sid in own[trip.ev_id]:
            ledgers[sid].cancel(trip.ev_id)

        evals = [_plan_cost(trip, plan, ledgers, settings.speed_kmh, exclude_self=False) for plan in trip.plans]
        mine = choice[trip.ev_id]
        best = _best([c for c, _ in evals])

        if best != mine and evals[mine][0] - evals[best][0] >= max(settings.min_gain_min, GAIN_EPS_MIN):
            choice[trip.ev_id] = mine = best
            switched += 1

        for arr, stop in zip(evals[mine][1], trip.plans[mine].stops, strict=True):
            ledgers[stop.station_id].commit(arr)

    return switched


def _improvable(ev: Evaluation, min_gain_min: float) -> int:
    return sum(
        1 for e in ev.current if ev.current[e] - ev.best_cost[e] >= max(min_gain_min, GAIN_EPS_MIN)
    )


def solve_ue(
    trips: Sequence[TripDemand],
    chargers: Mapping[str, tuple[Charger, ...]],
    settings: UESettings,
) -> UEResult:
    """균형 배정을 찾는다. 못 찾으면 UENotConverged (result 에 gap 이력 포함).

    같은 입력이면 같은 결과다. 난수를 쓰지 않는다 — 처리 순서는 (진입 시각, ev_id),
    동률이면 앞 계획. 시드는 수요를 만드는 쪽(synthetic_ev)이 갖는다.
    """

    trips = sorted(trips, key=lambda t: t.ev_id)
    ids = [t.ev_id for t in trips]

    if len(set(ids)) != len(ids):
        raise ValueError("ev_id 가 중복된다")

    missing = sorted({s for t in trips for p in t.plans for s in p.station_ids} - set(chargers))

    if missing:
        raise ValueError(f"충전기 정보가 없는 휴게소가 계획에 있다: {missing}")

    choice = free_flow_choice(trips, chargers, settings.speed_kmh)
    history: list[IterationStat] = []

    for k in range(settings.max_iter + 1):
        ev = evaluate(trips, choice, chargers, settings.speed_kmh)
        n_improvable = _improvable(ev, settings.min_gain_min)
        done = ev.rel_gap <= settings.gap_tol or n_improvable == 0

        if done or k == settings.max_iter:
            history.append(IterationStat(k, ev.rel_gap, sum(ev.current.values()), n_improvable, 0))
            break

        switched = _sweep(trips, choice, chargers, ev, settings)
        history.append(IterationStat(k, ev.rel_gap, sum(ev.current.values()), n_improvable, switched))

    result = UEResult(
        choice=dict(choice),
        history=tuple(history),
        converged=done,
        visits=ev.visits,
        dwell_by_ev=dict(ev.current),
    )

    if not done:
        raise UENotConverged(
            f"UE 가 {settings.max_iter}회 안에 수렴하지 않았다: "
            f"gap {result.final_gap:.4f} > {settings.gap_tol}, 더 줄일 수 있는 차 {n_improvable}대 "
            f"(policy.ue.max_iter / gap_tol 확인)",
            result,
        )

    return result


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def des_arrivals(trips: Sequence[TripDemand], result: UEResult) -> list[EVArrival]:
    """균형 배정의 정차들을 DES 입력으로. 도착 시각은 균형 상태에서 실제로 굴린 값이다."""

    by_id = {t.ev_id: t for t in trips}
    return [
        EVArrival(
            ev_id=v.ev_id,
            vclass_id=by_id[v.ev_id].vclass_id,
            station_id=v.station_id,
            t_arrive_min=v.t_arrive_min,
            soc_in=v.soc_in,
            soc_target=v.soc_out,
            battery_kwh=by_id[v.ev_id].battery_kwh,
            vmax_kw=by_id[v.ev_id].vmax_kw,
            curve=by_id[v.ev_id].curve,
            cold_factor=by_id[v.ev_id].cold_factor,
            stop_seq=v.stop_seq,
        )
        for v in result.visits
    ]


def solver_log_rows(result: UEResult) -> list[dict]:
    """solver_log 에 넣을 행. UE 는 iteration 과 rel_gap 두 칸만 채운다."""

    return [{"iteration": s.iteration, "rel_gap": s.rel_gap} for s in result.history]


def solve_and_log(
    trips: Sequence[TripDemand],
    chargers: Mapping[str, tuple[Charger, ...]],
    settings: UESettings,
    writer,
) -> UEResult:
    """solve_ue + gap 이력을 solver_log 에 쓴다. **수렴하지 못해도 이력은 남기고** 다시 던진다.

        with RunContext.open(cfg, seed=7) as run:
            result = solve_and_log(trips, chargers, settings, run.writer)
        # 수렴 실패면 run.status = FAILED, solver_log.parquet 에 반복별 gap 이 남는다

    조용히 넘어가면 "수렴 안 한 배정" 으로 만든 히트맵이 균형 결과처럼 쓰인다.
    writer 는 io.writers.ParquetRunWriter (engine 은 io 를 임포트하지 않아도 되게 받기만 한다).
    """

    try:
        result = solve_ue(trips, chargers, settings)
    except UENotConverged as exc:
        writer.append_many("solver_log", solver_log_rows(exc.result))
        raise

    writer.append_many("solver_log", solver_log_rows(result))
    return result
