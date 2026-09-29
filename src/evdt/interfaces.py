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
    #: 전비(kWh/km). **정책이 "어디까지 갈 수 있나" 를 스스로 계산하려면 반드시 필요하다** (#59).
    #: 이 값이 없으면 시뮬레이터가 대신 닿는 휴게소를 걸러서 넘겨야 하는데, 그건
    #: 시뮬레이터가 충전 판단(= 행동 모델)을 하는 것이라 설계 규칙 2를 깬다.
    consumption_kwh_km: float = 0.0
    #: 필요는 없지만 들른 김에 충전하는 차인가 (#54 기회 충전 레이어).
    #: 수요를 만들 때 한 번 뽑아서 **모든 스테이지가 같은 값을 받는다** — 여기서
    #: 다시 뽑으면 UE 와 S0 가 서로 다른 차량 집합을 상대하게 되어 비교가 깨진다.
    wants_opportunity_charge: bool = False
    #: 이미 선 횟수. max_stops 를 정책이 스스로 지킨다
    stops_done: int = 0
    #: 충전 곡선 (`evdt.world.charging.CurveSegment` 의 튜플).
    #: 타입을 Any 로 두는 이유는 forecast·ledger 와 같다 — interfaces 는 world 를
    #: 임포트하지 않는다 (test_interfaces_module_has_no_internal_dependencies).
    #:
    #: 왜 정책에게 주는가: 이걸 안 주면 정책이 "예상 충전시간" 을 전력으로만 어림해야
    #: 하고, 그러면 S0 의 근시안이 **두 가지**(줄을 모른다 + 제 충전시간도 모른다)가
    #: 섞인다. S0 의 주장은 하나여야 한다 — **제 충전은 정확히 알고, 도착했을 때
    #: 줄이 어떨지만 모른다.**
    curve: tuple[Any, ...] = ()


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
    #: **지금 도착하면 충전을 시작하기까지 기다릴 시간(분).** 앱 화면에 뜨는 숫자이고,
    #: 스냅샷 스트림의 `wait_min` 과 **같은 값**이다 (queue_rule.wait_if_arriving_now).
    #: S0 가 보는 유일한 혼잡 정보다 — 원장도 예측도 없이 이것만 본다.
    wait_min: float = 0.0


@dataclass(frozen=True, slots=True)
class WorldView:
    """decide() 에 넘어가는 세계의 스냅샷.

    forecast 와 ledger 는 **옵셔널**이다 (§10.1):
        S0      → 둘 다 보지 않는다 (UE 는 Policy 가 아니라 engine/ue.py 의 반복 균형)
        S1~     → ledger 를 본다
        S2~     → forecast 도 본다

    None 인 필드를 읽으려는 정책은 그 자리에서 터져야 한다. 조용히 빈 값으로
    돌아가면 "예측을 쓰는 줄 알았는데 안 쓰고 있던" 상태로 실험이 끝난다.
    """

    t_min: float
    stations: Mapping[str, StationView]
    temp_c: float
    cold_factor: float      # 저온 충전출력 계수 (충전이 느려진다)
    #: 저온 주행거리 계수 (전비가 나빠진다). 정책이 도달 가능성을 계산할 때 쓴다.
    #: cold_factor 와 짝이고, 둘 다 기온에서 나오는 **세계의 상태**라 여기 있다.
    range_factor: float = 1.0
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


# 휴게소가 아닌 두 가지 답 (#59)
#
# `decide` 의 반환에서 **빠진** ev_id 는 "이번 Δt 에는 아직 못 정했다" 는 뜻이고,
# 시뮬레이터는 다음 Δt 에 같은 차를 다시 물어본다. 그래서 "안 간다" 를 생략으로
# 표현할 수 없다 — 영원히 되묻게 된다. 두 경우를 값으로 준다.
#
# 이 판단을 시뮬레이터에 두지 않는 이유: "충전이 필요한가 / 줄이 너무 긴가" 는
# 행동 모델이고, 스테이지마다 다를 수 있다 (설계 규칙 2).

# 코리도를 벗어나 시내에서 충전하고 돌아온다 (#54). 비용은 demand.escape_cost_min.
# **이유를 값으로 가른다.** 실측에서 이탈의 79~91% 가 no_plan 이었다 — 줄이 길어서가
# 아니라 닿는 휴게소가 아예 없어서다 (docs/charger_gaps.md). 둘을 한 값으로 묶으면
# 엔진이 "이탈을 줄였다" 고 할 때 **고칠 수 없는 몫까지 성과로 세게 된다.**
# 어느 쪽인지는 후보를 훑어본 정책만 안다. 그래서 정책이 값으로 말한다.

#: 갈 수 있는 휴게소는 있는데 줄이 이탈보다 비싸다 — 엔진이 고칠 수 있는 이탈
ESCAPE_BALKED = "__escape_balked__"

#: 닿는 휴게소가 아예 없다 — 충전기 간격·출발 SoC 의 문제지 배정의 문제가 아니다
ESCAPE_NO_PLAN = "__escape_no_plan__"

#: 충전하지 않고 목적지까지 간다. 한 번 채우고 나면 대부분의 차가 여기로 온다.
NO_CHARGE = "__no_charge__"

#: escape_event.reason 에 그대로 들어간다 (UE 가 쓰는 값과 같아야 비교가 된다)
ESCAPE_REASON: Mapping[str, str] = {
    ESCAPE_BALKED: "balked",
    ESCAPE_NO_PLAN: "no_plan",
}

#: 휴게소 ID 가 아닌 답. `decide` 결과를 해석할 때 먼저 걸러낸다.
NON_STATION: frozenset[str] = frozenset({*ESCAPE_REASON, NO_CHARGE})


def balked_at(station_id: str) -> str:
    """"줄이 길어서 나갔다 — 남았다면 여기로 갔을 것이다".

    남았다면 갔을 휴게소를 같이 적는 이유: **어느 휴게소가 차를 밀어냈는가**가
    엔진이 손댈 자리다 (docs/charger_gaps.md · scripts/plot_escaped.py 의 셋째 칸).
    그건 후보를 훑어본 정책만 안다.
    """

    return f"{ESCAPE_BALKED}:{station_id}" if station_id else ESCAPE_BALKED


def read_answer(answer: str) -> tuple[str, str]:
    """decide() 의 답 하나를 (종류, 휴게소 ID) 로 가른다.

        "seoul_01"                  → ("seoul_01", "")          휴게소를 골랐다
        ESCAPE_BALKED + ":안성"      → (ESCAPE_BALKED, "안성")    나갔다 (안성이 밀어냈다)
        NO_CHARGE                   → (NO_CHARGE, "")
    """

    kind, _, payload = answer.partition(":")
    return (kind, payload) if kind in NON_STATION else (answer, "")


@runtime_checkable
class Policy(Protocol):
    """배정 정책. 시뮬레이터는 이 인터페이스만 알고 스테이지는 모른다."""

    stage: str

    def decide(self, evs: Sequence[EVState], world: WorldView) -> Mapping[str, str]:
        """{ev_id: station_id} 를 반환한다.

        값은 휴게소 ID 이거나 `ESCAPE` · `NO_CHARGE` 중 하나다.
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
#: vehicle 은 차량 스냅샷을 붙일 때 정한다 (아직 미정).
SNAPSHOT_STATES: Mapping[str, tuple[str, ...]] = {
    "station": (
        "wait_min",         # 지금 도착하면 기다릴 시간(분) — S0 가 보는 값
        "queue_len",        # 도착했지만 아직 충전을 시작하지 못한 차 수
        "chargers_busy",    # 충전 중인 충전기 수
        "chargers_total",   # 총 충전기 수
    ),
    # CTM 셀 (#56). 셀의 lat/lon 은 셀 **시작점**을 쓴다 — 셀은 선분이라 점 하나로
    # 줄여야 하는데, 끝점을 쓰면 이웃 셀과 겹쳐 보인다.
    "cell": (
        "speed_kmh",        # 유량 ÷ 밀도. 빈 셀은 자유속도
        "density_veh_km",   # 셀 전체(차로 합) 밀도. 차로당 값이 아니다
        "flow_veh_h",       # 셀에서 하류로 나간 유량
        "ev_count",         # 그 셀 안에 있는 합성 EV 수 (배경 교통은 빼고)
    ),
}

#: 스냅샷 좌표 범위. schema.sql station.lat/lon CHECK 와 같다 (한반도 남쪽).
SNAPSHOT_LAT_RANGE: tuple[float, float] = (33.0, 39.0)
SNAPSHOT_LON_RANGE: tuple[float, float] = (124.0, 132.0)
