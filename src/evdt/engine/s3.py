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

from evdt.engine.ledger import Arrival
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

        순서는 `group` 이 준 순서로 **고정**한다. 비용이 순서에 의존하므로, 두 배정을
        비교할 때 같은 순서로 재야 공정하다.

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

        total, worst, n_dead = 0.0, 0.0, 0
        arrs: dict[str, Arrival] = {}
        for ev_id in group:
            ev = by_id[ev_id]
            st = world.stations.get(answers[ev_id])
            if st is None:
                return None
            cand = self._candidate(ev, world, ledger, st, rf,
                                   opportunity=self._opportunity(ev))
            if cand is None:
                return None
            if not self._can_finish(ev, world, st.offset_km, cand[1].soc_to, rf):
                n_dead += 1
            total += cand[0]
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
        n = {"move_tried": 0, "move_done": 0, "swap_tried": 0, "swap_done": 0}

        for start in range(0, len(ids), window):
            group = ids[start:start + window]
            base = self._group_total(ledger, world, group, by_id, answers)
            if base is None:
                continue

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
                targets = sorted({answers[i] for i in group})
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

        for k, v in n.items():
            self._bump(k, float(v))
        self._bump("swap_ms", (time.perf_counter() - t0) * 1000.0)
        self._bump("swap_calls", 1.0)
        return answers
