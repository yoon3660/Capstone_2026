"""S4 의 목적함수 — `docs/objective.md` §2 의 식을 **코드로** 옮긴 것 (#143).

    min Σ_{i∈W} v·d_i(s | A_s(x))·x_{i,s}   ← (a) 체류
      + Σ_{i∈W_enroute} r_i·(1 − x_{i,σ₀(i)})  ← (b) 재배정 비용
      + Φ(x)                                  ← (c) terminal penalty (#144)

## 왜 이 파일이 따로 있나

세 가지를 한 자리에 모으기 위해서다.

1. **멘토링 2-c 의 답이 식 하나와 코드 하나로 맞아떨어진다.** 문서의 식과 구현이
   따로 놀면 *"solver 에게 무엇을 넘겼나"* 에 답할 수 없다
2. **휴리스틱과 MILP 가 같은 식을 본다.** 둘이 각자 비용을 계산하면 "어느 쪽이
   나은가" 에 *"식이 갈라진 몫"* 이 섞인다 (#139 가 `_candidate` 를, #143 이
   `_improve_group` 을 뽑아낸 것과 같은 이유)
3. **민감도 손잡이가 한 곳에 있다.** 시간가치·대기 가중치·재배정 비용은 전부
   *"정하지 않은 값"* 이다 (`docs/objective.md` §7). 흩어 두면 민감도를 돌릴 때
   무엇을 움직였는지 알 수 없다

## ⚠ 기본값은 **S3 와 비트 단위로 같아야** 한다

    time_value=1.0 · wait_weight=1.0 · reassign_base=0.0 · terminal_weight=0.0

이 값이면 목적함수가 `Σ d_i` 그대로다. 그래야 "S3 → S4" 차이가 **시간축을 연 몫**
이고, 목적함수를 바꾼 몫이 아니다. 테스트가 이것을 지킨다
(`tests/test_objective.py::test_the_default_objective_is_plain_dwell`).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Objective"]

#: 리드타임 분모의 하한. 도착 시각과 지금이 같을 때 0 으로 나누는 것을 막는다.
LEAD_EPS = 1e-6


@dataclass(frozen=True)
class Objective:
    """목적함수의 **값**들. 식 자체는 이 클래스의 메서드다.

    여기 있는 모든 값은 `docs/objective.md` §7 이 *"아직 정하지 않은 것"* 으로
    적어 둔 것이다. 기본값은 **S3 와 같아지는 값**이고, 민감도로 움직인다.
    """

    #: $v$ — 시간가치 (분당). 1.0 이면 "체류 1분 = 비용 1".
    #:
    #: 절대값은 순위를 안 바꾼다 (모든 항에 같이 곱해지지 않는 유일한 예외가
    #: `reassign_base` 다). **그래서 1.0 로 두고 `reassign_base` 를 그 단위로
    #: 읽는다** — 재배정 비용 5.0 은 "체류 5분만큼 싫다" 는 뜻이 된다.
    time_value: float = 1.0

    #: 대기 1분을 충전 1분의 몇 배로 세는가. §6.3 이 요구한 **1.0~1.5 민감도**.
    #:
    #: 1.0 이면 체류(대기+충전)를 그대로 쓴다 — S3 와 같다. 1.5 면 줄 서 있는
    #: 시간을 더 싫어하는 운전자다. **대기와 충전을 가를 수 있는 이유**는
    #: `StationLedger.evaluate_parts` 가 둘을 따로 돌려주기 때문이다.
    wait_weight: float = 1.0

    #: $r_{\\text{base}}$ — 가던 차를 다른 휴게소로 돌리는 **기본 비용** (분).
    #:
    #: 0.0 이면 재배정이 공짜다 (= #142 가 측정한 그 세계). 그 세계가 S3 보다
    #: 431h 나빴다 — 그래서 이 값이 0 이면 안 된다는 것이 **측정 결과**다
    #: (`docs/objective.md` §2b).
    reassign_base: float = 0.0

    #: 도착까지 이보다 적게 남은 차는 **못 옮긴다** (분) = $r_i = M$.
    #:
    #: ⚠ 이것을 **사후 거부로 구현하면 안 된다.** #142 에서 그렇게 했고,
    #: 재배정이 0회인데도 S3 보다 431h 나빴다 — 계획은 "이 차가 비켜 준다" 를
    #: 전제로 남을 옮겼는데 그 차만 되돌리니 **전제가 깨진 계획이 실행됐다.**
    #: 지금은 `S4Policy` 가 그 차를 **계획에서 빼기 전에 고정**한다.
    hard_lock_min: float = 10.0

    #: $\\Phi$ 의 가중치 — **#144 의 몫이다.** 0 이 아니면 그 자리에서 터진다.
    #:
    #: 자리만 두고 구현을 비워 두는 이유: #144 가 *"몰아넣기가 실제로 일어나는지
    #: 먼저 측정한다"* 고 적어 뒀다. 있지도 않은 문제를 고치면 안 된다 (#123).
    terminal_weight: float = 0.0

    #: $M$ — "못 옮긴다" 를 비용으로 쓸 때의 큰 수. 고정된 차는 애초에 결정변수에서
    #: 빠지므로 **보통 쓰이지 않는다.** MILP 가 고정을 비용으로 표현할 때만 쓴다.
    big_m: float = 1.0e9

    def __post_init__(self) -> None:
        if self.time_value <= 0:
            raise ValueError(f"시간가치는 0보다 커야 한다: {self.time_value}")
        if self.wait_weight < 1.0:
            # 대기가 충전보다 **싸다**고 보는 운전자는 이 모형에 없다. 1.0 미만을
            # 허용하면 "줄이 긴 곳이 더 좋다" 가 되고 그건 다른 모형이다
            raise ValueError(f"대기 가중치는 1.0 이상이어야 한다: {self.wait_weight}")
        if self.reassign_base < 0:
            raise ValueError(f"재배정 비용은 음수가 될 수 없다: {self.reassign_base}")
        if self.hard_lock_min < 0:
            raise ValueError(f"고정 리드타임은 음수가 될 수 없다: {self.hard_lock_min}")
        if self.terminal_weight != 0.0:
            raise NotImplementedError(
                "terminal penalty 는 #144 의 몫이다. 식에 자리만 있고 구현은 없다 — "
                "0 이 아닌 값을 주면 '넣었다고 믿는데 안 들어간' 상태가 된다")

    # -- (a) 체류 -------------------------------------------------------------
    @property
    def plain_dwell(self) -> bool:
        """체류 항이 **$\\sum d_i$ 그대로**인가. 참이면 S3 와 같은 산술이다."""

        return self.time_value == 1.0 and self.wait_weight == 1.0

    def dwell_cost(self, dwell_min: float, wait_min: float) -> float:
        """$v\\,d_i$ — 대기를 `wait_weight` 로 다시 센다.

        `dwell = wait + charge` 이므로 가중 체류는

            v · (charge + w·wait) = v · (dwell + (w−1)·wait)

        로 쓴다. `w = 1` 이면 **`dwell_min` 을 그대로 곱한 것과 비트 단위로 같다**
        — `wait_min` 을 아예 읽지 않는 형태로 적었다.
        """

        if self.wait_weight == 1.0:
            return self.time_value * dwell_min
        return self.time_value * (dwell_min + (self.wait_weight - 1.0) * wait_min)

    # -- (b) 재배정 비용 ------------------------------------------------------
    def is_locked(self, eta_min: float, t_min: float) -> bool:
        """도착이 임박해서 **못 옮기는** 차인가 ($r_i = M$).

        `hard_lock_min = 0` 이면 아무도 고정되지 않는다. 시간축을 끄는 것이
        아니라 **아무 때나 옮기는 것**이다 (끄려면 `reask_en_route=False`, 그건 S3).
        """

        return float(eta_min) - float(t_min) < float(self.hard_lock_min)

    def reassign_cost(self, eta_min: float, t_min: float) -> float:
        """$r_i$ — 이 차를 원래 예약에서 **떼어내는** 비용 (분).

            r_i = M                                       남은 시간 < hard_lock
                = r_base · (1 + hard_lock / (η − t))      그 밖

        임박할수록 비싸진다. 왜 상수가 아닌가: 도착 40분 전에 길을 바꾸는 것과
        12분 전에 바꾸는 것은 운전자에게 같은 일이 아니다. **연속적으로 비싸게
        만들면** solver 가 "조금 더 여유 있는 차를 대신 옮기는" 수를 둘 수 있다 —
        계단으로 두면 그 수가 안 보인다.

        고정된 차는 `S4Policy` 가 **결정변수에서 빼므로** 이 함수가 $M$ 을
        돌려주는 일은 드물다. 그래도 돌려주는 이유: MILP 가 고정을 비용으로
        표현하는 쪽을 고를 수 있고, 그때 두 길이 **같은 식**을 봐야 한다.
        """

        if self.is_locked(eta_min, t_min):
            return self.big_m
        if self.reassign_base == 0.0:
            return 0.0
        left = max(float(eta_min) - float(t_min), LEAD_EPS)
        return self.reassign_base * (1.0 + float(self.hard_lock_min) / left)

    # -- (c) terminal penalty -------------------------------------------------
    def terminal(self) -> float:
        """$\\Phi(x)$ — **#144.** 지금은 항상 0 이다 (`terminal_weight` 가 0 이라서).

        `__post_init__` 이 0 이 아닌 가중치를 막으므로 이 함수는 0 만 돌려준다.
        식에 자리를 남겨 두는 것은 #144 가 여기를 채우기 때문이다.
        """

        return 0.0
