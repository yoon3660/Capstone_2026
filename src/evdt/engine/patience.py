"""인내 — **차 한 대가 얼마나 기다릴 수 있나** (#146).

콜센터 문헌에서 가져온다. 충전소는 **다서버 큐 + 이탈**이고, 그건 Erlang-A
(M/M/n+M) 가 다루는 바로 그 구조다.

    Garnett, Mandelbaum & Reiman (2002)  Designing a call center with
        impatient customers. M&SOM 4(3), 208–227.
        → 다서버 + 이탈의 표준 모형. 인내를 **지수분포**로 가정한다
    Brown, Gans, Mandelbaum, Sakov, Shen, Zeltyn & Zhao (2005, JASA)
        Statistical analysis of a telephone call center.
        → 실측 인내 분포. **위험률이 일정하지 않다** — 지수 가정이 잘 안 맞는다

## 임계값이 아니라 **위험률**이다

핵심 전환은 이것이다. "120분이면 나간다" 는 임계값이고, 문헌이 쓰는 것은

    h(t) = f(t) / (1 − F(t))     "t 분을 기다린 지금, 다음 1분에 포기할 확률"

| 분포 | h(t) | 행동의 뜻 |
|---|---|---|
| 지수 (와이블 k=1) | 일정 | **무기억** — 얼마나 기다렸든 상관없다. Erlang-A 의 교과서 가정 |
| 와이블 k > 1 | 증가 | 기다릴수록 더 잘 터진다. 차례 시간이 다가오는 귀경길 |
| 와이블 k < 1 | 감소 | 이미 기다렸으니 못 나간다 — **매몰비용** (Arkes & Blumer 1985) |
| 로그정규 | 증가→감소 | 지금 쓰는 것. 지속시간 자료의 표준 선택 |
| 균등 | 증가 (상한에서 ∞) | 무정보. **상한이 있는 것이 행동으로는 어색하다** |

어느 쪽이 맞는지는 **모른다** (실측 충전 세션이 아직 없다). 그래서 **추정하지 않고
스윕한다** — *"인내의 모양이 결론을 바꾸는가"* 가 답이고, 그게 추정보다 방어하기
쉽다 (이탈비용 손익분기 759분과 같은 논법).

## 전부 **중앙값**으로 맞춘다

가족을 바꿀 때 수준까지 같이 움직이면 모양의 효과와 수준의 효과가 섞인다. 그래서
모든 가족이 `median_min` 을 중앙값으로 갖도록 모수화한다 — 이 프로젝트의
"한 번에 한 축" 규율 그대로다.

⚠ 지금 쓰는 `escape_cost_sigma` 의 로그정규는 `exp(N(log m, σ))` 라 **m 이
  중앙값**이고 평균은 `m·exp(σ²/2)` 다. σ=0.8 이면 평균이 165분이다 — 문서에
  "120분" 이라고 적힌 값은 중앙값으로 읽어야 한다.

## ⚠ 이 값은 **두 번 쓰인다**

`escape_cost_min` 은 (a) 얼마나 참나 **임계값**이자 (b) 나갔을 때 치르는 **비용**이다.
우연이 아니라 한 해석에서 같은 양이다 — *"IC 로 빠져 시내에서 충전하고 돌아오면
120분 걸린다. 휴게소가 그보다 비싸면 나간다."* 그래서 분포로 바꾸면 **비용도 같이
분포가 된다.** 총 사회적 비용을 `이탈수 × 120` 으로 계산하면 안 되고, 차마다 제
비용을 더해야 한다 (`escape_cost_total_h` KPI).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["Patience", "PATIENCE_KINDS", "POINT"]

#: 쓸 수 있는 가족. 모르는 이름은 로딩 시점에 거부한다 (`config.py`)
PATIENCE_KINDS: tuple[str, ...] = (
    "point",        # 전원이 같은 값 (#78 이전 동작)
    "lognormal",    # 지금 쓰는 것 (#78)
    "weibull",      # 권장 — shape 가 위험률의 모양을 직접 적는다
    "exponential",  # 와이블 k=1. Erlang-A 의 교과서 가정
    "uniform",      # 무정보 대조군
)

LN2 = math.log(2.0)


@dataclass(frozen=True)
class Patience:
    """인내 분포. **중앙값으로 모수화한다** — 가족을 바꿔도 수준이 안 움직인다."""

    #: 중앙값(분). `demand.escape_cost_min` 이 그대로 들어온다
    median_min: float = 0.0

    kind: str = "point"

    #: 가족마다 뜻이 다르다
    #:   lognormal    σ (밑 정규분포의 표준편차). 0 이면 점질량
    #:   weibull      k (모양). k<1 감소 위험률 · k=1 지수 · k>1 증가 위험률
    #:   exponential  쓰지 않는다 (k=1 고정)
    #:   uniform      반폭 비율 h. U(m(1−h), m(1+h)). 0<h≤1
    shape: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in PATIENCE_KINDS:
            raise ValueError(
                f"모르는 인내 분포다: {self.kind!r} (쓸 수 있는 것: {list(PATIENCE_KINDS)})")
        if self.median_min < 0:
            raise ValueError(f"인내 중앙값은 음수가 될 수 없다: {self.median_min}")
        if self.shape < 0:
            raise ValueError(f"shape 는 음수가 될 수 없다: {self.shape}")
        if self.kind == "weibull" and self.spread and self.shape == 0.0:
            raise ValueError("와이블은 shape(k)가 0 보다 커야 한다")
        if self.kind == "uniform" and self.spread and not 0.0 < self.shape <= 1.0:
            raise ValueError(f"균등분포의 반폭 비율은 0<h≤1 이어야 한다: {self.shape}")

    @property
    def spread(self) -> bool:
        """흩어뜨리나. 거짓이면 전원이 중앙값을 쓴다 (= 예전 동작과 비트 단위로 같다)."""

        if self.median_min <= 0.0 or self.kind == "point":
            return False
        if self.kind == "exponential":
            return True          # shape 를 안 쓴다
        return self.shape > 0.0

    def draw(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """차 n 대의 인내(분). 흩어뜨리지 않으면 전원이 중앙값이다.

        **뽑는 순서가 고정이어야** 같은 시드에 같은 세계가 나온다 — 호출자가
        한 번만, 전 차량을 한 배열로 뽑는다.
        """

        m = float(self.median_min)

        if not self.spread:
            return np.full(n, m, dtype=float)

        if self.kind == "lognormal":
            # exp(N(log m, σ)) — 중앙값 m, 평균 m·exp(σ²/2)
            return rng.lognormal(math.log(m), self.shape, size=n)

        if self.kind in ("weibull", "exponential"):
            k = 1.0 if self.kind == "exponential" else float(self.shape)
            # 중앙값이 m 이 되도록 척도를 맞춘다: λ·(ln2)^(1/k) = m
            lam = m / (LN2 ** (1.0 / k))
            return lam * rng.weibull(k, size=n)

        if self.kind == "uniform":
            h = float(self.shape)
            return rng.uniform(m * (1.0 - h), m * (1.0 + h), size=n)

        raise AssertionError(f"처리되지 않은 가족: {self.kind}")   # pragma: no cover

    def hazard_shape(self) -> str:
        """위험률이 어떤 모양인가 — 행동의 뜻. 로그와 그림 부제에 쓴다."""

        if not self.spread:
            return "점질량 (전원 같은 값)"
        if self.kind == "exponential":
            return "일정 위험률 — 무기억 (Erlang-A 교과서 가정)"
        if self.kind == "weibull":
            if self.shape < 1.0:
                return f"감소 위험률 (k={self.shape:g}) — 기다릴수록 못 나간다 (매몰비용)"
            if self.shape > 1.0:
                return f"증가 위험률 (k={self.shape:g}) — 기다릴수록 잘 터진다"
            return "일정 위험률 (k=1, 지수와 같다)"
        if self.kind == "lognormal":
            return f"증가→감소 위험률 (σ={self.shape:g})"
        return f"상한 있음 (±{self.shape:.0%})"

    def describe(self) -> str:
        """run params · 로그 한 줄. **기록에 박혀야** 나중에 어느 인내였는지 안다."""

        if not self.spread:
            return f"인내 {self.median_min:g}분 (전원 같음)"
        return (f"인내 중앙값 {self.median_min:g}분 · {self.kind}"
                f"{'' if self.kind == 'exponential' else f'({self.shape:g})'} "
                f"· {self.hazard_shape()}")

    def mean_min(self) -> float:
        """이론 평균. **중앙값과 다르다** — 총 사회적 비용을 어림할 때 필요하다."""

        m = float(self.median_min)
        if not self.spread:
            return m
        if self.kind == "lognormal":
            return m * math.exp(self.shape ** 2 / 2.0)
        if self.kind in ("weibull", "exponential"):
            k = 1.0 if self.kind == "exponential" else float(self.shape)
            return m / (LN2 ** (1.0 / k)) * math.gamma(1.0 + 1.0 / k)
        if self.kind == "uniform":
            return m
        raise AssertionError(f"처리되지 않은 가족: {self.kind}")   # pragma: no cover


#: 흩어뜨리지 않는 기본값.
POINT = Patience()
