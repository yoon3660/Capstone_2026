"""배차 시각은 **결정 시각보다 앞설 수 없다** (#147).

## 무엇이 고장나 있었나

충전을 마친 차는 `car.t_min = a.end_min` 을 갖는데, 그 값은 Δt 격자 위에 있지
않다 (103분에 끝났는데 다음 결정은 105분). 그런데 배차가

    eta = car.t_min + travel(..., car.t_min)      # 103 에서 출발한 셈
    car.left_at_min = t                           # 기록은 105

로 **서로 다른 시각**을 썼다. 결과는 둘이다.

1. **시간을 거슬러 출발한다.** 105분에 내린 결정인데 103분에 떠난 것이 된다
2. **FIFO 가 깨진다.** 바로 앞에 있는 휴게소를 고르면 eta 가 이번 스텝에서 이미
   처리한 도착보다 앞서고, `StationQueue.admit` 이 *"도착이 거꾸로 들어왔다"* 로
   멈춘다. 추석 S0 에서 실제로 터졌다

고친 모양은 `departure = max(t, car.t_min)` 이다. 두 방향 다 맞다 —
`car.t_min < t` 면 결정 시각으로 당기고, `car.t_min > t` (아직 안 끝난 차) 면
그대로 둔다.

## 왜 이 테스트가 필요한가

고친 PR 에 테스트가 없었다. 한 줄짜리 수정이라 되돌리기 쉽고, 되돌아가도
**데이터에 따라서만** 터진다 (설에서는 안 터졌고 추석에서 터졌다). 조용히 틀린
결과가 나오는 쪽이 더 흔하다 — 배차가 앞당겨지면 그 차가 FIFO 에서 제자리보다
앞에 서고, 뒤차의 대기가 **실제보다 길게** 계산된다.
"""

from __future__ import annotations

import pytest

from evdt.engine.s0 import S0Policy, S0Settings
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
DT = 5.0
AMOUNT = ChargeAmount(buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8)
KW = {"buffer_km": 30.0, "reserve_soc": 0.2, "target_soc_cap": 0.8, "max_stops": 2}


class _RecordingTravel(ConstantSpeed):
    """`minutes` 에 들어온 출발 시각을 그대로 모은다."""

    def __init__(self, speed_kmh: float) -> None:
        super().__init__(speed_kmh)
        object.__setattr__(self, "departures", [])

    def minutes(self, from_km: float, to_km: float, depart_min: float = 0.0) -> float:
        self.departures.append(float(depart_min))
        return super().minutes(from_km, to_km, depart_min)


class _RecordingPolicy(S0Policy):
    """정책이 본 화면의 시각을 **그 시점까지의 배차 수와 함께** 모은다.

    배차는 `decide` 바로 뒤에 일어나므로, 두 표식 사이의 배차는 전부 앞 표식의
    결정에 속한다. 그렇게 짝지어야 *"이 배차는 어느 결정의 것인가"* 를 말할 수
    있다 — 전체 최솟값과만 견주면 **고치기 전 코드도 통과한다** (진입 시각이
    0 보다 크므로).
    """

    def decide(self, evs, world):
        self.marks.append((float(world.t_min), len(self.travel.departures)))
        return super().decide(evs, world)


class _DeferOncePolicy(_RecordingPolicy):
    """차마다 **한 번은 답을 미룬다.** 버그를 재현하는 경로다 (#147).

    `corridor_sim` 은 답이 없으면 그 차를 다음 스텝에 다시 묻는다
    (*"이번 Δt 에는 못 정했다"*). 그런데 **`car.t_min` 은 그대로 둔다.** 그래서
    다음 스텝에서 답이 나오면 고치기 전 코드는 **낡은 시각에서 출발**시킨다 —
    진입 1.7분인 차가 5.0분에 내린 결정으로 1.7분에 떠난다.

    실제 정책에도 있는 경로다. 그룹 최적화가 이번 창에서 답을 못 내거나, 가던
    차에게 변경을 안 주는 경우가 전부 여기로 떨어진다.
    """

    def decide(self, evs, world):
        answers = dict(super().decide(evs, world))
        for ev in evs:
            if ev.ev_id not in self.deferred:
                self.deferred.add(ev.ev_id)
                answers.pop(ev.ev_id, None)
        return answers


def _pairs(policy) -> list[tuple[float, float]]:
    """(결정 시각, 그 결정이 만든 출발 시각) 목록."""

    out: list[tuple[float, float]] = []
    marks = policy.marks
    deps = policy.travel.departures

    for i, (tick, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(deps)
        out.extend((tick, d) for d in deps[start:end])

    return out


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, soc0: float) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=0.0,
        dest_offset_km=350.0, soc0=soc0, battery_kwh=60.0,
        consumption_kwh_km=0.2, vmax_kw=200.0, curve=FLAT,
    )


def _world():
    """두 번 서야 하는 무대. 두 번째 배차가 **충전을 마친 직후**의 배차다.

    A(60km) 에서 0.8 까지 채워도 240km 까지밖에 못 가므로, 350km 목적지에 가려면
    200km 근처에서 한 번 더 서야 한다. B 와 C 를 붙여 둔 것은 두 번째 배차의
    주행시간을 짧게 만들어 **시각이 거꾸로 가는 창**을 넓히기 위해서다.
    """

    stations = [_spec("A", 50.0, 1), _spec("B", 50.0, 1), _spec("C", 200.0, 1)]
    return stations, {"A": 60.0, "B": 200.0, "C": 205.0}


def _run(dt: float = DT, *, defer: bool = False):
    stations, offsets = _world()
    travel = _RecordingTravel(SPEED)
    cls = _DeferOncePolicy if defer else _RecordingPolicy
    policy = cls(S0Settings(**KW, escape_cost_min=0.0))
    object.__setattr__(policy, "marks", [])
    object.__setattr__(policy, "travel", travel)
    object.__setattr__(policy, "deferred", set())

    # 진입도 충전 종료도 Δt 격자에서 **벗어난 시각**이 되도록 흩뜨린다.
    evs = [_ev(f"e{i}", entry_min=float(i) * 1.7, soc0=0.30 + 0.01 * (i % 5))
           for i in range(12)]

    out = run_corridor(
        stations, offsets, evs, policy, travel, AMOUNT,
        corridor_id="c", dt_min=dt, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
    )
    return out, travel, policy


def test_the_run_does_not_trip_the_fifo_guard() -> None:
    """고치기 전에는 어떤 자료에서 `admit` 이 *"도착이 거꾸로 들어왔다"* 로 멈췄다."""

    out, _, _ = _run()

    assert out is not None


def test_a_car_never_departs_before_the_decision_that_sent_it() -> None:
    """**이 테스트가 본체다.**

    배차를 만든 결정의 시각보다 먼저 떠난 차가 있으면 안 된다. 먼저 떠나면
    그 차는 FIFO 에서 제자리보다 앞에 서고, 뒤차의 대기가 실제보다 길게 계산된다.
    """

    _, _, policy = _run()
    pairs = _pairs(policy)

    assert pairs, "배차가 한 번도 없었다 — 무대가 잘못됐다"

    early = [(tick, dep) for tick, dep in pairs if dep < tick - 1e-9]

    assert not early, (
        f"결정보다 {len(early)}건이 먼저 떠났다 (결정시각, 출발시각): {early[:5]}")


def test_a_deferred_answer_does_not_depart_in_the_past() -> None:
    """**이것이 고장을 재현하는 테스트다.**

    정책이 한 스텝 답을 미루면 `car.t_min` 이 낡은 채로 다음 결정에 들어간다.
    고치기 전 코드는 그 낡은 시각에서 출발시킨다 — 진입 1.7분인 차가 5.0분에
    내린 결정으로 **1.7분에** 떠난다. 그 차는 FIFO 에서 제자리보다 앞에 서고,
    뒤차의 대기가 실제보다 길게 계산된다.
    """

    _, _, policy = _run(defer=True)
    pairs = _pairs(policy)

    assert pairs, "배차가 한 번도 없었다 — 무대가 잘못됐다"

    early = [(tick, dep) for tick, dep in pairs if dep < tick - 1e-9]

    assert not early, (
        f"결정보다 {len(early)}건이 먼저 떠났다 (결정시각, 출발시각): {early[:5]}")


def test_the_deferred_scenario_really_reaches_the_bug() -> None:
    """무대 검사. 미뤄진 배차가 없으면 위 테스트는 고장난 코드도 통과한다.

    `max(t, car.t_min)` 가 당겨 놓은 자리 = 결정 시각과 **정확히 같은** 출발.
    """

    _, _, policy = _run(defer=True)
    pulled = [1 for tick, dep in _pairs(policy) if abs(dep - tick) < 1e-9]

    assert len(pulled) >= 3, (
        f"결정 시각으로 당겨진 배차가 {len(pulled)}건뿐이다 — 무대가 이 버그를 "
        "건드리지 못한다")


@pytest.mark.parametrize("dt", [1.0, 5.0, 15.0])
def test_the_guard_holds_for_any_step_size(dt: float) -> None:
    """Δt 가 클수록 충전 종료와 결정 사이의 틈이 넓어진다 — 거기서 터졌다."""

    _, _, policy = _run(dt)
    early = [(tick, dep) for tick, dep in _pairs(policy) if dep < tick - 1e-9]

    assert not early, f"Δt={dt}: 결정보다 먼저 떠난 배차 {early[:5]}"
