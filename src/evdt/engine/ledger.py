"""예약 원장 (설계문서 §5.4) — 휴게소 하나의 도착열과 충전기 상태 투영.

queue_rule 의 **두 번째 호출자**다 (첫 번째는 world/sim.py). 큐 계산이 필요한 엔진
코드는 queue_rule 을 직접 부르지 않고 여기를 거친다 — 호출자가 늘면 규칙이 갈라진다
(tests/test_queue_rule.py::test_queue_rule_has_at_most_two_callers).

지금 있는 것 (UE 균형이 쓰는 최소한, 이슈 #29)
    commit(arr)                    도착 한 건을 원장에 넣는다
    cancel(ev_id)                  뺀다
    evaluate(arr, exclude=...)     "이 차가 이 시각에 들어오면 몇 분 머무는가" — 원장은 안 바꾼다

§5.4 의 원칙은 그대로 지킨다
    - 연속시간(분)으로 저장한다
    - 시작·종료·충전기는 저장하지 않고 필요할 때 queue_rule 로 다시 쌓는다.
      다만 "j 번째 도착 직전 충전기 상태" 는 캐시한다 — 끼우거나 빼면 그 뒤만 버린다
    - 소프트 예약: 순서는 도착 시각 (FIFO), 동시 도착은 ev_id

아직 없는 것 (S1, Sprint 2): 예약 상태(PLANNED/CHARGING/DONE/NO_SHOW), update_eta,
occupancy 뷰. S1 을 만들 때 이 클래스를 넓힌다.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence

from evdt.world.queue_rule import Arrival, Charger, assign, chargers_after

__all__ = ["Arrival", "Charger", "StationLedger"]


class StationLedger:
    """휴게소 하나의 예약 원장.

    states[j] = j 번째 도착 직전 충전기 상태 (states[len] = 전부 들어간 뒤).
    유효한 앞부분만 들고 있고, 물어볼 때 필요한 만큼만 queue_rule 로 쌓는다.
    """

    def __init__(self, chargers: tuple[Charger, ...]) -> None:
        self._keys: list[tuple[float, str]] = []
        self._arrivals: list[Arrival] = []
        self._states: list[tuple[Charger, ...]] = [tuple(chargers)]
        self._key_of: dict[str, tuple[float, str]] = {}

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def arrivals(self) -> tuple[Arrival, ...]:
        """도착 시각 순. ETA 를 바꾸려면 원래 충전 요구를 알아야 한다 (#83)."""

        return tuple(self._arrivals)

    # -- 바꾸기 ---------------------------------------------------------------
    def commit(self, arr: Arrival) -> None:
        if arr.ev_id in self._key_of:
            raise ValueError(f"이미 원장에 있는 차다: {arr.ev_id} (한 휴게소에 한 번만 선다)")

        key = (float(arr.arrival_min), arr.ev_id)
        idx = bisect_left(self._keys, key)
        self._keys.insert(idx, key)
        self._arrivals.insert(idx, arr)
        self._key_of[arr.ev_id] = key
        del self._states[idx + 1:]

    def cancel(self, ev_id: str) -> None:
        idx = bisect_left(self._keys, self._key_of.pop(ev_id))
        del self._keys[idx], self._arrivals[idx]
        del self._states[idx + 1:]

    # -- 묻기 -----------------------------------------------------------------
    def _state_before(self, idx: int) -> tuple[Charger, ...]:
        while len(self._states) <= idx:
            j = len(self._states) - 1
            (a,) = assign([self._arrivals[j]], self._states[j])
            self._states.append(chargers_after(self._states[j], [a]))
        return self._states[idx]

    def evaluate(self, arr: Arrival, *, exclude: str | None = None,
                 extra: Sequence[Arrival] = ()) -> float:
        """체류시간(분)만. `evaluate_parts` 의 첫 칸이다 — 계산은 한 곳에만 있다."""

        return self.evaluate_parts(arr, exclude=exclude, extra=extra)[0]

    def evaluate_parts(self, arr: Arrival, *, exclude: str | None = None,
                       extra: Sequence[Arrival] = ()) -> tuple[float, float]:
        """이 차가 arr.arrival_min 에 들어오면 `(체류, 대기)` (분). 원장은 그대로.

        체류 = 대기 + 충전이다. **둘을 따로 돌려주는 이유**는 목적함수가 대기를
        충전보다 무겁게 셀 수 있어야 하기 때문이다 (`Objective.wait_weight`,
        설계문서 §6.3 의 1.0~1.5 민감도). 여기서 갈라 주지 않으면 정책이 대기만
        따로 알 길이 없고, 그러면 그 민감도를 아예 못 돌린다.

        충전시간은 **배정된 충전기 출력에 따라 달라진다** — 그래서 큐 규칙을
        거치지 않고는 가를 수 없다. `assign` 이 이미 둘을 다 들고 있다.

        FIFO 라서 **먼저 온 차들만** 답을 정한다. exclude 에 준 차가 원장에서 그보다
        앞에 있으면 그 차를 빼고 다시 쌓는다 — 자기 자신과 줄을 설 수는 없다.

        `extra` 는 **원장에 없는 가정된 도착**이다 (S2 의 예측, #118). 끼워서 계산만
        하고 원장에는 넣지 않는다 — 예측은 아직 일어나지 않은 일이라, 원장에 쓰면
        다른 차가 **있지도 않은 예약**을 확정된 것으로 본다 (#83 이 두 번째 정차를
        원장에서 뺀 것과 같은 이유).

        이미 원장에 있는 ev_id 는 `extra` 에서 버린다. 같은 차를 두 번 세면 예측이
        줄을 **길게** 만들고, 그 과대추정이 "예측을 쓰니 나빠졌다" 로 보고된다.
        """

        key = (float(arr.arrival_min), arr.ev_id)
        idx = bisect_left(self._keys, key)

        extras = sorted(
            ((float(e.arrival_min), e.ev_id), e) for e in extra
            # 내 뒤에 오는 차는 FIFO 상 내 대기에 영향이 없다. 나 자신과 이미
            # 원장에 있는 차는 두 번 세지 않는다
            if (float(e.arrival_min), e.ev_id) < key
            and e.ev_id != arr.ev_id and e.ev_id not in self._key_of
        )

        own = self._key_of.get(exclude) if exclude is not None else None
        if own is not None and own >= key:
            own = None      # 내 뒤에 있으면 빼도 답이 같다

        # 캐시는 **고쳐야 할 첫 자리 앞까지** 쓸 수 있다. 그 뒤만 다시 쌓는다
        cut = idx
        if extras:
            cut = min(cut, bisect_left(self._keys, extras[0][0]))
        if own is not None:
            cut = min(cut, bisect_left(self._keys, own))

        state = self._state_before(cut)

        merged = [(self._keys[k], self._arrivals[k]) for k in range(cut, idx)
                  if self._keys[k] != own]
        merged.extend(extras)
        merged.sort(key=lambda kv: kv[0])

        for _, each in merged:
            (one,) = assign([each], state)
            state = chargers_after(state, [one])

        (a,) = assign([arr], state)
        return a.end_min - float(arr.arrival_min), a.wait_min

    # -- 한계 외부비용 (#138) -------------------------------------------------
    def externality(self, arr: Arrival, *, exclude: str | None = None,
                    extra: Sequence[Arrival] = ()) -> float:
        """내가 끼어들면 **남들의 종료 시각 합**이 얼마나 밀리나 (분).

        `externality_parts` 의 첫 칸이다 — 계산은 한 곳에만 있다.
        """

        return self.externality_parts(arr, exclude=exclude, extra=extra)[0]

    def externality_parts(
        self, arr: Arrival, *, exclude: str | None = None,
        extra: Sequence[Arrival] = (),
    ) -> tuple[float, dict[str, float]]:
        """`(외부비용 합, ev_id → 밀린 시간)` (설계문서 §6.1).

            MEC = Σ(다른 차 j 의 **종료 시각**, 나 포함 투영)
                − Σ(j 의 종료 시각, 나 제외 투영)

        ## ⚠ 왜 대기시간이 아니라 **종료 시각**인가

        §6.1: *"내가 끼어들면 뒤차의 **충전기 배정**이 바뀌어 충전 출력, 즉
        충전시간까지 달라진다. 종료 시각은 대기와 충전시간 변화를 **모두**
        담는다."* 대기만 보면 *"100kW 자리를 빼앗아 50kW 로 밀어낸"* 몫이 빠진다.

        ## 내 **앞** 차들은 정확히 0 을 기여한다

        `assign` 은 도착순으로 처리하고 FIFO 라서, 나보다 먼저 온 차의 충전기
        선택과 종료 시각은 내가 있든 없든 같다. 그래서 합을 **전원**에 대해 내도
        앞 차들은 서로 상쇄된다 — §6.1 의 식이 *"다른 차량 j"* 라고만 적은 이유다.
        테스트가 그 0 을 직접 확인한다.

        ## 비용이 `evaluate` 보다 비싸다 — **내 뒤까지 다시 쌓아야 한다**

        `evaluate` 는 내 앞만 보면 끝난다. 여기서는 꼬리를 **두 번** 투영한다
        (나 포함 / 나 제외). 캐시는 **내 앞까지**만 도와주므로 그 뒤는 매번 다시
        쌓는다. S1 이 런 하나에 원장 질의를 약 50,000번 하므로, 이 배수가 그대로
        계산시간이 된다 (#77 · #138 이 재서 적기로 한 값).

        **뒤에 아무도 없으면 0 이다.** 가장 싼 가드이고, §6.1 예제에서 휴게소 B 의
        MEC 가 0 인 이유다.
        """

        key = (float(arr.arrival_min), arr.ev_id)
        idx = bisect_left(self._keys, key)

        own = self._key_of.get(exclude) if exclude is not None else None

        # 예측 도착은 **내 뒤도** 센다 — 내가 미는 상대가 바로 그 차들이다.
        # (`evaluate` 는 내 앞만 쓴다. 거기서는 뒤차가 내 대기에 영향이 없다)
        extras = [((float(e.arrival_min), e.ev_id), e) for e in extra
                  if e.ev_id != arr.ev_id and e.ev_id not in self._key_of]

        # 꼬리가 비어 있으면(뒤에 아무도 없고 예측도 없다) 밀 상대가 없다
        if idx >= len(self._keys) and not extras:
            return 0.0, {}

        cut = idx
        if extras:
            cut = min(cut, bisect_left(self._keys, min(k for k, _ in extras)))
        if own is not None:
            cut = min(cut, bisect_left(self._keys, own))

        state = self._state_before(cut)

        tail = [(self._keys[k], self._arrivals[k])
                for k in range(cut, len(self._keys)) if self._keys[k] != own]
        tail.extend(extras)
        tail.sort(key=lambda kv: kv[0])
        others = [a for _, a in tail]

        without = {a.ev_id: a.end_min for a in assign(others, state)}
        with_me = {a.ev_id: a.end_min for a in assign([*others, arr], state)}

        # 나 자신은 남이 아니다. 앞 차들은 차가 0 이라 합에 영향이 없다
        delta = {ev_id: with_me[ev_id] - end
                 for ev_id, end in without.items()}
        return sum(delta.values()), delta
