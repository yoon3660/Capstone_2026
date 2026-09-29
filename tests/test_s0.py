"""S0 정책과 Δt 루프 (이슈 #59).

핵심 완료조건은 하나다.

    5대가 1분 간격으로 출발하고 A 가 비어 보이면  →  S0 는 전원 A, UE 는 A·B 로 나뉜다

이게 사다리 첫 칸의 결론("정보를 주는 것만으로는 쏠림이 안 풀린다")의 최소 재현이다.
나머지 테스트는 그 결론이 **버그 때문에 나온 것이 아님**을 지킨다 — 차가 사라지지
않고, 큐가 DES 와 같은 규칙으로 돌고, 정책이 원장·예측을 몰래 보지 않는다.
"""

from __future__ import annotations

import pytest

from evdt.engine.s0 import S0Policy, S0Settings
from evdt.engine.ue import UESettings, solve_ue
from evdt.engine.ue_demand import Plan, PlannedStop, TripDemand
from evdt.interfaces import (
    ESCAPE_BALKED,
    ESCAPE_NO_PLAN,
    NO_CHARGE,
    EVState,
    Policy,
    StationView,
    WorldView,
    balked_at,
    read_answer,
)
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)     # 평탄 충전곡선 — 시간을 손으로 셀 수 있다
SPEED = 60.0                       # 1 km = 1 분
BATTERY = 100.0
CONSUMPTION = 0.2                  # kWh/km → 100 kWh 로 500 km (계수 1.0 기준)

AMOUNT = ChargeAmount(buffer_km=10.0, reserve_soc=0.1, target_soc_cap=0.8)


def _settings(escape_cost_min: float = 0.0) -> S0Settings:
    return S0Settings(buffer_km=10.0, reserve_soc=0.1, target_soc_cap=0.8,
                      max_stops=3, escape_cost_min=escape_cost_min)


SETTINGS = _settings()


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, *, soc0: float = 0.45,
        dest_km: float = 300.0, entry_km: float = 0.0) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=entry_km,
        dest_offset_km=dest_km, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
    )


def _run(evs, stations, offsets, *, settings=SETTINGS, dt_min=5.0, **kw):
    return run_corridor(
        stations, offsets, evs, S0Policy(settings), ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=dt_min, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED, **kw,
    )


# ---------------------------------------------------------------------------
# 완료조건 — S0 는 전원 A, UE 는 나뉜다
# ---------------------------------------------------------------------------


def test_s0_sends_everyone_to_the_station_that_looks_empty_ue_splits():
    """A 가 비어 보이면 S0 는 전원 A. 도착해서야 줄을 본다.

    A 와 B 는 충전기 1기씩이고 A 가 조금 더 빠르다(200 vs 150 kW). 출발 시점에는
    둘 다 대기 0 이므로 S0 는 다섯 대 전부 A 를 고른다 — 다섯 번째 차는 앞의 네 대가
    A 에 줄을 설 것을 **모른다.** UE 는 그 결과까지 알고 고르므로 나뉜다.
    """

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i}", float(i)) for i in range(5)]

    s0 = _run(evs, stations, offsets)
    chosen = {e["ev_id"]: e["station_id"] for e in s0.charge_events}

    assert set(chosen.values()) == {"A"}, "S0 는 화면이 빈 A 로 전원 몰려야 한다"
    assert len(chosen) == 5

    # 같은 차·같은 휴게소를 UE 로 풀면 나뉜다
    # 같은 정차·같은 충전량. A 가 200 kW, B 가 150 kW 라 빈 상태면 A 가 빠르다
    plans = (
        Plan((PlannedStop("A", 100.0, 0.25, 0.52),)),
        Plan((PlannedStop("B", 150.0, 0.15, 0.42),)),
    )
    trips = [
        TripDemand(f"e{i}", "vc", float(i), 0.0, 300.0, BATTERY, 200.0, FLAT, 1.0, plans)
        for i in range(5)
    ]
    chargers = {s.station_id: s.chargers for s in stations}
    ue = solve_ue(trips, chargers, UESettings(speed_kmh=SPEED, min_gain_min=0.5))

    assert len({p[0] for p in ue.station_of(trips).values()}) == 2, (
        "UE 는 도착 시점의 대기를 알기 때문에 A·B 로 나뉘어야 한다")


def test_s0_queue_oscillates_once_the_screen_turns_red():
    """몰림 → 화면이 빨개짐 → 다음 무리는 딴 데로. 이게 S0 의 큐 진동이다."""

    stations = [_spec("A", 200.0), _spec("B", 200.0)]
    offsets = {"A": 100.0, "B": 150.0}
    # **주행시간(100분)보다 길게** 편다. 그래야 뒤 무리가 앞 무리의 결과를 화면에서 본다.
    # 진입 간격(5분)이 충전시간(8분)보다 짧아 A 에 줄이 쌓인다
    evs = [_ev(f"e{i:02d}", i * 5.0) for i in range(60)]

    r = _run(evs, stations, offsets)
    by_station = {}
    for e in r.charge_events:
        by_station.setdefault(e["station_id"], []).append(e["ev_id"])

    assert set(by_station) == {"A", "B"}, "A 가 막히면 뒤 차들은 B 로 가야 한다"
    # 처음 결정한 무리는 전부 A (화면이 비어 있었다)
    first = sorted(r.charge_events, key=lambda e: e["t_arrive_min"])[:3]
    assert {e["station_id"] for e in first} == {"A"}


# ---------------------------------------------------------------------------
# 차가 사라지지 않는다 — 결론이 버그가 아니라는 최소 보장
# ---------------------------------------------------------------------------


def test_every_car_ends_somewhere():
    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i:02d}", i * 2.0) for i in range(25)]

    r = _run(evs, stations, offsets, settings=_settings(60.0), escape_cost_min=60.0)
    charged = {e["ev_id"] for e in r.charge_events}
    escaped = {e["ev_id"] for e in r.escape_events}

    assert charged | escaped == {ev.ev_id for ev in evs}
    assert not (charged & escaped), "충전도 하고 이탈도 한 차는 있을 수 없다"
    assert r.n_stranded == 0, "정책이 못 닿는 휴게소를 골랐다"


def test_late_entrants_are_not_dropped_at_the_horizon():
    """23:50 에 들어온 차도 끝까지 따라간다. 자르면 S0 만 KPI 가 좋아 보인다."""

    stations = [_spec("A", 200.0)]
    offsets = {"A": 100.0}
    evs = [_ev("late", 1430.0)]

    r = _run(evs, stations, offsets)

    assert len(r.charge_events) == 1
    assert r.charge_events[0]["t_arrive_min"] > 1440.0
    # 스냅샷은 horizon 까지만 (하루 그림의 축이 늘어나면 안 된다)
    assert max(s["t_min"] for s in r.snapshots) <= 1440.0


def test_a_car_that_needs_no_charge_just_drives():
    stations = [_spec("A", 200.0)]
    offsets = {"A": 100.0}
    r = _run([_ev("full", 0.0, soc0=0.95, dest_km=100.0)], stations, offsets)

    assert r.charge_events == ()
    assert r.escape_events == ()
    assert r.n_arrived == 1


def test_a_car_with_nothing_in_range_leaves_as_no_plan():
    """닿는 휴게소가 없어서 나가는 것과 줄이 길어서 나가는 것은 다른 일이다 (#54)."""

    stations = [_spec("far", 200.0)]
    offsets = {"far": 280.0}          # SoC 30% 로는 못 닿는다
    r = _run([_ev("thin", 0.0)], stations, offsets, settings=_settings(120.0),
             escape_cost_min=120.0)

    assert [e["reason"] for e in r.escape_events] == ["no_plan"]
    assert r.escape_events[0]["best_station_id"] == "", "닿는 곳이 없으니 적을 곳도 없다"


def test_a_car_that_balks_records_which_stop_pushed_it_out():
    stations = [_spec("A", 50.0)]     # 느린 충전기 1기(32분) — 줄이 금방 길어진다
    offsets = {"A": 100.0}
    evs = [_ev(f"e{i:02d}", i * 10.0) for i in range(15)]

    r = _run(evs, stations, offsets, settings=_settings(45.0), escape_cost_min=45.0)
    balked = [e for e in r.escape_events if e["reason"] == "balked"]

    assert balked, "줄이 이탈 비용보다 길어지면 나가야 한다"
    assert {e["best_station_id"] for e in balked} == {"A"}


# ---------------------------------------------------------------------------
# S0 가 보면 안 되는 것
# ---------------------------------------------------------------------------


def test_s0_never_touches_the_ledger_or_the_forecast():
    """S0 는 원장도 예측도 안 본다 (설계문서 §10.1). 보면 그 스테이지가 아니다."""

    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"S0 가 원장/예측을 봤다: .{name}")

    world = WorldView(
        t_min=0.0,
        stations={"A": StationView("A", 100.0, 36.0, 127.0, (200.0,), 0, 0, (0.0,), wait_min=0.0)},
        temp_c=20.0, cold_factor=1.0, range_factor=1.0,
        forecast=Boom(), ledger=Boom(),
    )
    ev = EVState("e", "vc", "c", 0.0, SPEED, 0.3, 0.8, BATTERY, 200.0, 300.0, True,
                 consumption_kwh_km=CONSUMPTION, curve=FLAT)

    assert S0Policy(SETTINGS).decide([ev], world) == {"e": "A"}


def test_s0_is_a_policy():
    assert isinstance(S0Policy(SETTINGS), Policy)
    assert S0Policy(SETTINGS).stage == "S0"


def test_s0_picks_the_shorter_screen_not_the_nearer_station():
    """같은 값이면 대기가 짧은 쪽. 거리로 고르는 것이 아니다."""

    world = WorldView(
        t_min=0.0,
        stations={
            "near": StationView("near", 100.0, 36.0, 127.0, (200.0,), 0, 0, (0.0,), wait_min=40.0),
            "far": StationView("far", 150.0, 36.0, 127.0, (200.0,), 0, 0, (0.0,), wait_min=0.0),
        },
        temp_c=20.0, cold_factor=1.0, range_factor=1.0,
    )
    ev = EVState("e", "vc", "c", 0.0, SPEED, 0.5, 0.8, BATTERY, 200.0, 300.0, True,
                 consumption_kwh_km=CONSUMPTION, curve=FLAT)

    assert S0Policy(SETTINGS).decide([ev], world) == {"e": "far"}


# ---------------------------------------------------------------------------
# 시뮬레이터는 단계를 모른다 (설계 규칙 2)
# ---------------------------------------------------------------------------


def test_the_loop_runs_any_policy_not_just_s0():
    """아무 정책이나 꽂아도 돈다. Δt 루프에 S0 가 박혀 있으면 사다리를 못 올린다."""

    class AlwaysB:
        stage = "TEST"

        def decide(self, evs, world):
            return {ev.ev_id: ("B" if ev.stops_done == 0 else NO_CHARGE) for ev in evs}

    stations = [_spec("A", 200.0), _spec("B", 200.0)]
    offsets = {"A": 100.0, "B": 150.0}
    r = _run([_ev(f"e{i}", float(i)) for i in range(4)], stations, offsets)
    assert {e["station_id"] for e in r.charge_events} == {"A"}   # S0 는 A

    r2 = run_corridor(
        stations, offsets, [_ev(f"e{i}", float(i)) for i in range(4)], AlwaysB(),
        ConstantSpeed(SPEED), AMOUNT, corridor_id="c", dt_min=5.0, horizon_min=1440.0,
        temp_c=20.0, cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
    )
    assert {e["station_id"] for e in r2.charge_events} == {"B"}


def test_the_loop_refuses_a_station_that_is_not_ahead():
    """앞으로만 간다.

    되돌아가기가 없다는 뜻이기도 하지만, **서 있는 자리를 다시 고르면 루프가 영원히
    돈다** (도착 → 넣을 것 없음 → 다시 물음 → 같은 답). 실제로 이 테스트를 쓰다가
    걸렸다. 조용히 허용하면 러너가 그냥 멈춘 것처럼 보인다.
    """

    class GoBack:
        stage = "TEST"

        def decide(self, evs, world):
            return {ev.ev_id: "A" for ev in evs}

    stations = [_spec("A", 200.0)]
    offsets = {"A": 50.0}
    evs = [_ev("e", 0.0, entry_km=200.0, dest_km=300.0)]

    with pytest.raises(ValueError, match="앞에 있지 않은 휴게소"):
        run_corridor(stations, offsets, evs, GoBack(), ConstantSpeed(SPEED), AMOUNT,
                     corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
                     cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED)


def test_an_undecided_car_is_asked_again_next_step():
    """빠진 ev_id 는 '이번 Δt 에는 못 정했다' 다. 버리면 차가 사라진다."""

    class Dithers:
        stage = "TEST"

        def __init__(self) -> None:
            self.calls = 0

        def decide(self, evs, world):
            self.calls += 1
            if self.calls < 3:
                return {}
            return {ev.ev_id: ("A" if ev.stops_done == 0 else NO_CHARGE) for ev in evs}

    stations = [_spec("A", 200.0)]
    offsets = {"A": 100.0}
    policy = Dithers()
    r = run_corridor(stations, offsets, [_ev("e", 0.0)], policy, ConstantSpeed(SPEED),
                     AMOUNT, corridor_id="c", dt_min=5.0, horizon_min=1440.0,
                     temp_c=20.0, cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED)

    assert policy.calls == 4          # 3번째에 A, 충전이 끝나고 한 번 더
    assert len(r.charge_events) == 1


# ---------------------------------------------------------------------------
# 정보의 나이 — Δt 가 곧 화면의 낡음
# ---------------------------------------------------------------------------


def test_a_car_sees_the_screen_as_of_the_last_refresh():
    """7.5분에 들어온 차는 Δt=5 에서 **t=5 의 화면**을 본다 (2.5분 낡은 정보).

    이 낡음이 S0 의 전부다. 차가 제 결정 시각의 화면을 본다면 그건 S0 가 아니라
    "느린 UE" 이고, 쏠림이 원리상 안 나온다.
    """

    seen: list[tuple[float, float]] = []

    class Recorder:
        stage = "TEST"

        def decide(self, evs, world):
            seen.extend((world.t_min, ev.offset_km) for ev in evs)
            return {ev.ev_id: NO_CHARGE for ev in evs}

    stations = [_spec("A", 200.0)]
    run_corridor(stations, {"A": 100.0}, [_ev("e", 7.5)], Recorder(), ConstantSpeed(SPEED),
                 AMOUNT, corridor_id="c", dt_min=5.0, horizon_min=60.0, temp_c=20.0,
                 cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED)

    assert seen == [(5.0, 0.0)], "화면은 t=5 의 것, 위치는 진입 지점 그대로여야 한다"


def test_the_drive_is_timed_from_the_cars_own_moment_not_the_grid():
    """정보만 낡는다. **주행시간은 근사하지 않는다** — 차의 실제 결정 시각에서 잰다.

    7.5분에 진입해 100 km 를 60 km/h 로 달리면 107.5분 도착이다. 화면을 t=5 에서
    봤다고 105분이 되면, Δt 를 키울 때마다 차가 공짜로 빨라진다.
    """

    stations = [_spec("A", 200.0)]
    r = _run([_ev("e", 7.5)], stations, {"A": 100.0}, dt_min=5.0)

    assert r.charge_events[0]["t_arrive_min"] == pytest.approx(107.5)


# ---------------------------------------------------------------------------
# 답 해석
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "expected"), [
    ("A", ("A", "")),
    (NO_CHARGE, (NO_CHARGE, "")),
    (ESCAPE_NO_PLAN, (ESCAPE_NO_PLAN, "")),
    (balked_at("안성"), (ESCAPE_BALKED, "안성")),
    (balked_at(""), (ESCAPE_BALKED, "")),
])
def test_read_answer(answer, expected):
    assert read_answer(answer) == expected


# ---------------------------------------------------------------------------
# 큐는 DES 와 같은 객체로 돈다
# ---------------------------------------------------------------------------


def test_station_queue_refuses_arrivals_that_go_backwards():
    """거꾸로 들어온 도착은 FIFO 를 깬다. 조용히 받으면 배치로 푼 결과와 달라진다."""

    from evdt.world.sim import Arrival, StationQueue

    q = StationQueue("A", _spec("A", 200.0).chargers)

    def arr(ev_id: str, t: float) -> Arrival:
        return Arrival(ev_id=ev_id, arrival_min=t, soc_from=0.2, soc_to=0.8,
                       battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)

    q.admit([arr("a", 10.0)])
    q.admit([arr("b", 20.0)])

    with pytest.raises(ValueError, match="거꾸로"):
        q.admit([arr("c", 15.0)])


def test_the_loop_gives_the_same_queue_as_the_des_for_the_same_arrivals():
    """Δt 루프의 충전 결과가 DES 와 같은 규칙을 따른다 (같은 세계의 전제).

    Δt 루프는 도착을 스텝마다 나눠서 넣고 DES 는 하루치를 한 번에 넣는다. FIFO 에서
    한 도착의 결과는 그보다 먼저 온 차들로만 정해지므로 **두 결과가 같아야 한다.**
    """

    from evdt.world.sim import EVArrival, run_charging_des

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i:02d}", i * 3.0) for i in range(15)]

    loop = _run(evs, stations, offsets)

    des = run_charging_des(stations, [
        EVArrival(ev_id=e["ev_id"], vclass_id=e["vclass_id"], station_id=e["station_id"],
                  t_arrive_min=e["t_arrive_min"], soc_in=e["soc_in"], soc_target=e["soc_out"],
                  battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, stop_seq=e["stop_seq"])
        for e in loop.charge_events
    ])

    got = {e["ev_id"]: (e["charger_id"], round(e["wait_min"], 9), round(e["t_end_min"], 9))
           for e in loop.charge_events}
    want = {e["ev_id"]: (e["charger_id"], round(e["wait_min"], 9), round(e["t_end_min"], 9))
            for e in des.charge_events}

    assert got == want
