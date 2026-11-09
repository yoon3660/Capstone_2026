"""기다리다 포기한다 (#147).

검사할 것은 넷이다.

1. **껐으면 비트 단위로 같다** — 아니면 "켰더니 달라졌다" 를 증명할 수 없다
2. **하나씩 뺀다** — 한 번에 다 빼면 너무 많이 뺀다 (앞차가 빠지면 뒤차는 참는다)
3. **떠난 순서가 맞다** — 인내가 가장 먼저 끝나는 차가 먼저 떠난다
4. **쓴 시간이 안 사라진다** — 포기한 차도 인내만큼은 기다렸다
"""

from __future__ import annotations

import math

import pytest

from evdt.world.queue_rule import Charger, assign, assign_with_renege
from evdt.world.sim import Arrival, StationQueue, expand_chargers

FLAT = ((0.0, 1.0, 10_000.0),)


def _arr(ev_id: str, at: float, *, soc_from: float = 0.2, soc_to: float = 0.8) -> Arrival:
    return Arrival(ev_id=ev_id, arrival_min=at, soc_from=soc_from, soc_to=soc_to,
                   battery_kwh=60.0, vmax_kw=200.0, curve=FLAT)


def _one_charger(power_kw: float = 50.0) -> tuple[Charger, ...]:
    return tuple(expand_chargers(
        [{"charger_id": "c", "power_kw": power_kw, "n_units": 1}]))


# ---------------------------------------------------------------------------
# 껐으면 꺼진 것인가
# ---------------------------------------------------------------------------


def test_no_patience_is_identical_to_plain_assign() -> None:
    arrivals = [_arr(f"e{i}", float(i)) for i in range(6)]
    chargers = _one_charger()

    got, reneged = assign_with_renege(arrivals, chargers, {})

    assert reneged == ()
    assert got == assign(arrivals, chargers)


def test_infinite_patience_never_reneges() -> None:
    """인내에 **없는** 차는 포기하지 않는다. 조용한 0 과 '값 없음' 은 다르다."""
    arrivals = [_arr(f"e{i}", 0.0) for i in range(5)]
    chargers = _one_charger()

    got, reneged = assign_with_renege(arrivals, chargers, {"e0": math.inf})

    assert reneged == ()
    assert len(got) == 5


def test_admit_and_admit_patient_agree_when_patience_is_empty() -> None:
    arrivals = [_arr(f"e{i}", float(i)) for i in range(5)]

    a = StationQueue("S", _one_charger()).admit(arrivals)
    b, reneged = StationQueue("S", _one_charger()).admit_patient(arrivals, {})

    assert a == b
    assert reneged == ()


# ---------------------------------------------------------------------------
# 식이 맞는가
# ---------------------------------------------------------------------------


def test_a_car_that_waits_past_its_patience_leaves() -> None:
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0)]
    # a 가 먼저 붙고 b 는 a 가 끝날 때까지 기다린다 — 그 대기가 5분보다 길다
    plain = assign(arrivals, chargers)
    assert plain[1].wait_min > 5.0

    got, reneged = assign_with_renege(arrivals, chargers, {"a": 999.0, "b": 5.0})

    assert reneged == ("b",)
    assert [a.ev_id for a in got] == ["a"]


def test_exactly_at_the_patience_is_tolerated() -> None:
    """120분 인내에 120분 대기는 **참는다.** 부동소수로 경계가 흔들리면 안 된다."""
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0)]
    wait = assign(arrivals, chargers)[1].wait_min

    _, reneged = assign_with_renege(arrivals, chargers, {"b": wait})

    assert reneged == ()


def test_only_one_leaves_when_that_is_enough() -> None:
    """**이 테스트가 설계의 핵심이다.**

    한 번에 다 빼면 너무 많이 뺀다. 앞차가 빠지면 뒤차의 대기가 줄어 **참을 수
    있게** 되는 경우가 있다. 하나씩 빼는 것이 근사가 아니라 정확하다.
    """
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0), _arr("c", 0.0)]
    plain = assign(arrivals, chargers)
    one_slot = plain[1].wait_min          # 한 대만 앞에 있을 때의 대기

    # b·c 둘 다 "두 대 앞" 은 못 참지만 "한 대 앞" 은 참는다
    patience = {"b": one_slot, "c": one_slot}
    got, reneged = assign_with_renege(arrivals, chargers, patience)

    assert len(reneged) == 1, f"하나만 빠져야 하는데 {reneged} 가 빠졌다"
    assert len(got) == 2


def test_the_first_to_run_out_of_patience_leaves_first() -> None:
    """떠나는 순서는 `arrival_min + 인내` 다 — 실제로 그 차가 먼저 떠난다.

    순서가 틀리면 뒤차의 줄이 잘못 짧아진다.
    """
    chargers = _one_charger()
    # 늦게 왔지만 인내가 짧아 **먼저** 바닥나는 차를 만든다
    arrivals = [_arr("a", 0.0), _arr("late", 1.0), _arr("early", 2.0)]
    patience = {"late": 1.0, "early": 100.0}

    _, reneged = assign_with_renege(arrivals, chargers, patience)

    assert reneged[0] == "late"


def test_leaving_frees_the_charger_for_the_cars_behind() -> None:
    """포기한 차는 충전기를 **안 잡는다** — 그게 포기의 효과다."""
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0), _arr("c", 0.0)]

    with_all = assign(arrivals, chargers)
    got, reneged = assign_with_renege(arrivals, chargers, {"b": 0.0})

    assert reneged == ("b",)
    c_before = next(x for x in with_all if x.ev_id == "c").wait_min
    c_after = next(x for x in got if x.ev_id == "c").wait_min
    assert c_after < c_before


def test_everyone_can_leave() -> None:
    """전원이 못 참으면 전원이 떠난다 — 무한 루프가 아니라 빈 배정으로 끝난다."""
    chargers = _one_charger()
    arrivals = [_arr(f"e{i}", 0.0) for i in range(4)]
    # e0 은 대기 0 이라 남는다. 나머지는 인내 0 이면 전부 떠난다
    got, reneged = assign_with_renege(
        arrivals, chargers, {f"e{i}": 0.0 for i in range(4)})

    assert len(got) == 1 and got[0].ev_id == "e0"
    assert len(reneged) == 3


def test_the_fifo_guard_still_fires() -> None:
    """`admit_patient` 도 `admit` 과 **같은 검사**를 거친다."""
    q = StationQueue("S", _one_charger())
    q.admit_patient([_arr("a", 20.0)], {})

    with pytest.raises(ValueError, match="거꾸로"):
        q.admit_patient([_arr("b", 10.0)], {})


# ---------------------------------------------------------------------------
# 배선 — 손잡이와 기록
# ---------------------------------------------------------------------------


def test_the_knob_is_off_by_default() -> None:
    from evdt.runner import load_config

    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")

    assert cfg.demand.renege is False


def test_the_run_records_whether_renege_was_on() -> None:
    """물리가 바뀌는 손잡이는 **반드시 기록에 남아야** 한다 (#101 과 같은 고장)."""
    from evdt.runner import load_config, run_params

    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")

    assert "renege" in run_params(cfg)


def test_an_old_run_cannot_be_reused_once_renege_is_on() -> None:
    import dataclasses

    from evdt.runner import load_config, stale_reason

    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")
    on = dataclasses.replace(
        cfg, demand=dataclasses.replace(cfg.demand, renege=True))

    # 옛 run 에는 renege 칸이 아예 없다
    why = stale_reason({"escape_cost_min": 120.0}, on)

    assert why and "renege" in why


def test_renege_needs_an_escape_cost() -> None:
    """이탈 비용 0 이면 인내도 0 이라 전원이 즉시 포기한다 — 로딩에서 막는다."""
    import yaml

    from evdt.config import ConfigError, ScenarioConfig
    from evdt.paths import CONFIG_DIR

    raw = yaml.safe_load(
        (CONFIG_DIR / "scenario_seollal_down.yaml").read_text(encoding="utf-8"))
    raw["demand"] = {**raw["demand"], "renege": True, "escape_cost_min": 0.0}

    with pytest.raises(ConfigError, match="즉시 포기"):
        ScenarioConfig.from_dict(raw, source="test")


def test_reneged_is_a_separate_escape_reason() -> None:
    """발길질과 섞으면 안 된다 — 엔진이 손댈 자리가 다르다."""
    from evdt.interfaces import ESCAPE_REASON, ESCAPE_RENEGED

    assert ESCAPE_REASON[ESCAPE_RENEGED] == "reneged"
    assert "reneged" not in (ESCAPE_REASON[k] for k in ESCAPE_REASON
                             if k != ESCAPE_RENEGED)


def test_the_escape_schema_carries_the_wasted_wait() -> None:
    """포기한 차가 쓴 시간이 사라지면 사다리가 실제보다 좋아 보인다."""
    from evdt.io.writers import SCHEMAS

    names = SCHEMAS["escape_event"].names

    assert "waited_min" in names
