"""엔진 ↔ 트윈 인터페이스 (설계문서 §10.1).

⚠ 설계문서는 이 Protocol 을 `engine/policy.py` 에 두라고 적혀 있지만,
   그러면 `world/sim.py` 가 타입 힌트 때문에 engine 을 임포트해야 한다.
   그건 설계 규칙 2("world 는 engine 을 임포트하지 않는다")를 깬다.

   그래서 **계약만** 중립 모듈인 여기에 두고, 구체 정책(UEPolicy, S1Policy …)은
   engine/ 에 둔다. world 도 engine 도 evdt.interfaces 를 임포트하지만
   서로는 임포트하지 않는다. 의존 그래프:

        world  ──┐
                 ├──►  interfaces
        engine ──┘

   tests/test_import_boundaries.py 가 이 방향을 강제한다.

스테이지 간 차이는 오직 decide() 뿐이다. 사후 최적해(ORACLE)조차
"전체 도착을 다 아는 Policy" 하나로 구현된다. 이것이 공정 비교의 핵심이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EVState:
    """배정 결정 시점에 엔진이 보는 차량 한 대의 상태."""

    ev_id: str
    vclass_id: str
    corridor_id: str
    offset_km: float          # 현재 선형 위치
    speed_kmh: float
    soc: float                # 현재 SoC (0~1)
    soc_target: float         # 목표 SoC (상한 0.8)
    battery_kwh: float
    vmax_kw: float
    dest_offset_km: float
    is_participant: bool      # False 면 배정을 무시하고 UE 처럼 행동한다


@dataclass(frozen=True, slots=True)
class StationView:
    """엔진이 보는 충전소 한 곳의 현재 상태."""

    station_id: str
    offset_km: float
    lat: float
    lon: float
    charger_powers_kw: tuple[float, ...]   # 개별 충전기 출력 목록
    n_charging: int
    n_waiting: int
    next_free_min: tuple[float, ...]       # 충전기별 해제 예정 시각(분)


@dataclass(frozen=True, slots=True)
class WorldView:
    """decide() 에 넘어가는 세계의 스냅샷.

    forecast 와 ledger 는 **옵셔널**이다 (§10.1):
        UE, S0  → 둘 다 보지 않는다
        S1~     → ledger 를 본다
        S2~     → forecast 도 본다

    None 인 필드를 읽으려는 정책은 그 자리에서 터져야 한다. 조용히 빈 값으로
    돌아가면 "예측을 쓰는 줄 알았는데 안 쓰고 있던" 상태로 실험이 끝난다.
    """

    t_min: float
    stations: Mapping[str, StationView]
    temp_c: float
    cold_factor: float
    forecast: Any | None = None    # evdt.engine.forecast.Forecast  (S2~)
    ledger: Any | None = None      # evdt.engine.ledger.ReservationLedger  (S1~)
    extra: dict[str, Any] = field(default_factory=dict)

    def require_ledger(self) -> Any:
        if self.ledger is None:
            raise RuntimeError("이 스테이지는 예약 원장이 필요한데 주입되지 않았다")
        return self.ledger

    def require_forecast(self) -> Any:
        if self.forecast is None:
            raise RuntimeError("이 스테이지는 예측이 필요한데 주입되지 않았다")
        return self.forecast


@runtime_checkable
class Policy(Protocol):
    """배정 정책. 시뮬레이터는 이 인터페이스만 알고 스테이지는 모른다."""

    stage: str

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """{ev_id: station_id} 를 반환한다.

        반환에서 빠진 ev_id 는 "이번 Δt 에는 결정하지 않음"을 뜻한다.
        """
        ...


# ---------------------------------------------------------------------------
# 스냅샷 스트림 계약 (설계 규칙 4 · 설계문서 T-17)
# ---------------------------------------------------------------------------
#
# 시뮬레이터(world)가 쓰고, 로거(io)가 검사하고, 렌더러(viz)가 읽는다.
# 세 계층이 같은 목록을 봐야 하는데 io·viz 는 world 를 임포트할 수 없다
# (tests/test_import_boundaries.py). 그래서 계약은 이 중립 모듈에 둔다.

#: 스냅샷 한 행의 컬럼. writers.SCHEMAS["snapshot"] 이 이 순서를 따른다.
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "t_min",        # 시뮬레이션 기준일 00:00 부터의 분 (시계 분)
    "entity_type",  # SNAPSHOT_STATES 의 키
    "entity_id",
    "lat",          # WGS84. 렌더러가 지도에 바로 찍는다
    "lon",
    "state",        # 지표 이름. 지표가 늘면 컬럼이 아니라 이 값이 는다
    "value",
)

#: entity_type 별로 내보낼 수 있는 state. **여기에 없는 값은 로거가 거부한다.**
#: 오타 난 state 는 조용히 저장되고, 렌더러에서 빈 칸으로만 드러난다.
#: cell·vehicle 은 Sprint 2 에서 CTM·차량 스냅샷을 붙일 때 state 를 정한다.
SNAPSHOT_STATES: Mapping[str, tuple[str, ...]] = {
    "station": (
        "wait_min",         # 지금 도착하면 기다릴 시간(분) — S0(UE) 가 보는 값
        "queue_len",        # 도착했지만 아직 충전을 시작하지 못한 차 수
        "chargers_busy",    # 충전 중인 충전기 수
        "chargers_total",   # 총 충전기 수
    ),
}

#: 스냅샷 좌표 범위. schema.sql station.lat/lon CHECK 와 같다 (한반도 남쪽).
SNAPSHOT_LAT_RANGE: tuple[float, float] = (33.0, 39.0)
SNAPSHOT_LON_RANGE: tuple[float, float] = (124.0, 132.0)
