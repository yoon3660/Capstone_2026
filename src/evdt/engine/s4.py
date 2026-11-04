"""S4 — 시간축을 연다: 이미 한 배정도 다시 바꾼다 (사다리 다섯 번째 칸, #142).

S3 와 **딱 한 가지**가 다르다. **세계에게 "가고 있는 차에게도 다시 물어 달라" 고
말한다** (`reask_en_route`).

    S0~S3   한 번 휴게소를 고른 차는 **도착할 때까지 다시 안 묻는다**
    S4      가고 있는 차도 매 Δt 다시 묻는다 → **경로가 바뀔 수 있다**

정보는 S2 그대로, 공간 결합은 S3 그대로다. **바뀌는 것은 "언제까지 바꿀 수 있나"**
하나뿐이다.

## 왜 이게 남은 과녁인가

| 칸 | 바닥–천장 격차 축소율 |
|---|---:|
| S1 | +4.5% |
| S2 (관측 예측) | **+21.0%** |
| S3 (공간 결합) | +22.2% ← **+1.2%p 뿐이었다** |
| 완전예측 (벤치마크) | 27.5% |

#139 가 공간축을 닫았다 (S3 − S2 = +1.20 ± 2.01 %p, 기준 5%p 미달). 정보로는
27.5% 에서 멈추고 공간으로는 1.2%p 였다. **남은 것은 시간축이다.**

## 구현이 거의 없는 이유 — 이 칸은 **세계의 제약**이었다

S3 의 이동·교환 알고리즘은 **이미 "윈도우 안 차들을 같이 정한다".** 그런데
윈도우에 들어오는 차가 *"이번 Δt 에 처음 묻는 차"* 뿐이었다. 가고 있는 차는
시뮬레이터가 아예 안 물었기 때문이다.

**그래서 S4 는 알고리즘을 안 바꾼다.** 같은 알고리즘에 **더 많은 차가 들어온다.**
그게 시간축 결합의 정확한 뜻이다.

> 이 구조를 발표에서 그대로 말할 수 있다 — *"S3 와 S4 는 같은 최적화를 쓴다.
> 다른 것은 **언제까지 마음을 바꿀 수 있는가** 뿐이다."*

## ⚠ 이미 꽂은 차는 못 옮긴다 — 그런데 원장이 막아 주지 않는다

`ReservationLedger.release()` 는 `PLANNED` 만 뺄 수 있고 `CHARGING` 이면 터진다.
**그런데 그 가드가 무장돼 있지 않다** — 시뮬레이터가 `mark()` 를 한 번도 부르지
않아서 모든 예약이 영원히 `PLANNED` 다 (`ledger.py` 모듈 문서가 "아직 없는 것"
으로 적어 둔 그대로다).

**그래서 시간으로 막는다.** 시뮬레이터는 `left_at_min <= t < eta_min` 인 차만
다시 묻는다 — **이미 도착한 차는 애초에 후보가 아니다.** 그 위에 정책이
`relocate_lead_min` 으로 한 겹 더 막는다:

    도착까지 남은 시간 < relocate_lead_min  →  **그 차는 안 건드린다**

현실에서도 도착 2분 전 차를 다른 휴게소로 돌릴 수는 없다. **운영 제약이지
구현 편의가 아니다.**

## 미리 정한 판정 (결과를 보기 전에 정했다)

> **S3 대비 S0→UE 격차를 5%p 이상** 더 메우지 못하면, **시간축도 과녁이 아니다**
> 라고 기록하고 남은 몫을 **용량과 점유 모형**으로 돌린다.

**천장이 낮다는 것을 먼저 아는 것이 성공이고 실패가 아니다** (#118 · #139 와 같다).

## 하지 않는 것

- **MILP 를 쓰지 않는다** — #143 의 몫
- **terminal penalty 를 넣지 않는다** — #144 의 몫
- **중간에 포기시키지 않는다** — 달리던 차에게서 무충전·이탈 답은 시뮬레이터가
  무시한다. 그건 시간축 결합이 아니라 **다른 종류의 변경**이고, 섞으면 S3→S4
  차이에 "중간 포기" 가 들어간다
- **되돌아가지 않는다** — 지나친 휴게소는 `_candidate` 가 이미 거른다
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evdt.engine.s3 import S3Policy, S3Settings
from evdt.interfaces import EVState, WorldView


@dataclass(frozen=True)
class S4Settings(S3Settings):
    """S4 가 쓰는 값. `relocate_lead_min` 말고는 **S3 와 같아야 한다**."""

    #: 도착까지 이보다 적게 남은 차는 **안 옮긴다** (분).
    #:
    #: `0` 이면 시간축을 끄는 것이 아니라 **아무 때나 옮긴다**. 끄려면
    #: `reask_en_route` 가 False 여야 하는데 그건 S3 다 — 즉 **S4Policy 자체가
    #: 스위치**다.
    #:
    #: 기본 10분: 현실에서 도착 직전 차를 돌릴 수 없고, 돌린다 해도 운전자가
    #: 따를 수 없다. **운영 제약이지 구현 편의가 아니다.**
    relocate_lead_min: float = 10.0


@dataclass(frozen=True)
class S4Policy(S3Policy):
    """가고 있는 차도 다시 정하는 정책. `S3Policy` 에서 **설정 하나**가 다르다."""

    stage: str = "S4"

    #: ★ 이 한 줄이 시간축을 연다. 시뮬레이터가 이것만 보고 가고 있는 차를
    #: `decide()` 에 넣는다 — **어느 스테이지인지는 모른다** (`interfaces.Policy`).
    reask_en_route: bool = True

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """★ S3 와의 **단 하나의 차이** — 도착이 임박한 차를 원래 자리로 되돌린다.

        배정 자체는 `super().decide()` 가 한다. S3 의 그리디 + 이동·교환이 그대로
        돌고, **들어오는 차 목록만 넓어진다** (가고 있는 차가 섞인다).

        여기서 하는 일은 **되돌리기 하나**다 — 도착까지 `relocate_lead_min` 보다
        적게 남은 차가 옮겨졌으면 **원래 예약으로 돌린다.**

        ## 왜 사후에 되돌리나

        S3 의 이동·교환은 그룹 **합**을 보고 수를 둔다. 임박한 차를 후보에서
        미리 빼면 그 차가 **다른 차의 비용 계산에서도 사라진다** — 실제로는 그
        휴게소에 갈 차인데 없는 것처럼 세게 되고, 그러면 남은 차들이 "거기가
        한산하다" 고 잘못 본다.

        그래서 **계산에는 넣고, 옮기는 것만 막는다.**
        """

        lead = float(self.settings.relocate_lead_min)
        ledger = world.require_ledger()

        # ★ **먼저 찍어 둔다.** `super().decide()` 가 원장을 고쳐 쓰므로, 끝난 뒤에
        #   물으면 "원래 어디로 가고 있었나" 를 되찾을 수 없다
        before = {}
        if lead > 0.0:
            for ev in evs:
                res = ledger.reservation(ev.ev_id)
                if res is not None:
                    before[ev.ev_id] = (res.station_id, float(res.eta_min))

        answers = dict(super().decide(evs, world))
        if lead <= 0.0:
            return answers

        rf = world.range_factor
        kept = 0
        for ev in evs:
            was = before.get(ev.ev_id)
            if was is None:
                continue
            old_sid, old_eta = was
            now = answers.get(ev.ev_id)
            if now is None or now == old_sid:
                continue
            if old_eta - float(world.t_min) >= lead:
                continue                      # 아직 여유가 있다 — 옮겨도 된다

            # 임박했다. **답과 원장을 둘 다** 되돌린다. 답만 되돌리면 원장은 새
            # 휴게소를 가리킨 채로 남아 뒤 차들이 없는 예약을 본다
            st = world.stations.get(old_sid)
            if st is None:
                continue
            cand = self._candidate(ev, world, ledger, st, rf,
                                   opportunity=self._opportunity(ev))
            if cand is None:
                continue                      # 되돌릴 수 없다 — 그대로 둔다
            ledger.book(ev.ev_id, old_sid, cand[1])
            answers[ev.ev_id] = old_sid
            kept += 1

        self._bump("relocate_vetoed", float(kept))
        return answers
