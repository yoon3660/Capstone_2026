"""S4 — 시간축을 연다: 이미 한 배정도 **값을 치르고** 다시 바꾼다 (#142 · #143).

S3 와 두 가지가 다르다.

    ① 세계에게 "가고 있는 차에게도 다시 물어 달라" 고 말한다 (`reask_en_route`)
    ② 목적함수에 **재배정 비용 $r_i$** 가 있다 (`docs/objective.md` §2b)

정보는 S2 그대로, 공간 결합은 S3 그대로다. 바뀌는 것은 **"언제까지 바꿀 수 있나"**
와 **"바꾸는 데 얼마가 드나"** 둘이다.

## ★ ② 가 #142 의 측정에서 나왔다 — 사후 거부는 **틀린 구현이었다**

처음에는 운영 제약("도착 직전 차는 못 돌린다")을 `relocate_lead_min` 으로
**사후 거부**로 구현했다. 계획을 세운 **뒤에** 임박한 차만 원래 자리로 되돌렸다.
리드타임을 쓸어 보니 이렇게 나왔다.

| 리드 | 총 사회적 비용 | S3 대비 | 재배정 | 거부 |
|---|---:|---:|---:|---:|
| **S3** | 6,402.3h | — | — | — |
| 10분 | 6,620.5h | +218.3h | 15,960 | 2,593 |
| 60분 | 7,216.7h | +814.4h | 4,552 | 17,015 |
| **100,000분** | 6,833.6h | **+431.4h** | **0** | 27,160 |

**재배정이 0회인데 431h 나쁘다.** 그러면 그 손해는 재배정이 만든 것이 아니다.
계획은 *"이 차가 비켜 준다"* 를 전제로 **다른 차들**을 옮겼는데, 그 차만 되돌리니
**전제가 깨진 계획이 그대로 실행됐다.**

그래서 지금은 제약을 **계획 안으로** 넣는다.

    도착까지 남은 시간 < hard_lock_min   →  그 차를 **계획에서 고정한다**
                                            (결정변수에서 빼고, 원장에는 남긴다)
    그 밖                                →  $r_i$ 를 **목적함수에 넣는다**

고정된 차는 계획을 세울 때부터 안 움직인다. 그래서 **사후에 뒤집히는 계획이 안
나온다.** 그리고 고정되지 않은 차는 "옮기면 이만큼 값을 치른다" 를 solver 가 알고
수를 두므로, 옮기지 않기로 하면 **그 결정을 전제로 나머지를 푼다.**

> **검산 하나가 생긴다.** `hard_lock_min = inf` 면 가던 차가 전부 고정되고,
> 그러면 **S3 와 비트 단위로 같은 답**이 나와야 한다. 사후 거부 구현은 이 검산을
> 통과하지 못했다 (431h 차이). 테스트가 이것을 지킨다
> (`tests/test_s4.py::test_locking_everything_reproduces_s3`).

## 왜 계산에는 남기고 옮기는 것만 막나

고정된 차를 **원장에서도** 빼면 그 차가 다른 차의 비용 계산에서 사라진다 —
실제로는 그 휴게소에 갈 차인데 없는 것처럼 세게 되고, 그러면 남은 차들이
*"거기가 한산하다"* 고 잘못 본다. 그래서 **예약은 그대로 두고 결정변수에서만
뺀다.**

## 이미 꽂은 차는 어차피 후보가 아니다

`ReservationLedger.release()` 는 `PLANNED` 만 뺄 수 있지만 그 가드는 무장돼 있지
않다 (시뮬레이터가 `mark()` 를 안 부른다). 그래서 **시간으로 막는다** — 시뮬레이터는
`left_at_min <= t < eta_min` 인 차만 다시 묻고, 정책은 **예약의 ETA 가 미래인
차만** 가던 차로 센다. 한 번 서고 다시 묻는 차는 ETA 가 과거라서 섞이지 않는다
(그 차의 묵은 예약을 "가던 길" 로 오해하면 **뒤에 있는 휴게소로 되돌리게 된다**).

## 하지 않는 것

- **terminal penalty 를 넣지 않는다** — #144 의 몫. `Objective` 에 자리만 있다
- **중간에 포기시키지 않는다** — 가던 차에게서 무충전·이탈 답을 **받지 않는다**
  (`_one` 이 막는다). 시뮬레이터가 그 답을 무시하는데 정책이 원장에서는 예약을
  빼 버려서, **예약 없는 차가 도착하는** 상태가 생긴다
- **되돌아가지 않는다** — 지나친 휴게소는 `_candidate` 가 이미 거른다
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from evdt.engine.ledger import Arrival
from evdt.engine.objective import Objective
from evdt.engine.s3 import S3Policy, S3Settings
from evdt.interfaces import EVState, WorldView


@dataclass(frozen=True)
class S4Settings(S3Settings):
    """S4 가 쓰는 값. **목적함수의 값들 말고는 S3 와 같아야 한다**."""

    #: 도착까지 이보다 적게 남은 차는 **계획에서 고정한다** (분) — $r_i = M$.
    #:
    #: `0` 이면 시간축을 끄는 것이 아니라 **아무 때나 옮긴다**. 끄려면
    #: `reask_en_route` 가 False 여야 하는데 그건 S3 다 — 즉 **S4Policy 자체가
    #: 스위치**다. `inf` 면 가던 차가 전부 고정돼 S3 와 같아진다 (검산).
    #:
    #: 기본 10분: 현실에서 도착 직전 차를 돌릴 수 없고, 돌린다 해도 운전자가
    #: 따를 수 없다. **운영 제약이지 구현 편의가 아니다.**
    hard_lock_min: float = 10.0

    #: $r_{\\text{base}}$ — 가던 차를 돌리는 기본 비용 (분). 0 이면 **공짜**다.
    #:
    #: 0 이 기본인 이유는 하나다 — **그 값이 S3 와 같은 목적함수**이고, 그래서
    #: "시간축을 연 몫" 과 "비용을 매긴 몫" 을 갈라서 잴 수 있다. 민감도로 올린다.
    reassign_base: float = 0.0

    #: 대기 1분을 충전 1분의 몇 배로 세나 (§6.3 의 1.0~1.5 민감도).
    wait_weight: float = 1.0

    #: $v$ — 시간가치 (분당). `reassign_base` 를 체류 분 단위로 읽기 위해 1.0.
    time_value: float = 1.0

    #: $\\Phi$ 의 가중치 — **#144 의 몫이다.** 0 이 아니면 그 자리에서 터진다.
    #:
    #: 손잡이를 **미리 내 두는** 이유: #144 가 이 자리를 채울 때 러너·KPI·
    #: `params_json` 통로를 다시 깔지 않아도 된다. 그리고 지금 0 이 아닌 값을
    #: 주면 `Objective` 가 막으므로 *"넣었다고 믿는데 안 들어간"* 상태가 없다.
    terminal_weight: float = 0.0

    @property
    def objective(self) -> Objective:
        """`docs/objective.md` §2 의 식. **값은 전부 여기서 나온다.**"""

        return Objective(
            time_value=self.time_value, wait_weight=self.wait_weight,
            reassign_base=self.reassign_base, hard_lock_min=self.hard_lock_min,
            terminal_weight=self.terminal_weight)

    def __post_init__(self) -> None:
        super().__post_init__()
        # 값이 말이 되나 — `Objective` 가 검사한다. **여기서 한 번 만들어 봐야**
        # 잘못된 설정이 `decide()` 한가운데가 아니라 **만들 때** 터진다
        assert self.objective is not None


@dataclass(frozen=True)
class S4Policy(S3Policy):
    """가고 있는 차도 다시 정하는 정책. 목적함수에 $r_i$ 가 있다."""

    stage: str = "S4"

    #: ★ 이 한 줄이 시간축을 연다. 시뮬레이터가 이것만 보고 가고 있는 차를
    #: `decide()` 에 넣는다 — **어느 스테이지인지는 모른다** (`interfaces.Policy`).
    reask_en_route: bool = True

    #: `decide()` 한 번 동안의 scratch. ev_id → (원래 휴게소 σ₀, 원래 도착).
    #:
    #: 원래 `Arrival` 까지 들고 있는 이유: 가던 차가 이탈 답을 내면 `_one` 이
    #: 원장에서 예약을 빼 버린다. **다시 재서** 복원하면 그때의 세계가 달라 값이
    #: 미세하게 달라질 수 있으니, 빼기 전의 것을 그대로 되넣는다.
    prior: dict[str, tuple[str, Arrival]] = field(default_factory=dict)

    #: ev_id → $r_i$. `decide()` 머리에서 한 번 계산한다 — `_extra_cost` 는 세계를
    #: 못 보기 때문이고(채점기가 안 넘겨 준다), 어차피 차마다 한 값이다.
    rcost: dict[str, float] = field(default_factory=dict)

    # -- 목적함수의 두 자리를 채운다 ------------------------------------------
    def _extra_cost(self, ev_id: str, station_id: str) -> float:
        """$r_i\\,(1 - x_{i,\\sigma_0(i)})$ — **원래 예약에서 떼어낼 때만** 든다."""

        prior = self.prior.get(ev_id)
        if prior is None or prior[0] == station_id:
            return 0.0
        return self.rcost.get(ev_id, 0.0)

    def _targets(self, group: Sequence[str], answers: Mapping[str, str]) -> list[str]:
        """S3 의 후보에 **원래 예약 σ₀** 를 더한다.

        안 더하면 그리디가 옮겨 놓은 차를 제자리로 되돌릴 후보가 아예 없다. 그러면
        $r_i$ 를 아무리 크게 줘도 목적함수가 그 수를 못 두고, `r_i = M` 검산이
        *"고정이 안 먹는다"* 가 아니라 *"후보가 없다"* 때문에 깨진다.
        """

        seen = {answers[i] for i in group}
        seen.update(self.prior[i][0] for i in group if i in self.prior)
        return sorted(seen)

    def _dwell(self, ledger, station_id: str, arr: Arrival, world: WorldView) -> float:
        """$v\\,(\\text{충전} + w\\,\\text{대기})$ — 대기를 `wait_weight` 로 다시 센다.

        `wait_weight == 1.0` 이고 `time_value == 1.0` 이면 **S2 의 것을 그대로
        부른다.** 기본값에서 S3 와 비트 단위로 같아야 하므로 분기로 적었다.

        그 밖에서는 예측 질의를 S2 와 **같은 인자로** 다시 쓴다 — 체류와 대기를
        갈라 받아야 하고 (`dwell_parts_if_i_go`), S2 는 체류 하나만 돌려준다.
        """

        objective = self.settings.objective
        if objective.plain_dwell:
            return super()._dwell(ledger, station_id, arr, world)

        forecast = world.require_forecast()
        dwell, wait = ledger.dwell_parts_if_i_go(
            station_id, arr, future=forecast.arrivals_between(
                station_id, after_min=world.t_min, before_min=arr.arrival_min))
        return objective.dwell_cost(dwell, wait)

    # -- 가던 차에게서 이탈을 받지 않는다 --------------------------------------
    def _one(self, ev: EVState, world: WorldView) -> str:
        """가던 차가 무충전·이탈을 답하면 **원래 예약으로 되돌린다.**

        ## 왜 막아야 하나 — 예약 없는 차가 도착한다

        시뮬레이터는 가던 차의 무충전·이탈 답을 **무시한다** (의도된 것이다 —
        "중간 포기" 는 시간축 결합이 아니라 다른 종류의 변경이다). 그런데
        `S1Policy._one` 은 이탈을 고를 때 **원장에서 예약을 뺀다.** 둘이 겹치면

            정책: 예약을 뺐다        세계: 차는 원래 휴게소로 계속 간다
            ⇒ 뒤 차들은 그 자리가 비었다고 보고, 실제로는 그 차가 거기 선다

        여기서 막는 것이 `decide()` 끝에서 고치는 것보다 나은 이유: 되돌린 답이
        **그룹 최적화에 들어간다.** 끝에서 고치면 그 차가 윈도우에서 빠진 채로
        다른 차들이 정해지고, 그 계획은 또 틀린 전제 위에 선다 (#142 와 같은 모양).
        """

        answer = super()._one(ev, world)
        prior = self.prior.get(ev.ev_id)
        if prior is None or answer in world.stations:
            return answer

        station_id, arr = prior
        world.require_ledger().book(ev.ev_id, station_id, arr)
        self._bump("enroute_opt_out_blocked", 1.0)
        return station_id

    # -- 고정 → 그 다음은 전부 S3 다 -------------------------------------------
    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """★ S3 와의 차이 — **가던 차를 고정하거나 값을 매긴 뒤**, S3 를 그대로 돌린다.

        배정 자체는 `super().decide()` 가 한다. S3 의 그리디 + 이동·교환(또는 전수
        열거·MILP)이 그대로 돌고, **들어오는 차 목록만 넓어진다.**

        ## 세 갈래

            새로 묻는 차          그냥 S3 의 입력이다
            가던 차 · 고정        결정변수에서 뺀다. 답은 σ₀, 원장은 **그대로**
            가던 차 · 안 고정     S3 의 입력에 넣고, $r_i$ 를 목적함수에 더한다

        ## "가던 차" 를 어떻게 아나

        **예약의 ETA 가 미래인 차**다. 한 번 서고 다시 묻는 차에게도 묵은 예약이
        남아 있지만(시뮬레이터가 `mark()` 를 안 부른다) 그 ETA 는 과거다. 그걸
        "가던 길" 로 오해하면 **이미 지나친 휴게소로 되돌리는 답**이 나온다.
        """

        objective = self.settings.objective
        ledger = world.require_ledger()
        t = float(world.t_min)

        self.prior.clear()
        self.rcost.clear()

        pinned: dict[str, str] = {}
        free: list[EVState] = []

        for ev in sorted(evs, key=lambda e: e.ev_id):
            res = ledger.reservation(ev.ev_id)
            if res is None or float(res.eta_min) <= t:
                free.append(ev)                      # 새로 묻는 차
                continue

            if objective.is_locked(res.eta_min, t):
                # ★ 고정. 원장은 **안 건드린다** — 그래야 남들이 이 차를 본다
                pinned[ev.ev_id] = res.station_id
                continue

            arr = next((a for a in ledger.stations[res.station_id].arrivals
                        if a.ev_id == ev.ev_id), None)
            if arr is None:
                # 원장이 예약은 안다는데 도착이 없다 — 복원할 것이 없으니 고정한다
                pinned[ev.ev_id] = res.station_id
                continue

            self.prior[ev.ev_id] = (res.station_id, arr)
            self.rcost[ev.ev_id] = objective.reassign_cost(res.eta_min, t)
            free.append(ev)

        answers = dict(super().decide(free, world)) if free else {}
        answers.update(pinned)

        self._bump("locked", float(len(pinned)))
        self._bump("reassignable", float(len(self.prior)))
        moved = sum(1 for i, (sid, _) in self.prior.items() if answers.get(i) != sid)
        self._bump("reassigned", float(moved))
        self._bump("reassign_cost_min",
                   sum(self.rcost[i] for i, (sid, _) in self.prior.items()
                       if answers.get(i) != sid))
        return answers
