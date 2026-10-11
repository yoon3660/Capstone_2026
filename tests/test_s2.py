"""S2 — 아직 결정하지 않은 차까지 보는 운전자 (#118).

이 파일의 **첫 번째 테스트가 나머지 전부를 떠받친다.**

    빈 예측으로 돈 S2 == S1  (비트 단위로)

이게 깨지면 "S2 가 S1 보다 좋다/나쁘다" 가 **예측 때문인지 배선 때문인지 구분할 수
없다.** S0→S1 비교에서 설정값을 하나도 다르게 두지 않은 것과 같은 이유다.
"""

from __future__ import annotations

import pytest

from evdt.engine.forecast import ArrivalSpec, OracleForecast, describe_shift
from evdt.engine.ledger import Arrival
from evdt.engine.reservation import ReservationLedger
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.engine.s2 import S2Policy, S2Settings
from evdt.interfaces import WorldView
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
BATTERY = 60.0
CONSUMPTION = 0.2
AMOUNT = ChargeAmount(buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8)
KW = {"buffer_km": 30.0, "reserve_soc": 0.2, "target_soc_cap": 0.8, "max_stops": 1}
SPEC = ArrivalSpec(battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, soc0: float = 0.65) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=0.0,
        dest_offset_km=300.0, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
    )


def _ledger(stations) -> ReservationLedger:
    return ReservationLedger.build({s.station_id: s.chargers for s in stations})


def _run(policy, stations, offsets, evs, *, ledger=None, forecast=None):
    return run_corridor(
        stations, offsets, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
        ledger=ledger, forecast=forecast,
    )


def _oracle(rows: list[tuple[str, str, float, float, float]]) -> OracleForecast:
    """(ev_id, station_id, t_arrive, soc_in, soc_out) 로 예측을 만든다."""

    return OracleForecast.from_rows(
        [{"ev_id": e, "station_id": s, "t_arrive_min": t,
          "soc_in": a, "soc_out": b, "cold_factor": 1.0} for e, s, t, a, b in rows],
        {e: SPEC for e, *_ in rows}, source_run_id="src")


# ---------------------------------------------------------------------------
# ★ 이 파일의 근거 — 빈 예측이면 S1 이다
# ---------------------------------------------------------------------------
def test_empty_forecast_is_bit_identical_to_s1():
    """**이게 통과하지 않으면 S1↔S2 비교가 성립하지 않는다.**

    예측에 아무것도 없으면 S2 는 S1 과 같은 질문을 같은 원장에 한다. 충전 이벤트가
    한 글자라도 다르면, 나중에 측정할 "예측의 가치" 에 배선 차이가 섞여 든다.
    """

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i}", float(i)) for i in range(5)]

    s1 = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations))
    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=_oracle([]))

    assert s2.charge_events == s1.charge_events, "빈 예측의 S2 는 S1 과 같아야 한다"
    assert s2.escape_events == s1.escape_events
    assert s2.n_answers == s1.n_answers


def test_empty_forecast_identity_holds_when_s1_actually_splits():
    """위 테스트가 '둘 다 아무것도 안 했다' 로 통과하는 것을 막는다.

    S1 이 실제로 A·B 로 갈라지는 설정에서 같은지를 본다 — 원장을 읽고 쓰는 경로가
    **살아 있는 상태**에서의 동일성이어야 의미가 있다.
    """

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i}", float(i)) for i in range(5)]

    s1 = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations))
    picks = {e["station_id"] for e in s1.charge_events}
    assert picks == {"A", "B"}, "전제: 이 설정에서 S1 은 갈라진다 (#83)"

    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=_oracle([]))
    assert s2.charge_events == s1.charge_events


# ---------------------------------------------------------------------------
# 예측이 실제로 선택을 바꾼다
# ---------------------------------------------------------------------------
def test_forecast_moves_the_car_s1_sends_into_a_queue():
    """**S2 의 존재 이유.**

    차 한 대뿐이라 원장은 비어 있다 — S1 이 더 반복해도 비어 있다. 그런데 그 차가
    A 에 도착할 무렵, 아직 질문도 받지 않은 차들이 A 에 줄을 만든다.

    S1 은 그걸 **구조적으로** 못 본다 (원장은 과거 결정의 기록이다). S2 는 본다.
    """

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev("me", 0.0)]

    s1 = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations))
    assert [e["station_id"] for e in s1.charge_events] == ["A"], (
        "전제: 빈 원장에서는 A 가 더 빠르다 (200kW)")

    # me 는 100km / 60kmh = 100분에 A 에 닿는다. 그 직전에 다섯 대를 세워 둔다
    crowd = [(f"x{i}", "A", 90.0 + i, 0.2, 0.8) for i in range(5)]
    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=_oracle(crowd))

    assert [e["station_id"] for e in s2.charge_events] == ["B"], (
        "A 에 줄이 설 것을 알면 B 로 가야 한다")


def test_forecast_after_my_arrival_does_not_move_me():
    """내 **뒤에** 오는 차는 FIFO 상 내 대기를 늘리지 않는다.

    이게 틀리면 예측이 줄을 과대추정하고, 그 과대추정이 "예측을 쓰니 나빠졌다" 로
    보고된다.
    """

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev("me", 0.0)]

    late = [(f"x{i}", "A", 300.0 + i, 0.2, 0.8) for i in range(20)]
    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=_oracle(late))

    assert [e["station_id"] for e in s2.charge_events] == ["A"]


def test_forecast_before_now_is_ignored():
    """지난 도착은 **원장과 실제 큐에 이미 있다.** 예측에서 또 주면 두 번 센다."""

    station = _spec("A", 200.0)
    ledger = _ledger([station])
    world = WorldView(t_min=100.0, stations={}, temp_c=20.0, cold_factor=1.0,
                      forecast=_oracle([("old", "A", 10.0, 0.2, 0.8)]))
    arr = Arrival(ev_id="me", arrival_min=200.0, soc_from=0.2, soc_to=0.8,
                  battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)

    policy = S2Policy(S2Settings(**KW))
    assert policy._dwell(ledger, "A", arr, world) == pytest.approx(
        ledger.dwell_if_i_go("A", arr)), "t_min 이전 도착은 안 세야 한다"


# ---------------------------------------------------------------------------
# 한 대를 두 번 세지 않는다
# ---------------------------------------------------------------------------
def test_car_already_in_the_ledger_is_not_counted_twice():
    """예측의 그 차가 **이번 런에서 이미 결정했다면** 원장에 진짜 예약이 있다.

    둘을 다 세면 한 대가 두 대가 되고, 예측이 줄을 길게 만든다.
    """

    station = _spec("A", 200.0)
    ledger = _ledger([station])
    booked = Arrival(ev_id="x0", arrival_min=50.0, soc_from=0.2, soc_to=0.8,
                     battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)
    ledger.book("x0", "A", booked)

    arr = Arrival(ev_id="me", arrival_min=100.0, soc_from=0.2, soc_to=0.8,
                  battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)
    plain = ledger.dwell_if_i_go("A", arr)

    # 같은 차를 예측으로 또 준다
    again = ledger.dwell_if_i_go("A", arr, future=[booked])
    assert again == pytest.approx(plain), "원장에 있는 차를 예측이 또 세면 안 된다"


def test_car_booked_at_another_station_is_not_counted():
    """다른 휴게소로 간 차는 **그 휴게소의 StationLedger 에서 안 보인다.**

    코리도 원장만 전체 예약을 안다 — 거기서 걸러야 한다.
    """

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    ledger = _ledger(stations)
    # x0 는 예측에서는 A 에 오지만, 이번 런에서는 B 로 갔다
    ledger.book("x0", "B", Arrival(
        ev_id="x0", arrival_min=50.0, soc_from=0.2, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT))

    arr = Arrival(ev_id="me", arrival_min=100.0, soc_from=0.2, soc_to=0.8,
                  battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)
    stale = Arrival(ev_id="x0", arrival_min=60.0, soc_from=0.2, soc_to=0.8,
                    battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)

    assert ledger.dwell_if_i_go("A", arr, future=[stale]) == pytest.approx(
        ledger.dwell_if_i_go("A", arr)), "B 로 간 차가 A 의 줄을 만들면 안 된다"


# ---------------------------------------------------------------------------
# 예측 없이는 돌지 않는다
# ---------------------------------------------------------------------------
def test_s2_without_forecast_raises():
    """조용히 S1 처럼 돌면 "예측을 쓰는 줄 알았는데 안 쓰고 있던" 결과가 나온다."""

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    with pytest.raises(RuntimeError, match="예측"):
        _run(S2Policy(S2Settings(**KW)), stations, {"A": 100.0, "B": 150.0},
             [_ev("me", 0.0)], ledger=_ledger(stations))


def test_s2_without_ledger_raises():
    """S2 는 S1 을 물려받는다 — 원장도 여전히 필요하다."""

    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    with pytest.raises(RuntimeError, match="원장"):
        _run(S2Policy(S2Settings(**KW)), stations, {"A": 100.0, "B": 150.0},
             [_ev("me", 0.0)], forecast=_oracle([]))


# ---------------------------------------------------------------------------
# 오라클 만들기
# ---------------------------------------------------------------------------
def test_oracle_refuses_a_run_with_different_cars():
    """모집단이 다른 런을 오라클로 쓰면 **있지도 않은 차가 줄을 만든다.**"""

    with pytest.raises(ValueError, match="없는 차"):
        OracleForecast.from_rows(
            [{"ev_id": "ghost", "station_id": "A", "t_arrive_min": 10.0,
              "soc_in": 0.2, "soc_out": 0.8, "cold_factor": 1.0}],
            {"real": SPEC}, source_run_id="other")


def test_oracle_uses_this_runs_physics_not_the_old_charge_time():
    """과거 런의 `charge_min` 을 쓰면 그때 잡은 **충전기 출력**이 박혀 들어온다.

    예측은 "누가 언제 어디로" 만 과거에서 읽고, 배터리·최대출력·커브는 이번 런의
    차량 집합에서 읽는다.
    """

    rows = [{"ev_id": "a", "station_id": "A", "t_arrive_min": 10.0,
             "soc_in": 0.2, "soc_out": 0.8, "cold_factor": 1.0,
             "charge_min": 999.0}]
    f = OracleForecast.from_rows(rows, {"a": ArrivalSpec(80.0, 350.0, FLAT)},
                                 source_run_id="src")
    (arr,) = f.arrivals_between("A", after_min=0.0, before_min=100.0)
    assert arr.battery_kwh == 80.0 and arr.vmax_kw == 350.0, (
        "충전 물리는 이번 런의 값이어야 한다")


# ---------------------------------------------------------------------------
# 고정점 판정 재료
# ---------------------------------------------------------------------------
def test_describe_shift_counts_cars_that_moved():
    """회차 간 도착 변화 — **수렴/진동 판정의 재료** (#118)."""

    before = _oracle([("a", "A", 10.0, 0.2, 0.8), ("b", "A", 20.0, 0.2, 0.8)])
    after = _oracle([("a", "B", 12.0, 0.2, 0.8), ("b", "A", 25.0, 0.2, 0.8)])

    d = describe_shift(before, after)
    assert d["n_moved"] == 1.0 and d["moved_share"] == pytest.approx(0.5)
    assert d["mean_abs_shift_min"] == pytest.approx(5.0), "A 에 남은 b 는 5분 늦어졌다"


def test_describe_shift_reports_zero_when_nothing_moved():
    """**0 이면 배정이 고정점이다** — 자기 예측과 일치하는 해다."""

    f = _oracle([("a", "A", 10.0, 0.2, 0.8), ("b", "B", 20.0, 0.2, 0.8)])
    d = describe_shift(f, f)
    assert d["n_moved"] == 0.0 and d["mean_abs_shift_min"] == 0.0
