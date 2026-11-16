"""민폐 랭킹이 **무엇을 세는지** 못 박는다 (Q3 차별점의 검정, #138 후속).

이 분석의 결론("쏠림 유발 차를 식별했다")은 재구성이 원본과 **정확히** 같을
때만 성립한다. 어긋나면 민폐 수치는 숫자가 아니라 잡음이다. 그리고 한계값을
**합치면** 총비용이 다섯 배로 부푼다 — 그 둘을 테스트로 막는다.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from evdt.engine.ledger import StationLedger
from evdt.paths import PROJECT_ROOT
from evdt.world.queue_rule import Arrival, Charger

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
rank = importlib.import_module("externality_rank")


def car(ev_id: str, t: float, *, soc_to: float = 0.8) -> Arrival:
    return Arrival(ev_id=ev_id, arrival_min=t, soc_from=0.2, soc_to=soc_to,
                   battery_kwh=60.0, vmax_kw=150.0,
                   curve=((0.0, 1.0, 100.0),))


ONE = (Charger(charger_id="c1", power_kw=100.0),)


# ---------------------------------------------------------------------------
# 무엇을 세는가
# ---------------------------------------------------------------------------
def test_a_car_with_nobody_behind_it_pushes_nobody() -> None:
    """뒤에 아무도 없으면 민폐는 0 이다 (§6.1 의 가장 싼 가드)."""

    led = StationLedger(ONE)
    first, last = car("A", 0.0), car("B", 1000.0)
    led.commit(first)
    led.commit(last)

    assert led.externality_parts(last, exclude="B")[0] == 0.0


def test_the_car_in_front_pushes_the_one_behind() -> None:
    """충전기 한 대에 두 대가 겹치면 앞차가 뒤차를 정확히 밀어낸 만큼이다."""

    led = StationLedger(ONE)
    a, b = car("A", 0.0), car("B", 1.0)
    led.commit(a)
    led.commit(b)

    pushed, parts = led.externality_parts(a, exclude="A")

    assert set(parts) == {"B"}
    assert pushed > 0.0

    # A 가 없으면 B 는 도착 즉시 시작한다. 그 종료 시각 차이가 곧 민폐다
    from evdt.world.queue_rule import assign

    with_a = {x.ev_id: x.end_min for x in assign([a, b], ONE)}
    without_a = {x.ev_id: x.end_min for x in assign([b], ONE)}

    assert pushed == pytest.approx(with_a["B"] - without_a["B"], rel=1e-12)


def test_the_car_behind_does_not_push_the_one_in_front() -> None:
    """FIFO 라서 뒤차는 앞차에게 0 을 기여한다 — 랭킹의 방향이 여기서 나온다."""

    led = StationLedger(ONE)
    a, b = car("A", 0.0), car("B", 1.0)
    led.commit(a)
    led.commit(b)

    assert led.externality_parts(b, exclude="B")[0] == 0.0


# ---------------------------------------------------------------------------
# 반사실의 정의
# ---------------------------------------------------------------------------
def test_removing_a_visit_only_counts_the_rest() -> None:
    """빠진 정차 자신의 체류는 세지 않는다 — 수요 감소와 혼잡 완화를 가른다."""

    by_station = {"S": [car("A", 0.0), car("B", 1.0)]}
    chargers = {"S": ONE}

    full = rank.dwell_of_rest(chargers, by_station, set())
    without_a = rank.dwell_of_rest(chargers, by_station, {("A", "S")})

    # A 를 빼면 남는 것은 B 하나이고, B 는 기다리지 않는다
    assert without_a < full
    assert without_a > 0.0


def test_the_counterfactual_is_zero_when_nothing_overlaps() -> None:
    """겹치지 않으면 누구를 빼도 남은 차의 체류는 그대로다."""

    by_station = {"S": [car("A", 0.0), car("B", 10_000.0)]}
    chargers = {"S": ONE}

    full = rank.dwell_of_rest(chargers, by_station, set())
    minus_a = rank.dwell_of_rest(chargers, by_station, {("A", "S")})
    b_only = rank.dwell_of_rest({"S": ONE}, {"S": [car("B", 10_000.0)]}, set())

    assert minus_a == pytest.approx(b_only)
    assert full > minus_a


# ---------------------------------------------------------------------------
# 합치면 안 된다
# ---------------------------------------------------------------------------
def test_the_sum_of_marginals_is_not_the_benefit_of_removing_them() -> None:
    """★ 한계값을 **더하면 안 된다**.

    같은 충전기에 세 대가 줄줄이 선다. A 와 B 를 빼면 남는 C 의 체류가 얼마나
    줄까? A 의 민폐 + B 의 민폐가 **아니다.** B 가 미는 몫은 A 가 미는 몫
    **안에 이미 들어 있다** — 둘을 같이 빼면 그 겹침을 두 번 센다.

    그래서 "상위 5% 의 민폐 합" 은 "상위 5% 를 빼면 줄어드는 양" 이 아니다.
    설 상행 S0 에서 전자가 12,842시간, 후자가 1,685.7시간이었다.
    합계를 `social_cost_h` 와 나란히 놓는 순간 그 표는 틀린다.
    """

    led = StationLedger(ONE)
    cars = [car("A", 0.0), car("B", 1.0), car("C", 2.0)]
    for c in cars:
        led.commit(c)

    summed = sum(led.externality_parts(c, exclude=c.ev_id)[0]
                 for c in cars if c.ev_id in ("A", "B"))

    by_station, chargers = {"S": cars}, {"S": ONE}
    base = rank.dwell_of_rest(chargers, by_station, set())
    gained = (base - rank.dwell_of_rest(
        chargers, by_station, {("A", "S"), ("B", "S")})) * 60.0

    assert summed > 0.0 and gained > 0.0
    assert summed != pytest.approx(gained, rel=1e-6), (
        "한계값의 합이 실제 이득과 같아졌다 — 그러면 이 경고가 필요 없다는 "
        "뜻이므로 docstring 도 함께 고칠 것")


def test_the_script_warns_against_summing() -> None:
    """경고가 문서에 남아 있어야 한다. 지우면 다음 사람이 합친다.

    둘을 다 못 박는다 — (1) 총비용으로 쓰지 말 것, (2) **칸 비교에 쓰지 말 것**.
    두 번째가 더 위험하다: S2 의 민폐 합계가 S0 보다 큰데 총비용은 낮다
    (S2 가 발길질을 줄여 줄에 남는 차가 많아지기 때문이다). 그걸 모르고
    사다리 표에 얹으면 **엔진이 나빠졌다고 보고하게 된다.**
    """

    doc = rank.__doc__ or ""

    assert "합치지 말 것" in doc
    assert "social_cost_h" in doc
    assert "칸끼리 비교" in doc


def test_the_script_does_not_define_its_own_externality() -> None:
    """식은 `StationLedger` 에만 있다 (`ledger.py:155`)."""

    src = (PROJECT_ROOT / "scripts" / "externality_rank.py").read_text(encoding="utf-8")

    assert "externality_parts" in src
    for banned in ("end_min - end", "with_me", "without["):
        assert banned not in src, (
            f"{banned!r} — 민폐 식을 여기서 다시 쓰고 있다. ledger.py 를 부를 것")


# ---------------------------------------------------------------------------
# 재구성 관문
# ---------------------------------------------------------------------------
def test_the_reconstruction_gate_refuses_a_mismatch() -> None:
    """★ 관문이 실제로 막는지 확인한다.

    공급이 run 이후 바뀌면(충전기가 늘거나 줄면) 다시 쌓은 대기가 원본과
    달라진다. 그때 **조용히 넘어가면** 민폐 랭킹이 거짓이 된다.
    """

    import pandas as pd

    cars = [car("A", 0.0), car("B", 1.0)]
    by_station, chargers = {"S": cars}, {"S": ONE}

    # 원본이라고 주장하는 기록 — 대기가 0 이라고 적혀 있다 (충전기가 두 대였던 때)
    ev = pd.DataFrame([
        {"ev_id": "A", "station_id": "S", "t_end_min": 999.0, "wait_min": 0.0},
        {"ev_id": "B", "station_id": "S", "t_end_min": 999.0, "wait_min": 0.0},
    ])

    with pytest.raises(SystemExit, match="재구성이 원본과 다르다"):
        rank.check_reconstruction(chargers, by_station, ev)


def test_the_gate_passes_when_the_rebuild_matches() -> None:
    import pandas as pd

    from evdt.world.queue_rule import assign

    cars = [car("A", 0.0), car("B", 1.0)]
    by_station, chargers = {"S": cars}, {"S": ONE}

    ev = pd.DataFrame([
        {"ev_id": a.ev_id, "station_id": "S",
         "t_end_min": a.end_min, "wait_min": a.wait_min}
        for a in assign(cars, ONE)
    ])

    rank.check_reconstruction(chargers, by_station, ev)      # 멈추지 않는다


# ---------------------------------------------------------------------------
# 대조군 설계
# ---------------------------------------------------------------------------
def test_both_controls_are_in_the_script() -> None:
    """대조 B(휴게소마다 같은 건수)가 없으면 "차를 탔깃했다" 를 말할 수 없다.

    대조 A 만 이기는 것은 **쏠리는 휴게소를 골랐다** 까지다 — 그건 증설 랭킹
    이야기이고 Q3 의 차량 타깃팅이 아니다.
    """

    src = (PROJECT_ROOT / "scripts" / "externality_rank.py").read_text(encoding="utf-8")

    assert "대조 A" in src and "대조 B" in src
    assert "per_station" in src, "층을 맞추는 코드가 없다 — 대조 B 가 A 와 같아진다"


def test_ci95_is_the_usual_one() -> None:
    mu, lo, hi = rank.ci95([10.0, 12.0, 14.0, 16.0])

    assert mu == pytest.approx(13.0)
    assert lo < mu < hi
    assert (hi - mu) == pytest.approx(1.96 * (2.5819888974716112 / 2.0), rel=1e-6)
