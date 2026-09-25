"""S0 — 앱 화면만 보고 고르는 운전자 (사다리의 첫 칸, #59).

    "지금 화면에 뜬 대기 + 내 예상 충전시간" 이 가장 짧은 휴게소로 간다.

UE 와 정확히 무엇이 다른가
    **딱 하나다.** UE 운전자는 자기가 **도착했을 때의** 대기를 안다 (명절마다 같은
    길을 다니며 "안성은 9시에 막힌다" 를 아는 사람). S0 운전자는 **지금 이 순간의**
    대기만 안다. 100 km 를 달리는 동안 그 숫자는 낡는다.

    나머지는 전부 같다. 같은 차, 같은 SoC, 같은 충전량 규칙(설계문서 §2.3),
    같은 큐 규칙, 같은 통행시간. 그래야 차이를 정책 탓으로 돌릴 수 있다.

    제 충전시간은 **정확히** 안다 (`charge_time_min` 을 그대로 부른다 — 큐가 쓰는
    함수와 같다). S0 의 근시안은 한 가지여야 한다: **도착했을 때 줄이 어떨지 모른다.**
    여기에 "제 충전시간도 어림한다" 를 섞으면, 나중에 S0 가 진 이유를 못 가른다.

원장도 예측도 보지 않는다 (설계문서 §10.1)
    `WorldView.ledger` · `forecast` 를 읽지 않는다. S1 이 원장을, S2 가 예측을 더한다.
    읽는 순간 그 스테이지가 아니게 된다.

왜 이게 안 통하는가 — 이 파일이 증명하려는 것
    **같은 화면을 본 차들이 한꺼번에 같은 곳으로 간다.** A 가 비어 보이면 그 Δt 에
    결정한 차 전원이 A 로 간다. 도착하면 A 가 막혀 있고, 화면이 빨개지면 다음 무리는
    전원 B 로 간다. 큐가 진동한다 (scripts/plot_station_wait.py).

    "정보를 주는 것만으로는 쏠림이 안 풀린다" — 이게 사다리 첫 칸의 결론이고,
    S1 이 예약 원장을 들고 오는 이유다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evdt.interfaces import (
    ESCAPE_NO_PLAN,
    NO_CHARGE,
    EVState,
    WorldView,
    balked_at,
)
from evdt.world.charge_decision import (
    calculate_arrival_soc,
    calculate_target_soc,
    can_reach_destination,
    can_reach_station_with_buffer,
)
from evdt.world.charging import charge_time_min

#: SoC 비교 허용 오차 (ue_demand · corridor_sim 과 같다)
SOC_EPS = 1e-9


@dataclass(frozen=True)
class S0Settings:
    """S0 가 쓰는 값. **전부 config 에서 온다 — 여기서 만든 손잡이는 하나도 없다.**

    UE 와 같은 값을 받아야 한다 (`policy.ue.max_stops`, `demand.*`). 한쪽만 다르면
    그 차이가 정책의 차이로 보고된다.
    """

    buffer_km: float
    reserve_soc: float
    target_soc_cap: float
    max_stops: int
    #: 휴게소 계획이 전부 이보다 비싸면 코리도를 벗어난다 (#54). 0 이면 이탈이 없다
    escape_cost_min: float = 0.0


@dataclass(frozen=True)
class S0Policy:
    """`Policy` 구현. 상태를 갖지 않는다 — 같은 화면이면 같은 답이다."""

    settings: S0Settings
    stage: str = "S0"

    # -- 도달 가능성 ---------------------------------------------------------
    def _reaches_dest(self, ev: EVState, soc: float, offset_km: float,
                      range_factor: float) -> bool:
        return can_reach_destination(
            target_soc=min(max(soc, 0.0), 1.0),
            battery_kwh=ev.battery_kwh,
            consumption_kwh_km=ev.consumption_kwh_km,
            range_factor=range_factor,
            distance_to_dest_km=max(ev.dest_offset_km - offset_km, 0.0),
            buffer_km=self.settings.buffer_km,
            arrival_reserve_soc=self.settings.reserve_soc,
        )

    def _soc_out(self, ev: EVState, soc_in: float, offset_km: float,
                 range_factor: float, *, opportunity: bool) -> float:
        """이 휴게소에서 얼마까지 채우나. **corridor_sim.ChargeAmount 와 같은 계산이다.**

        정책이 예상 충전시간을 재려면 충전량을 알아야 하는데, 충전량은 정책의
        결정변수가 아니다 (설계문서 §2.3). 그래서 같은 규칙을 그대로 쓴다 —
        여기가 갈라지면 정책이 재는 시간과 실제 점유시간이 어긋난다.
        """

        need = calculate_target_soc(
            battery_kwh=ev.battery_kwh, consumption_kwh_km=ev.consumption_kwh_km,
            range_factor=range_factor,
            distance_to_dest_km=max(ev.dest_offset_km - offset_km, 0.0),
            buffer_km=self.settings.buffer_km,
            target_soc_cap=self.settings.target_soc_cap,
            arrival_reserve_soc=self.settings.reserve_soc,
        )
        return max(soc_in, need, self.settings.target_soc_cap if opportunity else 0.0)

    # -- 결정 -----------------------------------------------------------------
    def _one(self, ev: EVState, world: WorldView) -> str:
        rf = world.range_factor
        opportunity = ev.wants_opportunity_charge and ev.stops_done == 0

        # 갈 수 있으면 안 선다. 기회 충전 차는 한 번은 선다 (#54 레이어)
        if not opportunity and self._reaches_dest(ev, ev.soc, ev.offset_km, rf):
            return NO_CHARGE

        if ev.stops_done >= self.settings.max_stops:
            # max_stops 번 서고도 못 간다 = 휴게소로는 답이 없다. UE 의 n_infeasible 과 같은 경우다
            return ESCAPE_NO_PLAN

        best_id = ""
        best_cost = float("inf")

        # 정렬해서 훑는다 — 동률일 때 dict 순서에 답이 달리면 재현이 안 된다
        for st in sorted(world.stations.values(), key=lambda s: (s.offset_km, s.station_id)):
            if not (ev.offset_km < st.offset_km < ev.dest_offset_km):
                continue   # 지나쳤거나 목적지 너머다

            gap_km = st.offset_km - ev.offset_km

            if not can_reach_station_with_buffer(
                soc=min(max(ev.soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf,
                distance_to_station_km=gap_km, buffer_km=self.settings.buffer_km,
            ):
                continue   # 안전버퍼를 남기고 닿지 못한다

            soc_in = calculate_arrival_soc(
                departure_soc=min(max(ev.soc, 0.0), 1.0), battery_kwh=ev.battery_kwh,
                consumption_kwh_km=ev.consumption_kwh_km, range_factor=rf,
                distance_km=gap_km,
            )
            soc_out = self._soc_out(ev, soc_in, st.offset_km, rf, opportunity=opportunity)

            if soc_out <= soc_in + SOC_EPS:
                continue   # 넣을 것이 없는 곳은 정차가 아니다

            # 앱이 광고하는 최고출력으로 어림한다. 실제로 어느 기에 물릴지는 도착해야
            # 알고 (유휴 중 최고출력 / 없으면 최단 해제), 그건 화면에 안 뜬다.
            charge_min = charge_time_min(
                soc_from=soc_in, soc_to=soc_out, battery_kwh=ev.battery_kwh,
                vmax_kw=ev.vmax_kw, charger_kw=max(st.charger_powers_kw),
                curve=ev.curve, charge_power_factor=world.cold_factor,
            )
            # ★ S0 의 전부: 지금 화면의 대기 + 내 충전시간. 도착 시각을 보지 않는다
            cost = st.wait_min + charge_min

            if cost < best_cost:
                best_id, best_cost = st.station_id, cost

        if not best_id:
            return ESCAPE_NO_PLAN

        if self.settings.escape_cost_min > 0 and best_cost > self.settings.escape_cost_min:
            # 가장 나은 곳도 시내 다녀오는 것보다 비싸다 (#54). 남았다면 갔을 곳을 적는다
            return balked_at(best_id)

        return best_id

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """전원이 이번 Δt 에 답한다. 미루면 정보가 더 낡을 뿐 나아지지 않는다."""

        return {ev.ev_id: self._one(ev, world) for ev in evs}
