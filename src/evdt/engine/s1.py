"""S1 — 예약 원장을 보고 고르는 운전자 (사다리 두 번째 칸, #83).

S0 와 **딱 두 가지**가 다르다. 나머지는 전부 같다.

    S0   지금 화면의 대기 + 내 충전시간      ← 도착 시각을 모른다
    S1   **원장이 말하는 내 도착 시각의 체류**  ← 앞서 배정된 차를 센다

    S0   전원이 같은 스냅샷을 보고 동시에 고른다
    S1   **한 대 고를 때마다 원장에 쓰고** 다음 차가 그걸 본다 (순차 결합)

## 왜 이 둘인가

#59 가 S0 가 지는 이유를 숫자로 보였다 — 평균 대기 +40.8%. 원인은 "정보가 낡아서" 가
아니라 **낡은 정보를 모두가 같이 보고 같이 움직여서**다.

    같은 5분 안에 결정한 차 전원이 같은 화면 → 같은 계산 → 같은 곳
    40분 뒤 도착하면 앞에 30대가 서 있다

**첫 번째 차이**가 "도착할 때" 를 보게 하고, **두 번째 차이**가 "같이 움직이는 것" 을
깬다. 둘 중 하나만 하면 안 된다 — 원장을 **읽기만** 하고 안 쓰면 전원이 같은 원장을
보고 또 같이 몰린다.

## UE 와 무엇이 다른가

UE 도 "도착했을 때의 대기" 를 안다. 차이는 **누가 그걸 아느냐**다.

    UE   전원이 **서로의 최종 선택**을 안다 (반복 균형의 결과)
    S1   각자 **자기 앞까지의 선택**만 안다 (한 번 훑고 끝)

그래서 S1 은 UE 보다 정보가 적다. **그런데도 UE 에 가까워진다면**, 그 차이가
"반복 균형 없이도 조율만으로 얻을 수 있는 것" 이다.

## 하지 않는 것

- **예측을 보지 않는다** (그건 S2). `world.forecast` 를 건드리면 그 자리에서 터진다
- **충전량을 정하지 않는다** — 설계문서 §2.3 이 목표 SoC 를 결정변수에서 뺐다.
  S0 와 **같은 함수**를 쓴다 (여기가 갈라지면 충전량 차이가 정책 차이로 보고된다)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# ⚠ `queue_rule` 을 직접 임포트하지 않는다. 큐 계산이 필요한 엔진 코드는 **원장을
# 거친다** — 호출자가 늘면 규칙이 갈라진다
# (tests/test_queue_rule.py::test_queue_rule_has_at_most_two_callers).
from evdt.engine.ledger import Arrival
from evdt.engine.s0 import ESCAPE_NO_PLAN, NO_CHARGE, S0Policy, S0Settings, balked_at
from evdt.interfaces import EVState, WorldView
from evdt.world.charge_decision import calculate_arrival_soc, can_reach_station_with_buffer

#: SoC 비교 허용 오차 (s0 과 같다)
SOC_EPS = 1e-9


@dataclass(frozen=True)
class S1Settings(S0Settings):
    """S1 이 쓰는 값. **S0 와 같아야 한다** — 한쪽만 다르면 그 차이가 정책 차이로 보인다."""


@dataclass(frozen=True)
class S1Policy(S0Policy):
    """원장을 읽고 **쓰는** 정책.

    `S0Policy` 를 물려받아 도달 가능성·충전량·이탈 판정을 **그대로** 쓴다. 다시 쓰면
    두 정책이 다른 물리를 쓰게 되고, 비교가 성립하지 않는다.
    """

    stage: str = "S1"

    def _one(self, ev: EVState, world: WorldView) -> str:
        rf = world.range_factor
        opportunity = ev.wants_opportunity_charge and ev.stops_done == 0

        if not opportunity and self._reaches_dest(ev, ev.soc, ev.offset_km, rf):
            return NO_CHARGE

        if ev.stops_done >= self.settings.max_stops:
            return ESCAPE_NO_PLAN

        ledger = world.require_ledger()
        best_id, best_cost, best_arr = "", float("inf"), None

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

            # ★ 차이 1 — **내가 도착할 시각**에 원장이 뭐라고 하는가.
            #   S0 는 `st.wait_min`(지금 화면)을 썼다. 여기서는 그 시각까지 가는 데
            #   걸리는 시간을 더해 **그때의 줄**을 묻는다.
            eta = world.t_min + gap_km / max(ev.speed_kmh, 1e-6) * 60.0
            arr = Arrival(
                ev_id=ev.ev_id, arrival_min=eta, soc_from=soc_in, soc_to=soc_out,
                battery_kwh=ev.battery_kwh, vmax_kw=ev.vmax_kw, curve=ev.curve,
                charge_power_factor=world.cold_factor,
            )
            cost = ledger.dwell_if_i_go(st.station_id, arr)

            # ★ 여기서 멈추면 **가까운 곳만 고른다.** 줄이 짧다는 이유로 조금만 채우고
            #   또 서게 되고, 그 두 번째 정차가 비용에 안 들어간다. 실제로 그렇게
            #   돌렸더니 2회 이상 정차가 S0 547 → S1 629 로 늘고 평균 대기가 S0 보다
            #   나빠졌다 (#83).
            #
            #   코리도에서는 **주행시간이 상수다** — 어느 휴게소를 고르든 목적지까지
            #   총 거리는 같다. 그래서 선택을 가르는 것은 "또 서야 하는가" 하나다.
            #   UE 는 계획 전체를 열거해서 이걸 자동으로 본다. S1 은 **한 칸 앞**까지만
            #   본다 (두 칸 이상은 S2 의 예측이다).
            if not self._reaches_dest(ev, soc_out, st.offset_km, rf):
                cost += self._next_stop_cost(ev, world, ledger, st.offset_km, soc_out, eta, rf)

            if cost < best_cost:
                best_id, best_cost, best_arr = st.station_id, cost, arr

        if not best_id:
            return ESCAPE_NO_PLAN

        # 이탈 비용은 S0 와 **같은 값**을 쓴다. 한쪽만 다르면 이탈 수 차이가 정책
        # 차이로 보고된다. (#78 이 차별 비용을 넣으면 여기도 EVState 를 보게 된다)
        escape_cost = self.settings.escape_cost_min
        if escape_cost > 0 and best_cost > escape_cost:
            # 나가기로 했으면 **원장에서도 빠져야** 한다. 안 그러면 안 올 차 때문에
            # 뒤 차들이 더 붐빈다고 믿는다
            ledger.release(ev.ev_id)
            return balked_at(best_id)

        # ★ 차이 2 — **쓴다.** 이게 없으면 뒤 차가 나를 못 보고 같은 곳으로 온다.
        ledger.book(ev.ev_id, best_id, best_arr)
        return best_id


    def _next_stop_cost(self, ev: EVState, world: WorldView, ledger,
                        from_km: float, soc: float, t_min: float, rf: float) -> float:
        """여기서 충전하고도 목적지에 못 가면, **다음 정차에 드는 체류**를 더한다.

        원장에 **쓰지 않는다** — 아직 고르지도 않은 두 번째 정차를 예약하면 다른 차가
        있지도 않은 줄을 본다. 묻기만 한다.

        닿는 곳이 하나도 없으면 0 을 돌려준다. 그 선택이 막다른 길이라는 것은
        `max_stops` 와 이탈 판정이 따로 잡는다 — 여기서 큰 수를 돌려주면 "못 가는 곳"
        과 "줄이 긴 곳" 이 같은 값으로 섞인다.
        """

        best = float("inf")
        for st in sorted(world.stations.values(), key=lambda s: (s.offset_km, s.station_id)):
            if not (from_km < st.offset_km < ev.dest_offset_km):
                continue
            gap = st.offset_km - from_km
            if not can_reach_station_with_buffer(
                soc=min(max(soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf,
                distance_to_station_km=gap, buffer_km=self.settings.buffer_km,
            ):
                continue
            soc_in = calculate_arrival_soc(
                departure_soc=min(max(soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf, distance_km=gap,
            )
            soc_out = self._soc_out(ev, soc_in, st.offset_km, rf, opportunity=False)
            if soc_out <= soc_in + SOC_EPS:
                continue
            arr = Arrival(
                ev_id=ev.ev_id, arrival_min=t_min + gap / max(ev.speed_kmh, 1e-6) * 60.0,
                soc_from=soc_in, soc_to=soc_out, battery_kwh=ev.battery_kwh,
                vmax_kw=ev.vmax_kw, curve=ev.curve, charge_power_factor=world.cold_factor,
            )
            best = min(best, ledger.dwell_if_i_go(st.station_id, arr))

        return 0.0 if best == float("inf") else best

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """**한 대씩 순서대로** 묻는다 (순차 결합).

        S0 는 전원이 같은 스냅샷을 보고 동시에 골랐다. 여기서는 앞 차의 배정이 원장에
        들어간 뒤 뒤 차가 고른다 — 그게 "같이 몰리는 것" 을 깨는 유일한 장치다.

        순서는 **ev_id** 다. 도착 순서나 dict 순서로 하면 같은 입력에 다른 답이 나온다.
        """

        return {ev.ev_id: self._one(ev, world) for ev in sorted(evs, key=lambda e: e.ev_id)}
