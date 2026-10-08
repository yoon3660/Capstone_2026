"""예약 원장 — 코리도 전체 (#83).

여기서 지키는 것은 **"원장이 실제 점유와 어긋나지 않는다"** 하나다. 어긋나면 정책이
재는 시간과 차가 실제로 머무는 시간이 달라지고, 그 차이가 정책의 성과로 보고된다.
"""

from __future__ import annotations

import pytest

from evdt.engine.reservation import ReservationLedger, ResState
from evdt.world.queue_rule import Arrival, Charger


def _chargers(n: int, kw: float = 100.0) -> tuple[Charger, ...]:
    return tuple(Charger(charger_id=f"c{i}", power_kw=kw) for i in range(n))


def _arr(ev_id: str, t: float, soc_from: float = 0.3) -> Arrival:
    return Arrival(ev_id=ev_id, arrival_min=t, soc_from=soc_from, soc_to=0.8,
                   battery_kwh=60.0, vmax_kw=150.0, curve=((0.0, 1.0, 150.0),))


def _ledger(n_chargers: int = 1) -> ReservationLedger:
    return ReservationLedger.build({"A": _chargers(n_chargers), "B": _chargers(n_chargers)})


def test_a_booking_is_visible_to_the_next_car():
    """**이것이 S1 의 전부다.** 앞 차를 쓰지 않으면 뒤 차가 같은 곳으로 몰린다."""
    led = _ledger()
    alone = led.dwell_if_i_go("A", _arr("second", 10.0))

    led.book("first", "A", _arr("first", 0.0))
    after = led.dwell_if_i_go("A", _arr("second", 10.0))

    assert after > alone


def test_asking_does_not_change_the_ledger():
    """`dwell_if_i_go` 는 묻기만 한다. 물어보는 것만으로 자리가 잡히면 안 된다."""
    led = _ledger()
    led.book("first", "A", _arr("first", 0.0))

    before = led.booked("A")
    led.dwell_if_i_go("A", _arr("ghost", 5.0))

    assert led.booked("A") == before
    assert led.reservation("ghost") is None


def test_a_car_does_not_queue_behind_itself():
    """이미 예약한 차가 같은 곳을 다시 물으면 **자기 자신은 빼고** 센다."""
    led = _ledger()
    led.book("me", "A", _arr("me", 0.0))

    assert led.dwell_if_i_go("A", _arr("me", 0.0)) == pytest.approx(
        _ledger().dwell_if_i_go("A", _arr("me", 0.0))
    )


def test_rebooking_moves_the_car_rather_than_doubling_it():
    """다른 곳으로 보내면 **옮겨진다.** 양쪽에 남으면 원장이 실제보다 붐벼 보인다."""
    led = _ledger()
    led.book("me", "A", _arr("me", 0.0))
    led.book("me", "B", _arr("me", 0.0))

    assert led.booked("A") == 0
    assert led.booked("B") == 1
    assert led.reservation("me").station_id == "B"


def test_a_charging_car_cannot_be_released():
    """꽂은 차를 원장에서 빼면 **원장과 실제 점유가 어긋난다.**"""
    led = _ledger()
    led.book("me", "A", _arr("me", 0.0))
    led.mark("me", ResState.CHARGING)

    with pytest.raises(ValueError, match="어긋난다"):
        led.release("me")


def test_a_later_eta_moves_the_car_behind_the_one_that_now_arrives_first():
    """ETA 가 바뀌면 **순서가 바뀌고, 그 뒤 차들의 대기가 전부 달라진다** (FIFO).

    자리만 고쳐 넣으면 순서가 깨지므로 빼고 다시 넣는다.
    """
    led = _ledger()
    led.book("early", "A", _arr("early", 0.0))
    led.book("late", "A", _arr("late", 5.0))

    # late 가 먼저 오게 되면 early 가 뒤로 밀린다
    led.update_eta("late", -5.0)

    assert led.stations["A"].arrivals[0].ev_id == "late"
    assert led.reservation("late").eta_min == pytest.approx(-5.0)


def test_booked_counts_only_cars_that_have_not_arrived():
    """정책이 보는 '몰림' 신호다. 이미 충전 중인 차는 **미래의 경쟁자가 아니다.**"""
    led = _ledger(2)
    led.book("a", "A", _arr("a", 0.0))
    led.book("b", "A", _arr("b", 1.0))
    led.mark("a", ResState.CHARGING)

    assert led.booked("A") == 1


def test_an_unknown_station_is_refused_rather_than_ignored():
    """조용히 무시하면 정책이 '배정했다' 고 믿는데 원장엔 없다."""
    led = _ledger()

    with pytest.raises(KeyError):
        led.book("me", "없는휴게소", _arr("me", 0.0))
    with pytest.raises(KeyError):
        led.dwell_if_i_go("없는휴게소", _arr("me", 0.0))
