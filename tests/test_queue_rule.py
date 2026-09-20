"""큐 규칙 단일 모듈 (T-15).

이 규칙이 두 곳에 생기면 시뮬레이터가 계산한 대기시간과 원장이 약속한 대기시간이
달라진다. 그래서 규칙 자체와 **호출자가 둘뿐이라는 구조**를 같이 검사한다.
"""

from __future__ import annotations

import ast
from itertools import permutations
from pathlib import Path

import pytest

from evdt.world.queue_rule import (
    Arrival,
    Assignment,
    Charger,
    arrival_order,
    assign,
    chargers_after,
    choose_charger,
    wait_if_arriving_now,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "evdt"

#: 출력 제한이 차량·충전기 쪽에서만 걸리도록 충분히 높은 평탄 곡선
FLAT_CURVE = ((0.0, 1.0, 10_000.0),)

DELTA_SOC = 0.5


def _arrival(
    ev_id: str,
    arrival_min: float,
    service_min: float,
    *,
    vmax_kw: float = 100.0,
) -> Arrival:
    """점유시간이 service_min 분이 되도록 배터리 용량을 역산한 도착 한 건.

    t = ΔSoC × battery / min(vmax, charger) × 60  이므로,
    충전기가 vmax 이상이면 t 는 충전기와 무관하게 service_min 이 된다.
    """

    battery_kwh = service_min * vmax_kw / 60.0 / DELTA_SOC

    return Arrival(
        ev_id=ev_id,
        arrival_min=arrival_min,
        soc_from=0.2,
        soc_to=0.2 + DELTA_SOC,
        battery_kwh=battery_kwh,
        vmax_kw=vmax_kw,
        curve=FLAT_CURVE,
    )


def _waits(assignments: tuple[Assignment, ...]) -> list[float]:
    return [a.wait_min for a in assignments]


# ---------------------------------------------------------------------------
# 완료조건 테스트
# ---------------------------------------------------------------------------


def test_three_arrivals_one_charger_wait_is_cumulative():
    """충전기 1대에 3대 동시 도착 → 대기시간 0, t1, t1+t2."""

    t1, t2, t3 = 30.0, 45.0, 20.0
    arrivals = [
        _arrival("a", 0.0, t1),
        _arrival("b", 0.0, t2),
        _arrival("c", 0.0, t3),
    ]

    result = assign(arrivals, [Charger("C1", 200.0)])

    assert [a.ev_id for a in result] == ["a", "b", "c"]
    assert _waits(result) == pytest.approx([0.0, t1, t1 + t2])
    assert [a.end_min for a in result] == pytest.approx([t1, t1 + t2, t1 + t2 + t3])
    assert {a.charger_id for a in result} == {"C1"}


def test_n_chargers_n_arrivals_all_wait_zero():
    """충전기 n대에 n대 도착 → 전원 대기 0, 서로 다른 충전기."""

    n = 4
    arrivals = [_arrival(f"ev{i}", 0.0, 30.0 + i) for i in range(n)]
    chargers = [Charger(f"C{i}", 100.0 + 10 * i) for i in range(n)]

    result = assign(arrivals, chargers)

    assert _waits(result) == [0.0] * n
    assert len({a.charger_id for a in result}) == n


def test_fifo_result_is_independent_of_input_order():
    """도착 순서를 섞어도 결과가 같다. 24가지 순열 전부 확인한다."""

    base = [
        _arrival("a", 0.0, 30.0),
        _arrival("b", 10.0, 25.0),
        _arrival("c", 12.0, 40.0),
        _arrival("d", 50.0, 15.0),
    ]
    chargers = [Charger("C1", 200.0), Charger("C2", 150.0)]

    expected = assign(base, chargers)

    for shuffled in permutations(base):
        assert assign(list(shuffled), chargers) == expected


def test_simultaneous_arrivals_break_ties_deterministically():
    """동시 도착도 입력 순서에 흔들리지 않는다 (ev_id 사전순)."""

    a, b = _arrival("a", 5.0, 30.0), _arrival("b", 5.0, 30.0)

    assert [x.ev_id for x in arrival_order([b, a])] == ["a", "b"]
    assert assign([b, a], [Charger("C1", 100.0)]) == assign(
        [a, b], [Charger("C1", 100.0)]
    )


def test_same_input_gives_same_output_and_does_not_mutate():
    """순수성: 같은 입력 → 같은 출력, 입력은 그대로."""

    arrivals = [_arrival("a", 0.0, 30.0), _arrival("b", 0.0, 30.0)]
    chargers = [Charger("C1", 100.0, available_from_min=7.0)]

    first = assign(arrivals, chargers)
    second = assign(arrivals, chargers)

    assert first == second
    assert first is not second
    # 입력은 건드리지 않는다
    assert chargers[0].available_from_min == 7.0
    assert [a.ev_id for a in arrivals] == ["a", "b"]


def test_zero_chargers_raises_instead_of_waiting_forever():
    """충전기 0대 → 예외. 무한 대기로 KPI 를 오염시키지 않는다."""

    with pytest.raises(ValueError, match="충전기가 0대"):
        assign([_arrival("a", 0.0, 30.0)], [])


# ---------------------------------------------------------------------------
# 규칙 2·3 — 충전기가 여러 대일 때의 선택
# ---------------------------------------------------------------------------


def test_idle_chargers_prefer_highest_power():
    """비어 있는 충전기가 여럿이면 최고출력을 고른다 (설계문서 §7.2 규칙 4)."""

    chargers = [Charger("slow", 50.0), Charger("fast", 350.0), Charger("mid", 100.0)]
    free_at = {"slow": 0.0, "fast": 0.0, "mid": 0.0}

    assert choose_charger(chargers, free_at, arrival_min=0.0) == "fast"


def test_when_all_busy_waits_for_the_earliest_release():
    """전부 사용 중이면 가장 빨리 비는 충전기. 고출력이라도 늦게 비면 안 고른다."""

    chargers = [Charger("fast", 350.0), Charger("slow", 50.0)]
    free_at = {"fast": 40.0, "slow": 10.0}

    assert choose_charger(chargers, free_at, arrival_min=0.0) == "slow"


def test_release_time_tie_prefers_higher_power():
    chargers = [Charger("slow", 50.0), Charger("fast", 350.0)]
    free_at = {"slow": 20.0, "fast": 20.0}

    assert choose_charger(chargers, free_at, arrival_min=0.0) == "fast"


def test_occupied_charger_delays_the_first_arrival():
    """이미 물려 있는 차가 끝날 때까지 기다린다."""

    result = assign(
        [_arrival("a", 0.0, 30.0)],
        [Charger("C1", 100.0, available_from_min=12.0)],
    )

    assert result[0].wait_min == pytest.approx(12.0)
    assert result[0].start_min == pytest.approx(12.0)
    assert result[0].end_min == pytest.approx(42.0)


def test_service_time_depends_on_charger_power():
    """같은 차가 50kW 와 200kW 에서 다른 점유시간을 갖는다 (T-14 와 같은 답)."""

    arrival = _arrival("a", 0.0, 30.0, vmax_kw=200.0)

    slow = assign([arrival], [Charger("C1", 50.0)])[0]
    fast = assign([arrival], [Charger("C1", 200.0)])[0]

    assert slow.end_min == pytest.approx(fast.end_min * 4.0)


def test_second_car_can_finish_first_on_a_free_charger():
    """두 대가 비어 있으면 뒤차가 먼저 끝날 수 있다. 시작 순서는 FIFO 그대로다."""

    arrivals = [_arrival("a", 0.0, 60.0), _arrival("b", 1.0, 10.0)]
    result = assign(arrivals, [Charger("C1", 100.0), Charger("C2", 100.0)])

    assert [x.ev_id for x in result] == ["a", "b"]
    assert _waits(result) == [0.0, 0.0]
    assert result[1].end_min < result[0].end_min


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chargers", "message"),
    [
        ([Charger("C1", 0.0)], "출력은 0보다"),
        ([Charger("C1", -10.0)], "출력은 0보다"),
        ([Charger("C1", 100.0), Charger("C1", 50.0)], "중복"),
        ([Charger("C1", 100.0, available_from_min=-1.0)], "음수"),
        ([Charger("C1", float("inf"))], "유한한 값"),
    ],
)
def test_bad_chargers_are_rejected(chargers, message):
    with pytest.raises(ValueError, match=message):
        assign([_arrival("a", 0.0, 30.0)], chargers)


def test_duplicate_ev_id_is_rejected():
    arrivals = [_arrival("a", 0.0, 30.0), _arrival("a", 5.0, 30.0)]

    with pytest.raises(ValueError, match="EV ID"):
        assign(arrivals, [Charger("C1", 100.0)])


def test_negative_arrival_time_is_rejected():
    with pytest.raises(ValueError, match="도착 시각"):
        assign([_arrival("a", -1.0, 30.0)], [Charger("C1", 100.0)])


def test_no_arrivals_is_not_an_error():
    assert assign([], [Charger("C1", 100.0)]) == ()


# ---------------------------------------------------------------------------
# 원장이 다음 Δt 로 넘길 상태
# ---------------------------------------------------------------------------


def test_chargers_after_carries_release_times_forward():
    chargers = [Charger("C1", 100.0), Charger("C2", 100.0)]
    result = assign([_arrival("a", 0.0, 30.0)], chargers)

    updated = chargers_after(chargers, result)
    by_id = {c.charger_id: c for c in updated}

    assert by_id["C1"].available_from_min == pytest.approx(30.0)
    assert by_id["C2"].available_from_min == 0.0
    assert chargers[0].available_from_min == 0.0   # 원본 불변


def test_incremental_projection_matches_single_shot():
    """원장이 Δt 마다 다시 투영해도 한 번에 투영한 것과 같은 답이 나온다.

    이게 이 모듈을 만든 이유 그 자체다. 원장은 매 Δt 마다 상태를 갱신하며 다시
    예측하고, 시뮬레이터는 한 번 쭉 돌린다. 둘이 같은 답을 내려면 규칙이 좌에서
    우로 접히는 순수 함수여야 한다. (도착 시각 순서를 가르는 지점에서만 쪼갠다.)
    """

    arrivals = [
        _arrival("a", 0.0, 40.0),
        _arrival("b", 5.0, 35.0),
        _arrival("c", 90.0, 25.0),
        _arrival("d", 95.0, 30.0),
    ]
    chargers = [Charger("C1", 200.0), Charger("C2", 100.0)]

    single_shot = assign(arrivals, chargers)

    first_half = assign(arrivals[:2], chargers)
    second_half = assign(arrivals[2:], chargers_after(chargers, first_half))

    assert first_half + second_half == single_shot


def test_chargers_after_rejects_unknown_charger():
    bogus = Assignment("a", "C9", 0.0, 0.0, 30.0, 0.0)

    with pytest.raises(ValueError, match="모르는 충전기"):
        chargers_after([Charger("C1", 100.0)], [bogus])


# ---------------------------------------------------------------------------
# S0(UE) 가 보는 "지금 도착하면 얼마나 기다리나"
# ---------------------------------------------------------------------------


def test_empty_station_shows_no_wait():
    assert wait_if_arriving_now([Charger("C1", 100.0)], [], now_min=0.0) == 0.0


def test_wait_is_time_until_the_earliest_charger_frees():
    chargers = [Charger("C1", 100.0, available_from_min=30.0), Charger("C2", 50.0, 18.0)]

    assert wait_if_arriving_now(chargers, [], now_min=10.0) == pytest.approx(8.0)


def test_wait_counts_the_cars_already_in_line():
    """줄 서 있는 차가 먼저다. 그들이 끝나야 내 차례가 온다."""

    chargers = [Charger("C1", 100.0)]
    queued = [_arrival("a", 0.0, 30.0), _arrival("b", 0.0, 20.0)]

    # a 가 0~30, b 가 30~50 을 쓰므로 지금(0분) 도착하면 50분 기다린다
    assert wait_if_arriving_now(chargers, queued, now_min=0.0) == pytest.approx(50.0)


def test_wait_does_not_depend_on_the_arriving_car():
    """대기시간은 충전기가 언제 비는가로만 정해진다. 내 충전시간과 무관하다."""

    chargers = [Charger("C1", 100.0, available_from_min=25.0)]

    assert wait_if_arriving_now(chargers, [], now_min=5.0) == pytest.approx(20.0)


def test_wait_is_never_negative():
    """이미 비어 있는 충전기를 과거 시각으로 세지 않는다."""

    chargers = [Charger("C1", 100.0, available_from_min=3.0)]

    assert wait_if_arriving_now(chargers, [], now_min=90.0) == 0.0


def test_wait_matches_what_assign_would_give_the_next_car():
    """관측값과 실제 배정이 어긋나면 안 된다. 같은 규칙에서 나와야 한다."""

    chargers = [Charger("C1", 200.0), Charger("C2", 100.0)]
    queued = [_arrival("a", 0.0, 40.0), _arrival("b", 0.0, 35.0)]
    now = 5.0

    observed = wait_if_arriving_now(chargers, queued, now_min=now)
    actual = assign([*queued, _arrival("z", now, 10.0)], chargers)

    assert observed == pytest.approx(next(x.wait_min for x in actual if x.ev_id == "z"))


def test_wait_needs_chargers():
    with pytest.raises(ValueError, match="충전기가 0대"):
        wait_if_arriving_now([], [], now_min=0.0)


# ---------------------------------------------------------------------------
# 구조 — 이 모듈이 지켜야 하는 제약을 코드로 강제한다
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))

    return out


@pytest.mark.parametrize("forbidden", ["simpy", "evdt.engine"])
def test_queue_rule_does_not_import(forbidden: str):
    """SimPy 에 종속되면 원장이 못 쓴다. engine 에 종속되면 설계 규칙 2 위반이다."""

    offenders = [
        f"{lineno}행 → {mod}"
        for mod, lineno in _imported_modules(SRC / "world" / "queue_rule.py")
        if mod == forbidden or mod.startswith(forbidden + ".")
    ]

    assert not offenders, f"queue_rule.py 가 {forbidden} 를 임포트한다: {offenders}"


def test_queue_rule_has_at_most_two_callers():
    """호출자는 시뮬레이터와 예약 원장 **둘뿐**이다.

    세 번째가 생기는 순간 규칙이 갈라지기 시작한다. 주석으로만 적어두면
    3주 뒤에 누군가 편의상 한 줄 임포트한다.
    """

    allowed = {"world/sim.py", "engine/ledger.py"}
    callers = set()

    for py in sorted(SRC.rglob("*.py")):
        if py.name == "queue_rule.py":
            continue

        for mod, _ in _imported_modules(py):
            if mod == "evdt.world.queue_rule":
                callers.add(py.relative_to(SRC).as_posix())

    assert callers <= allowed, (
        "큐 규칙을 호출할 수 있는 곳은 시뮬레이터(world/sim.py)와 "
        f"예약 원장(engine/ledger.py) 뿐입니다. 허용되지 않은 호출자: "
        f"{sorted(callers - allowed)}"
    )
