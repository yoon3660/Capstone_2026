"""MNL — 확률적으로 고르는 운전자 (#168, 천장 재설계 1/2).

    "지금 화면의 대기 + 내 충전시간" 을 보되, **사람마다 다르게 느낀다.**

S0 와 무엇이 다른가
    **고르는 규칙 하나뿐이다.** 후보 휴게소(방향·도달·충전량), 충전시간, 이탈 판정,
    정보(지금 화면의 대기)는 S0 와 **같다.** S0 는 비용이 가장 낮은 곳을 반드시
    고르고, MNL 은 인지 오차 ε 를 더한 비용이 가장 낮은 곳을 고른다.

        인지 비용  C̃_is = w·W_s + T_charge_is − ε_is / θ        ε ~ Gumbel(0, 1)

    `argmin C̃` 는 정확히 다항 로짓 `P_is ∝ exp(−θ · C_is)` 를 따른다 (Gumbel 의 성질).

이탈 판정 — **"어디서" 만 MNL, "이탈할지" 는 S0** (`escape_mode`, 기본 "s0")
    "s0"     이탈 여부는 S0 와 똑같이 잡음 없는 비용으로 정한다:
             min_s (W_s + T_charge_is) > escape_cost_i 이면 이탈. 이탈하지 않는 차만
             인지 비용으로 휴게소를 고른다. 그래서 MNL 은 **분배만** 바꾸고 이탈 수는
             S0 와 같은 규칙에서 나온다 — 두 세계의 차이가 "선택이 퍼지는가" 하나로 남는다
    "noisy"  (#168 첫 실행의 방식, 민감도용) 이탈도 선택지로 두고 같은 척도의 ε 를 준다:
             C̃_i,esc = escape_cost_i − ε_i,esc / θ. 막히는 무대에서 줄 이탈이 크게 늘고,
             그 몫이 격차 축소율을 좌우했다 (`docs/data_validation/mnl_ceiling_168_results.md`
             §3.1). 이탈 행동은 충전이력으로 식별되지 않으므로 기본값으로 쓰지 않는다

θ 가 하는 일 (척도, 1/분)
    θ → ∞   ε/θ → 0. **S0 와 비트 단위로 같다** (w = 1 일 때) — 테스트가 고정한다
    θ → 0   비용과 무관하게 후보 안에서 균등하게 고른다

ε 를 차마다 **고정**하는 이유 (공통 난수)
    ε 는 (seed, ev_id, 선택지) 의 해시에서 나온다. 난수 흐름을 소비하지 않으므로

    - 다른 차의 선택이 바뀌어도 **내 ε 는 그대로다** → 엔진·Oracle 비교가 짝지어진 비교로 남는다
    - 기존 수요 난수(`DEST_STREAM`, `OPPORTUNITY_STREAM`)를 **건드리지 않는다**
    - 같은 사람은 같은 휴게소를 늘 비슷하게 좋아한다 (잠재 선호)

하지 않는 것
    - **충전 필요 여부·충전량을 정하지 않는다** — S0 와 같은 함수다 (설계문서 §2.3)
    - 원장·예측을 보지 않는다 (S0 와 같다)
    - 요금·편의시설·우회시간은 효용에 없다 — 자료가 없거나 코리도에서 0 이다
      (`docs/mnl_oracle_design.md` 부록)
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from evdt.engine.ledger import Arrival  # queue_rule 의 호출자를 늘리지 않는다 (test_queue_rule)
from evdt.engine.s0 import S0Policy, S0Settings
from evdt.interfaces import (
    ESCAPE_NO_PLAN,
    NO_CHARGE,
    EVState,
    WorldView,
    balked_at,
)
from evdt.world.charge_decision import (
    calculate_arrival_soc,
    can_reach_station_with_buffer,
)
from evdt.world.charging import charge_time_min

#: SoC 비교 허용 오차 (s0 과 같다)
SOC_EPS = 1e-9

#: 이탈 선택지의 ε 키. 휴게소 ID 와 겹치지 않는다
ESCAPE_ALT = "__escape__"

#: 이탈 판정 방식 (MnlSettings.escape_mode)
ESCAPE_MODES = ("s0", "noisy")

#: 해시 흐름 이름. 같은 (seed, ev_id) 라도 흐름이 다르면 독립이다
TASTE_STREAM = "mnl_taste"


def hashed_uniform(*key: object) -> float:
    """키 → (0, 1) 의 균등 난수. 난수 생성기를 쓰지 않는다 — 순서와 무관하다.

    blake2b 64비트를 2^64 로 나눈다. 0 과 1 을 피하려고 반 칸 민다.
    """

    h = hashlib.blake2b("|".join(map(str, key)).encode("utf-8"), digest_size=8)
    return (int.from_bytes(h.digest(), "big") + 0.5) / 2.0**64


def gumbel(seed: int, ev_id: str, alt: str) -> float:
    """(seed, ev_id, 선택지) 에 고정된 Gumbel(0, 1)."""

    return -math.log(-math.log(hashed_uniform(TASTE_STREAM, seed, ev_id, alt)))


@dataclass(frozen=True)
class MnlSettings:
    """MNL 의 손잡이. **둘 다 가정이다** — 결과의 manifest 에 그대로 남긴다."""

    #: 척도 θ (1/분). inf 면 S0 와 같다. 추석 점유율로 정한 잠정값 0.05 (#168)
    theta: float = math.inf
    #: 대기 가중치 w. 대기가 충전보다 몇 배 무겁게 느껴지나. 1.0 이면 S0 와 같은 비용
    wait_weight: float = 1.0
    #: ε 의 시드. **시나리오 시드와 같은 값**을 준다 (같은 시드 = 같은 사람들)
    seed: int = 0
    #: 이탈 판정 방식. "s0" = 잡음 없이 S0 규칙 (기본) · "noisy" = 이탈에도 ε (민감도)
    escape_mode: str = "s0"

    def __post_init__(self) -> None:
        if not self.theta > 0:
            raise ValueError(f"theta 는 양수여야 한다 (inf 허용): {self.theta}")
        if self.wait_weight < 0:
            raise ValueError(f"wait_weight 는 음수일 수 없다: {self.wait_weight}")
        if self.escape_mode not in ESCAPE_MODES:
            raise ValueError(f"escape_mode 는 {ESCAPE_MODES} 중 하나: {self.escape_mode!r}")

    def noise(self, ev_id: str, alt: str) -> float:
        """인지 비용에서 빼는 값 ε/θ (분). θ = inf 면 0."""

        if math.isinf(self.theta):
            return 0.0
        return gumbel(self.seed, ev_id, alt) / self.theta


@dataclass(frozen=True)
class MnlPolicy(S0Policy):
    """`Policy` 구현. S0 의 후보·충전량·이탈 규칙을 그대로 쓰고 **고르는 규칙만** 다르다.

    상태를 갖지 않는다 — 같은 화면·같은 ε 면 같은 답이다.
    """

    mnl: MnlSettings = field(default_factory=MnlSettings)
    stage: str = "MNL"
    reask_en_route: bool = False

    def _one(self, ev: EVState, world: WorldView) -> str:
        rf = world.range_factor
        opportunity = ev.wants_opportunity_charge and ev.stops_done == 0

        # ↓ 여기부터 후보 루프까지 **S0Policy._one 과 같다.** 갈라지면 MNL 과 S0 의
        #   차이에 "후보가 달랐다" 가 섞인다 (tests/test_mnl.py 가 θ=inf 에서 고정한다)
        if not opportunity and self._reaches_dest(ev, ev.soc, ev.offset_km, rf):
            return NO_CHARGE

        if ev.stops_done >= self.settings.max_stops:
            return ESCAPE_NO_PLAN

        best_id = ""
        best_cost = float("inf")
        s0_best = float("inf")     # 잡음 없는 S0 비용의 최솟값 — escape_mode="s0" 의 이탈 판정

        for st in sorted(world.stations.values(), key=lambda s: (s.offset_km, s.station_id)):
            if not (ev.offset_km < st.offset_km < ev.dest_offset_km):
                continue

            gap_km = st.offset_km - ev.offset_km

            if not can_reach_station_with_buffer(
                soc=min(max(ev.soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf,
                distance_to_station_km=gap_km, buffer_km=self.settings.buffer_km,
            ):
                continue

            soc_in = calculate_arrival_soc(
                departure_soc=min(max(ev.soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf,
                distance_km=gap_km,
            )
            soc_out = self._soc_out(ev, soc_in, st.offset_km, rf, opportunity=opportunity)

            if soc_out <= soc_in + SOC_EPS:
                continue

            charge_min = charge_time_min(
                soc_from=soc_in, soc_to=soc_out, battery_kwh=ev.battery_kwh,
                vmax_kw=ev.vmax_kw, charger_kw=max(st.charger_powers_kw),
                curve=ev.curve, charge_power_factor=world.cold_factor,
            )
            s0_best = min(s0_best, st.wait_min + charge_min)
            # ★ MNL 의 전부: S0 의 비용에 대기 가중치를 걸고, 이 사람의 ε 를 뺀다
            cost = (self.mnl.wait_weight * st.wait_min + charge_min
                    - self.mnl.noise(ev.ev_id, st.station_id))

            if cost < best_cost:
                best_id, best_cost = st.station_id, cost

        if not best_id:
            return ESCAPE_NO_PLAN

        escape_cost = ev.escape_cost_min or self.settings.escape_cost_min
        if escape_cost > 0:
            if self.mnl.escape_mode == "s0":
                # S0 와 같은 판정 — 잡음 없는 최소 비용이 이탈 비용보다 비싸면 이탈.
                # 남았다면 갔을 곳은 MNL 이 고른 곳으로 적는다. θ=inf·w=1 이면 S0 와 같다
                if s0_best > escape_cost:
                    return balked_at(best_id)
            else:
                # "noisy": 이탈도 선택지 — 같은 척도의 ε. θ=inf 면 S0 의 판정과 같다
                esc = escape_cost - self.mnl.noise(ev.ev_id, ESCAPE_ALT)
                if best_cost > esc:
                    return balked_at(best_id)

        return best_id

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        return {ev.ev_id: self._one(ev, world) for ev in evs}

    def plan_arrival(self, ev: EVState, world: WorldView, station_id: str) -> Arrival | None:
        """이 차가 `station_id` 로 가면 원장에 올릴 도착 (시각 + 충전 요구).

        참여율 래퍼가 **비참여자를 원장에 보이게** 할 때 쓴다(`participation.py`).
        도착 시각은 S1 의 `_eta` 와 같은 식이고, 충전 요구는 `_one` 과 같은 계산이다.
        고른 곳이 후보가 아니면 None.
        """

        st = world.stations.get(station_id)
        if st is None or not (ev.offset_km < st.offset_km < ev.dest_offset_km):
            return None
        rf = world.range_factor
        opportunity = ev.wants_opportunity_charge and ev.stops_done == 0
        gap_km = st.offset_km - ev.offset_km
        soc_in = calculate_arrival_soc(
            departure_soc=min(max(ev.soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
            consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf, distance_km=gap_km)
        soc_out = self._soc_out(ev, soc_in, st.offset_km, rf, opportunity=opportunity)
        if soc_out <= soc_in + SOC_EPS:
            return None
        return Arrival(
            ev_id=ev.ev_id, arrival_min=world.t_min + gap_km / max(ev.speed_kmh, 1e-6) * 60.0,
            soc_from=soc_in, soc_to=soc_out, battery_kwh=ev.battery_kwh, vmax_kw=ev.vmax_kw,
            curve=ev.curve, charge_power_factor=world.cold_factor)


def background_settings(engine_settings: object) -> S0Settings:
    """엔진 설정에서 S0 가 쓰는 값만 뽑는다. **같은 세계 값**을 MNL 운전자에게 준다.

    S1~S4 의 설정은 `S0Settings` 의 필드를 그대로 갖는다 (상속). 하나라도 다르면
    참여자와 비참여자가 다른 충전량 규칙을 쓰고, 그 차이가 참여율 효과로 보고된다.
    """

    from dataclasses import fields

    return S0Settings(**{f.name: getattr(engine_settings, f.name) for f in fields(S0Settings)})
