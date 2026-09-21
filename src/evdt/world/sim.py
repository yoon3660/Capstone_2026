"""휴게소 충전 큐 이산사건 시뮬레이션 (T-16).

    (휴게소·충전기, 도착 목록) → charge_event 로그 + 스냅샷 스트림

대기시간이 나오는 곳이다. 이 모듈이 내는 숫자가 KPI 의 출발점이고, Sprint 3 의
한계 외부비용이 전부 여기서 파생된다.

SimPy 는 시간만 굴린다. 큐 판단은 전부 world/queue_rule.py 가 한다
    `simpy.Resource` 를 쓰면 편하지만 쓰지 않는다. Resource 는 **자기 나름의
    FIFO 배정 규칙**을 갖고 있어서, 그걸 쓰는 순간 큐 규칙이 두 곳에 생긴다.
    우리 규칙은 "유휴 중 최고출력 / 전부 사용 중이면 최단 해제" 라서 Resource 의
    규칙과 다르고, 원장은 Resource 를 쓸 수 없으니 둘이 갈라진다 (설계 규칙 1).

    그래서 SimPy 에게는 `timeout` 만 시킨다. **누가 어느 충전기를 언제 쓰는가는
    queue_rule.assign 이 정한다.** 이 모듈은 그 답대로 시간을 흘려보낼 뿐이다.

같은 시각에 도착한 차들
    한 묶음으로 모아 `assign` 을 한 번 부른다. 그래야 배치로 한 번에 투영한
    결과와 완전히 같아진다 (T-15 test_incremental_projection_matches_single_shot).
    SimPy 프로세스가 깨어나는 순서에 결과가 흔들리면 원장과 어긋난다.

IO 를 하지 않는다
    DB 도 파일도 읽지 않고 쓰지 않는다. 호출자가 읽어서 넣어주고, 결과 레코드를
    받아서 쓴다 (지금은 `scripts/smoke_run.py`, 이후 T-19 러너). world 가 io 를
    임포트하면 계층이 무너진다.

    호출자는 `check_queue_config` 도 불러야 한다. 시나리오의 queue 설정이 이 구현과
    다르면 멈추는 검사인데, 이 함수 안에서는 시나리오를 모르므로 부를 수 없다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import groupby

import simpy

from evdt.interfaces import SNAPSHOT_STATES
from evdt.world.charging import CurveSegment
from evdt.world.queue_rule import (
    Arrival,
    Assignment,
    Charger,
    assign,
    chargers_after,
    wait_if_arriving_now,
)

#: 충전기 한 기의 ID = "<DB charger_id><UNIT_SEP><번호>".
#: charger 한 행은 같은 사양 n_units 기를 뜻하므로 (schema.sql §4) 개별 기로 펼친다.
#: '#' 앞을 자르면 DB 행으로 돌아간다 (`db_charger_id`).
UNIT_SEP = "#"

#: 스냅샷 스트림에서 휴게소가 내보내는 state 값. 계약 원본은 evdt.interfaces 에 있다
#: (설계 규칙 4). io 의 로거와 viz 의 렌더러도 같은 목록을 봐야 해서 중립 모듈로 옮겼다.
STATION_SNAPSHOT_STATES: tuple[str, ...] = SNAPSHOT_STATES["station"]

DEFAULT_SNAPSHOT_EVERY_MIN = 5.0

#: 시나리오의 queue 설정에서 이 모듈이 실제로 구현한 값.
#: config 키가 있는데 코드가 무시하면 "LIFO 로 돌렸다" 고 믿는 실험이 생긴다.
SUPPORTED_DISCIPLINE = "FIFO"
SUPPORTED_CHARGER_SELECT = "MAX_POWER_IDLE"


def check_queue_config(discipline: str, charger_select: str) -> None:
    """시나리오의 queue 설정이 이 구현과 같은지 확인한다.

    `queue.discipline` / `queue.charger_select` 는 설정 파일에 있지만 큐 규칙은
    world/queue_rule.py 에 박혀 있다. 둘이 어긋나면 **설정에 적힌 것과 다른 규칙으로
    돈 결과**가 나오고, 그 결과는 재현도 해석도 안 된다. 그래서 다르면 멈춘다.

    규칙을 늘리려면 queue_rule 에 구현하고 여기 목록을 늘린다.
    """

    if discipline != SUPPORTED_DISCIPLINE:
        raise ValueError(
            f"queue.discipline={discipline!r} 은 구현되지 않았습니다 "
            f"(지원: {SUPPORTED_DISCIPLINE}). world/queue_rule.py 를 먼저 고칠 것."
        )

    if charger_select != SUPPORTED_CHARGER_SELECT:
        raise ValueError(
            f"queue.charger_select={charger_select!r} 은 구현되지 않았습니다 "
            f"(지원: {SUPPORTED_CHARGER_SELECT}). world/queue_rule.py 를 먼저 고칠 것."
        )


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StationSpec:
    """휴게소 한 곳과 그곳의 충전기들."""

    station_id: str
    lat: float
    lon: float
    chargers: tuple[Charger, ...]


@dataclass(frozen=True)
class EVArrival:
    """충전하러 휴게소에 도착한 차 한 대."""

    ev_id: str
    vclass_id: str
    station_id: str
    t_arrive_min: float
    soc_in: float
    soc_target: float
    battery_kwh: float
    vmax_kw: float
    curve: tuple[CurveSegment, ...]
    cold_factor: float = 1.0


@dataclass(frozen=True)
class SimResult:
    """writers.py 스키마 그대로의 레코드. 호출자가 Parquet 로 쓴다."""

    charge_events: tuple[dict, ...]
    snapshots: tuple[dict, ...]


def unit_id(charger_id: str, index: int) -> str:
    return f"{charger_id}{UNIT_SEP}{index}"


def db_charger_id(unit: str) -> str:
    """충전기 기 ID 에서 DB charger 행의 ID 로 되돌린다."""

    return unit.split(UNIT_SEP, 1)[0]


def expand_chargers(charger_rows: Iterable[dict]) -> tuple[Charger, ...]:
    """DB charger 행(= 같은 사양 n_units 기)을 개별 충전기로 펼친다.

    대수를 코드에 박지 않는다. 휴게소마다 다르고, 늘어나면 결과가 통째로 달라진다.
    `is_active` 가 0 인 행은 제외한다 — 없는 충전기로 대기를 계산하면 안 된다.
    """

    chargers: list[Charger] = []

    for row in charger_rows:
        if not int(row.get("is_active", 1)):
            continue

        n_units = int(row["n_units"])

        if n_units < 1:
            raise ValueError(f"n_units 는 1 이상이어야 합니다: {row['charger_id']} {n_units}")

        chargers.extend(
            Charger(
                charger_id=unit_id(str(row["charger_id"]), i),
                power_kw=float(row["power_kw"]),
            )
            for i in range(1, n_units + 1)
        )

    return tuple(chargers)


def station_specs(
    station_rows: Iterable[dict],
    charger_rows: Iterable[dict],
) -> tuple[StationSpec, ...]:
    """DB 에서 읽은 station/charger 행을 시뮬레이터 입력으로 바꾼다.

    충전기가 한 기도 없는 휴게소는 제외한다. queue_rule 이 충전기 0대를 예외로
    처리하므로(무한 대기 금지), 애초에 후보에서 빼는 것이 맞다.
    """

    by_station: dict[str, list[dict]] = {}

    for row in charger_rows:
        by_station.setdefault(str(row["station_id"]), []).append(row)

    specs: list[StationSpec] = []

    for row in station_rows:
        station_id = str(row["station_id"])
        chargers = expand_chargers(by_station.get(station_id, []))

        if not chargers:
            continue

        specs.append(
            StationSpec(
                station_id=station_id,
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                chargers=chargers,
            )
        )

    return tuple(specs)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


@dataclass
class _StationState:
    spec: StationSpec
    chargers: tuple[Charger, ...]
    n_waiting: int = 0
    n_charging: int = 0
    n_done: int = 0
    n_expected: int = 0
    events: list[dict] = field(default_factory=list)
    snapshots: list[dict] = field(default_factory=list)


def _to_arrival(ev: EVArrival) -> Arrival:
    return Arrival(
        ev_id=ev.ev_id,
        arrival_min=ev.t_arrive_min,
        soc_from=ev.soc_in,
        soc_to=ev.soc_target,
        battery_kwh=ev.battery_kwh,
        vmax_kw=ev.vmax_kw,
        curve=ev.curve,
        charge_power_factor=ev.cold_factor,
    )


def _charge_event(ev: EVArrival, a: Assignment) -> dict:
    charge_min = a.end_min - a.start_min

    return {
        "ev_id": ev.ev_id,
        "vclass_id": ev.vclass_id,
        "station_id": ev.station_id,
        "charger_id": a.charger_id,
        "power_kw": None,          # 아래에서 채운다 (배정된 기의 출력)
        "t_arrive_min": a.arrival_min,
        "t_start_min": a.start_min,
        "t_end_min": a.end_min,
        "wait_min": a.wait_min,
        "charge_min": charge_min,
        "dwell_min": a.wait_min + charge_min,
        "soc_in": ev.soc_in,
        "soc_out": ev.soc_target,
        "soc_target": ev.soc_target,
        "energy_kwh": (ev.soc_target - ev.soc_in) * ev.battery_kwh,
        "cold_factor": ev.cold_factor,
        # 재배정은 S3·S4 의 개념이다. DES 는 배정된 대로만 돌린다.
        "was_reassigned": False,
    }


def _snapshot_rows(state: _StationState, t_min: float) -> list[dict]:
    """설계 규칙 4 스키마 한 줄씩. 지표마다 한 행이라 컬럼이 늘지 않는다."""

    spec = state.spec
    values = {
        "wait_min": wait_if_arriving_now(state.chargers, [], t_min),
        "queue_len": float(state.n_waiting),
        "chargers_busy": float(state.n_charging),
        "chargers_total": float(len(state.chargers)),
    }

    return [
        {
            "t_min": float(t_min),
            "entity_type": "station",
            "entity_id": spec.station_id,
            "lat": spec.lat,
            "lon": spec.lon,
            "state": name,
            "value": values[name],
        }
        for name in STATION_SNAPSHOT_STATES
    ]


def _admit(
    env: simpy.Environment,
    state: _StationState,
    group: Sequence[EVArrival],
) -> None:
    """같은 시각에 도착한 차들을 한 묶음으로 배정한다.

    묶어서 한 번에 `assign` 을 부르는 이유: 한 대씩 부르면 SimPy 프로세스가 깨어나는
    순서에 따라 동시 도착의 순서가 달라지고, 그러면 원장이 배치로 투영한 결과와
    어긋난다. `assign` 은 동시 도착을 ev_id 순으로 고정한다.
    """

    assignments = assign([_to_arrival(ev) for ev in group], state.chargers)
    state.chargers = chargers_after(state.chargers, assignments)

    power_by_unit = {c.charger_id: c.power_kw for c in state.chargers}
    ev_by_id = {ev.ev_id: ev for ev in group}

    for a in assignments:
        state.n_waiting += 1
        env.process(_charge_process(env, state, ev_by_id[a.ev_id], a, power_by_unit))


def _charge_process(
    env: simpy.Environment,
    state: _StationState,
    ev: EVArrival,
    a: Assignment,
    power_by_unit: dict[str, float],
):
    """한 대의 대기 → 충전 → 종료. 시각은 queue_rule 이 이미 정했다."""

    if a.wait_min > 0:
        yield env.timeout(a.wait_min)

    state.n_waiting -= 1
    state.n_charging += 1

    yield env.timeout(a.end_min - a.start_min)

    state.n_charging -= 1
    state.n_done += 1

    row = _charge_event(ev, a)
    row["power_kw"] = power_by_unit[a.charger_id]
    state.events.append(row)


def _arrival_feed(
    env: simpy.Environment,
    state: _StationState,
    arrivals: Sequence[EVArrival],
):
    """도착 시각 순으로 묶어서 흘려보낸다."""

    ordered = sorted(arrivals, key=lambda ev: (ev.t_arrive_min, ev.ev_id))

    for t_arrive, group in groupby(ordered, key=lambda ev: ev.t_arrive_min):
        yield env.timeout(t_arrive - env.now)
        _admit(env, state, list(group))


def _snapshot_loop(
    env: simpy.Environment,
    state: _StationState,
    every_min: float,
):
    """일정 간격으로 휴게소 상태를 찍는다. 전부 끝나면 마지막 한 장을 찍고 멈춘다.

    horizon 을 인자로 받지 않는 이유: 언제 끝날지는 큐가 정한다. 남은 차 수로
    종료를 판단하면 시나리오마다 끝 시각을 손으로 맞출 필요가 없다.

    시각은 `env.now` 가 아니라 **격자값 tick × every_min** 을 쓴다. `env.now` 는
    timeout 을 더해 나간 값이라 간격이 0.1 처럼 2진수로 안 떨어지는 값이면 오차가
    누적된다. 하루치를 돌리면 스냅샷이 격자에서 조금씩 밀리고, 시간대별 집계에서
    경계에 걸친 행이 옆 칸으로 넘어간다. 곱셈 한 번이면 그럴 일이 없다.

    (값 자체의 부동소수 오차는 다른 얘기다. SoC 0.2→0.7 이 0.49999999999999994 라
    충전시간이 29.999999999999996 분으로 나오는 것은 정상이고 반올림하지 않는다.)
    """

    tick = 0
    state.snapshots.extend(_snapshot_rows(state, 0.0))

    while state.n_done < state.n_expected:
        tick += 1
        target_min = tick * every_min
        yield env.timeout(target_min - env.now)
        state.snapshots.extend(_snapshot_rows(state, target_min))


def run_charging_des(
    stations: Sequence[StationSpec],
    arrivals: Sequence[EVArrival],
    *,
    snapshot_every_min: float = DEFAULT_SNAPSHOT_EVERY_MIN,
) -> SimResult:
    """충전 큐를 돌리고 charge_event 와 스냅샷을 돌려준다.

    같은 입력이면 같은 결과다. 난수를 쓰지 않는다 — 도착 목록을 만드는 쪽이
    시드를 갖는다 (io/synthetic_ev.py). 그래야 시드 고정 재현성이 도착 생성과
    큐 동작 어느 쪽에서 깨졌는지 구분된다.
    """

    if snapshot_every_min <= 0:
        raise ValueError(f"스냅샷 간격은 0보다 커야 합니다: {snapshot_every_min}")

    known = {s.station_id for s in stations}
    unknown = sorted({ev.station_id for ev in arrivals} - known)

    if unknown:
        raise ValueError(
            "충전기가 있는 휴게소가 아닌 곳으로 도착이 배정됐습니다: "
            f"{unknown}. 배정 단계에서 후보를 잘못 고른 것입니다."
        )

    for ev in arrivals:
        if ev.soc_target <= ev.soc_in:
            raise ValueError(
                f"충전할 것이 없는 차가 충전소에 도착했습니다: {ev.ev_id} "
                f"({ev.soc_in} → {ev.soc_target})"
            )

    env = simpy.Environment()
    states = [
        _StationState(spec=spec, chargers=tuple(spec.chargers)) for spec in stations
    ]

    by_station: dict[str, list[EVArrival]] = {s.spec.station_id: [] for s in states}

    for ev in arrivals:
        by_station[ev.station_id].append(ev)

    for state in states:
        state.n_expected = len(by_station[state.spec.station_id])
        env.process(_snapshot_loop(env, state, snapshot_every_min))

        if state.n_expected:
            env.process(_arrival_feed(env, state, by_station[state.spec.station_id]))

    env.run()

    events = [row for state in states for row in state.events]
    snapshots = [row for state in states for row in state.snapshots]

    events.sort(key=lambda r: (r["t_end_min"], r["ev_id"]))
    snapshots.sort(key=lambda r: (r["t_min"], r["entity_id"], r["state"]))

    return SimResult(charge_events=tuple(events), snapshots=tuple(snapshots))
