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


# ---------------------------------------------------------------------------
# extra= — 원장에 없는 가정된 도착 (S2 의 예측, #118)
# ---------------------------------------------------------------------------
def test_extra_is_the_same_as_committing_then_rolling_back():
    """**이게 extra= 의 정의다.** 끼워서 계산한 값은 넣고 재고 뺀 값과 같아야 한다.

    `evaluate` 는 속도를 위해 "고쳐야 할 첫 자리" 앞까지만 캐시를 쓰고 그 뒤를 다시
    쌓는다. 그 자르는 위치가 틀리면 조용히 **다른 큐**를 재게 된다 — 터지지 않고
    숫자만 틀리므로, 실제로 넣어 본 값과 대조하는 것이 유일한 검사다.
    """
    rng = random.Random(7)
    chargers = expand_chargers([{"charger_id": "c", "power_kw": 200.0, "n_units": 2}])

    base = [_arr(f"b{i:02d}", rng.uniform(0, 300), rng.uniform(10, 40)) for i in range(40)]
    extra = [_arr(f"x{i:02d}", rng.uniform(0, 300), rng.uniform(10, 40)) for i in range(10)]

    for t in (37.0, 120.0, 180.5, 299.0):
        me = _arr("me", t, 25.0)

        led = _ledger(2)
        for a in base:
            led.commit(a)
        # 캐시를 먼저 달궈 둔다 — 캐시가 없는 상태로만 맞으면 반쪽 검사다
        led.evaluate(me)
        got = led.evaluate(me, extra=extra)

        truth = StationLedger(chargers)
        for a in sorted(base + extra, key=lambda a: (a.arrival_min, a.ev_id)):
            truth.commit(a)
        want = truth.evaluate(me)

        assert got == pytest.approx(want, abs=1e-9), f"t={t} 에서 끼운 값이 틀렸다"


def test_extra_with_exclude_matches_committing():
    """`exclude` 와 `extra` 가 같이 쓰일 때도 같아야 한다.

    S1·S2 는 늘 `exclude=나` 로 묻는다 (앞 Δt 의 내 예약을 빼고). 그 차가 끼워 넣는
    자리보다 앞에 있으면 **둘 다** 캐시를 무효화한다 — 자르는 위치는 더 앞쪽이다.
    """
    chargers = expand_chargers([{"charger_id": "c", "power_kw": 200.0, "n_units": 1}])
    base = [_arr("a", 10.0), _arr("me", 20.0), _arr("b", 30.0), _arr("c", 40.0)]

    led = _ledger()
    for a in base:
        led.commit(a)
    led.evaluate(_arr("me", 100.0))        # 캐시 달구기

    me = _arr("me", 100.0, 25.0)
    extra = [_arr("x", 15.0), _arr("y", 35.0)]
    got = led.evaluate(me, exclude="me", extra=extra)

    truth = StationLedger(chargers)
    for a in sorted([b for b in base if b.ev_id != "me"] + extra,
                    key=lambda a: (a.arrival_min, a.ev_id)):
        truth.commit(a)
    assert got == pytest.approx(truth.evaluate(me), abs=1e-9)


def test_empty_extra_changes_nothing():
    """빈 예측은 S1 과 **같은 계산**이어야 한다 (tests/test_s2.py 의 근거)."""
    led = _ledger()
    for t in (0.0, 10.0, 20.0):
        led.commit(_arr(f"e{t}", t))

    me = _arr("me", 50.0)
    assert led.evaluate(me, extra=()) == led.evaluate(me)
    assert led.evaluate(me, exclude="me", extra=()) == led.evaluate(me, exclude="me")


def test_extra_does_not_change_the_ledger():
    """예측은 **계산에만** 들어간다 — 원장에 쓰면 다른 차가 없는 예약을 본다 (#83)."""
    led = _ledger()
    led.commit(_arr("a", 10.0))
    before = len(led), [x.ev_id for x in led.arrivals]

    led.evaluate(_arr("me", 100.0), extra=[_arr("x", 20.0), _arr("y", 30.0)])
    assert (len(led), [x.ev_id for x in led.arrivals]) == before


def test_extra_duplicate_of_a_committed_car_is_dropped():
    """같은 차를 두 번 세면 예측이 줄을 **길게** 만들고, 그 과대추정이 결과가 된다."""
    led = _ledger()
    booked = _arr("a", 10.0)
    led.commit(booked)

    me = _arr("me", 100.0)
    assert led.evaluate(me, extra=[booked]) == pytest.approx(led.evaluate(me))
