"""예약 원장 (engine/ledger.py) — queue_rule 의 두 번째 호출자.

설계문서 T-15 완료조건: 같은 입력에서 DES 결과와 원장 투영의 시작·종료가 일치.
"""

from __future__ import annotations

import random

import pytest

from evdt.engine.ledger import Arrival, StationLedger
from evdt.world.sim import EVArrival, StationSpec, expand_chargers, run_charging_des

FLAT = ((0.0, 1.0, 10_000.0),)


def _arr(ev_id: str, t: float, service_min: float = 30.0) -> Arrival:
    # 200 kW 충전기 · 평탄 곡선 · SoC 0.25→0.75 가 정확히 service_min 분
    return Arrival(ev_id, t, 0.25, 0.75, service_min * 200.0 / 60.0 / 0.5, 200.0, FLAT)


def _ledger(n: int = 1) -> StationLedger:
    return StationLedger(expand_chargers([{"charger_id": "c", "power_kw": 200.0, "n_units": n}]))


def test_evaluate_counts_only_earlier_arrivals():
    led = _ledger()
    led.commit(_arr("a", 0.0))
    led.commit(_arr("late", 100.0))

    assert led.evaluate(_arr("x", 10.0)) == pytest.approx(20.0 + 30.0)   # a 가 끝날 때까지
    assert len(led) == 2                                                  # 원장은 그대로


def test_commit_in_the_middle_updates_later_answers():
    led = _ledger()
    led.commit(_arr("a", 0.0))
    before = led.evaluate(_arr("x", 40.0))

    led.commit(_arr("b", 5.0))            # x 보다 먼저 온 차가 끼어든다

    assert before == pytest.approx(30.0)
    assert led.evaluate(_arr("x", 40.0)) == pytest.approx(20.0 + 30.0)


def test_cancel_restores_the_earlier_answer():
    led = _ledger()
    led.commit(_arr("a", 0.0))
    led.commit(_arr("b", 5.0))
    led.cancel("b")

    assert led.evaluate(_arr("x", 40.0)) == pytest.approx(30.0)


def test_evaluate_can_leave_out_the_asking_car():
    """'나 혼자 늦게 오면?' — 원장에 있는 내 기존 도착과 줄을 서면 안 된다."""
    led = _ledger()
    led.commit(_arr("me", 0.0))
    led.commit(_arr("b", 10.0))

    assert led.evaluate(_arr("me", 20.0), exclude="me") == pytest.approx(20.0 + 30.0)   # b 뒤
    assert led.evaluate(_arr("me", 20.0)) != pytest.approx(50.0)                        # 빼지 않으면 틀린다


def test_same_car_cannot_be_committed_twice():
    led = _ledger()
    led.commit(_arr("a", 0.0))

    with pytest.raises(ValueError, match="이미 원장"):
        led.commit(_arr("a", 50.0))


def test_ledger_projection_matches_the_des():
    """같은 도착을 원장과 DES 에 넣으면 차마다 체류가 같다 (큐 규칙이 한 곳이라는 증거)."""
    rng = random.Random(0)
    arrivals = sorted((_arr(f"e{i:03d}", rng.uniform(0, 200), rng.uniform(10, 40)) for i in range(80)),
                      key=lambda a: (a.arrival_min, a.ev_id))
    chargers = expand_chargers([{"charger_id": "c", "power_kw": 200.0, "n_units": 3}])

    led = StationLedger(chargers)
    projected = {}
    for a in arrivals:
        projected[a.ev_id] = led.evaluate(a)
        led.commit(a)

    sim = run_charging_des(
        [StationSpec("s", 36.5, 127.5, chargers)],
        [EVArrival(a.ev_id, "vc", "s", a.arrival_min, a.soc_from, a.soc_to, a.battery_kwh, a.vmax_kw, FLAT)
         for a in arrivals],
    )

    for e in sim.charge_events:
        assert e["dwell_min"] == pytest.approx(projected[e["ev_id"]], abs=1e-9)
