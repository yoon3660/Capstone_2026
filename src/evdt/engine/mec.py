"""MEC — 결정 기준만 사회적 비용으로 바꾼다 (**기준 축**, #138).

S1 과 **딱 한 가지**가 다르다. 메서드 하나다 (`_mec`).

    S1    고를 때 **내 체류**를 본다                     사적 비용
    MEC   + **내가 남들의 종료 시각을 얼마나 미는지**     사회적 비용

정보도 결합도 S1 그대로다. 원장을 보고 한 대씩 순서대로 고른다. 예측을 쓰지
않는다. 하나라도 같이 바꾸면 **그 차이가 기준의 차이로 보고된다.**

## 왜 결합과 따로 하나 — 축이 세 개다

설계문서 §5.1: *"한계 외부비용 기반 배정은 **세 번째 종류의 변경**이다. 정보와
결합은 그대로 두고 **결정 기준**을 바꾼다."*

    S0 → S1 → S2   정보 축
    S2 → S3        결합 축 (윈도우 내 동시 결정)
    S1 → MEC       **기준 축** ← 이 칸

한꺼번에 바꾸면 무엇이 효과를 냈는지 모른다. #118·#119 에서 정보 축만 올려
*"21.0% 는 정보의 값"* 이라고 말할 수 있었던 것과 같은 이유다.

## 식 (설계문서 §6.1)

    사적 비용   PC  = 내 체류 (대기 + 충전)
    한계 외부비용 MEC = Σ(남들의 **종료 시각**, 나 포함) − Σ(종료 시각, 나 제외)
    사회적 비용 SC  = PC + MEC

**종료 시각**을 쓰는 이유: 내가 끼어들면 뒤차의 **충전기 배정**이 바뀌어 충전
출력, 즉 충전시간까지 달라진다. 대기만 보면 *"100kW 자리를 빼앗아 50kW 로
밀어낸"* 몫이 빠진다.

## ⚠ 이탈 판정은 **사적 비용**으로 한다

이탈은 *"나한테 너무 비싸다"* 이지 *"남에게 비용을 떠넘긴다"* 가 아니다. 사회적
비용으로 재면 **남의 몫 때문에 내가 여행을 포기**하는 셈이고, 그러면 이탈 수가
기준 변경 때문에 흔들려서 **"이탈을 맞춘 비교" 가 성립하지 않는다**
(`docs/compare_rule.md`). `S1Policy._one` 이 argmin 과 이탈 판정을 가른다.

## ⚠ 두 번째 정차의 외부비용은 세지 않는다

`_candidate` 는 목적지에 못 닿으면 **다음 정차의 체류**를 더한다
(`_next_stop_cost`). 그 정차는 **아직 고르지도 않았고 원장에 쓰이지도 않는다.**
거기에 MEC 를 매기면 *"있지도 않은 예약이 남을 밀었다"* 를 세는 셈이다 (#83 이
두 번째 정차를 원장에서 뺀 것과 같은 이유). 그래서 **첫 정차에만** 매긴다.

## ⚠ 비용 — 원장 질의가 비싸진다

`evaluate` 는 FIFO 라서 **내 앞**만 보면 끝난다. MEC 는 **내 뒤까지** 두 번
투영해야 한다 (나 포함 / 나 제외). S1 이 런 하나에 원장 질의를 약 50,000번
하므로 그 배수가 그대로 계산시간이다. **재서 적는다** (#77).

    KPI: mec_calls · mec_ms · mec_probed_min · mec_chosen_min

## 미리 정한 판정 (결과를 보기 전에 정했다)

> **S1 대비 S0→UE 격차를 3%p 이상** 더 메우지 못하면, **기준 변경만으로는
> 부족하다**고 기록하고 S3(결합)와 **같이** 쓸 때의 효과만 본다.

3%p 인 이유: S1→S2 가 정보 한 칸으로 **16.5%p** 를 움직였다. 기준 변경이 그 1/5
도 못 움직이면 단독 칸으로 세울 값어치가 없다. **접는다는 뜻은 아니다** —
S3/S4 의 MILP 가 시간 초과했을 때의 fallback 으로는 여전히 필요하다 (§5.1).

## 하지 않는 것

- **예측을 보지 않는다** — S1 그대로다. `world.forecast` 를 건드리면 터진다
- **같이 결정하지 않는다** — 여전히 ev_id 순으로 한 대씩이다
- **충전량을 정하지 않는다** — S0·S1 과 **같은 함수**를 쓴다 (설계문서 §2.3)
- **시간가치를 곱하지 않는다** — §6.1 이 `× 시간가치` 를 적었지만 PC·MEC 에 **같은
  계수**가 곱해지므로 argmin 이 안 바뀐다. 분 단위로 둬서 KPI 를 읽을 수 있게 한다
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from evdt.engine.ledger import Arrival
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.interfaces import WorldView


@dataclass(frozen=True)
class MecSettings(S1Settings):
    """MEC 가 쓰는 값. **S1 과 같아야 한다** — 기준 축만 바꾸는 칸이다.

    `mec_weight` 하나만 더 있다. 1.0 이 사회적 비용이고 0.0 이면 S1 과 **비트
    단위로 같다** — 배선이 살아 있는지 보는 스위치다.
    """

    #: MEC 를 얼마나 세나. 1.0 = 설계문서 §6.1 의 사회적 비용. 0.0 = S1.
    #:
    #: 민감도 손잡이로도 쓴다 — 외부비용을 **부분만** 내재화하는 것이
    #: (피구세의 일부) 전부 내재화하는 것보다 나을 수 있다. 전부 내재화하면
    #: 모두가 한산한 곳으로 밀려가 **그쪽이 붐빈다.**
    mec_weight: float = 1.0

    def __post_init__(self) -> None:
        if self.mec_weight < 0.0:
            raise ValueError(f"MEC 가중치는 음수가 될 수 없다: {self.mec_weight}")


@dataclass(frozen=True)
class MecPolicy(S1Policy):
    """사회적 비용으로 고르는 정책. `S1Policy` 에서 `_mec` 하나만 다르다."""

    stage: str = "MEC"

    #: 질의 수·시간·외부비용 합. **KPI 로 나간다** (#77 · #138).
    #:
    #: `mec_chosen_min` 이 0 이면 *"기준을 바꿨는데 아무것도 안 바뀐다"* 의 이유가
    #: 그 자리에서 설명된다 — 고른 자리의 외부비용이 실제로 0 이었다는 뜻이다.
    counters: dict[str, float] = field(default_factory=dict)

    def _mec(self, ledger, station_id: str, arr: Arrival, world: WorldView) -> float:
        """★ S1 과의 **단 하나의 차이** — 내가 남들을 얼마나 미는가.

        원장에게 묻는다. **정책이 직접 큐를 쌓지 않는다** — 호출자가 늘면 규칙이
        갈라지고, 그러면 정책이 재는 외부비용과 실제 점유가 어긋난다
        (`tests/test_queue_rule.py::test_queue_rule_has_at_most_two_callers`).

        `future=` 를 주지 않는다. **예측은 S2 의 것**이고, 이 칸은 S1 의 정보를
        쓴다. 예측을 섞으면 "기준을 바꾼 몫" 에 *"정보가 늘어난 몫"* 이 들어간다.
        """

        weight = float(self.settings.mec_weight)
        if weight == 0.0:
            return 0.0          # S1 과 비트 단위로 같아야 한다 — 질의도 하지 않는다

        t0 = time.perf_counter()
        mec = ledger.externality(station_id, arr)
        self.counters["mec_ms"] = self.counters.get("mec_ms", 0.0) + (
            time.perf_counter() - t0) * 1000.0
        self.counters["mec_calls"] = self.counters.get("mec_calls", 0.0) + 1.0
        # 후보마다 재는 값. **내재화된 양이 아니다** — 아래 `_chose` 가 그걸 센다
        self.counters["mec_probed_min"] = (
            self.counters.get("mec_probed_min", 0.0) + mec)
        if mec > 0.0:
            self.counters["mec_nonzero"] = self.counters.get("mec_nonzero", 0.0) + 1.0
        return weight * mec

    def _chose(self, extra: float) -> None:
        """**고른 자리의** 외부비용을 센다 — #138 이 보고할 양이다.

        후보마다 재는 `mec_probed_min` 과 다르다. 차 한 대가 코리도의 여러 곳을
        재 보고 한 곳을 고르므로, *"실제로 내재화된 외부비용"* 은 고른 자리의
        것만 세야 한다. 둘을 섞으면 수십 배 과대보고가 된다.
        """

        self.counters["mec_chosen_min"] = (
            self.counters.get("mec_chosen_min", 0.0) + extra)
        if extra > 0.0:
            self.counters["mec_chosen_nonzero"] = (
                self.counters.get("mec_chosen_nonzero", 0.0) + 1.0)
