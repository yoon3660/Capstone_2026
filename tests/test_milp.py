"""MILP 정식화 — 손으로 만든 문제에서 정답이 나오나 (#143, `engine/milp.py`).

세계를 **안 쓴다.** 숫자만 넣는다. 그래야 틀렸을 때 *"정식화가 틀렸나, 계수를 잘못
쟀나"* 를 가를 수 있다 — 세계와 같이 테스트하면 둘이 섞인다.

    ① 정답    쌍별 상호작용이 있을 때 최소 조합을 고르나
    ② 가드    막다른 길 예산 · 개인 상한 · 필터 ablation
    ③ 결정성  같은 입력에 **같은 답** (완료조건)
"""

from __future__ import annotations

import pytest

from evdt.engine.milp import GroupProblem, count_binaries, solve_group

#: A 가 빠르고 B 가 느리다. 둘 다 A 로 가면 뒤차가 기다린다.
EVS = ("c1", "c2")
CANDS = {"c1": ("A", "B"), "c2": ("A", "B")}
COST = {("c1", "A"): 10.0, ("c1", "B"): 30.0,
        ("c2", "A"): 10.0, ("c2", "B"): 30.0}


def _problem(**kw) -> GroupProblem:
    base = dict(evs=EVS, cands=CANDS, cost=COST, dwell=COST,
                delta={("c2", "c1", "A"): 15.0})
    return GroupProblem(**{**base, **kw})


# -- ① 정답 ------------------------------------------------------------------

def test_it_picks_the_cheapest_combination_including_the_interaction():
    """쏠림이 정답인 경우를 고르나.

        둘 다 A    10 + 10 + 15 = 35    ← 정답
        나눠서     10 + 30      = 40
        둘 다 B    30 + 30      = 60

    #139 의 손으로 만든 예제와 **같은 모양**이다 — 빠른 충전기에 줄 서는 것이
    느린 충전기보다 싸서 쏠림이 정답이다. 이걸 못 고르면 연산자가 약한 것이고,
    그 상태에서 "결합은 과녁이 아니다" 를 보고하게 된다.
    """

    sol = solve_group(_problem())
    assert sol.solved, sol.status
    assert sol.assign == {"c1": "A", "c2": "A"}
    assert sol.objective == 35.0


def test_a_big_interaction_makes_it_split():
    """상호작용이 커지면 나눠야 한다 — $\\delta$ 가 실제로 목적함수에 들어간다."""

    sol = solve_group(_problem(delta={("c2", "c1", "A"): 50.0}))
    assert sol.solved and sol.objective == 40.0
    assert sorted(sol.assign.values()) == ["A", "B"], sol.assign


def test_only_predecessors_interact():
    """$\\delta$ 가 **없는 쌍**은 비용이 0 이다 (FIFO — 뒤차는 내 대기와 무관).

    `delta` 를 비우면 둘 다 A 가 20 이고, 그게 최소다. 정식화가 뒤차까지 세면
    이 값이 커진다.
    """

    sol = solve_group(_problem(delta={}))
    assert sol.solved and sol.objective == 20.0
    assert sol.assign == {"c1": "A", "c2": "A"}


# -- ② 가드 ------------------------------------------------------------------

def test_the_dead_end_budget_is_respected():
    """막다른 길을 **늘리지 못한다** — 하나까지만 허용하면 둘 다 A 를 못 쓴다."""

    sol = solve_group(_problem(
        dead=frozenset({("c1", "A"), ("c2", "A")}), dead_budget=1))
    assert sol.solved
    assert sorted(sol.assign.values()) == ["A", "B"], sol.assign


def test_the_individual_cap_excludes_a_stacked_solution():
    """개인 상한이 걸리면 **한 대를 이탈 비용 위로 밀어내는 해**를 못 고른다.

    둘 다 A 면 뒤차가 25(= 10 + 15) 다. 상한 26 이면 통과하고, 20 이면 그 해가
    **없어진다.**

    ## ⚠ 그런데 여기서는 "못 풀었다" 가 나온다 — 그리고 그게 맞다

    쏠림이 정답이라는 것은 **다른 선택지가 더 비싸다는 뜻**이다 (여기서는 B 가
    30). 그래서 쏠린 해의 개인 비용(25)을 자르는 상한은 대안(30)도 같이 자른다.
    그러면 가능해가 없다.

    **이것이 `_milp_group` 이 상한을 `max(이탈비용, 그리디의 최대 개인비용)` 으로
    잡는 이유다.** 들어온 해가 반드시 만족하는 값으로 잡지 않으면 MILP 가 매번
    이 자리로 떨어지고, 그러면 "MILP 를 붙였는데 아무것도 안 바뀐다" 가 된다.
    """

    assert solve_group(_problem(worst_cap=26.0)).assign == {"c1": "A", "c2": "A"}

    tight = solve_group(_problem(worst_cap=20.0))
    assert tight.assign != {"c1": "A", "c2": "A"}, "상한을 넘는 해를 골랐다"
    assert not tight.solved and tight.status != "Optimal", (
        "가능해가 없는데 뭔가를 내놨다 — 고쳐서 내놓으면 안 된다")


def test_banned_pairs_make_the_model_bigger_without_changing_the_answer():
    """필터 ablation — **변수는 늘고 답은 같다.**

    못 가는 휴게소를 그냥 빼면 모형 크기가 안 변해서 *"필터를 끄면 얼마나
    느려지나"* 를 아무것도 못 잰다. 그래서 변수로 만들고 0 으로 묶는다.
    """

    cands = {"c1": ("A", "B", "Z"), "c2": ("A", "B", "Z")}
    cost = {**COST, ("c1", "Z"): 0.0, ("c2", "Z"): 0.0}
    wide = solve_group(GroupProblem(
        evs=EVS, cands=cands, cost=cost, dwell=cost,
        delta={("c2", "c1", "A"): 15.0},
        banned=frozenset({("c1", "Z"), ("c2", "Z")})))

    assert wide.solved
    assert wide.assign == {"c1": "A", "c2": "A"}, "0 으로 묶은 칸을 골랐다"
    assert wide.objective == 35.0
    assert wide.n_binaries == 6 > solve_group(_problem()).n_binaries == 4


def test_a_car_with_no_candidate_is_not_solved():
    """갈 곳이 없는 차가 있으면 **세우지 않는다** — 빈 답을 만들어 내지 않는다."""

    sol = solve_group(GroupProblem(
        evs=EVS, cands={"c1": ("A",), "c2": ()}, cost=COST, dwell=COST))
    assert not sol.solved and sol.assign is None


# -- ③ 결정성 ----------------------------------------------------------------

def test_the_same_input_gives_the_same_answer():
    """완료조건 — 같은 입력에 같은 답. **동점에서도** 같아야 한다.

    동점을 만든다: 두 휴게소가 완전히 같은 비용이다. 변수 생성 순서가 정렬돼
    있고 HiGHS 를 단일 스레드로 묶었으므로 답이 흔들리지 않아야 한다.
    """

    tie = {("c1", "A"): 10.0, ("c1", "B"): 10.0,
           ("c2", "A"): 10.0, ("c2", "B"): 10.0}
    p = GroupProblem(evs=EVS, cands=CANDS, cost=tie, dwell=tie)
    answers = {tuple(sorted(solve_group(p).assign.items())) for _ in range(8)}
    assert len(answers) == 1, f"같은 입력에 답이 흔들린다: {answers}"


def test_the_solver_runs_single_threaded_for_determinism():
    """스레드를 묶는 코드가 **있어야** 한다 — 지워지면 위 테스트가 드물게 깨진다."""

    import inspect

    from evdt.engine import milp

    assert "threads=1" in inspect.getsource(milp._solver), (
        "HiGHS 를 단일 스레드로 묶는 코드가 없다 — 병렬 MIP 는 동점에서 답이 흔들린다")


# -- 변수 수 ------------------------------------------------------------------

def test_the_binary_count_is_the_candidate_count():
    """#143 완료조건 — 후보 필터링 전후 **변수 수**를 찍는다."""

    assert count_binaries(CANDS) == 4
    assert count_binaries({"c1": tuple("ABCDEFG")}) == 7


# -- 모형 동치성 — `banned` 를 더해도 답이 바뀌지 않는다 -----------------------

def test_adding_banned_variables_never_changes_the_answer():
    """★ 못 가는 칸을 변수로 더해도 **답과 목적값이 같아야** 한다.

    필터 ablation 이 성립하는 근거다. `all` 은 `feasible` 에 0 으로 묶인 변수를
    **더하기만** 한다 — 수학적으로 같은 문제다.

    ## 왜 동점을 많이 만들어서 재나

    실제 시나리오에서 `feasible` 과 `all` 이 하루 **115.8h** 갈렸다. 모형이
    같은데 왜 갈리나를 좁히려고 이 테스트를 썼다. 비용을 굵은 격자에서 뽑아
    동점을 잔뜩 만들고, 금지 칸 이름이 실제 휴게소 **사이에 끼어들게** 해서
    변수 번호까지 밀리게 한다.

    **그래도 300/300 같은 답이 나온다.** 즉 그 115.8h 는 이 정식화의 성질이
    아니라, *"거의 같은 사회적 비용을 내는 배정이 아주 많다"* (#123) 는 세계의
    성질이 하루에 걸쳐 증폭된 것이다 — `docs/objective.md` §9.
    """

    import random

    rng = random.Random(7)
    real = ("A", "B", "C", "D")
    zs = tuple(f"{c}{k}" for c in "ABCD" for k in range(3))   # 사이에 끼어든다
    evs = tuple(f"c{i}" for i in range(4))
    banned = frozenset((i, z) for i in evs for z in zs)
    differed = 0

    for _ in range(300):
        cost = {(i, s): float(rng.choice([10.0, 12.0, 14.0]))
                for i in evs for s in real}
        delta = {(evs[b], evs[a], s): float(rng.choice([0.0, 2.0, 4.0]))
                 for s in real for a in range(4) for b in range(a + 1, 4)
                 if rng.random() < 0.6}
        wide = dict(cost)
        wide.update({(i, z): 0.0 for i in evs for z in zs})

        narrow = solve_group(GroupProblem(
            evs=evs, cands={i: real for i in evs}, cost=cost, dwell=cost, delta=delta))
        broad = solve_group(GroupProblem(
            evs=evs, cands={i: real + zs for i in evs}, cost=wide, dwell=wide,
            delta=delta, banned=banned))

        if not (narrow.solved and broad.solved):
            continue
        assert narrow.objective == pytest.approx(broad.objective), (
            "0 으로 묶은 변수를 더했는데 목적값이 달라졌다")
        assert not (set(broad.assign.values()) & set(zs)), (
            f"0 으로 묶은 칸을 골랐다: {broad.assign}")
        differed += narrow.assign != broad.assign

    assert differed == 0, f"동치 모형인데 배정이 {differed}번 달라졌다"
