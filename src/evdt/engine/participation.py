"""참여율 — 누구의 `decide()` 를 엔진이 하는가 (#168, 설계문서 §2.3).

    참여자   (u_i <  p)  엔진(또는 Oracle) 이 고른 휴게소로 간다 — 완전 준수
    비참여자 (u_i >= p)  스스로 고른다 (background, 기본은 MNL)

## 참여율이 건드리는 것은 휴게소 선택 하나뿐이다

충전이 필요한가, 얼마나 채우나, 기회 충전, 이탈 비용은 **수요를 만들 때 한 번** 정해지고
(`ue_demand.build_trip_demands`) 모든 스테이지가 같은 값을 받는다. 참여 여부가 그것들을
바꾸면 참여율의 효과에 "충전량이 달랐다" 가 섞인다. 그래서 이 래퍼는 **답을 누구에게
물을지만** 고른다.

## u_i — 차마다 한 번, 해시로 고정

    u_i = hashed_uniform("participation", seed, ev_id)

- **기존 난수 흐름을 쓰지 않는다.** `OPPORTUNITY_STREAM` 은 이탈 비용·habit_soc·기회
  충전을 **순서대로** 뽑는다. 거기서 하나를 더 뽑으면 기존 차량 속성이 전부 밀린다
- **중첩**: p=0.3 의 참여자 ⊂ p=0.6 ⊂ p=0.9. p 를 올리면 "참여자가 늘어난 효과" 만 보인다
- **공통 난수**: 같은 시드의 S1~S4 와 Oracle 이 **같은 참여자**를 상대한다

## 주행 중인 비참여자 (S4 의 `reask_en_route`)

시뮬레이터는 `reask_en_route` 가 켜진 정책에게 **가고 있는 차도** 묻는다. 비참여자는
엔진의 재배정을 받지 않으므로, 이미 고른 정차(같은 `stops_done`)에 대해서는 **답에서
뺀다.** 빠진 주행 차는 가던 길을 그대로 간다 (`world/corridor_sim.py` 의 `raw is None`
분기).

## 원장과 비참여자 — 측정하려는 효과다, 버그가 아니다

S1~S4 의 예약 원장은 **엔진만 쓴다** (`S1Policy._one` 의 `ledger.book`). 비참여자는
원장에 안 보이고, p 가 낮을수록 엔진이 아는 것이 줄어든다. 현실의 "엔진은 참여자의
계획만 안다" 에 해당한다.

Oracle 은 다르다 — **전부 안다.** 그래서 Oracle 에만 `NonParticipantAwareForecast` 를
씌워 비참여자의 실현 도착(과거 포함)을 보여 준다.

## 엔진이 비참여자를 얼마나 아나 — `nonpart_info` (#168 결정 문서 조건 ②)

위 기본 동작("none")은 엔진이 **지금 줄 서 있는 비참여자조차** 모르게 만든다. S1 은
대기를 원장으로만 재고 화면(`wait_min`)을 쓰지 않기 때문이다. 그러면 p < 1 의 격차에
"미래를 모르는 몫" 과 "현재를 못 보는 몫" 이 섞인다. 그래서 세 가지를 둔다.

    "none"     지금 그대로. 비참여자는 원장에 없다 (하한)
    "present"  **현재는 공개, 미래 계획은 비공개.** 비참여자가 휴게소에 실제로 도착하는
               시각이 되면 그때 원장에 올린다 — 충전기 점유는 공개 대기 정보다 (공식 비교용)
    "plans"    비참여자가 휴게소를 고르는 순간 원장에 올린다 — 계획까지 안다 (상한, 비현실적)

p = 1 이면 비참여자가 없어 세 방식이 같다 (테스트가 고정한다).

근사: 원장에 올리는 충전 요구는 그 차의 실제 요구다(꽂으면 충전기가 SoC 를 본다).
같은 차가 두 번째로 서면 `ReservationLedger.book` 이 첫 예약을 지운다 — 엔진 참여자와
같은 동작이다. 줄에서 포기·이탈한 비참여자는 원장에 남는다(조금 비관적인 쪽).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from evdt.engine.mnl import hashed_uniform
from evdt.interfaces import NON_STATION, EVState, Policy, WorldView, read_answer

#: 엔진이 비참여자를 아는 정도 (ParticipationPolicy.nonpart_info)
NONPART_INFO = ("none", "present", "plans")

#: 해시 흐름 이름 — MNL 의 ε 와 독립이다
PARTICIPATION_STREAM = "participation"


def participation_u(seed: int, ev_id: str) -> float:
    """차 한 대의 참여 난수 u_i ∈ (0, 1). 같은 (seed, ev_id) 면 언제나 같다."""

    return hashed_uniform(PARTICIPATION_STREAM, seed, ev_id)


def is_participant(seed: int, ev_id: str, p: float) -> bool:
    """u_i < p. p ≥ 1 이면 전원, p ≤ 0 이면 아무도 아니다."""

    if p >= 1.0:
        return True
    if p <= 0.0:
        return False
    return participation_u(seed, ev_id) < p


def participants(seed: int, ev_ids: Iterable[str], p: float) -> frozenset[str]:
    return frozenset(e for e in ev_ids if is_participant(seed, e, p))


@dataclass
class ParticipationPolicy:
    """엔진과 비참여자 행동 모델을 하나의 `Policy` 로 묶는다.

    시뮬레이터에게는 정책 하나로 보인다 — 세계는 참여율을 모른다 (설계 규칙 2).
    """

    engine: Policy
    background: Policy
    p: float
    seed: int
    #: 엔진·비참여자에게 각각 몇 번 물었나 (검증 V3 · 결과 manifest)
    counters: dict[str, int] = field(default_factory=lambda: {
        "engine_asked": 0, "background_asked": 0, "background_en_route_skipped": 0})
    #: 엔진이 비참여자를 얼마나 아나: "none" · "present" · "plans" (모듈 머리말)
    nonpart_info: str = "none"
    #: 비참여자가 이미 휴게소를 고른 (ev_id, stops_done)
    _committed: set[tuple[str, int]] = field(default_factory=set)
    #: "present" 에서 아직 도착 전인 비참여자 (도착 시각, ev_id, 휴게소, 도착)
    _pending: list[tuple[float, str, str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 0.0 <= self.p <= 1.0:
            raise ValueError(f"참여율은 0~1 이어야 한다: {self.p}")
        if self.nonpart_info not in NONPART_INFO:
            raise ValueError(f"nonpart_info 는 {NONPART_INFO} 중 하나: {self.nonpart_info!r}")
        self.counters.setdefault("nonpart_booked", 0)

    def _book(self, world: WorldView, ev_id: str, station_id: str, arr: Any) -> None:
        ledger = getattr(world, "ledger", None)
        if ledger is None or station_id not in getattr(ledger, "stations", {}):
            return
        ledger.book(ev_id, station_id, arr)
        self.counters["nonpart_booked"] += 1

    def _flush_arrived(self, world: WorldView) -> None:
        """"present": 도착 시각이 지난 비참여자를 원장에 올린다 (이제 관측된다)."""

        if not self._pending:
            return
        keep = []
        for item in self._pending:
            if item[0] <= world.t_min:
                self._book(world, item[1], item[2], item[3])
            else:
                keep.append(item)
        self._pending = keep

    @property
    def stage(self) -> str:
        return f"{self.engine.stage}@p{self.p:g}"

    @property
    def reask_en_route(self) -> bool:
        # 엔진이 원하면 시뮬레이터가 주행 차도 묻는다. 비참여자는 아래에서 걸러낸다
        return bool(getattr(self.engine, "reask_en_route", False))

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        mine: list[EVState] = []
        theirs: list[EVState] = []

        for ev in evs:
            if is_participant(self.seed, ev.ev_id, self.p):
                mine.append(ev)
            elif (ev.ev_id, ev.stops_done) in self._committed:
                # 이미 고르고 가는 중이다 — 엔진의 재질문은 비참여자에게 해당하지 않는다
                self.counters["background_en_route_skipped"] += 1
            else:
                theirs.append(ev)

        out: dict[str, str] = {}
        if self.nonpart_info == "present":
            self._flush_arrived(world)     # 엔진이 묻기 **전에** 지금까지 도착한 비참여자를 올린다
        if mine:
            self.counters["engine_asked"] += len(mine)
            out.update(self.engine.decide(mine, world))
        if theirs:
            self.counters["background_asked"] += len(theirs)
            got = self.background.decide(theirs, world)
            for ev in theirs:
                ans = got.get(ev.ev_id)
                if ans is None:
                    continue
                kind, _ = read_answer(str(ans))
                if kind not in NON_STATION:
                    self._committed.add((ev.ev_id, ev.stops_done))
                    self._reveal(ev, world, str(ans))
            out.update(got)
        return out


    def _reveal(self, ev: EVState, world: WorldView, station_id: str) -> None:
        """비참여자가 고른 곳을 정보 방식에 맞춰 원장에 올린다 (또는 도착까지 미룬다)."""

        if self.nonpart_info == "none":
            return
        plan = getattr(self.background, "plan_arrival", None)
        arr = plan(ev, world, station_id) if plan else None
        if arr is None:
            return
        if self.nonpart_info == "plans":
            self._book(world, ev.ev_id, station_id, arr)
        else:
            self._pending.append((float(arr.arrival_min), ev.ev_id, station_id, arr))


@dataclass(frozen=True)
class NonParticipantAwareForecast:
    """Oracle 전용 — 비참여자의 실현 도착을 **과거까지** 보여 준다.

    `OracleForecast.arrivals_between` 은 `after_min` **초과**만 준다. 지난 도착은 원장과
    실제 큐에 이미 있다는 전제다. 그런데 참여율 < 1 이면 비참여자는 **원장에 없다.**
    그대로 두면 Oracle 이 이미 줄 서 있는 비참여자를 못 보고 대기를 낮게 잰다.

    그래서 비참여자의 도착은 하한 없이 `before_min` 까지 전부 준다. 원장은 받은 도착을
    시각순으로 섞어 FIFO 를 처음부터 다시 계산하므로(`StationLedger.evaluate_parts` 의
    `extra`), 일찍 와서 이미 끝난 차는 자연히 대기에 기여하지 않는다.

    참여자는 원장에 진짜 예약이 있으므로 기존 동작 그대로다.
    """

    inner: Any
    non_participants: frozenset[str]
    #: 이보다 오래전에 온 비참여자는 **이미 떠났다고 본다** (분).
    #:
    #: 원장은 받은 도착 중 가장 이른 것부터 FIFO 를 다시 쌓는다(`StationLedger`
    #: 의 캐시가 그 앞까지만 쓰인다). 하루 시작부터 넘기면 매 질문마다 하루치를
    #: 다시 계산해서 회차 하나에 7분이 걸렸다 (#168 시험 실행). 체류(대기+충전)가
    #: 이보다 긴 비참여자는 놓친다 — 실행 결과의 wait/dwell 최대값과 같이 본다
    lookback_min: float = 240.0

    @cached_property
    def _np_by_station(self) -> dict[str, tuple[tuple[float, ...], tuple[Any, ...]]]:
        """휴게소별 비참여자 도착 (시각 목록, 도착). Δt 마다 다시 거르면 제일 비싸다."""

        out = {}
        for sid, arrs in self.inner.by_station.items():
            mine = tuple(a for a in arrs if a.ev_id in self.non_participants)
            out[sid] = (tuple(a.arrival_min for a in mine), mine)
        return out

    def arrivals_between(self, station_id: str, *,
                         after_min: float, before_min: float) -> tuple[Any, ...]:
        future = self.inner.arrivals_between(
            station_id, after_min=after_min, before_min=before_min)
        times, arrs = self._np_by_station.get(station_id, ((), ()))
        if not times or after_min >= before_min:
            return future
        # 비참여자는 [after − lookback, after] — inner 는 after **초과**만 주므로 경계도
        # 여기서 집는다. 정렬은 원장이 한다 (`StationLedger.evaluate_parts`)
        lo = bisect_left(times, float(after_min) - self.lookback_min)
        hi = bisect_right(times, float(after_min))
        if lo >= hi:
            return future
        return arrs[lo:hi] + tuple(future)

    def describe(self) -> str:
        return (f"{self.inner.describe()} + 비참여자 {len(self.non_participants):,}대의 "
                "과거 도착까지 (Oracle 전용)")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
