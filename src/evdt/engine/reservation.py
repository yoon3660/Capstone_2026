"""예약 원장 — 코리도 전체 (#83, 설계문서 §5.4).

`StationLedger`(휴게소 하나)를 코리도 전체로 묶은 것이다. **S1 이 보는 세계**다.

## 왜 이게 S0 를 고치나

#59 가 S0 가 지는 이유를 숫자로 보였다 — 평균 대기 +40.8%. 원인은 "정보가 낡아서" 가
아니라 **낡은 정보를 모두가 같이 보고 같이 움직여서**다.

    같은 5분 안에 결정한 차 전원이 같은 화면 → 같은 계산 → 같은 곳
    40분 뒤 도착하면 앞에 30대가 서 있다

**원장은 그 화면에 "남들이 어디로 갈지" 를 더한다.** 내 앞에 이미 배정된 차가 보이면
같은 곳으로 몰리지 않는다.

## 순차 결합이 핵심이다

한 Δt 안에서도 **앞 차의 배정이 원장에 들어간 뒤 뒤 차가 고른다.** 전원이 같은
스냅샷을 보고 동시에 고르면 원장이 있어도 S0 와 똑같이 몰린다 — 원장을 **읽기만**
하고 **쓰지 않으면** 아무것도 안 바뀐다.

## 상태

    PLANNED    배정됐고 아직 안 왔다 (ETA 로만 존재)
    CHARGING   도착해서 꽂았다
    DONE       끝났다
    NO_SHOW    배정됐는데 안 왔다 (이탈했거나 계획이 바뀌었다)

`PLANNED` 만 취소·이동할 수 있다. 꽂은 차를 원장에서 빼면 실제 점유와 어긋난다.

## 큐 규칙은 여기서 새로 쓰지 않는다

`StationLedger` 를 거치고, 그것은 `queue_rule` 을 부른다. 호출자가 둘뿐이라는 테스트가
있다 (`tests/test_queue_rule.py::test_queue_rule_has_at_most_two_callers`).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum

from evdt.engine.ledger import Arrival, Charger, StationLedger

__all__ = ["Reservation", "ReservationLedger", "ResState"]


class ResState(Enum):
    PLANNED = "PLANNED"
    CHARGING = "CHARGING"
    DONE = "DONE"
    NO_SHOW = "NO_SHOW"


@dataclass(frozen=True, slots=True)
class Reservation:
    ev_id: str
    station_id: str
    eta_min: float
    state: ResState = ResState.PLANNED


@dataclass
class ReservationLedger:
    """코리도 전체의 예약 원장. 휴게소마다 `StationLedger` 하나."""

    stations: dict[str, StationLedger]
    _res: dict[str, Reservation] = field(default_factory=dict)

    @classmethod
    def build(cls, chargers_by_station: dict[str, tuple[Charger, ...]]) -> ReservationLedger:
        return cls({sid: StationLedger(ch) for sid, ch in chargers_by_station.items()})

    # -- 묻기 -----------------------------------------------------------------
    def dwell_if_i_go(self, station_id: str, arr: Arrival) -> float:
        """이 차가 이 시각에 그 휴게소에 들어오면 머무는 시간(분). **원장은 안 바뀐다.**

        `StationLedger.evaluate` 를 그대로 쓴다 — 큐 계산이 갈라지면 정책이 재는 시간과
        실제 점유시간이 어긋난다.
        """

        led = self.stations.get(station_id)
        if led is None:
            raise KeyError(f"원장에 없는 휴게소다: {station_id}")
        return led.evaluate(arr, exclude=arr.ev_id)

    def booked(self, station_id: str) -> int:
        """그 휴게소에 **아직 안 온** 예약 수. 정책이 몰림을 보는 가장 싼 신호다."""

        return sum(1 for r in self._res.values()
                   if r.station_id == station_id and r.state is ResState.PLANNED)

    def reservation(self, ev_id: str) -> Reservation | None:
        return self._res.get(ev_id)

    # -- 바꾸기 ---------------------------------------------------------------
    def book(self, ev_id: str, station_id: str, arr: Arrival) -> None:
        """배정을 원장에 **쓴다.** 이게 있어야 뒤 차가 앞 차를 본다.

        같은 차가 이미 예약돼 있으면 옮긴다 (ETA 가 바뀌었거나 다른 곳으로 보냈다).
        """

        if ev_id in self._res:
            self.release(ev_id)
        led = self.stations.get(station_id)
        if led is None:
            raise KeyError(f"원장에 없는 휴게소다: {station_id}")
        led.commit(arr)
        self._res[ev_id] = Reservation(ev_id, station_id, float(arr.arrival_min))

    def release(self, ev_id: str) -> None:
        """예약을 뺀다. **아직 안 온 차만** 뺄 수 있다."""

        res = self._res.get(ev_id)
        if res is None:
            return
        if res.state is not ResState.PLANNED:
            raise ValueError(
                f"{ev_id} 는 {res.state.value} 다 — 꽂은 차를 원장에서 빼면 "
                "원장과 실제 점유가 어긋난다"
            )
        self.stations[res.station_id].cancel(ev_id)
        del self._res[ev_id]

    def update_eta(self, ev_id: str, eta_min: float) -> None:
        """주행 중 ETA 가 바뀌면 원장도 바뀐다 (설계문서 §5.4).

        FIFO 라서 **도착 순서가 바뀌면 그 뒤 차들의 대기가 전부 달라진다.** 그래서
        빼고 다시 넣는다 — 자리만 고쳐 넣으면 순서가 깨진다.
        """

        res = self._res.get(ev_id)
        if res is None or res.state is not ResState.PLANNED:
            return
        led = self.stations[res.station_id]
        old = next(a for a in led.arrivals if a.ev_id == ev_id)
        led.cancel(ev_id)
        # 충전 요구는 그대로 두고 **도착 시각만** 바꾼다
        led.commit(dataclasses.replace(old, arrival_min=float(eta_min)))
        self._res[ev_id] = Reservation(ev_id, res.station_id, float(eta_min), res.state)

    def mark(self, ev_id: str, state: ResState) -> None:
        res = self._res.get(ev_id)
        if res is not None:
            self._res[ev_id] = Reservation(res.ev_id, res.station_id, res.eta_min, state)
