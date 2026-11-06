"""그룹 하나의 배정을 MILP 로 푼다 — HiGHS (#143).

`docs/objective.md` §3 이 *"글로 쓴 목적함수를 그대로 solver 에 넘길 수 없다"* 를
보였다. 이 모듈이 **넘길 수 있는 형태**를 만든다. 세 가지를 지킨다.

1. **결정변수는 $x_{i,s}$ 하나뿐이다.** 충전기·서비스 순서를 변수로 올리면
   solver 가 우리가 구현하지 않은 큐 규칙을 고르게 되고, 그 개선을 S4 의 값으로
   보고하면 거짓이 된다 (§3 의 "C 는 쓰면 안 된다")
2. **비용 상수는 실제 모형을 불러서 잰다.** 충전곡선·큐 규칙을 다시 쓰지 않는다
3. **근사 오차를 재서 남긴다.** MILP 해를 정확한 채점기(`_group_total`)로 다시
   재서 차를 기록한다 (§3 의 마지막 식)

## 정식화 — §3 의 "A. 쌍별 상호작용"

$$
\\min \\sum_{i}\\sum_{s \\in C_i} c_{i,s}\\,x_{i,s}
    + \\sum_{(i,j,s)} \\delta_{i,j,s}\\,y_{i,j,s}
$$

$$
\\sum_{s \\in C_i} x_{i,s} = 1,\\quad
y_{i,j,s} \\ge x_{i,s} + x_{j,s} - 1,\\quad
x \\in \\{0,1\\},\\; y \\in [0,1]
$$

    c_{i,s}      i 가 s 에 **혼자** 갔을 때의 비용 + 재배정 비용 r_i
    δ_{i,j,s}    j 가 s 에 **먼저** 와 있을 때 i 가 더 쓰는 시간 (≥ 0)

`y` 를 이진으로 둘 필요가 없다. $\\delta \\ge 0$ 이고 최소화하므로 `y` 는 하한
$\\max(0, x_i + x_j - 1)$ 로 밀리고, $x$ 가 이진이면 그 값이 곧 $x_i x_j$ 다.
**이진변수가 $\\sum_i \\lvert C_i \\rvert$ 개로 유지된다.**

### 왜 선행자만 쌍으로 거나

FIFO 라서 **내 뒤에 오는 차는 내 대기에 영향이 없다** (§3 의 "순서는 미리 안다").
도착 시각 $\\eta_{i,s}$ 가 배정과 무관하게 정해지므로 어느 쌍이 선행인지 **미리
안다.** 그래서 $\\eta_{j,s} < \\eta_{i,s}$ 인 쌍만 만든다 — 변수가 절반으로 준다.

### ⚠ 이 정식화의 오차는 **부호가 정해져 있지 않다**

처음에는 *"겹침을 과대 계산하므로 목적값 ≥ 참값"* 이라고 적었다. **틀렸다** —
실제 시나리오에서 `candidate_scope="all"` 일 때 +35.6분이 나왔다. 큐는 열 효과가
더해지지도 않고 겹치지도 않는다. **둘 다 가능하다.**

    과대 — 선행자 둘의 지연이 **겹친다.** 둘이 같은 충전기를 기다리는 동안 내
           대기는 한 번만 늘어나는데, 쌍별로 더하면 두 번 센다
    과소 — 선행자 하나만으로는 내가 **안 기다린다** (그 차가 끝난 뒤 도착).
           그런데 둘이 같이 있으면 점유가 길어져 기다리게 된다. 쌍별 δ 는
           각각 0 이므로 합도 0 이고, 참값은 0 보다 크다

    근사 오차 = (정확한 Σd_i) − (MILP 목적값)

이 값을 그룹마다 재서 `milp_approx_err_min`(부호 유지) 과
`milp_approx_abs_min`(크기) 에 남긴다. **크기만 읽는다.**
그리고 **MILP 해가 휴리스틱 해보다 정확한 채점에서 나쁘면 버린다** — 근사가
만든 해를 "최적" 이라고 부르지 않기 위해서다 (`fallback_used`).

## 충전곡선 piecewise 근사는 **필요 없다** — 이미 그 형태다

#143 의 완료조건이 *"충전곡선 piecewise linear 근사와 그 오차"* 를 적어 뒀다.
재 보니 **오차가 0 이다.** 두 가지 이유가 겹친다.

1. `world/charging.py` 의 충전곡선은 **구간상수 출력**이다
   (`CurveSegment = (soc_from, soc_to, power_kw)`). 그래서 충전시간이 SoC 구간에
   대해 **이미 정확히 구간선형**이다 — 근사할 비선형 곡선이 애초에 없다
2. 더 중요한 것: 이 정식화에서 SoC 는 **결정변수가 아니다.** 목표 SoC 는 설계문서
   §2.3 이 결정변수에서 뺐고, 도착 SoC 는 $s$ 가 정해지면 정해진다. 그래서 충전시간이
   $(i, s)$ 마다 **상수**로 들어간다 — 곡선이 어떤 모양이든 선형성에 영향이 없다

**근사는 다른 곳에서 생긴다** — 큐 상호작용이고, 위의 겹침 과대계산이다. 그것을
재서 적는 것이 §5.3 이 요구한 일이다.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pulp

__all__ = ["GroupProblem", "GroupSolution", "solve_group"]

#: 상태 문자열. PuLP 의 `LpStatus` 를 그대로 쓴다 ('Optimal' · 'Infeasible' · …).
OPTIMAL = "Optimal"


@dataclass(frozen=True)
class GroupProblem:
    """MILP 하나의 **입력.** 세계를 모른다 — 숫자만 들고 있다.

    세계를 모르게 만든 이유: 이 정식화를 **세계 없이 테스트할 수 있어야** 한다.
    손으로 만든 2대 예제에서 "정답이 나오는가" 를 재는 자리가 있어야, 틀렸을 때
    그게 정식화 탓인지 측정 탓인지 가를 수 있다.
    """

    #: 결정 순서. **정렬돼 들어와야 한다** — 변수 생성 순서가 결정성의 근거다.
    evs: tuple[str, ...]
    #: i → 후보 휴게소 (정렬). 비면 그 차는 풀 수 없다.
    cands: Mapping[str, tuple[str, ...]]
    #: (i, s) → 목적함수 계수 = 혼자 갔을 때의 비용 + 재배정 비용
    cost: Mapping[tuple[str, str], float]
    #: (i, s) → 혼자 갔을 때의 **체류만**. 개인 상한(이탈 가드)에 쓴다
    dwell: Mapping[tuple[str, str], float]
    #: (i, j, s) → j 가 **먼저** 와 있을 때 i 가 더 쓰는 시간 (≥ 0)
    delta: Mapping[tuple[str, str, str], float] = field(default_factory=dict)
    #: 막다른 길이 되는 (i, s). 합이 `dead_budget` 을 못 넘는다
    dead: frozenset[tuple[str, str]] = frozenset()
    #: 허용하는 막다른 길 대수. 보통 그리디 해의 값 — **늘리지 못하게** 막는다
    dead_budget: int = 0
    #: 개인 비용 상한 (분). `inf` 면 안 건다. 한 대를 이탈로 밀어내는 해를 막는다
    worst_cap: float = float("inf")
    #: **변수는 만들되 0 으로 묶는** (i, s). 필터 ablation 전용이다 (#143).
    #:
    #: 왜 변수를 만드나: *"필터를 끄면 얼마나 느려지나"* 를 재려면 모형이 실제로
    #: 커져야 한다. 못 가는 휴게소를 그냥 빼면 모형 크기가 안 변해서 ablation 이
    #: 아무것도 재지 못한다.
    banned: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class GroupSolution:
    """MILP 하나의 **결과.** `solver_log` 의 한 행이 여기서 나온다."""

    assign: dict[str, str] | None
    objective: float
    status: str
    n_binaries: int
    n_constraints: int
    solve_time_s: float
    dual_bound: float = float("nan")
    mip_gap: float = float("nan")
    time_limit_hit: bool = False

    @property
    def solved(self) -> bool:
        return self.assign is not None and self.status == OPTIMAL


def _solver(time_limit_s: float):
    """HiGHS 를 **단일 스레드·조용히** 부른다.

    왜 HiGHS 인가: 오픈소스이고 MILP 를 풀고, `highspy` 로 **in-process 호출**이
    된다. 하루 289 스텝 × 그룹 수백 개를 돌리므로 호출마다 프로세스를 띄우는
    solver 는 쓸 수 없다 (`requirements.txt` 가 이미 들고 있다).

    **스레드를 1 로 묶는 이유는 결정성이다.** 병렬 MIP 는 노드 처리 순서가
    실행마다 달라서 동점일 때 다른 최적해를 줄 수 있다. 완료조건이 *"같은 입력에
    같은 답"* 이다.
    """

    try:
        return pulp.HiGHS(msg=False, timeLimit=time_limit_s, threads=1)
    except TypeError:          # PuLP 버전에 따라 threads 를 안 받는다
        return pulp.HiGHS(msg=False, timeLimit=time_limit_s)


def solve_group(problem: GroupProblem, *, time_limit_s: float = 5.0) -> GroupSolution:
    """`problem` 을 풀어 배정을 돌려준다. 못 풀면 `assign is None`.

    변수·제약을 **정렬된 순서로** 만든다. 이름도 번호로 붙인다 — ev_id 를 이름에
    넣으면 PuLP 가 특수문자를 치환해 이름이 충돌할 수 있고, 그러면 같은 입력에
    다른 모형이 된다.
    """

    evs = tuple(problem.evs)
    if not evs or any(not problem.cands.get(i) for i in evs):
        return GroupSolution(None, float("nan"), "Undefined", 0, 0, 0.0)

    prob = pulp.LpProblem("group_assignment", pulp.LpMinimize)

    # `prob.add_variable` 를 쓴다 — PuLP 4.0 이 `LpVariable(...)` 직접 생성을
    # 없앤다고 예고했다. duckdb 2.0 이 `Relation.fetchone()` 을 없애서 CI 가 이틀
    # 빨갰던 것과 같은 종류의 일이고, 그때 배운 것은 **예고를 보면 지금 옮긴다** 다
    x: dict[tuple[str, str], pulp.LpVariable] = {}
    for a, i in enumerate(evs):
        for b, s in enumerate(problem.cands[i]):
            x[i, s] = prob.add_variable(f"x_{a}_{b}", cat=pulp.LpBinary)

    # 선행쌍은 δ 가 들어온 것만 만든다 (δ = 0 이면 변수를 만들 이유가 없다)
    pairs = sorted(k for k, v in problem.delta.items()
                   if v > 0.0 and (k[0], k[2]) in x and (k[1], k[2]) in x)
    y = {k: prob.add_variable(f"y_{n}", lowBound=0.0, upBound=1.0)
         for n, k in enumerate(pairs)}

    prob += (
        pulp.lpSum(problem.cost[i, s] * v for (i, s), v in x.items())
        + pulp.lpSum(problem.delta[k] * v for k, v in y.items())
    )

    n_con = 0
    for k in sorted(problem.banned):                # 필터 ablation — 0 으로 묶는다
        if k in x:
            prob += x[k] == 0
            n_con += 1

    for i in evs:                                   # 한 대는 한 곳
        prob += pulp.lpSum(x[i, s] for s in problem.cands[i]) == 1
        n_con += 1

    for (i, j, s), v in y.items():                  # y >= x_i + x_j − 1
        prob += v >= x[i, s] + x[j, s] - 1
        n_con += 1

    if problem.dead:                                # 막다른 길을 **늘리지 못하게**
        here = [x[k] for k in sorted(problem.dead) if k in x]
        if here:
            prob += pulp.lpSum(here) <= int(problem.dead_budget)
            n_con += 1

    cap = float(problem.worst_cap)
    if cap < float("inf"):
        # 개인 비용 상한 — 합을 줄이려고 **한 대를 이탈 비용 위로 밀어내는** 해를
        # 막는다. 휴리스틱의 `_accept` 가 거는 가드와 같은 뜻이다
        # (docs/compare_rule.md: 엔진을 비교할 때는 이탈을 맞춘다)
        for i in evs:
            terms = [problem.dwell[i, s] * x[i, s] for s in problem.cands[i]]
            terms += [problem.delta[k] * v for k, v in y.items() if k[0] == i]
            prob += pulp.lpSum(terms) <= cap
            n_con += 1

    t0 = time.perf_counter()
    status = pulp.LpStatus[prob.solve(_solver(time_limit_s))]
    took = time.perf_counter() - t0

    n_bin = len(x)
    if status != OPTIMAL:
        return GroupSolution(None, float("nan"), status, n_bin, n_con, took,
                             time_limit_hit=took >= time_limit_s)

    assign = {i: s for (i, s), v in x.items() if (v.value() or 0.0) > 0.5}
    if len(assign) != len(evs):
        # 이진 해가 깨졌다 (시간초과로 분기가 안 끝난 경우). **고치지 않고 버린다**
        return GroupSolution(None, float("nan"), "Fractional", n_bin, n_con, took,
                             time_limit_hit=True)

    return GroupSolution(assign, float(pulp.value(prob.objective)), status,
                         n_bin, n_con, took, time_limit_hit=took >= time_limit_s)


def count_binaries(cands: Mapping[str, Sequence[str]]) -> int:
    """후보 집합이 만드는 이진변수 수. 필터 ablation 이 이 수를 비교한다."""

    return sum(len(v) for v in cands.values())
