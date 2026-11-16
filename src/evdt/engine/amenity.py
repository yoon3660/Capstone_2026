"""휴게소 편의시설 선호 — **쏠림을 만드는 기제** (#146).

## 왜 필요한가

줄이 없으면 체류 = 충전시간이고, 충전시간은 SoC·차종으로 정해져 **휴게소와
무관**하다. 한 대의 비용이 세 휴게소에서 소수 9자리까지 같았다 (#143 §3.8). 즉
혼잡이 없을 때 지금 모형의 휴게소 선택은 **사실상 동점 깨기**다.

그래서 차가 인위적으로 흩어진다. 추석 ev10% 에서 유효 휴게소수 9.4/16, 가장 붐비는
휴게소조차 하루 가동률 37% 다. **쏠림이 생길 수가 없는 구조**이고, 그 상태에서
"추석엔 혼잡이 없다" 고 결론 내리면 **모형 누락을 실측으로 보고하는 것**이 된다.

`ladder.md` §7 이 *"'병목' 은 공급 부족으로 들림. 우리 주장은 **분배 문제**"* 라고
못 박아 뒀다. 분배 문제를 만드는 기제가 모형에 있어야 한다.

## 식 — 항이 **둘**이다

    U_j = −d_j − (θ_j − 1)·W_j + β·a_j
          ────   ─────────────   ──────
          체류    체감 대기        편의시설 끌림

    a_j ∈ [0,1]  편의시설 지수
    θ_j = exp(−γ·a_j) ≤ 1

| 항 | 역할 | 혼잡이 0 일 때 |
|---|---|---|
| **β (끌림)** | 쏠림을 **만든다** | **작동한다** |
| **γ → θ (참을성)** | 쏠림을 **유지한다** | 작동하지 않는다 (W≈0) |

θ 만으로는 질문에 답할 수 없다. (θ−1)·W 는 W≈0 이면 0 이다. 실제 운전자가 큰
휴게소로 가는 것은 줄이 짧아서가 아니라 **거기 가고 싶어서**이고, 그건 체감 대기가
아니라 **대안 고유 상수**다 — McFadden (1974) 의 그 항이다.

## β 의 단위는 **분**이다

"이 휴게소에 가려고 체류를 β분 더 감수한다". 체류와 같은 단위라 해석이 바로 되고,
`Objective.time_value` 를 1.0 로 두는 규약(`objective.py`)과도 맞는다.

## ⚠ 어디에 넣고 어디에 넣지 않는가

    ① 물리 (원장)        ❌ **절대 안 된다.** 큰 휴게소의 대기를 0.7배로 **기록**하면
                            거기로 몰아넣을수록 "대기가 줄었다" 고 보고된다 —
                            이름만 바꿔서 이기는 것이다. 모든 KPI 가 오염된다
    ② 사적비용 private   ❌ (기본). 이탈 기준 120분 자체가 "얼마나 참나" 를 담은
                            행동 파라미터다. θ 를 곱하면 같은 심리를 두 번 센다.
                            그리고 `compare_rule.md` 의 이탈 매칭이 private 위에
                            서 있다 — 건드리면 사다리 비교가 깨진다
    ③ 결정비용 key       ✅ **여기.** `_extra_cost`(r_i) · `_mec` 와 같은 자리다

β·a_j 는 **이득**이므로 비용에서는 부호가 음수다 (`adjust` 가 음수를 돌려준다).

## 기본값은 꺼짐

`pull = 0.0` · `patience = 0.0` 이면 `adjust` 가 **정확히 0.0** 을 돌려주고, 그러면
사다리가 지금과 **비트 단위로 같다**. MEC 의 `mec_weight=0` 규약과 같다.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["AmenityPrefs", "NO_AMENITY"]


@dataclass(frozen=True)
class AmenityPrefs:
    """편의시설 선호의 **값**들. 식은 이 클래스의 메서드다."""

    #: β — 편의시설 지수 1.0 인 휴게소에 가려고 감수하는 **추가 체류(분)**.
    #: 0.0 이면 꺼짐. 혼잡이 없어도 작동하는 **유일한** 항이다
    pull: float = 0.0

    #: γ — 체감 대기 계수. θ_j = exp(−γ·a_j). 0.0 이면 θ≡1 (꺼짐).
    #:
    #: ⚠ 켜면 후보마다 **대기를 따로 묻는 질의가 하나 늘어난다** (체류만으로는
    #:   대기를 분리할 수 없다). γ=0 이면 그 질의를 아예 하지 않는다
    patience: float = 0.0

    #: station_id → a_j ∈ [0,1]. 없는 휴게소는 0.0 (= 효과 없음) 으로 본다.
    #: 조용히 0 으로 두지 않으려면 `require` 로 커버리지를 검사할 것
    index: Mapping[str, float] = field(default_factory=dict)

    #: 지수가 어디서 왔나. **run params 에 박힌다** — 대리변수로 돌린 결과를
    #: 나중에 실측으로 돌린 것과 구분할 수 없으면 안 된다
    source: str = "none"

    def __post_init__(self) -> None:
        if self.pull < 0.0:
            raise ValueError(f"편의시설 끌림은 음수가 될 수 없다: {self.pull}")
        if self.patience < 0.0:
            # 음수면 "편의시설이 좋을수록 대기가 더 괴롭다" 가 된다. 그건 다른 모형이다
            raise ValueError(f"체감 대기 계수는 음수가 될 수 없다: {self.patience}")
        bad = {k: v for k, v in self.index.items() if not 0.0 <= float(v) <= 1.0}
        if bad:
            raise ValueError(f"편의시설 지수는 0~1 이어야 한다: {dict(list(bad.items())[:3])}")
        if self.enabled and not self.index:
            raise ValueError(
                "편의시설 선호를 켰는데 지수가 비어 있다 — 전원이 a_j=0 이라 "
                "효과가 없다. 껐다고 생각하지 않도록 여기서 멈춘다")

    @property
    def enabled(self) -> bool:
        """꺼져 있으면 `adjust` 가 0.0 이고 사다리가 지금과 비트 단위로 같다."""

        return self.pull > 0.0 or self.patience > 0.0

    @property
    def needs_wait(self) -> bool:
        """대기를 따로 물어야 하나. γ 가 0 이면 묻지 않는다 (질의 하나를 아낀다)."""

        return self.patience > 0.0

    def amenity(self, station_id: str) -> float:
        return float(self.index.get(station_id, 0.0))

    def theta(self, station_id: str) -> float:
        """θ_j = exp(−γ·a_j) ∈ (0, 1]. 1.0 이면 체감 = 실제."""

        if self.patience == 0.0:
            return 1.0
        return math.exp(-self.patience * self.amenity(station_id))

    def adjust(self, station_id: str, wait_min: float = 0.0) -> float:
        """**결정비용에 더할 값.** 좋은 휴게소면 음수(= 더 끌린다).

            (θ_j − 1)·W_j  −  β·a_j

        꺼져 있으면 정확히 0.0 을 돌려준다 — 부동소수 연산을 아예 거치지 않는다.
        """

        if not self.enabled:
            return 0.0

        a = self.amenity(station_id)
        out = -self.pull * a

        if self.patience > 0.0:
            out += (self.theta(station_id) - 1.0) * max(float(wait_min), 0.0)

        return out

    def require(self, station_ids) -> None:
        """지수가 없는 휴게소가 있으면 멈춘다. 조용한 0 은 '껐다' 와 구분이 안 된다."""

        if not self.enabled:
            return
        missing = sorted(s for s in station_ids if s not in self.index)
        if missing:
            raise ValueError(
                f"편의시설 지수가 없는 휴게소 {len(missing)}곳: {missing[:5]} — "
                "지수를 채우거나 선호를 끌 것")


#: 꺼진 기본값. S1~S4 가 이것을 쓴다.
NO_AMENITY = AmenityPrefs()
