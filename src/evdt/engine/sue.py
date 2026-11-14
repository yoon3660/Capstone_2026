"""SUE — 확률적 사용자 균형, MNL 모드 B (#173, MNL 확장 1단계).

    UE 의 반복을 그대로 쓰고, **계획을 고를 때만** 사람마다 다른 인지 오차를 더한다.

    인지 비용  C̃_ik = C_ik − ε_ik / θ        ε ~ Gumbel(0, 1), (seed, ev_id, 계획) 에 고정

    C_ik 는 UE 와 같은 값이다 — 계획 k 로 갔을 때 **도착 시점의 실제 대기 + 충전**
    (`ue._plan_cost`, 예약 원장으로 잰다).

모드 A(#168 `MnlPolicy`)와 무엇이 다른가
    모드 A  지금 화면의 대기를 보고 근시로 고른다. θ → ∞ 이면 S0
    모드 B  도착했을 때의 대기를 알고 고른다(경험으로 아는 운전자). θ → ∞ 이면 UE

ε 를 고정하는 이유
    ε 가 반복마다 바뀌지 않으므로 고정 ε 위의 "결정적 UE" 를 푸는 것과 같다. 그래서
    UE 의 Gauss-Seidel 반복이 그대로 수렴하고 MSA 가 필요 없다. 큰 모집단에서 고정
    ε 균형의 선택 분포는 SUE 확률(로짓)과 같다. 시드를 바꾸면 ε 표본이 바뀐다.

이탈 판정 — 모드 A 의 `escape_mode="s0"` 와 같은 원칙
    이탈할지는 **잡음 없는 비용**으로 정한다: 코리도 안 계획의 최소 실제 비용이 이탈
    비용보다 비싸면 이탈한다. 이탈하지 않는 차만 인지 비용으로 계획을 고른다.
    그래서 SUE 는 **분배만** 퍼뜨리고, 이탈은 UE 와 같은 규칙에서 나온다.

w (대기 가중치)
    지원하지 않는다(w = 1). 원장은 체류(대기 + 충전)를 한 값으로 돌려준다. w 가
    필요하면 대기와 충전을 나눠 받아야 하고, 그건 `ue.py` 를 고쳐야 한다.

θ = ∞
    `ue.solve_ue` 를 그대로 부른다. 유한한 큰 θ(예: 1e12)에서도 같은 답이 나오는지
    테스트가 따로 고정한다 — 아래의 일반 경로가 UE 로 수렴한다는 확인이다.

기존 파일은 고치지 않는다. `ue.py` 의 내부 함수(`_roll`, `_plan_cost`, `_ledgers`,
`_arrival`)를 가져다 쓴다. runner 에는 `runner.solve_and_log` 를 잠깐 바꿔 끼워 넣는다
(`scripts/run_sue_world.py`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evdt.engine import ue as U
from evdt.engine.ledger import Charger
from evdt.engine.mnl import gumbel
from evdt.engine.ue_demand import Plan, TripDemand

#: ε 키의 접두어. 모드 A 의 휴게소 키와 겹치지 않게 한다 (계획 ≠ 휴게소)
PLAN_PREFIX = "plan:"


@dataclass(frozen=True)
class SueSettings:
    """SUE 의 손잡이. 결과 manifest 에 그대로 남긴다."""

    #: 척도 θ (1/분). inf 면 UE 와 같다
    theta: float = math.inf
    #: ε 의 시드. 시나리오 시드와 같은 값을 준다 (같은 시드 = 같은 사람들)
    seed: int = 0

    def __post_init__(self) -> None:
        if not self.theta > 0:
            raise ValueError(f"theta 는 양수여야 한다 (inf 허용): {self.theta}")

    def noise(self, ev_id: str, plan_key: str) -> float:
        """인지 비용에서 빼는 값 ε/θ (분). θ = inf 면 0."""

        if math.isinf(self.theta):
            return 0.0
        return gumbel(self.seed, ev_id, PLAN_PREFIX + plan_key) / self.theta


def plan_key(k: int, plan: Plan) -> str:
    """계획의 ε 키. 같은 휴게소 순서라도 계획 번호가 다르면 다른 선택지다."""

    return f"{k}:{'>'.join(plan.station_ids)}" if plan.stops else f"{k}:escape"


# ---------------------------------------------------------------------------
# 한 차의 선택 — 이탈은 잡음 없이, 휴게소 계획은 인지 비용으로
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Choice:
    target: int          # 고른 계획 번호
    gain: float          # 지금 계획 대비 줄어드는 (인지) 비용. 0 이상이면 바꿀 이유


def _perceived(trip: TripDemand, costs: Sequence[float], sue: SueSettings) -> list[float]:
    out = []
    for k, (plan, c) in enumerate(zip(trip.plans, costs, strict=True)):
        if plan.is_escape:
            out.append(c)
        else:
            out.append(c - sue.noise(trip.ev_id, plan_key(k, plan)))
    return out


def choose(trip: TripDemand, costs: Sequence[float], mine: int, sue: SueSettings) -> _Choice:
    """실제 비용 `costs` 에서 이 차가 고를 계획과, 지금 계획(`mine`) 대비 이득.

    이득의 정의
        같은 쪽(둘 다 코리도 안)이면 인지 비용의 차이
        이탈 ↔ 코리도 안이 바뀌면 **실제 비용**의 차이 (이탈 판정은 잡음이 없다)
    θ = inf 면 UE 의 `_best` 와 이득 정의가 같다.
    """

    inside = [k for k, p in enumerate(trip.plans) if not p.is_escape]
    escape = [k for k, p in enumerate(trip.plans) if p.is_escape]
    if not inside:
        target = escape[0]
        return _Choice(target, costs[mine] - costs[target])

    real_in = min(costs[k] for k in inside)
    esc = escape[0] if escape else None
    if esc is not None and real_in > costs[esc]:
        target = esc
    else:
        pc = _perceived(trip, costs, sue)
        target = min(inside, key=lambda k: (pc[k], k))

    if trip.plans[mine].is_escape != trip.plans[target].is_escape:
        best_real = costs[target] if trip.plans[target].is_escape else real_in
        return _Choice(target, costs[mine] - best_real)
    if target == mine:
        return _Choice(mine, 0.0)
    pc = _perceived(trip, costs, sue)
    return _Choice(target, pc[mine] - pc[target])


# ---------------------------------------------------------------------------
# 평가 · 반복 — ue.evaluate / ue._sweep 과 같은 틀, 고르는 규칙만 choose
# ---------------------------------------------------------------------------


def _all_costs(trip, ledgers, travel, escape_cost_min, *, exclude_self):
    return [U._plan_cost(trip, p, ledgers, travel, exclude_self=exclude_self,
                         escape_cost_min=escape_cost_min) for p in trip.plans]


@dataclass(frozen=True)
class SueEvaluation:
    current: dict[str, float]      # 실제 체류(또는 이탈 비용)
    excess: dict[str, float]       # 혼자 바꿨을 때 줄어드는 인지 비용 (0 이상)
    visits: tuple

    @property
    def rel_gap(self) -> float:
        total = sum(self.current.values())
        return sum(self.excess.values()) / total if total > 0 else 0.0


def evaluate_sue(trips, choice, chargers, travel, escape_cost_min, sue: SueSettings) -> SueEvaluation:
    ledgers, visits = U._roll(trips, choice, chargers, travel, escape_cost_min)
    current: dict[str, float] = {}
    for v in visits:
        current[v.ev_id] = current.get(v.ev_id, 0.0) + v.dwell_min
    for trip in trips:
        if trip.plans[choice[trip.ev_id]].is_escape:
            current[trip.ev_id] = U.escape_cost_of(trip, escape_cost_min)

    excess: dict[str, float] = {}
    for trip in trips:
        mine = choice[trip.ev_id]
        costs = [c for c, _ in _all_costs(trip, ledgers, travel, escape_cost_min, exclude_self=True)]
        costs[mine] = current[trip.ev_id]   # 내 계획은 실제로 굴린 값 (ue.evaluate 와 같다)
        excess[trip.ev_id] = max(0.0, choose(trip, costs, mine, sue).gain)
    return SueEvaluation(current, excess, tuple(visits))


def _sweep_sue(trips, choice, chargers, current: SueEvaluation, settings: U.UESettings,
               travel, sue: SueSettings) -> int:
    by_id = {t.ev_id: t for t in trips}
    ledgers = U._ledgers(chargers)
    own: dict[str, list[str]] = {}
    for v in current.visits:
        trip = by_id[v.ev_id]
        ledgers[v.station_id].commit(
            U._arrival(trip, v.stop_seq - 1, v.t_arrive_min, trip.plans[choice[v.ev_id]]))
        own.setdefault(v.ev_id, []).append(v.station_id)

    switched = 0
    for trip in sorted(trips, key=lambda t: (t.entry_min, t.ev_id)):
        for sid in own.get(trip.ev_id, ()):
            ledgers[sid].cancel(trip.ev_id)
        evals = _all_costs(trip, ledgers, travel, settings.escape_cost_min, exclude_self=False)
        mine = choice[trip.ev_id]
        ch = choose(trip, [c for c, _ in evals], mine, sue)
        if ch.target != mine and ch.gain >= max(settings.min_gain_min, U.GAIN_EPS_MIN):
            choice[trip.ev_id] = mine = ch.target
            switched += 1
        for arr, stop in zip(evals[mine][1], trip.plans[mine].stops, strict=True):
            ledgers[stop.station_id].commit(arr)
    return switched


def free_flow_choice_sue(trips, chargers, travel, escape_cost_min, sue: SueSettings) -> dict[str, int]:
    """빈 휴게소 기준 각자 고른 계획 (반복 0). 이탈 판정·인지 비용 규칙은 choose 와 같다."""

    empty = U._ledgers(chargers)
    out = {}
    for trip in trips:
        costs = [c for c, _ in _all_costs(trip, empty, travel, escape_cost_min, exclude_self=False)]
        # mine 자리에 아무 값이나 — target 만 쓴다
        out[trip.ev_id] = choose(trip, costs, 0, sue).target
    return out


def solve_sue(trips: Sequence[TripDemand], chargers: Mapping[str, tuple[Charger, ...]],
              settings: U.UESettings, sue: SueSettings) -> U.UEResult:
    """고정 ε 위의 균형. 반환형·예외는 `ue.solve_ue` 와 같다 (UENotConverged 포함)."""

    if math.isinf(sue.theta):
        return U.solve_ue(trips, chargers, settings)

    trips = sorted(trips, key=lambda t: t.ev_id)
    ids = [t.ev_id for t in trips]
    if len(set(ids)) != len(ids):
        raise ValueError("ev_id 가 중복된다")
    missing = sorted({s for t in trips for p in t.plans for s in p.station_ids} - set(chargers))
    if missing:
        raise ValueError(f"충전기 정보가 없는 휴게소가 계획에 있다: {missing}")

    travel = settings.travel_time()
    choice = free_flow_choice_sue(trips, chargers, travel, settings.escape_cost_min, sue)
    history: list[U.IterationStat] = []
    floor = max(settings.min_gain_min, U.GAIN_EPS_MIN)

    for k in range(settings.max_iter + 1):
        ev = evaluate_sue(trips, choice, chargers, travel, settings.escape_cost_min, sue)
        n_improvable = sum(1 for e in ev.excess.values() if e >= floor)
        done = ev.rel_gap <= settings.gap_tol or n_improvable == 0
        if done or k == settings.max_iter:
            history.append(U.IterationStat(k, ev.rel_gap, sum(ev.current.values()), n_improvable, 0))
            break
        switched = _sweep_sue(trips, choice, chargers, ev, settings, travel, sue)
        history.append(U.IterationStat(k, ev.rel_gap, sum(ev.current.values()), n_improvable, switched))

    result = U.UEResult(choice=dict(choice), history=tuple(history), converged=done,
                        visits=ev.visits, dwell_by_ev=dict(ev.current))
    if not done:
        raise U.UENotConverged(
            f"SUE(θ={sue.theta}) 가 {settings.max_iter}회 안에 수렴하지 않았다: "
            f"gap {result.final_gap:.4f} > {settings.gap_tol}, 더 줄일 수 있는 차 {n_improvable}대",
            result)
    return result


def solve_and_log_sue(sue: SueSettings):
    """`runner.solve_and_log` 와 같은 시그니처의 함수를 만든다 (주입용)."""

    def _solve_and_log(trips, chargers, settings, writer):
        try:
            result = solve_sue(trips, chargers, settings, sue)
        except U.UENotConverged as exc:
            writer.append_many("solver_log", U.solver_log_rows(exc.result))
            raise
        writer.append_many("solver_log", U.solver_log_rows(result))
        return result

    return _solve_and_log
