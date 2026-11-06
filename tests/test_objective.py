"""S4 목적함수 — 식이 코드와 맞나 (#143, `docs/objective.md` §2).

이 파일이 지켜야 하는 세 가지.

    ① **기본값이 S3 와 같다** — 그래야 "시간축을 연 몫" 과 "비용을 매긴 몫" 을
       갈라서 잴 수 있다
    ② **$r_i$ 가 임박할수록 비싸진다** — 계단이 아니라 연속이어야 solver 가
       "조금 더 여유 있는 차를 대신 옮기는" 수를 둘 수 있다
    ③ **$\\Phi$ 는 넣은 척하지 않는다** — #144 의 몫이고, 0 이 아닌 가중치를
       주면 그 자리에서 터져야 한다
"""

from __future__ import annotations

import pytest

from evdt.engine.objective import Objective

# -- ① 기본값 ----------------------------------------------------------------

def test_the_default_objective_is_plain_dwell():
    """기본값에서 목적함수가 $\\sum d_i$ **그대로**여야 한다.

    하나라도 기본값이 다르면 S3 → S4 차이에 *"목적함수를 바꾼 몫"* 이 섞이고,
    그러면 사다리의 그 칸이 무엇을 쟀는지 말할 수 없다.
    """

    o = Objective()
    assert o.plain_dwell, "기본 목적함수가 Σd_i 가 아니다"
    assert o.dwell_cost(42.0, 17.0) == 42.0, "기본값에서 체류를 그대로 쓰지 않는다"
    assert o.reassign_cost(eta_min=1000.0, t_min=0.0) == 0.0, "기본값에서 재배정이 공짜가 아니다"
    assert o.terminal() == 0.0


def test_the_wait_weight_only_touches_the_waiting_part():
    """대기 가중치는 **대기에만** 걸린다 — 충전시간은 그대로다 (§6.3)."""

    o = Objective(wait_weight=1.5)
    # 체류 40분 중 대기 10분 · 충전 30분 → 30 + 1.5×10 = 45
    assert o.dwell_cost(40.0, 10.0) == pytest.approx(45.0)
    # 대기가 0 이면 가중치가 아무것도 안 바꾼다
    assert o.dwell_cost(40.0, 0.0) == pytest.approx(40.0)


def test_the_wait_cannot_be_cheaper_than_charging():
    """1.0 미만은 *"줄이 긴 곳이 더 좋다"* 가 된다 — 그건 다른 모형이다."""

    with pytest.raises(ValueError, match="대기 가중치"):
        Objective(wait_weight=0.9)


# -- ② 재배정 비용 ------------------------------------------------------------

def test_a_car_inside_the_lead_time_is_locked():
    """`hard_lock_min` 안이면 고정이고, 비용은 $M$ 이다."""

    o = Objective(hard_lock_min=10.0, reassign_base=1.0)
    assert o.is_locked(eta_min=105.0, t_min=100.0), "5분 남았는데 안 고정했다"
    assert not o.is_locked(eta_min=130.0, t_min=100.0), "30분 남았는데 고정했다"
    assert o.reassign_cost(105.0, 100.0) == o.big_m


def test_the_reassignment_cost_grows_as_arrival_gets_closer():
    """임박할수록 비싸진다. **단조**여야 한다.

    계단으로 두면 *"조금 더 여유 있는 차를 대신 옮긴다"* 는 수가 solver 에게
    안 보인다 — 두 차가 같은 값이면 고를 이유가 없다.
    """

    o = Objective(hard_lock_min=10.0, reassign_base=4.0)
    far = o.reassign_cost(eta_min=200.0, t_min=100.0)      # 100분 남았다
    near = o.reassign_cost(eta_min=120.0, t_min=100.0)      # 20분 남았다
    assert 0 < far < near < o.big_m, f"단조가 아니다: {far} → {near}"
    # 식: r_base·(1 + lock/남은시간) = 4·(1 + 10/100) = 4.4
    assert far == pytest.approx(4.4)
    assert near == pytest.approx(6.0)


def test_a_zero_base_is_free_even_when_time_is_short():
    """`reassign_base = 0` 이면 고정 밖에서는 **공짜**다 — #142 가 측정한 세계."""

    o = Objective(hard_lock_min=10.0, reassign_base=0.0)
    assert o.reassign_cost(eta_min=11.0, t_min=0.0) == 0.0
    assert o.reassign_cost(eta_min=5.0, t_min=0.0) == o.big_m, "고정은 공짜가 아니다"


def test_locking_everything_is_expressible():
    """`inf` 면 **전부 고정**이다 — S3 재현 검산이 이것을 쓴다 (§2b)."""

    o = Objective(hard_lock_min=float("inf"))
    assert o.is_locked(eta_min=1.0e9, t_min=0.0)


# -- ③ terminal penalty ------------------------------------------------------

def test_the_terminal_penalty_refuses_to_pretend():
    """$\\Phi$ 는 #144 다. 0 이 아닌 가중치를 받으면 **터져야** 한다.

    조용히 0 을 돌려주면 *"terminal penalty 를 넣었는데 결과가 같다"* 가 되고,
    그걸 설명하느라 #144 를 처음부터 다시 읽게 된다.
    """

    with pytest.raises(NotImplementedError, match="#144"):
        Objective(terminal_weight=0.5)
