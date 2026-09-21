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

    def evaluate(self, arr: Arrival, *, exclude: str | None = None) -> float:
        """이 차가 arr.arrival_min 에 들어오면 머무는 시간(분) = 대기 + 충전. 원장은 그대로.

        FIFO 라서 **먼저 온 차들만** 답을 정한다. exclude 에 준 차가 원장에서 그보다
        앞에 있으면 그 차를 빼고 다시 쌓는다 — 자기 자신과 줄을 설 수는 없다.
        """

        idx = bisect_left(self._keys, (float(arr.arrival_min), arr.ev_id))
        own = self._key_of.get(exclude) if exclude is not None else None

        if own is not None and own < (float(arr.arrival_min), arr.ev_id):
            j = bisect_left(self._keys, own)
            state = self._state_before(j)
            for k in range(j + 1, idx):
                (a,) = assign([self._arrivals[k]], state)
                state = chargers_after(state, [a])
        else:
            state = self._state_before(idx)

        (a,) = assign([arr], state)
        return a.end_min - float(arr.arrival_min)
