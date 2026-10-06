"""S1 — 예약 원장을 보고 고르는 운전자 (#83).

완료조건의 최소 재현:

    5대가 1분 간격으로 출발하고 A 가 비어 보이면  →  S0 는 전원 A, **S1 은 갈라진다**

S0 가 지는 이유는 "정보가 낡아서" 가 아니라 **낡은 정보를 모두가 같이 보고 같이
움직여서**였다 (#59). 그래서 S1 의 차이도 둘이다 — 도착 시각을 보는 것, 그리고
**한 대 고를 때마다 원장에 쓰는 것**. 둘 중 하나만 하면 안 풀린다.
"""

from __future__ import annotations

import pytest

from evdt.engine.reservation import ReservationLedger
from evdt.engine.s0 import S0Policy, S0Settings
from evdt.engine.s1 import S1Policy, S1Settings
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


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, soc0: float = 0.65) -> SimEV:
    # ⚠ soc0 는 **둘 다 닿을 만큼** 이어야 한다. 0.45 면 B(150km)가 사정권 밖이라
    #   선택지가 하나뿐이고, 그러면 "몰린다" 가 아니라 "갈 데가 거기뿐" 이다.
    #   150km + 안전버퍼 30km = 36kWh ÷ 60kWh = 0.6 이 하한이다.
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=0.0,
        dest_offset_km=300.0, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
    )


def _run(policy, stations, offsets, evs, *, ledger=None):
    return run_corridor(
        stations, offsets, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED, ledger=ledger,
    )


def _world(stations: dict) -> ReservationLedger:
    return ReservationLedger.build({s.station_id: s.chargers for s in stations})


# ---------------------------------------------------------------------------
# 완료조건
# ---------------------------------------------------------------------------
def test_s1_splits_where_s0_sends_everyone_to_the_same_place():
    """**이 테스트가 S1 의 존재 이유다.**

    A·B 모두 충전기 1기, A 가 조금 빠르다. 출발 시점에 둘 다 대기 0 이라 S0 는 다섯
    대를 전부 A 로 보낸다 — 다섯 번째 차는 앞의 네 대가 A 에 설 것을 모른다.

    S1 은 앞 차의 배정이 원장에 들어간 뒤 다음 차가 고르므로 **나뉜다.**
    """
    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i}", float(i)) for i in range(5)]

    s0 = _run(S0Policy(S0Settings(**KW)), stations, offsets, evs)
    s0_pick = {e["ev_id"]: e["station_id"] for e in s0.charge_events}
    assert set(s0_pick.values()) == {"A"}, "S0 는 화면이 빈 A 로 전원 몰려야 한다"

    s1 = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs,
              ledger=_world(stations))
    s1_pick = {e["ev_id"]: e["station_id"] for e in s1.charge_events}

    assert len(s1_pick) == 5, "차가 사라지면 안 된다"
    assert len(set(s1_pick.values())) == 2, (
        "S1 은 앞 차를 원장에서 보므로 A·B 로 나뉘어야 한다")


def test_s1_waits_less_than_s0_on_the_same_cars():
    """갈라졌으면 대기가 줄어야 한다. 안 줄면 가른 의미가 없다."""
    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i}", float(i)) for i in range(5)]

    s0 = _run(S0Policy(S0Settings(**KW)), stations, offsets, evs)
    s1 = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs, ledger=_world(stations))

    s0_wait = sum(e["wait_min"] for e in s0.charge_events)
    s1_wait = sum(e["wait_min"] for e in s1.charge_events)

    assert s1_wait < s0_wait


def test_s1_refuses_to_run_without_a_ledger():
    """원장을 안 주면 **그 자리에서 터져야 한다.**

    조용히 S0 처럼 돌면 "원장을 쓰는 줄 알았는데 안 쓰던" 상태로 실험이 끝난다.
    """
    stations = [_spec("A", 200.0), _spec("B", 150.0)]

    with pytest.raises(RuntimeError, match="예약 원장"):
        _run(S1Policy(S1Settings(**KW)), stations, {"A": 100.0, "B": 150.0},
             [_ev("e0", 0.0)], ledger=None)


def test_s1_does_not_peek_at_the_forecast():
    """예측은 S2 의 것이다. 몰래 보면 사다리 칸이 섞인다."""
    stations = [_spec("A", 200.0)]
    policy = S1Policy(S1Settings(**KW))
    world = WorldView(t_min=0.0, stations={}, temp_c=20.0, cold_factor=1.0,
                      ledger=_world(stations))

    with pytest.raises(RuntimeError, match="예측"):
        world.require_forecast()
    assert policy.decide([], world) == {}


def test_the_order_of_asking_is_fixed():
    """순차 결합이라 **순서가 답을 정한다.** dict 순서에 달리면 재현이 안 된다."""
    stations = [_spec("A", 200.0), _spec("B", 150.0)]
    offsets = {"A": 100.0, "B": 150.0}
    evs = [_ev(f"e{i}", float(i)) for i in range(5)]

    a = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs, ledger=_world(stations))
    b = _run(S1Policy(S1Settings(**KW)), stations, offsets, list(reversed(evs)),
             ledger=_world(stations))

    assert {e["ev_id"]: e["station_id"] for e in a.charge_events} == \
           {e["ev_id"]: e["station_id"] for e in b.charge_events}
