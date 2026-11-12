"""MNL 정책과 참여율 래퍼 (#168).

지키는 것 (설계 `docs/mnl_oracle_design.md` §5 의 V1~V7)

    V1  θ=inf, w=1 이면 MNL 은 S0 와 **충전 이벤트까지** 같다
    V2  p=1 이면 래퍼는 엔진 런과 같다
    V3  p=0 이면 엔진은 한 번도 불리지 않는다
    V4  참여자 집합은 p 에 대해 중첩되고, 같은 시드면 재현된다
    V6  Gumbel ε 로 고른 점유율이 로짓 확률과 맞는다
    V7  MNL 은 못 닿는 휴게소를 고르지 않는다 (stranded 0)
    +   escape_mode="s0" 면 누가 이탈하는지는 θ·w 와 무관하게 S0 와 같다
    +   비참여자는 주행 중 재질문을 받지 않는다 (S4 의 reask_en_route)
    +   Oracle 용 예측은 비참여자의 과거 도착까지 준다
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from evdt.engine.forecast import ArrivalSpec, OracleForecast
from evdt.engine.mnl import MnlPolicy, MnlSettings, background_settings, gumbel
from evdt.engine.participation import (
    NonParticipantAwareForecast,
    ParticipationPolicy,
    is_participant,
    participants,
    participation_u,
)
from evdt.engine.reservation import ReservationLedger
from evdt.engine.s0 import S0Policy, S0Settings
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.interfaces import (
    ESCAPE_BALKED,
    EVState,
    Policy,
    StationView,
    WorldView,
    read_answer,
)
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
BATTERY = 100.0
CONSUMPTION = 0.2
AMOUNT = ChargeAmount(buffer_km=10.0, reserve_soc=0.1, target_soc_cap=0.8)


def _settings(escape_cost_min: float = 0.0) -> S0Settings:
    return S0Settings(buffer_km=10.0, reserve_soc=0.1, target_soc_cap=0.8,
                      max_stops=3, escape_cost_min=escape_cost_min)


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, *, soc0: float = 0.45, dest_km: float = 300.0,
        escape_cost_min: float = 0.0) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=0.0,
        dest_offset_km=dest_km, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
        escape_cost_min=escape_cost_min,
    )


def _run(evs, stations, offsets, policy, *, escape_cost_min: float = 0.0, **kw):
    return run_corridor(
        stations, offsets, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
        escape_cost_min=escape_cost_min, **kw,
    )


def _key(result) -> tuple:
    """두 런이 같은지 비교할 재료 — 누가 어디서 언제 얼마나."""

    ch = sorted((e["ev_id"], e["station_id"], round(e["t_arrive_min"], 9),
                 round(e["dwell_min"], 9)) for e in result.charge_events)
    es = sorted((e["ev_id"], e["reason"]) for e in result.escape_events)
    return tuple(ch), tuple(es)


# 혼잡이 생기는 작은 코리도 — 60대가 5분 간격, 충전기 1기씩 (test_s0 의 진동 예제)
STATIONS = [_spec("A", 200.0), _spec("B", 200.0), _spec("C", 100.0)]
OFFSETS = {"A": 100.0, "B": 150.0, "C": 200.0}


def _fleet(n: int = 60, escape: float = 0.0, gap_min: float = 5.0,
           socs: tuple[float, ...] = (0.45,)) -> list[SimEV]:
    return [_ev(f"e{i:02d}", i * gap_min, soc0=socs[i % len(socs)], escape_cost_min=escape)
            for i in range(n)]


# ---------------------------------------------------------------------------
# V1 — θ=inf 면 S0 와 같다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape_mode", ["s0", "noisy"])
@pytest.mark.parametrize("escape", [0.0, 10.0])
def test_mnl_with_infinite_theta_is_s0(escape: float, escape_mode: str):
    # SoC 가 섞이면 충전시간이 갈리고, 이탈 비용 10분에서 일부(20/60)만 나간다 —
    # 이탈 판정까지 같아야 한다 (두 이탈 방식 모두)
    evs = _fleet(escape=escape, gap_min=1.0, socs=(0.3, 0.45, 0.6))
    s0 = _run(evs, STATIONS, OFFSETS, S0Policy(_settings(escape)), escape_cost_min=escape)
    mnl = _run(evs, STATIONS, OFFSETS,
               MnlPolicy(_settings(escape),
                         mnl=MnlSettings(theta=math.inf, seed=7, escape_mode=escape_mode)),
               escape_cost_min=escape)

    assert _key(mnl) == _key(s0)
    if escape:
        assert s0.escape_events, "이탈 비용을 낮게 줬으니 이탈이 있어야 비교가 의미 있다"


def test_finite_theta_changes_choices_but_keeps_everyone():
    evs = _fleet()
    mnl = _run(evs, STATIONS, OFFSETS,
               MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=1)))
    s0 = _run(evs, STATIONS, OFFSETS, S0Policy(_settings()))

    assert _key(mnl) != _key(s0)
    assert mnl.n_stranded == 0                                      # V7
    assert len(mnl.charge_events) + len(mnl.escape_events) == len(s0.charge_events) + len(
        s0.escape_events)


# ---------------------------------------------------------------------------
# V6 — 로짓 확률
# ---------------------------------------------------------------------------


def test_gumbel_argmin_matches_logit_probabilities():
    costs = {"A": 10.0, "B": 12.0, "C": 15.0}
    theta, n = 0.4, 20_000
    hits = dict.fromkeys(costs, 0)
    for i in range(n):
        ev = f"ev{i}"
        pick = min(costs, key=lambda s: costs[s] - gumbel(3, ev, s) / theta)
        hits[pick] += 1

    z = sum(math.exp(-theta * c) for c in costs.values())
    for s, c in costs.items():
        p = math.exp(-theta * c) / z
        sd = math.sqrt(p * (1 - p) / n)
        assert abs(hits[s] / n - p) < 4 * sd, (s, hits[s] / n, p)


def test_noise_is_fixed_per_person_and_independent_of_others():
    m = MnlSettings(theta=0.5, seed=11)
    assert m.noise("x", "A") == m.noise("x", "A")
    assert m.noise("x", "A") != m.noise("y", "A")
    assert MnlSettings(theta=0.5, seed=12).noise("x", "A") != m.noise("x", "A")
    assert MnlSettings(seed=11).noise("x", "A") == 0.0          # θ=inf


def _view(wait_a: float, wait_b: float) -> WorldView:
    def st(sid, off, wait):
        return StationView(station_id=sid, offset_km=off, lat=36.0, lon=127.0,
                           charger_powers_kw=(200.0,), n_charging=0, n_waiting=0,
                           next_free_min=(0.0,), wait_min=wait)
    return WorldView(t_min=0.0, stations={"A": st("A", 100.0, wait_a),
                                          "B": st("B", 150.0, wait_b)},
                     temp_c=20.0, cold_factor=1.0)


def _state(ev_id: str, **kw) -> EVState:
    base = dict(ev_id=ev_id, vclass_id="vc", corridor_id="c", offset_km=0.0,
                speed_kmh=SPEED, soc=0.45, soc_target=0.8, battery_kwh=BATTERY,
                vmax_kw=200.0, dest_offset_km=300.0, is_participant=True,
                consumption_kwh_km=CONSUMPTION, curve=FLAT)
    base.update(kw)
    return EVState(**base)


def test_a_drivers_answer_does_not_depend_on_who_else_is_asked():
    pol = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.1, seed=5))
    w = _view(10.0, 10.0)
    alone = pol.decide([_state("me")], w)["me"]
    crowd = pol.decide([_state(f"o{i}") for i in range(30)] + [_state("me")], w)["me"]
    assert alone == crowd


def test_small_theta_spreads_choices_large_wait_gap_does_not_matter_much():
    pol = MnlPolicy(_settings(), mnl=MnlSettings(theta=1e-6, seed=2))
    got = pol.decide([_state(f"e{i}") for i in range(2000)], _view(0.0, 60.0))
    share_a = sum(1 for v in got.values() if v == "A") / len(got)
    assert 0.45 < share_a < 0.55, "θ→0 이면 비용과 무관하게 거의 반반"


# ---------------------------------------------------------------------------
# 이탈 판정 — escape_mode="s0" 면 이탈 여부는 θ·w 와 무관하게 S0 와 같다
# ---------------------------------------------------------------------------


def _balked(answer: str) -> bool:
    return read_answer(answer)[0] == ESCAPE_BALKED


@pytest.mark.parametrize("theta", [0.01, 0.05, 1.0])
@pytest.mark.parametrize("wait_weight", [1.0, 1.5])
def test_s0_escape_mode_balks_exactly_when_s0_does(theta: float, wait_weight: float):
    # 대기·이탈 비용을 섞어 S0 가 일부만 나가는 화면들. 잡음이 커도(θ=0.01)
    # 누가 나가는지는 S0 와 한 대도 다르지 않아야 한다 — MNL 은 "어디서" 만 바꾼다
    s0 = S0Policy(_settings())
    mnl = MnlPolicy(_settings(), mnl=MnlSettings(theta=theta, wait_weight=wait_weight,
                                                 seed=3, escape_mode="s0"))
    n_balk = 0
    for wa, wb in ((0.0, 0.0), (20.0, 40.0), (60.0, 30.0), (90.0, 120.0)):
        view = _view(wa, wb)
        evs = [_state(f"e{i}", soc=(0.3, 0.45, 0.6)[i % 3], escape_cost_min=esc)
               for i, esc in enumerate([40.0, 60.0, 90.0, 120.0] * 50)]
        a, b = s0.decide(evs, view), mnl.decide(evs, view)
        for ev in evs:
            assert _balked(a[ev.ev_id]) == _balked(b[ev.ev_id]), ev.ev_id
        n_balk += sum(_balked(v) for v in a.values())
    assert 0 < n_balk < 800, "일부만 나가는 화면이어야 비교가 의미 있다"


def test_noisy_escape_mode_balks_more_often_than_s0_when_costs_are_close():
    # 충전 비용이 이탈 비용 근처면 "noisy" 는 잡음으로 일부가 더 나간다 — #168 첫 실행의 현상
    view = _view(40.0, 60.0)
    evs = [_state(f"e{i}", escape_cost_min=90.0) for i in range(1000)]
    s0 = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=3, escape_mode="s0"))
    noisy = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=3, escape_mode="noisy"))
    n_s0 = sum(_balked(v) for v in s0.decide(evs, view).values())
    n_noisy = sum(_balked(v) for v in noisy.decide(evs, view).values())
    assert n_noisy > n_s0


def test_rejects_unknown_escape_mode():
    with pytest.raises(ValueError):
        MnlSettings(escape_mode="maybe")


def test_mnl_never_picks_a_station_it_cannot_reach():
    # SoC 0.12 → 12 kWh → 60 km. 버퍼 10 km 면 A(100 km)·B(150 km) 둘 다 못 간다
    pol = MnlPolicy(_settings(), mnl=MnlSettings(theta=1e-6, seed=2))
    got = pol.decide([_state(f"e{i}", soc=0.12) for i in range(200)], _view(0.0, 0.0))
    assert {read_answer(v)[0] for v in got.values()} == {"__escape_no_plan__"}


def test_background_settings_copies_world_values_from_engine_settings():
    s1 = S1Settings(buffer_km=12.0, reserve_soc=0.15, target_soc_cap=0.8, max_stops=2,
                    habit_soc=0.6, escape_cost_min=90.0)
    s0 = background_settings(s1)
    assert type(s0) is S0Settings
    assert (s0.buffer_km, s0.reserve_soc, s0.max_stops, s0.habit_soc, s0.escape_cost_min) == (
        12.0, 0.15, 2, 0.6, 90.0)


# ---------------------------------------------------------------------------
# V2 · V3 · V4 — 참여율 래퍼
# ---------------------------------------------------------------------------


class Spy:
    """불린 횟수만 세는 엔진. 답은 S0 에게 맡긴다."""

    def __init__(self, inner: Policy, reask: bool = False) -> None:
        self.inner, self.calls, self.asked = inner, 0, []
        self.stage = "SPY"
        self.reask_en_route = reask

    def decide(self, evs, world):
        self.calls += 1
        self.asked.extend(ev.ev_id for ev in evs)
        return self.inner.decide(evs, world)


def test_full_participation_is_the_engine_run():                    # V2
    evs = _fleet()
    engine = S0Policy(_settings())
    bg = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=1))
    wrapped = _run(evs, STATIONS, OFFSETS, ParticipationPolicy(engine, bg, p=1.0, seed=1))
    plain = _run(evs, STATIONS, OFFSETS, engine)
    assert _key(wrapped) == _key(plain)


# ---------------------------------------------------------------------------
# 엔진이 비참여자를 얼마나 아나 — nonpart_info (결정 문서 조건 ②)
# ---------------------------------------------------------------------------


def _s1_kw(escape_cost_min: float = 0.0) -> dict:
    return dict(buffer_km=10.0, reserve_soc=0.1, target_soc_cap=0.8, max_stops=3,
                escape_cost_min=escape_cost_min)


def _ledger():
    return ReservationLedger.build({s.station_id: s.chargers for s in STATIONS})


@pytest.mark.parametrize("info", ["none", "present", "plans"])
def test_full_participation_ignores_nonparticipant_info(info: str):
    # p = 1 이면 비참여자가 없다 — 정보 방식이 무엇이든 엔진 단독 실행과 같아야 한다
    evs = _fleet(escape=10.0, gap_min=1.0, socs=(0.3, 0.45, 0.6))
    bg = MnlPolicy(_settings(10.0), mnl=MnlSettings(theta=0.05, seed=1))
    wrapped = _run(evs, STATIONS, OFFSETS,
                   ParticipationPolicy(S1Policy(S1Settings(**_s1_kw(10.0))), bg, p=1.0,
                                       seed=1, nonpart_info=info),
                   escape_cost_min=10.0, ledger=_ledger())
    plain = _run(evs, STATIONS, OFFSETS, S1Policy(S1Settings(**_s1_kw(10.0))),
                 escape_cost_min=10.0, ledger=_ledger())
    assert _key(wrapped) == _key(plain)


def test_partial_participation_books_nonparticipants_only_when_asked_to():
    evs = _fleet(gap_min=1.0, socs=(0.3, 0.45, 0.6))
    booked = {}
    for info in ("none", "present", "plans"):
        bg = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=2))
        w = ParticipationPolicy(S1Policy(S1Settings(**_s1_kw())), bg, p=0.5, seed=2,
                                nonpart_info=info)
        _run(evs, STATIONS, OFFSETS, w, ledger=_ledger())
        booked[info] = w.counters["nonpart_booked"]
    assert booked["none"] == 0
    assert 0 < booked["present"] <= booked["plans"]


class _FakeLedger:
    def __init__(self):
        self.stations = {"A": object(), "B": object()}
        self.calls: list[tuple[str, str, float]] = []

    def book(self, ev_id, station_id, arr):
        self.calls.append((ev_id, station_id, arr.arrival_min))


def test_present_mode_reveals_a_nonparticipant_only_after_it_arrives():
    # 비참여자 하나가 t=0 에 A(100 km)를 고른다. 60 km/h 라 도착은 t=100
    led = _FakeLedger()
    view = replace(_view(0.0, 60.0), ledger=led)
    bg = MnlPolicy(_settings(), mnl=MnlSettings(theta=math.inf, seed=1))
    w = ParticipationPolicy(S0Policy(_settings()), bg, p=0.0, seed=1, nonpart_info="present")
    assert w.decide([_state("np")], view)["np"] == "A"
    assert led.calls == [], "고른 순간에는 아직 안 보인다 (미래 계획은 비공개)"
    w.decide([], replace(view, t_min=50.0))
    assert led.calls == []
    w.decide([], replace(view, t_min=120.0))
    assert led.calls == [("np", "A", pytest.approx(100.0))]


def test_plans_mode_reveals_a_nonparticipant_when_it_chooses():
    led = _FakeLedger()
    view = replace(_view(0.0, 60.0), ledger=led)
    bg = MnlPolicy(_settings(), mnl=MnlSettings(theta=math.inf, seed=1))
    w = ParticipationPolicy(S0Policy(_settings()), bg, p=0.0, seed=1, nonpart_info="plans")
    w.decide([_state("np")], view)
    assert led.calls == [("np", "A", pytest.approx(100.0))]


def test_rejects_unknown_nonpart_info():
    with pytest.raises(ValueError):
        ParticipationPolicy(S0Policy(_settings()), S0Policy(_settings()), p=0.5, seed=1,
                            nonpart_info="psychic")


def test_zero_participation_never_calls_the_engine():               # V3
    evs = _fleet()
    spy = Spy(S0Policy(_settings()))
    bg = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=1))
    wrapper = ParticipationPolicy(spy, bg, p=0.0, seed=1)
    wrapped = _run(evs, STATIONS, OFFSETS, wrapper)
    alone = _run(evs, STATIONS, OFFSETS, bg)
    assert spy.calls == 0
    assert wrapper.counters["engine_asked"] == 0
    assert _key(wrapped) == _key(alone)


def test_partial_participation_splits_by_the_fixed_draw():
    evs = _fleet()
    spy = Spy(S0Policy(_settings()))
    bg = MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=4))
    _run(evs, STATIONS, OFFSETS, ParticipationPolicy(spy, bg, p=0.5, seed=4))
    expected = participants(4, (e.ev_id for e in evs), 0.5)
    assert set(spy.asked) <= expected
    assert 0 < len(expected) < len(evs)


def test_participant_sets_are_nested_and_reproducible():            # V4
    ids = [f"ev{i}" for i in range(5000)]
    sets = [participants(9, ids, p) for p in (0.3, 0.6, 0.9)]
    assert sets[0] <= sets[1] <= sets[2]
    for p, s in zip((0.3, 0.6, 0.9), sets, strict=True):
        assert abs(len(s) / len(ids) - p) < 0.03
    assert participants(9, ids, 0.6) == sets[1]
    assert participants(10, ids, 0.6) != sets[1]
    assert participation_u(9, "ev1") != gumbel(9, "ev1", "A")   # 흐름이 다르다
    assert is_participant(9, "x", 1.0) and not is_participant(9, "x", 0.0)


def test_rejects_participation_outside_unit_interval():
    with pytest.raises(ValueError):
        ParticipationPolicy(S0Policy(_settings()), S0Policy(_settings()), p=1.2, seed=0)


def test_non_participants_are_not_reasked_en_route():
    """엔진이 reask_en_route 를 켜도, 이미 고른 비참여자는 배경 모델에 다시 묻지 않는다."""

    evs = _fleet(30)
    engine = Spy(S0Policy(_settings()), reask=True)
    bg = Spy(MnlPolicy(_settings(), mnl=MnlSettings(theta=0.05, seed=3)))
    wrapper = ParticipationPolicy(engine, bg, p=0.5, seed=3)
    r = _run(evs, STATIONS, OFFSETS, wrapper)

    assert wrapper.reask_en_route is True
    assert wrapper.counters["background_en_route_skipped"] > 0
    # 비참여자는 정차마다 한 번만 고른다 → 질문 수 = 고른 정차 수 (+ 무충전·이탈 답)
    non = [e for e in evs if not is_participant(3, e.ev_id, 0.5)]
    asked_once = {e.ev_id for e in non}
    assert set(bg.asked) <= asked_once
    nonpart_visits = sum(1 for e in r.charge_events if e["ev_id"] in asked_once)
    assert len(bg.asked) <= nonpart_visits + len(asked_once)
    assert r.n_stranded == 0


# ---------------------------------------------------------------------------
# Oracle 용 예측 — 비참여자는 과거 도착까지 보인다
# ---------------------------------------------------------------------------


def _oracle() -> OracleForecast:
    rows = [
        {"ev_id": "np_early", "station_id": "A", "t_arrive_min": 10.0, "soc_in": 0.2, "soc_out": 0.8},
        {"ev_id": "np_edge", "station_id": "A", "t_arrive_min": 50.0, "soc_in": 0.2, "soc_out": 0.8},
        {"ev_id": "p_early", "station_id": "A", "t_arrive_min": 20.0, "soc_in": 0.2, "soc_out": 0.8},
        {"ev_id": "np_late", "station_id": "A", "t_arrive_min": 70.0, "soc_in": 0.2, "soc_out": 0.8},
        {"ev_id": "p_late", "station_id": "A", "t_arrive_min": 80.0, "soc_in": 0.2, "soc_out": 0.8},
        {"ev_id": "np_after", "station_id": "A", "t_arrive_min": 200.0, "soc_in": 0.2, "soc_out": 0.8},
    ]
    spec = ArrivalSpec(BATTERY, 200.0, FLAT)
    return OracleForecast.from_rows(rows, {r["ev_id"]: spec for r in rows}, source_run_id="t")


def test_oracle_forecast_shows_past_non_participants_only():
    inner = _oracle()
    fc = NonParticipantAwareForecast(inner, frozenset({"np_early", "np_edge", "np_late",
                                                       "np_after"}))
    got = sorted(a.ev_id for a in fc.arrivals_between("A", after_min=50.0, before_min=100.0))
    # 과거 비참여자(10, 50 경계 포함) + 미래 전원(70, 80). 과거 참여자(20)는 원장의 몫
    assert got == ["np_early", "np_edge", "np_late", "p_late"]
    # lookback 밖(210 − 30 = 180 보다 이른) 비참여자는 떠났다고 본다
    short = NonParticipantAwareForecast(inner, fc.non_participants, lookback_min=30.0)
    assert [a.ev_id for a in short.arrivals_between("A", after_min=210.0, before_min=300.0)] == [
        "np_after"]
    assert [a.ev_id for a in short.arrivals_between("A", after_min=50.0, before_min=60.0)] == [
        "np_edge"]
    assert [a.ev_id for a in inner.arrivals_between("A", after_min=50.0, before_min=100.0)] == [
        "np_late", "p_late"]
    assert fc.arrivals_between("B", after_min=0.0, before_min=10.0) == ()
    assert fc.round_no == inner.round_no                              # 나머지는 위임
