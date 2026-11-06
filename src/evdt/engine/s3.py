"""S3 — 같은 윈도우의 차들을 **같이** 정한다 (사다리 네 번째 칸, #139).

S2 와 **딱 한 가지**가 다르다. 메서드 하나다 (`decide`).

    S1·S2   한 대씩 — 앞 차가 원장에 쓰고 나서 뒤 차가 고른다   (순차 결합)
    S3      그 윈도우의 차들을 **같이** 정한다                   (공간축 결합)

정보는 S2 그대로다. 체류 질의(`_dwell`)도 S2 것을 그대로 쓴다 — 예측을 포함한
같은 계산이다. 도달 가능성·충전량·이탈 판정도 전부 물려받는다. 하나라도 다시 쓰면
그 차이가 **결합의 차이로 보고된다.**

## 왜 순차로는 부족한가

S1·S2 는 `ev_id` 순으로 고른다. **먼저 묻는 차가 유리하다.**

    같은 Δt 에 A·B 두 대, 휴게소 X(빠름)·Y(느림)
    순차:  A 가 X, B 는 Y            — A 가 먼저 물었기 때문
    동시:  둘의 **합**이 작은 쪽      — B 가 X 를 쓰는 게 전체로 나을 수 있다

#123 에서 *"여러 배정이 거의 같은 사회적 비용을 낸다"* 를 봤다 (오라클 고정점이 안
가라앉고 배정만 32~37%씩 바뀌었다). **거의 같다면 그중 더 나은 것을 고를 여지가 있다.**

## 어떻게 — 그리디 + 지역 개선 (이동·교환)

단순 할당 문제(Hungarian)로 **안 풀린다.** 한 차를 넣으면 다른 차의 비용이 바뀐다 —
같은 휴게소를 고르면 서로의 대기가 늘어난다. 비용이 서로 의존하므로 비용행렬이 없다.

    ① 그리디   `super().decide()` — S2 를 그대로 한 번 돌린다
    ② 이동     한 대를 다른 휴게소로 보내고 **그룹 합이 줄면** 둔다
    ③ 교환     두 대의 배정을 맞바꾸고 **그룹 합이 줄면** 둔다

### ⚠ 이동이 없으면 아무것도 안 바뀐다 — 재서 알았다

티켓(#139)은 **교환만** 적어 뒀다. 교환만 넣고 돌려 보니 **성공이 0 이었다** (두
휴게소·여러 수요 조합 네 가지 전부). 이유가 분명하다:

**그리디가 쏠려 놓으면 교환이 손을 못 댄다.** 둘이 같은 휴게소면 바꿀 것이 없어서
그 쌍을 건너뛴다. 그런데 **쏠림이 바로 고쳐야 할 실패 모드다.**

손으로 만든 2 대 예제에서도 확인했다 — A(200kW)·B(50kW) 에 소량·대량 차 한 대씩:

    둘 다 A       13.80 분   ← 그리디가 고른 것. **네 배정 중 최적**
    소량A·대량B   38.40
    소량B·대량A   18.60
    둘 다 B       55.20

빠른 충전기에 **줄 서는 것이 느린 충전기보다 싸서** 쏠림이 정답인 경우다. 교환만
두면 이런 곳을 *"개선할 것이 없다"* 가 아니라 **"연산자가 손을 못 댄다"** 로 넘기고,
그 상태에서 *"공간축은 과녁이 아니다"* 를 보고하게 된다. **연산자가 약해서 나온
결론**이 되는 것이다.

그래서 이동을 더했다. 더하자 같은 설정에서 총체류가 **−3.50%** 움직였다 (이동 1 회,
이탈 0 → 0 그대로). 반복 최적반응(전원이 다시 고르기)은 안 쓴다 — **수렴 안 할 수
있다.** #113 에서 목표 SoC 상한의 점질량이 상행 균형을 진동시킨 것과 같은 종류다.
MILP 는 5차(S4)다.

이 티켓의 목적은 최적해가 아니라 ***"동시에 정하면 달라지는가"*** 에 답하는 것이다.

## 비용은 **그룹 단위로 같은 순서에서** 잰다

한 대를 옮기면 **떠난 곳과 간 곳의 다른 차들** 비용이 같이 바뀐다. 그 둘만 보고
판단하면 옮긴 차가 남에게 떠넘긴 몫을 못 센다 — 그게 순차 결합이 놓치는 것이고,
이 칸이 재려는 것이다. 그래서 수마다 **그룹 전체를 같은 순서로 다시 잰다**
(`_group_total`). 비교가 공정해지고 결과가 결정적이 된다.

## 비용 — 재서 정했다

상행 adoption 시드1 하루: S2 **24.8초** · S3(window=4) **76.8초** = **3.1배**.
`_candidate` 호출은 120,448 → 206,947 회. window=12 는 10 분을 넘겨서 기본값을
**4** 로 뒀다. 거부한 수는 **재평가 없이 되돌린다**(`_rebook`) — 이것만으로
145.9 → 76.8초가 됐고 결과는 비트 단위로 같았다. 계산시간은 KPI 로 나간다 (#77).

> 그리디가 재던 비용과 여기서 재는 비용은 다를 수 있다 — 그리디는 *자기 차례의*
> 원장을 봤고, 여기서는 **전원이 들어간 최종 원장**을 본다. 교환 비교에는 후자가
> 맞다. 양쪽을 같은 맥락에서 재야 "바꾸면 나아지나" 가 성립한다.

## 윈도우는 손잡이다 — 이탈을 맞춰야 한다

윈도우를 넓히면 더 많은 차가 같이 결정하고, **이탈 수가 달라질 수 있다.**
`docs/compare_rule.md`: **엔진을 비교할 때는 이탈을 맞춘다. 못 맞추면 격차 축소율을
보고하지 않는다.** 이 프로젝트에서 네 번 속을 뻔한 통로다.

`window` 가 0·1 이면 교환을 아예 안 하고, 그때 **S2 와 비트 단위로 같다.**

## 묶는 순서는 `ev_id` 가 아니라 **위치**다

그리디는 `ev_id` 순이다(S1 에서 물려받는다). 그런데 교환 후보를 `ev_id` 로 묶으면
300km 떨어진 두 차가 한 그룹에 들어온다 — 후보 휴게소가 겹치지 않아 교환이 성립할
수 없고, 평가만 버린다. 그래서 **`(offset_km, ev_id)`** 로 정렬해 묶는다. 둘째 키가
`ev_id` 라서 같은 위치에서도 순서가 결정적이다.

## 하지 않는 것

- **재배정하지 않는다.** 이미 도착해 꽂은 차는 원장이 막는다. 옮기는 것은 시간축(S4)
- **이탈을 새로 만들지 않는다.** 교환이 어느 한 대의 비용을 이탈비용 위로 올리면
  그 교환을 버린다 — 이탈 판정은 S2 에서 물려받은 그대로 둔다
- **MILP 를 쓰지 않는다** — 5차의 몫
- **예측을 바꾸지 않는다** — S2 의 관측 예측을 그대로 쓴다
- **결정 기준을 바꾸지 않는다** — 사적 비용 그대로. 사회적 비용은 #138(MEC) 이 따로 본다
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import product

from evdt.engine.ledger import Arrival
from evdt.engine.milp import GroupProblem, solve_group
from evdt.engine.s2 import S2Policy, S2Settings
from evdt.interfaces import EVState, WorldView
from evdt.world.charge_decision import can_reach_station_with_buffer

#: 이보다 작은 개선은 교환하지 않는다. 부동소수 잡음으로 무한히 교환하는 것을 막는다.
COST_EPS = 1e-9

#: `_group_total` 의 반환 — (비용 합, 최대 1인 비용, 막다른 길 수, ev_id→도착)
_Total = tuple[float, float, int, dict[str, Arrival]]


@dataclass(frozen=True)
class S3Settings(S2Settings):
    """S3 가 쓰는 값. 윈도우 둘 말고는 **S2 와 같아야 한다**.

    `window` 가 0·1 이면 교환을 안 하고 S2 와 같다 — 배선이 살아 있는지 보는 스위치다.
    """

    #: 같이 정하는 최대 대수. 그룹 하나에서 이동·교환을 모두 본다.
    #:
    #: **4 로 둔 것은 재서 정했다.** 상행 adoption 시드1 하루에서 S2 24.4초 대비
    #: window=4 가 145.9초(**6.0배**)였고, window=12 는 10분을 넘겼다. 비용이
    #: 대략 window² ~ window³ 로 커진다 — 그룹당 이동 window·(대상−1), 교환
    #: window(window−1)/2 쌍이고, 평가 한 번이 그룹 전체를 다시 재기 때문이다.
    window: int = 4

    #: 교환 개선 패스 수. 한 패스에서 아무것도 안 바뀌면 거기서 멈춘다
    max_rounds: int = 2

    #: 이동·교환 대신 **그룹을 전수 열거**한다 (#143). **사다리 칸이 아니라 진단이다.**
    #:
    #: 후보가 그룹이 쓰는 휴게소로 제한되므로 조합이 `window ** window` 이하이고,
    #: `window=4` 면 256 이다. 채점기(`_group_total`)가 근사가 아니므로 **참
    #: 목적함수의 진짜 최적해**가 나온다 — 그래서 이 크기에서 MILP 는 할 일이
    #: 없다 (`docs/objective.md` §3.5).
    #:
    #: 켜면 휴리스틱도 같이 돌려 `exact_gain_min` 에 *"휴리스틱이 얼마나 졌나"* 를
    #: 남긴다. 기본값 False — 켜면 느리고, 무엇보다 **S3 가 아니게 된다.**
    exact_group: bool = False

    #: 그룹 하나를 **무엇으로 푸는가** (#143). `exact_group` 보다 넓은 손잡이다.
    #:
    #:     "local"   이동·교환 휴리스틱            — S3 그대로. 기본값
    #:     "exact"   전수 열거                     — `exact_group=True` 와 같다
    #:     "milp"    MILP (HiGHS) + 전수 재채점    — `engine/milp.py`
    #:
    #: **왜 셋을 다 두는가.** 사다리의 뜻이 *"같은 문제를 점점 더 제대로 푼다"* 이고,
    #: 그러면 비교 대상이 남아 있어야 한다. 셋은 전부 **같은 목적함수**를 보고
    #: (`_group_total`), 다른 방법으로 푼다 — 그래서 차이가 *"푸는 방법의 몫"* 이다.
    #:
    #: 시간 복잡도가 셋의 존재 이유다. 그룹당 채점 호출 수가
    #:
    #:     local  ≈ W·(T−1) + W(W−1)/2  (패스마다)      — W² 꼴
    #:     exact  = T^W                                  — **지수**
    #:     milp   = W·T + (선행쌍)·T ≤ W²·T              — W² 꼴, 그리고 **한 번만**
    #:
    #: 즉 W 를 키울 수 있는 것은 `local` 과 `milp` 뿐이고, 그중 목적함수를
    #: **전역으로** 푸는 것은 `milp` 뿐이다 (`docs/objective.md` §6).
    solver: str = "local"

    #: MILP 가 각 차에게 몇 곳을 후보로 주는가 (#143 의 필터 ablation).
    #:
    #:     "group"     그룹이 이미 쓰는 휴게소만      — S3 의 규칙. 기본값
    #:     "feasible"  방향 + SoC 도달성 필터를 통과한 곳 (설계문서 §5.3-1)
    #:     "all"       필터 없음 — 코리도의 모든 휴게소
    #:
    #: 기본값이 `"group"` 인 이유: 후보를 넓히면 **축이 섞인다.** 새 휴게소를
    #: 여는 것은 "결합을 더 잘 푼 것" 이 아니라 "탐색 범위가 커진 것" 이고,
    #: 그러면 S3→S4 차이에 그 몫이 들어간다. `"feasible"` · `"all"` 은
    #: **ablation 용**이다 (변수 수와 계산시간을 재는 자리).
    candidate_scope: str = "group"

    #: MILP 한 번에 허용하는 시간(초). 넘기면 그때까지의 최선해를 쓰고
    #: `time_limit_hit` 을 남긴다. 시간초과 **fallback 정책**은 #144 의 몫이다.
    milp_time_limit_s: float = 5.0

    def __post_init__(self) -> None:
        if self.solver not in ("local", "exact", "milp"):
            raise ValueError(f"모르는 solver 다: {self.solver!r} (local·exact·milp)")
        if self.candidate_scope not in ("group", "feasible", "all"):
            raise ValueError(f"모르는 candidate_scope 다: {self.candidate_scope!r}")
        if self.exact_group and self.solver not in ("local", "exact"):
            # 둘이 모순이면 **어느 쪽이 이겼는지** 를 run_id 로 알 수 없다 (#142)
            raise ValueError(
                f"exact_group=True 와 solver={self.solver!r} 가 모순이다 — "
                "둘 중 하나만 쓴다 (exact_group 은 solver='exact' 의 옛 이름이다)")

    @property
    def solver_mode(self) -> str:
        """실제로 쓸 solver. `exact_group=True` 는 `solver="exact"` 의 옛 이름이다."""

        return "exact" if self.exact_group else self.solver


@dataclass(frozen=True)
class S3Policy(S2Policy):
    """윈도우 안에서 같이 정하는 정책. `S2Policy` 에서 `decide` 하나만 다르다."""

    stage: str = "S3"

    #: 교환 횟수·시간. **KPI 로 나간다** — 0 이면 순차와 같다는 뜻이고, 그러면 결과가
    #: 안 바뀐 이유가 바로 설명된다 (#139). 계산시간은 #77 의 입력이다.
    counters: dict[str, float] = field(default_factory=dict)

    # -- 거드는 것 ------------------------------------------------------------
    def _opportunity(self, ev: EVState) -> bool:
        """`S1Policy._one` 과 **같은 식**이어야 한다 — 갈라지면 충전량이 달라진다."""

        return ev.wants_opportunity_charge and ev.stops_done == 0

    def _bump(self, key: str, by: float) -> None:
        self.counters[key] = self.counters.get(key, 0.0) + by

    # -- 목적함수의 빈 자리 ----------------------------------------------------
    #
    # S3 의 목적함수는 Σd_i 다. S4 는 거기에 **재배정 비용 r_i** 를 더하고
    # (`docs/objective.md` §2b), 그러려면 그리디·연산자·채점기가 **셋 다** 그걸
    # 알아야 한다. `_extra_cost` 는 `S1Policy` 에 있다 (그리디가 거기 있다).
    # 여기 있는 `_targets` 는 그 항이 쓸 수 있는 **수를 남겨 두는** 자리다.
    #
    # 왜 S4 에서 `_group_total` 을 복사하지 않나: 복사하면 "시간축을 연 몫" 에
    # *"채점기가 갈라진 몫"* 이 섞인다. #139 가 `_candidate` 를, #143 이
    # `_improve_group` 을 뽑아낸 것과 같은 이유다.

    def _targets(self, group: Sequence[str], answers: Mapping[str, str]) -> list[str]:
        """이동·열거의 후보 휴게소. **S3 는 그룹이 이미 쓰는 곳**이다.

        후보를 그룹이 쓰는 집합으로 제한하는 이유는 `_improve_group` ① 에 적혀
        있다 — 조정은 *이미 고려된 곳들 사이의 재분배*이고, 새 휴게소를 여는 것은
        정보가 늘어난 것이어서 축이 섞인다.

        S4 는 여기에 **원래 예약** σ₀ 를 더한다. 안 더하면 그리디가 옮겨 놓은 차를
        제자리로 **되돌릴 후보가 아예 없고**, 그러면 $r_i$ 를 아무리 크게 줘도
        목적함수가 그 수를 못 둔다 (= `r_i = M` 검산이 깨진다).
        """

        return sorted({answers[i] for i in group})

    def _can_finish(self, ev: EVState, world: WorldView, at_km: float,
                    soc_out: float, rf: float) -> bool:
        """여기서 충전하고 나면 **목적지까지 갈 길이 있나.**

        ## 왜 이 검사가 S3 에만 있나

        `_next_stop_cost` 는 닿는 곳이 하나도 없으면 **0 을 돌려준다** — S1 에서는
        의도된 설계다(그 문서에 적혀 있다): 큰 수를 돌려주면 *"못 가는 곳"* 과
        *"줄이 긴 곳"* 이 같은 값으로 섞인다. S1 은 차마다 자기 최소를 고르고,
        막다른 길은 `max_stops` 와 이탈 판정이 따로 잡는다.

        **그런데 합을 줄이는 연산자에게는 그 0 이 "가장 싼 곳" 으로 보인다.**
        실제로 그렇게 됐다 — 이동을 넣자 이탈이 S2 보다 2건 늘었다. 합은 줄었지만
        줄어든 몫이 **이탈 통로로 빠져나간 것**이다. 이 프로젝트에서 네 번 속은
        그 통로다 (`docs/compare_rule.md`).

        그래서 S3 는 막다른 길을 **싸게 보는 대신 아예 못 고르게** 한다.
        """

        if self._reaches_dest(ev, soc_out, at_km, rf):
            return True
        if ev.stops_done + 1 >= self.settings.max_stops:
            return False  # 여기가 마지막 정차인데 목적지에 못 닿는다
        for st in world.stations.values():
            if not (at_km < st.offset_km < ev.dest_offset_km):
                continue
            if can_reach_station_with_buffer(
                soc=min(max(soc_out, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf,
                distance_to_station_km=st.offset_km - at_km,
                buffer_km=self.settings.buffer_km,
            ):
                return True
        return False

    def _group_total(self, ledger, world: WorldView, group: Sequence[str],
                     by_id: Mapping[str, EVState],
                     answers: Mapping[str, str]) -> _Total | None:
        """그룹 전원을 `answers` 대로 넣어 보고 `(합, 최대 1인, 막다른 길, 도착들)`.

        못 쓰는 배정이 하나라도 있으면 `None`.

        **끝나면 원장이 `answers` 상태로 남는다** — 호출자가 되돌릴 필요가 없다.
        그래서 "바꿔 보고 나빠지면 원래 answers 로 한 번 더 부른다" 가 복원이 된다.

        ## 왜 그룹 전체를 다시 재나

        비용이 서로 의존한다. 한 대를 옮기면 **떠난 곳과 간 곳의 다른 차들** 비용이
        같이 바뀐다. 그 둘만 보고 판단하면 옮긴 차가 남에게 떠넘긴 몫을 못 센다 —
        그게 바로 순차 결합이 놓치는 것이고, 이 칸이 재려는 것이다.

        ## ★ 순서는 **도착 시각** 이다 — 그룹 순서로 재면 서로를 못 본다 (#143)

        처음에는 `group` 이 준 순서(= `offset_km` 오름차순)로 쌓았다. **그게
        틀렸다.** 속도가 모두 같으므로 **멀리 있는 차가 늦게 도착한다** — 즉
        그룹 순서는 어느 휴게소에서든 **도착 순서의 정확한 역순**이다. 그래서

            group[0] (가장 멀다 = 가장 늦게 온다)  → 원장이 비어 있다. 대기 0
            group[1]                              → 원장에 group[0] 이 있지만
                                                     **내 뒤에 오므로** FIFO 가 버린다
            …

        **그룹 안의 상호작용이 한 번도 안 세어졌다.** 재서 확인했다 — 50kW 한 대인
        휴게소에 두 대를 같이 보내면

            그룹 순서로:  75.6 분  (39.6 + 36.0)   ← 둘이 서로를 못 본다
            진짜 FIFO:    81.6 분  (36.0 + 45.6)   ← 뒤차가 6.0 분 기다린다

        이 함수가 재려던 것이 **바로 그 6분**이다 (위의 "왜 그룹 전체를 다시
        재나"). 그래서 `_eta` 로 정렬해서 쌓는다 — `StationLedger` 의 FIFO
        타이브레이크와 같은 `(도착시각, ev_id)` 다.

        > ⚠ **이 고침은 #139 의 판정을 다시 묻는다.** *"공간축은 과녁이 아니다
        > (S3 − S2 = +1.20 ± 2.01 %p)"* 는 **윈도우 안 외부효과를 못 보는 채점기**로
        > 잰 값이다. 그 값이 작았던 것이 결합이 쓸모없어서인지 채점기가 눈을 감고
        > 있어서인지는 **다시 재야 안다** (`docs/ladder.md` §8).

        정렬 자체는 배정에 의존하지 않는다 ($\\eta_{i,s}$ 는 $s$ 만으로 정해진다).
        그래서 두 배정을 비교할 때 각자의 **올바른** 순서로 재게 되고, 비교는
        여전히 결정적이다.

        돌려주는 넷:

            total    비용 합
            worst    최대 1인 비용   — 이탈 가드에 쓴다
            n_dead   막다른 길 대수  — **늘리지 못하게** 막는다
            arrs     ev_id → 도착    — 거부했을 때 **재평가 없이** 되돌리는 데 쓴다

        막다른 길을 아예 금지하지 않는 이유: `max_stops` 가 1 이면 *목적지에 못 닿는
        차*가 그리디 배정에 이미 들어 있다 (S1·S2 가 그렇게 배정하고, 나중에 이탈로
        처리한다). 그걸 금지로 다루면 **그런 차 한 대가 그룹 전체의 조정을 막는다** —
        실제로 그랬다. 그래서 "있는 것은 그대로, **새로 만들지는 못하게**" 로 둔다.
        """

        rf = world.range_factor
        for ev_id in group:
            ledger.release(ev_id)

        # ★ 도착 시각 순으로 쌓는다. `(eta, ev_id)` 는 `StationLedger` 의 FIFO
        #   타이브레이크와 **같은 키**다 — 다르면 채점기가 세계와 다른 줄을 본다
        order: list[tuple[float, str]] = []
        for ev_id in group:
            st = world.stations.get(answers[ev_id])
            if st is None:
                return None
            order.append((self._eta(by_id[ev_id], world, st), ev_id))
        order.sort()

        total, worst, n_dead = 0.0, 0.0, 0
        arrs: dict[str, Arrival] = {}
        for _, ev_id in order:
            ev = by_id[ev_id]
            st = world.stations[answers[ev_id]]
            cand = self._candidate(ev, world, ledger, st, rf,
                                   opportunity=self._opportunity(ev))
            if cand is None:
                return None
            if not self._can_finish(ev, world, st.offset_km, cand[1].soc_to, rf):
                n_dead += 1
            total += cand[0] + self._extra_cost(ev_id, answers[ev_id])
            # ★ `worst` 는 **체류만** 본다. 이탈 가드가 비교하는 상대가
            #   `escape_cost_min`(체류 임계값) 이라서, 재배정 비용을 섞으면
            #   "길이 막혔다" 와 "되돌리기 싫다" 가 같은 값으로 들어간다
            worst = max(worst, cand[0])
            arrs[ev_id] = cand[1]
            ledger.book(ev_id, answers[ev_id], cand[1])

        return total, worst, n_dead, arrs

    def _rebook(self, ledger, group: Sequence[str], answers: Mapping[str, str],
                arrs: Mapping[str, Arrival]) -> None:
        """이미 재 둔 도착으로 원장을 되돌린다 — **다시 재지 않는다.**

        거부한 수마다 그룹 전체를 다시 재면 비용이 두 배가 된다. 도착은 (차, 휴게소,
        세계) 만으로 정해지므로 저장해 둔 것을 그대로 넣으면 같은 상태가 된다.
        """

        for ev_id in group:
            ledger.release(ev_id)
        for ev_id in group:
            ledger.book(ev_id, answers[ev_id], arrs[ev_id])

    def _accept(self, base: _Total | None, trial: _Total | None) -> bool:
        """바꾼 쪽을 쓸까. 합이 줄고, **새로 이탈을 만들지 않을 때만** 쓴다."""

        if trial is None or base is None:
            return False
        if trial[0] >= base[0] - COST_EPS:
            return False
        # 막다른 길을 새로 만들면서 합을 줄이는 것은 개선이 아니다 (`_can_finish` 참조)
        if trial[2] > base[2]:
            return False
        esc = self.settings.escape_cost_min
        # 이탈 판정은 S2 에서 물려받은 그대로 둬야 "이탈을 맞춘 비교" 가 성립한다
        # (docs/compare_rule.md). 합을 줄이려고 한 대를 밖으로 밀어내면 그건 개선이
        # 아니라 **이탈 통로로 비용을 흘리는 것**이다 — 이 프로젝트에서 네 번 속았다
        return not (esc > 0 and trial[1] > esc and trial[1] > base[1])


    def _improve_group(self, ledger, world: WorldView, group: Sequence[str],
                       by_id: Mapping[str, EVState], answers: dict[str, str],
                       base: _Total, n: dict[str, int]) -> _Total:
        """휴리스틱 — 이동·교환을 **이웃만** 본다. `decide()` 에서 그대로 옮겼다.

        뽑아낸 이유는 `_exact_group` 이 **같은 그룹을 두 번 풀어** 둘을 비교하기
        때문이다 (#143). 비교할 두 쪽이 같은 `_group_total` 을 써야 하므로 이
        본문을 복사하면 안 된다 — 복사하면 "휴리스틱이 지는 몫" 에 *"비용 계산이
        갈라진 몫"* 이 섞인다 (#139 가 `_candidate` 를 S1 에서 꺼낸 것과 같은 이유).
        """
        for _ in range(max(1, int(self.settings.max_rounds))):
            changed = 0

            # ① 이동 — 한 대를 **그룹이 이미 쓰는 다른 휴게소**로 보낸다.
            #
            #    교환만으로는 쏠린 배정을 못 바꾼다. 둘이 같은 곳이면 바꿀 것이
            #    없어서 그 쌍은 건너뛴다. 그런데 **쏠림이 바로 고칠 대상**이다 —
            #    교환만 두면 "연산자가 손을 못 대는 곳" 을 "개선할 것이 없는 곳"
            #    으로 보고하게 된다.
            #
            #    후보를 그룹이 쓰는 집합으로 제한한다: 조정은 **이미 고려된 곳들
            #    사이의 재분배**다. 새 휴게소를 열면 그건 정보가 늘어난 것이고,
            #    그러면 정보 축과 섞인다.
            targets = self._targets(group, answers)
            for ev_id in group:
                here = answers[ev_id]
                for there in targets:
                    if there == here:
                        continue
                    n["move_tried"] += 1
                    answers[ev_id] = there
                    trial = self._group_total(ledger, world, group, by_id, answers)
                    if self._accept(base, trial):
                        base = trial
                        n["move_done"] += 1
                        changed += 1
                        here = there
                    else:
                        answers[ev_id] = here
                        self._rebook(ledger, group, answers, base[3])

            # ② 교환 — 두 대의 배정을 맞바꾼다. 어느 한 대의 단독 이동으로는
            #    나아지지 않지만 **둘을 동시에 바꾸면** 나아지는 경우를 잡는다
            for x in range(len(group)):
                for y in range(x + 1, len(group)):
                    a, b = group[x], group[y]
                    if answers[a] == answers[b]:
                        continue
                    n["swap_tried"] += 1
                    answers[a], answers[b] = answers[b], answers[a]
                    trial = self._group_total(ledger, world, group, by_id, answers)
                    if self._accept(base, trial):
                        base = trial
                        n["swap_done"] += 1
                        changed += 1
                    else:
                        answers[a], answers[b] = answers[b], answers[a]
                        self._rebook(ledger, group, answers, base[3])

            if not changed:
                break  # 이 패스에서 아무것도 안 바뀌었다 — 더 돌 이유가 없다
        return base

    def _exact_group(self, ledger, world: WorldView, group: Sequence[str],
                     by_id: Mapping[str, EVState], answers: dict[str, str],
                     base: _Total, n: dict[str, int]) -> _Total:
        """★ 전수 열거 — 이 그룹의 **참 최적해**를 근사 없이 구한다 (#143).

        사다리 칸이 **아니다.** 진단 도구다 (`exact_group` 기본값 False).

        ## 왜 이게 가능한가 — 조합 수에 상한이 있다

        이동 연산자의 후보는 **그룹이 이미 쓰는 휴게소로 제한된다** (`targets`).
        그래서 배정 조합이 `len(targets) ** len(group)` 이하이고, `window=4` 면
        **4^4 = 256** 이다. 256 번은 그냥 다 재면 된다.

        그리고 채점기(`_group_total`)는 **근사가 아니다** — 정확한 Σd_i 를 순차
        투영으로 계산한다. 즉 **참 목적함수의 진짜 최적해**가 나온다.

        > 그래서 이 크기에서는 MILP 가 할 일이 없다. MILP 의 값은 *"같은 W 를 더
        > 잘 푸는 것"* 이 아니라 **W 를 키울 수 있게 하는 것**이다
        > (`docs/objective.md` §3.5).

        ## 무엇을 재는가

        휴리스틱을 **먼저 돌려 보고**, 그리디로 되돌린 뒤 전수 열거한다. 둘의 차를
        `exact_gain_min` 으로 남긴다 — *"S3 의 이동·교환이 그룹 안에서 얼마나
        손해 보는가"*. **지금까지 이 숫자가 없었다.**

        비용은 열거만 한 것보다 약 14% 더 든다 (휴리스틱 36회 + 열거 256회).
        그 값으로 **같은 그룹에 대한 직접 비교**를 얻는다 — 런 단위로 따로 돌려
        비교하면 그룹 경계가 달라져 섞인다.

        ## 가능해 판정은 휴리스틱과 **같은 규칙**을 쓴다

        `_accept` 의 가드를 그리디 기준으로 그대로 적용한다 — 막다른 길을 새로
        만들지 않고, 한 대를 이탈비용 위로 밀어내지 않는다. 그래야 "휴리스틱이
        지는 몫" 이 **탐색 범위 차이**만으로 설명된다.
        """

        greedy = {i: answers[i] for i in group}
        targets = self._targets(group, answers)

        # ① 휴리스틱을 먼저 돌린다 (원장과 answers 가 휴리스틱 해로 남는다)
        heur = self._improve_group(ledger, world, group, by_id, answers, base, n)
        heur_assign = {i: answers[i] for i in group}

        # ② 그리디로 되돌린다. `_group_total` 이 원장을 answers 상태로 맞춘다
        for i in group:
            answers[i] = greedy[i]
        start = self._group_total(ledger, world, group, by_id, answers)
        if start is None:                 # 그리디가 이미 불가능 — 휴리스틱 해로 돌아간다
            for i in group:
                answers[i] = heur_assign[i]
            self._rebook(ledger, group, answers, heur[3])
            return heur

        # ③ 전수 열거. 가드는 **그리디 기준**으로 고정한다
        esc = self.settings.escape_cost_min
        best: _Total | None = None
        best_combo: tuple[str, ...] | None = None

        for combo in product(targets, repeat=len(group)):
            for i, s in zip(group, combo, strict=True):
                answers[i] = s
            trial = self._group_total(ledger, world, group, by_id, answers)
            n["exact_tried"] += 1
            if trial is None:
                continue
            if trial[2] > start[2]:                       # 막다른 길을 새로 만든다
                continue
            if esc > 0 and trial[1] > esc and trial[1] > start[1]:
                continue                                  # 한 대를 이탈로 밀어낸다
            # 동점은 combo 사전순으로 깨뜨린다 — 같은 입력에 같은 답이 나와야 한다
            if best is None or trial[0] < best[0] - COST_EPS:
                best, best_combo = trial, combo

        if best is None or best_combo is None:
            for i in group:
                answers[i] = heur_assign[i]
            self._rebook(ledger, group, answers, heur[3])
            return heur

        # ④ 최적해를 적용하고, 휴리스틱이 얼마나 졌는지 남긴다
        for i, s in zip(group, best_combo, strict=True):
            answers[i] = s
        self._rebook(ledger, group, answers, best[3])

        n["exact_groups"] += 1
        gain = heur[0] - best[0]
        if gain > COST_EPS:
            n["exact_better_groups"] += 1
        self._bump("exact_gain_min", max(gain, 0.0))
        return best

    # -- MILP (#143) ----------------------------------------------------------
    def _scope(self, group: Sequence[str], answers: Mapping[str, str],
               world: WorldView) -> list[str]:
        """MILP 가 후보로 **시도할** 휴게소. `candidate_scope` 가 고른다.

            "group"     그룹이 이미 쓰는 곳 — S3 와 **같은 축**이다. 기본값
            "feasible"  코리도 전체를 주고 `_candidate` 가 거른다 (§5.3-1 필터)
            "all"       같지만 **못 가는 곳도 변수로 만든다** — 필터 ablation

        `"feasible"` 과 `"all"` 이 같은 집합을 시도하고 **변수 수만 다른** 이유:
        필터는 "못 가는 곳을 후보에서 빼는 것" 이고, 끄면 변수가 남는다. 답은 같고
        모형만 커진다 — 그 차이가 ablation 이 재려는 것이다.
        """

        if self.settings.candidate_scope == "group":
            return self._targets(group, answers)
        return sorted(world.stations)

    def _measure(self, ledger, world: WorldView, group: Sequence[str],
                 by_id: Mapping[str, EVState], answers: Mapping[str, str],
                 start: _Total, n: dict[str, int]) -> GroupProblem | None:
        """MILP 의 **계수를 잰다.** 못 세우면 `None`.

        ## 왜 재나 — 모형을 다시 쓰지 않기 위해서다

        $c_{i,s}$ 와 $\\delta_{i,j,s}$ 를 식으로 유도하려면 충전곡선과 큐 규칙을
        MILP 쪽에 한 번 더 써야 한다. 그러면 **규칙이 두 곳에 있게 되고**, 한쪽만
        고치는 일이 반드시 생긴다 (설계 규칙 1). 그래서 `_candidate` 를 **불러서**
        잰다 — 정책이 쓰는 그 함수 그대로다.

        ## 재는 비용이 지수가 아니다

            c_{i,s}      그룹을 원장에서 다 빼고 한 대씩          — W·T 회
            δ_{i,j,s}    j 만 넣고 i 를 다시 잰다 (선행쌍만)      — ≤ W²·T 회

        `window=4` · 후보 4곳이면 16 + 48 = **64 회**다. 전수 열거는 같은 자리에서
        256 조합 × 4 대 = **1,024 회**를 쓴다. **이 차이가 W 를 키울 수 있게
        하는 것**이고, MILP 를 붙이는 이유다 (`docs/objective.md` §6).

        ## 끝나면 원장을 **들어온 상태로** 돌려 둔다

        재는 동안 원장을 헤집는다. 호출자가 복원을 신경 쓰지 않도록 여기서
        `start` 상태로 되돌린다 — `_group_total` 과 같은 약속이다.
        """

        rf = world.range_factor
        scope = self._scope(group, answers, world)
        wide = self.settings.candidate_scope == "all"

        # ① 혼자 갔을 때 — 그룹 전원을 원장에서 뺀다. 서로가 보이면 "혼자" 가 아니다
        for ev_id in group:
            ledger.release(ev_id)

        cands: dict[str, tuple[str, ...]] = {}
        cost: dict[tuple[str, str], float] = {}
        dwell: dict[tuple[str, str], float] = {}
        arrs: dict[tuple[str, str], Arrival] = {}
        eta: dict[tuple[str, str], tuple[float, str]] = {}
        dead: set[tuple[str, str]] = set()
        banned: set[tuple[str, str]] = set()

        for ev_id in group:
            ev = by_id[ev_id]
            opportunity = self._opportunity(ev)
            opts: list[str] = []
            for sid in scope:
                st = world.stations.get(sid)
                if st is None:
                    continue
                cand = self._candidate(ev, world, ledger, st, rf,
                                       opportunity=opportunity)
                if cand is None:
                    if wide:            # 필터를 끈 자리 — 변수는 만들고 0 으로 묶는다
                        opts.append(sid)
                        cost[ev_id, sid] = dwell[ev_id, sid] = 0.0
                        banned.add((ev_id, sid))
                    continue
                opts.append(sid)
                cost[ev_id, sid] = cand[0] + self._extra_cost(ev_id, sid)
                dwell[ev_id, sid] = cand[0]
                arrs[ev_id, sid] = cand[1]
                eta[ev_id, sid] = (float(cand[1].arrival_min), ev_id)
                if not self._can_finish(ev, world, st.offset_km, cand[1].soc_to, rf):
                    dead.add((ev_id, sid))
            usable = [s for s in opts if (ev_id, s) not in banned]
            if not usable:
                # 이 차가 갈 곳이 하나도 없다. 그리디 해가 있었으니 생길 수 없는
                # 일이지만, 생기면 **MILP 를 세우지 않는다** (고치지 않는다)
                self._rebook(ledger, group, answers, start[3])
                return None
            cands[ev_id] = tuple(sorted(opts))

        # ② 선행쌍 — j 만 원장에 넣고 i 를 다시 잰다.
        #
        #    FIFO 순서는 (도착시각, ev_id) 다 (`StationLedger`). 그래서 어느 쪽이
        #    선행인지 **배정과 무관하게** 미리 정해진다 — 그게 이 정식화가 쌍별로
        #    끝날 수 있는 이유다 (docs/objective.md §3 "순서는 미리 안다")
        delta: dict[tuple[str, str, str], float] = {}
        for sid in sorted({s for (_, s) in eta}):
            st = world.stations[sid]
            for j in group:
                if (j, sid) not in arrs:
                    continue
                ledger.book(j, sid, arrs[j, sid])
                for i in group:
                    if i == j or (i, sid) not in arrs:
                        continue
                    if eta[j, sid] >= eta[i, sid]:
                        continue            # j 가 내 뒤에 온다 — 내 대기와 무관하다
                    cand = self._candidate(by_id[i], world, ledger, st, rf,
                                           opportunity=self._opportunity(by_id[i]))
                    n["milp_pairs"] += 1
                    if cand is None:
                        continue
                    gap = cand[0] - dwell[i, sid]
                    if gap > COST_EPS:
                        delta[i, j, sid] = gap
                ledger.release(j)

        self._rebook(ledger, group, answers, start[3])

        esc = self.settings.escape_cost_min
        # 상한은 **그리디가 반드시 만족하는 값**으로 잡는다 (`_accept` 와 같은 뜻).
        # 그리디보다 좁게 잡으면 들어온 해조차 비가능해가 되고 MILP 가 매번 실패한다
        cap = max(float(esc), start[1]) if esc > 0 else float("inf")

        n["milp_binaries"] += sum(len(v) for v in cands.values())
        n["milp_cands"] += sum(1 for k in cost if k not in banned)
        return GroupProblem(
            evs=tuple(group), cands=cands, cost=cost, dwell=dwell, delta=delta,
            dead=frozenset(dead), dead_budget=int(start[2]), worst_cap=cap,
            banned=frozenset(banned))

    def _milp_group(self, ledger, world: WorldView, group: Sequence[str],
                    by_id: Mapping[str, EVState], answers: dict[str, str],
                    base: _Total, n: dict[str, int]) -> _Total:
        """★ 그룹을 **MILP 로** 푼다 (#143). 근사이므로 **휴리스틱과 견준다.**

        순서가 `_exact_group` 과 같다 — 휴리스틱을 먼저 돌리고, 그리디로 되돌린
        뒤 MILP 를 풀고, **정확한 채점기로 다시 재서** 둘 중 나은 것을 쓴다.

        ## 왜 "둘 중 나은 것" 인가

        정식화가 근사다 (`milp.py` 의 겹침 과대계산). 근사가 만든 해를 정확한
        채점에서 재면 **휴리스틱보다 나쁠 수 있다.** 그때 그것을 쓰면서 "최적해"
        라고 부르면 #118 · #139 와 같은 종류의 거짓이 된다. 그래서

            근사 오차 = (정확한 Σd_i + r_i) − (MILP 목적값)      ← 재서 남긴다
            졌으면 버린다                                        ← fallback_used

        둘을 같은 그룹에서 비교하는 이유도 `_exact_group` 과 같다 — 런을 따로
        돌려 비교하면 그룹 경계가 달라져 섞인다.

        ## 가드는 **그리디 기준**으로 고정한다

        막다른 길을 늘리지 않고, 한 대를 이탈비용 위로 밀어내지 않는다. MILP 쪽은
        그 둘을 **제약으로** 받았고 (`dead_budget` · `worst_cap`), 여기서 정확한
        채점으로 **한 번 더** 확인한다 — 근사 때문에 제약이 샐 수 있다.
        """

        greedy = {i: answers[i] for i in group}

        heur = self._improve_group(ledger, world, group, by_id, answers, base, n)
        heur_assign = {i: answers[i] for i in group}

        def keep_heuristic(why: str) -> _Total:
            for i in group:
                answers[i] = heur_assign[i]
            self._rebook(ledger, group, answers, heur[3])
            n[why] += 1
            return heur

        for i in group:
            answers[i] = greedy[i]
        start = self._group_total(ledger, world, group, by_id, answers)
        if start is None:
            return keep_heuristic("milp_fallback")

        problem = self._measure(ledger, world, group, by_id, answers, start, n)
        if problem is None:
            return keep_heuristic("milp_fallback")

        sol = solve_group(problem, time_limit_s=float(self.settings.milp_time_limit_s))
        self._bump("milp_solve_s", sol.solve_time_s)
        # 완료조건이 **변수와 제약을 둘 다** 요구한다 (#143). 제약 수는 모형이
        # 커지는 모양을 보여 준다 — 변수는 |C_i| 로 늘고 제약은 선행쌍으로 늘어서
        # 둘이 다른 속도로 커진다
        self._bump("milp_rows", float(sol.n_constraints))
        n["milp_groups"] += 1
        if sol.time_limit_hit:
            n["milp_timeouts"] += 1
        if not sol.solved or sol.assign is None:
            return keep_heuristic("milp_fallback")

        for i, sid in sol.assign.items():
            answers[i] = sid
        trial = self._group_total(ledger, world, group, by_id, answers)
        if trial is None:
            return keep_heuristic("milp_fallback")

        # 근사 오차 — **부호를 살려서** 더한다. 겹침을 과대 계산하므로 보통 음수다
        self._bump("milp_approx_err_min", trial[0] - sol.objective)
        self._bump("milp_approx_abs_min", abs(trial[0] - sol.objective))

        if not self._ok_vs(start, trial):
            return keep_heuristic("milp_rejected")
        if trial[0] >= heur[0] - COST_EPS:
            # **비긴 것과 진 것을 가른다.** 비김은 "휴리스틱이 이미 최적" 이라는
            # 뜻이고, 짐은 "근사가 틀린 해를 골랐다" 는 뜻이다 — 섞으면 근사의
            # 품질을 전혀 알 수 없다
            return keep_heuristic(
                "milp_tied" if trial[0] <= heur[0] + COST_EPS else "milp_lost")

        n["milp_better"] += 1
        self._bump("milp_gain_min", heur[0] - trial[0])
        return trial

    def _ok_vs(self, start: _Total, trial: _Total) -> bool:
        """`_accept` 의 **가드만** 쓴다 — 합이 줄었는지는 따로 본다.

        `_accept` 는 "합이 줄고 + 가드 통과" 를 한 번에 본다. MILP 는 비교 상대가
        그리디가 아니라 **휴리스틱 해**라서 둘을 갈라야 한다.
        """

        if trial[2] > start[2]:
            return False
        esc = self.settings.escape_cost_min
        return not (esc > 0 and trial[1] > esc and trial[1] > start[1])

    # -- 하나뿐인 차이 --------------------------------------------------------
    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """★ S2 와의 **단 하나의 차이** — 그리디로 한 번 정하고, **이동·교환**을 본다.

        ① 는 `super().decide()` 그대로다. 그래서 `window < 2` 면 S2 와 **비트 단위로
        같다** — 개선 패스를 아예 안 돈다. 상행 adoption 시드1 하루를 돌려
        `_candidate` 호출 수까지 같은 것을 확인했다 (120,448 회).
        """

        answers = dict(super().decide(evs, world))

        window = int(self.settings.window)
        if window < 2:
            return answers

        ledger = world.require_ledger()
        by_id = {e.ev_id: e for e in evs}

        # 실제 휴게소를 받은 차만 본다. 무충전·이탈은 S2 의 판정을 그대로 둔다
        picked = sorted(
            (by_id[i].offset_km, i) for i, ans in answers.items()
            if i in by_id and ans in world.stations
        )
        ids = [i for _, i in picked]
        if len(ids) < 2:
            return answers

        t0 = time.perf_counter()
        n = {"move_tried": 0, "move_done": 0, "swap_tried": 0, "swap_done": 0,
             "exact_tried": 0, "exact_groups": 0, "exact_better_groups": 0,
             "milp_groups": 0, "milp_better": 0, "milp_fallback": 0,
             "milp_lost": 0, "milp_tied": 0, "milp_rejected": 0,
             "milp_timeouts": 0, "milp_binaries": 0,
             "milp_cands": 0, "milp_pairs": 0}
        mode = self.settings.solver_mode

        for start in range(0, len(ids), window):
            group = ids[start:start + window]
            base = self._group_total(ledger, world, group, by_id, answers)
            if base is None:
                continue

            if mode == "exact":
                base = self._exact_group(ledger, world, group, by_id, answers, base, n)
            elif mode == "milp":
                base = self._milp_group(ledger, world, group, by_id, answers, base, n)
            else:
                base = self._improve_group(ledger, world, group, by_id, answers, base, n)

        for k, v in n.items():
            self._bump(k, float(v))
        self._bump("swap_ms", (time.perf_counter() - t0) * 1000.0)
        self._bump("swap_calls", 1.0)
        return answers
