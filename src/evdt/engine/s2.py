"""S2 — 아직 결정하지 않은 차까지 보는 운전자 (사다리 세 번째 칸, #118).

S1 과 **딱 한 가지**가 다르다. 메서드 하나다 (`_dwell`).

    S1   원장이 말하는 내 도착 시각의 체류    ← **이미 배정된** 차만 센다
    S2   + 예측이 말하는 도착들               ← **아직 배정되지 않은** 차도 센다

나머지 전부 — 도달 가능성, 충전량, 이탈 판정, 순차 결합, ev_id 순서 — 는 `S1Policy`
를 그대로 물려받는다. 하나라도 다시 쓰면 그 차이가 **정보의 차이로 보고된다.**

## 왜 이 하나인가

S1 의 원장에는 **이미 질문을 받은 차만** 있다. 지금 시각 t 에 결정하는 차는 자기보다
먼저 결정한 차들만 본다. 그런데 내가 40분 뒤 도착할 때, 지금 100km 상류에 있어서
아직 질문도 받지 않은 차 30대가 나보다 먼저 그 휴게소에 도착할 수 있다.

**S1 은 그 30대를 구조적으로 못 본다.** 더 많이 반복해도 안 보인다 — 원장은 과거
결정의 기록이고, 그 차들은 아직 결정하지 않았다.

## 이 티켓이 재는 것은 "예측의 가치" 다 — 예측기가 아니다

여기서 쓰는 예측은 **직전 런의 실제 도착**이다 (`OracleForecast`). 만들 수 있는
예측기가 아니라, *"완전히 알면 얼마나 좋아지나"* 를 재는 자다.

예측기를 먼저 만들면 결과가 나빠도 **예측이 나쁜 건지 예측이 애초에 쓸모없는 건지
구분할 수 없다.** 그래서 천장을 먼저 잰다. 관측 가능한 것으로 예측하는 일은 #119 다.

**그 수치는 상한이 아니라 상한 추정치다** — 이유는 `forecast.py` 의 모듈 문서에
적혀 있다 (오라클이 보는 미래는 S1 의 미래다). 고정점 반복으로 좁힌다.

## 미리 정한 판정 기준 (#118, 결과를 보기 전에 정했다)

    S1 대비 총 사회적 비용이 5% 이상 줄지 않으면 → S2 를 접고 S3 로 간다

5% 인 이유: #59 가 S0→S1 에서 평균 대기 **40.8%** 를 움직였다. 정보를 한 칸 더 줘서
그 1/8 도 못 움직인다면, 남은 격차는 **정보가 아니라 결합**에 있다는 뜻이다 —
그게 S3(윈도우 내 동시 결정) 의 전제다. 천장이 낮다는 것을 먼저 아는 것이 이 티켓의
성공이고, 실패가 아니다.

## 하지 않는 것

- **예측을 원장에 쓰지 않는다.** 계산에만 끼운다 (`dwell_if_i_go(..., future=)`)
- **재배정하지 않는다** — 한번 쓴 예약을 옮기는 것은 S3 다
- **같이 결정하지 않는다** — 여전히 ev_id 순으로 한 대씩이다. 동시 결정은 S3 다
- **충전량을 정하지 않는다** — S0·S1 과 **같은 함수**를 쓴다 (설계문서 §2.3)
"""

from __future__ import annotations

from dataclasses import dataclass

from evdt.engine.ledger import Arrival
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.interfaces import WorldView


@dataclass(frozen=True)
class S2Settings(S1Settings):
    """S2 가 쓰는 값. **S0·S1 과 같아야 한다** — 한쪽만 다르면 그 차이가 정책 차이로 보인다."""


@dataclass(frozen=True)
class S2Policy(S1Policy):
    """예측을 더해서 고르는 정책. `S1Policy` 에서 `_dwell` 하나만 다르다."""

    stage: str = "S2"

    def _dwell(self, ledger, station_id: str, arr: Arrival, world: WorldView) -> float:
        """★ S1 과의 **단 하나의 차이** — 아직 결정하지 않은 차들을 줄에 더해서 센다.

        `(지금, 내 도착)` **사이**만 묻는다.

        - 지난 도착은 **원장과 실제 큐에 이미 있다.** 또 세면 두 번 센다
        - 내 뒤에 오는 차는 FIFO 상 내 대기를 늘리지 않는다

        예측은 **계산에만** 들어간다 — `dwell_if_i_go` 가 원장을 바꾸지 않는다.
        원장에 쓰면 다른 차가 아직 아무도 약속하지 않은 예약을 확정으로 본다 (#83).

        이미 이번 런에서 결정한 차는 `ReservationLedger` 가 걸러낸다 — 원장의 진짜
        예약과 예측의 그 차를 둘 다 세면 한 대가 두 대가 된다.
        """

        forecast = world.require_forecast()
        return ledger.dwell_if_i_go(station_id, arr, future=forecast.arrivals_between(
            station_id, after_min=world.t_min, before_min=arr.arrival_min,
        ))
