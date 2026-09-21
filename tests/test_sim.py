"""휴게소 충전 큐 DES (T-16).

대기시간이 나오는 곳이라, 여기가 틀리면 KPI 도 한계 외부비용도 전부 틀린다.
큐 판단이 정말 queue_rule 에서 오는지도 같이 검사한다 (설계 규칙 1).
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pyarrow as pa
import pytest

from evdt.io.writers import SCHEMAS
from evdt.world.queue_rule import Charger
from evdt.world.sim import (
    STATION_SNAPSHOT_STATES,
    EVArrival,
    StationSpec,
    check_queue_config,
    db_charger_id,
    expand_chargers,
    run_charging_des,
    station_specs,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "evdt"

#: 출력 제한이 차량·충전기에서만 걸리도록 충분히 높은 평탄 곡선
FLAT_CURVE = ((0.0, 1.0, 10_000.0),)

#: 이 헬퍼로 만든 차는 정확히 service_min 분을 쓴다 (충전기가 vmax 이상일 때)
DELTA_SOC = 0.5


def _ev(
    ev_id: str,
    t_arrive_min: float,
    service_min: float,
    *,
    station_id: str = "s1",
    vmax_kw: float = 100.0,
) -> EVArrival:
    return EVArrival(
        ev_id=ev_id,
        vclass_id="vc",
        station_id=station_id,
        t_arrive_min=t_arrive_min,
        soc_in=0.25,
        soc_target=0.25 + DELTA_SOC,
        battery_kwh=service_min * vmax_kw / 60.0 / DELTA_SOC,
        vmax_kw=vmax_kw,
        curve=FLAT_CURVE,
    )


def _station(n_units: int, *, station_id: str = "s1", power_kw: float = 200.0) -> StationSpec:
    return StationSpec(
        station_id=station_id,
        lat=36.5,
        lon=127.5,
        chargers=expand_chargers(
            [{"charger_id": f"{station_id}_dc", "power_kw": power_kw, "n_units": n_units}]
        ),
    )


def _series(result, station_id: str, state: str) -> list[tuple[float, float]]:
    return [
        (r["t_min"], r["value"])
        for r in result.snapshots
        if r["entity_id"] == station_id and r["state"] == state
    ]


# ---------------------------------------------------------------------------
# 충전기 대수는 DB 에서 온다
# ---------------------------------------------------------------------------


def test_charger_rows_expand_into_individual_units():
    """charger 한 행 = 같은 사양 n_units 기. 대수를 코드에 박지 않는다."""

    chargers = expand_chargers(
        [
            {"charger_id": "anseong_200", "power_kw": 200.0, "n_units": 3},
            {"charger_id": "anseong_100", "power_kw": 100.0, "n_units": 1},
        ]
    )

    assert [c.charger_id for c in chargers] == [
        "anseong_200#1",
        "anseong_200#2",
        "anseong_200#3",
        "anseong_100#1",
    ]
    assert [c.power_kw for c in chargers] == [200.0, 200.0, 200.0, 100.0]
    assert db_charger_id("anseong_200#2") == "anseong_200"


def test_inactive_chargers_are_excluded():
    """없는 충전기로 대기를 계산하면 안 된다."""

    chargers = expand_chargers(
        [
            {"charger_id": "a", "power_kw": 200.0, "n_units": 2, "is_active": 0},
            {"charger_id": "b", "power_kw": 100.0, "n_units": 1, "is_active": 1},
        ]
    )

    assert [c.charger_id for c in chargers] == ["b#1"]


def test_station_without_chargers_is_dropped():
    specs = station_specs(
        station_rows=[
            {"station_id": "has", "lat": 36.0, "lon": 127.0},
            {"station_id": "none", "lat": 36.1, "lon": 127.1},
        ],
        charger_rows=[{"charger_id": "c", "station_id": "has", "power_kw": 100.0, "n_units": 2}],
    )

    assert [s.station_id for s in specs] == ["has"]
    assert len(specs[0].chargers) == 2


def test_zero_units_is_rejected():
    with pytest.raises(ValueError, match="n_units"):
        expand_chargers([{"charger_id": "c", "power_kw": 100.0, "n_units": 0}])


def test_charger_count_comes_from_the_database(seeded_db):
    """대수는 DB charger.n_units 에서 온다. DB → DES 경로를 통째로 지난다."""

    from evdt.io.db import get_conn
    from evdt.io.stations import read_station_chargers

    with get_conn(seeded_db, readonly=True) as conn:
        station_rows, charger_rows = read_station_chargers(conn, corridor_id="gyeongbu_down")

    assert [r["n_units"] for r in charger_rows] == [4]

    specs = station_specs(station_rows, charger_rows)

    assert [s.station_id for s in specs] == ["st_anseong"]
    assert len(specs[0].chargers) == 4
    assert specs[0].lat == pytest.approx(37.0075)

    # 4기가 실제로 병렬로 쓰인다 — 4대가 동시에 와도 아무도 안 기다린다
    evs = [_ev(f"ev{i}", 0.0, 30.0, station_id="st_anseong") for i in range(4)]
    result = run_charging_des(specs, evs)

    assert [e["wait_min"] for e in result.charge_events] == [0.0] * 4
    assert len({e["charger_id"] for e in result.charge_events}) == 4

    # 5대째는 기다린다 (대수를 4보다 크게 박아뒀다면 여기서 깨진다)
    result5 = run_charging_des(specs, [*evs, _ev("ev4", 0.0, 30.0, station_id="st_anseong")])

    assert max(e["wait_min"] for e in result5.charge_events) == pytest.approx(30.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 완료조건 — 이벤트 시각
# ---------------------------------------------------------------------------


def test_event_times_are_ordered():
    """t_end > t_start >= t_arrive 가 전부 성립한다.

    t_start 는 t_arrive 와 **같을 수 있다** — 빈 충전소에 도착하면 대기가 0이다.
    여기에 강부등호를 걸면 정상 동작이 실패로 잡힌다. 강부등호가 성립해야 하는
    것은 t_end > t_start 쪽이고, 그건 충전할 것이 없는 차를 입력에서 막기 때문에
    (run_charging_des 의 soc 검사) 항상 참이다.
    """

    evs = [_ev(f"ev{i:02d}", float(i % 7) * 3.0, 20.0 + i) for i in range(20)]
    result = run_charging_des([_station(2)], evs)

    assert len(result.charge_events) == len(evs)

    for e in result.charge_events:
        assert e["t_end_min"] > e["t_start_min"] >= e["t_arrive_min"]
        assert e["wait_min"] == pytest.approx(e["t_start_min"] - e["t_arrive_min"])
        assert e["charge_min"] == pytest.approx(e["t_end_min"] - e["t_start_min"])
        assert e["dwell_min"] == pytest.approx(e["wait_min"] + e["charge_min"])
        assert e["wait_min"] >= 0.0


def test_empty_station_gives_zero_wait():
    result = run_charging_des([_station(4)], [_ev("a", 0.0, 30.0)])

    (event,) = result.charge_events
    assert event["wait_min"] == 0.0
    assert event["t_start_min"] == event["t_arrive_min"]


def test_car_with_nothing_to_charge_is_rejected():
    """충전할 것이 없는 차가 충전소에 있으면 t_end == t_start 가 된다. 막는다."""

    ev = EVArrival("a", "vc", "s1", 0.0, 0.6, 0.6, 80.0, 200.0, FLAT_CURVE)

    with pytest.raises(ValueError, match="충전할 것이 없는"):
        run_charging_des([_station(2)], [ev])


def test_arrival_at_unknown_station_is_rejected():
    with pytest.raises(ValueError, match="휴게소가 아닌 곳"):
        run_charging_des([_station(2)], [_ev("a", 0.0, 30.0, station_id="nowhere")])


# ---------------------------------------------------------------------------
# 완료조건 — 안정 / 포화
# ---------------------------------------------------------------------------


def _poisson_arrivals(seed: int, mean_gap_min: float, n: int) -> list[EVArrival]:
    """지수 간격 도착. 한 대당 30분 충전이므로 충전기 1기의 서비스율은 2대/시간."""

    rng = random.Random(seed)
    evs: list[EVArrival] = []
    t = 0.0

    for i in range(n):
        t += rng.expovariate(1.0 / mean_gap_min)
        evs.append(_ev(f"ev{i:04d}", round(t, 3), 30.0))

    return evs


@pytest.mark.parametrize("seed", [7, 11, 99])
def test_wait_does_not_blow_up_when_arrivals_are_slower_than_service(seed):
    """도착률 < 서비스율 이면 대기시간이 발산하지 않는다.

    충전기 1기, 한 대당 30분 → 서비스율 2대/시간. 도착은 평균 40분 간격 = 1.5대/시간
    이므로 이용률 0.75 다.

    도착을 20분마다 딱딱 끊어 넣으면 대기가 아예 0 이라 아무것도 검사하지 못한다
    (처음에 그렇게 썼다가 전부 0 인 것을 보고 고쳤다). 대기열이 생기려면 도착이
    몰렸다 뜸했다 해야 한다.

    "발산하지 않는다" 를 무엇으로 볼 것인가
        평균이 얼마 이하라는 식의 기준은 분포와 시드에 흔들린다. 안정된 큐의 성질은
        **반복해서 비워진다** 는 것이다. 발산하는 큐는 초반 이후 다시는 안 빈다.
    """

    evs = _poisson_arrivals(seed, mean_gap_min=40.0, n=400)
    result = run_charging_des([_station(1)], evs, snapshot_every_min=10.0)

    queue = [v for _, v in _series(result, "s1", "queue_len")]

    # 관측: 시드별로 41~57% 가 빈 상태였다
    assert queue.count(0.0) > len(queue) * 0.25
    # 끝부분에서도 여전히 비는 순간이 있다 — 발산하면 여기가 깨진다
    assert 0.0 in queue[int(len(queue) * 0.75):]

    waits = [e["wait_min"] for e in result.charge_events]
    first = sum(waits[:100]) / 100
    last = sum(waits[-100:]) / 100

    # 대기가 있기는 해야 검사에 의미가 있다
    assert first > 0.0
    # 선형으로 늘어나지 않는다 (관측 비율 0.8~1.3배)
    assert last < 3.0 * first


def test_queue_grows_without_bound_when_arrivals_outpace_service():
    """도착률 > 서비스율 이면 큐가 단조증가한다.

    충전기 1기, 한 대당 30분 → 서비스율 2대/시간. 도착은 10분에 한 대 = 6대/시간.
    """

    evs = [_ev(f"ev{i:03d}", i * 10.0, 30.0) for i in range(30)]
    result = run_charging_des([_station(1)], evs, snapshot_every_min=10.0)

    queue = _series(result, "s1", "queue_len")
    last_arrival_min = evs[-1].t_arrive_min

    growing = [v for t, v in queue if t <= last_arrival_min]

    # 도착이 이어지는 동안 큐는 줄어들지 않는다
    assert all(b >= a for a, b in zip(growing, growing[1:], strict=False))
    assert growing[-1] > growing[0]

    # 안정된 큐와 달리 초반 이후로는 다시 비지 않는다 (위 안정성 테스트의 반대)
    assert 0.0 not in growing[3:]

    # 대기시간도 뒤로 갈수록 길어진다
    waits = [e["wait_min"] for e in result.charge_events]
    assert waits[-1] > waits[0] + 100.0


# ---------------------------------------------------------------------------
# 완료조건 — 재현성
# ---------------------------------------------------------------------------


def _random_arrivals(seed: int, n: int = 120) -> list[EVArrival]:
    rng = random.Random(seed)

    return [
        _ev(
            f"ev{i:04d}",
            round(rng.uniform(0.0, 600.0), 3),
            round(rng.uniform(15.0, 45.0), 3),
            station_id=rng.choice(["s1", "s2"]),
        )
        for i in range(n)
    ]


def test_same_seed_gives_identical_runs():
    """시드 고정 재현성. DES 자체는 난수를 쓰지 않으므로 도착이 같으면 결과가 같다."""

    stations = [_station(2), _station(3, station_id="s2")]

    first = run_charging_des(stations, _random_arrivals(42))
    second = run_charging_des(stations, _random_arrivals(42))

    assert first.charge_events == second.charge_events
    assert first.snapshots == second.snapshots


def test_different_seed_gives_a_different_run():
    stations = [_station(2), _station(3, station_id="s2")]

    assert run_charging_des(stations, _random_arrivals(42)).charge_events != run_charging_des(
        stations, _random_arrivals(43)
    ).charge_events


def test_input_order_does_not_change_the_result():
    """도착 목록을 뒤집어 넣어도 같은 결과. 큐 규칙이 FIFO 를 고정한다."""

    stations = [_station(2)]
    evs = [_ev(f"ev{i:02d}", float(i % 5) * 4.0, 25.0) for i in range(15)]

    assert run_charging_des(stations, evs) == run_charging_des(stations, list(reversed(evs)))


# ---------------------------------------------------------------------------
# 출력 스키마 — writers.py 와 스냅샷 계약
# ---------------------------------------------------------------------------


def test_charge_event_rows_match_the_writer_schema():
    """writers.py 스키마 그대로여야 한다. 컬럼이 하나라도 다르면 여기서 잡는다."""

    result = run_charging_des([_station(2)], [_ev("a", 0.0, 30.0), _ev("b", 0.0, 20.0)])
    schema = SCHEMAS["charge_event"]
    expected = set(schema.names) - {"run_id"}

    for row in result.charge_events:
        assert set(row) == expected

    # 실제로 그 스키마로 Parquet 테이블이 만들어지는지까지 확인한다
    filled = [{"run_id": "t", **row} for row in result.charge_events]
    table = pa.Table.from_pylist(filled, schema=schema)

    assert table.num_rows == 2


def test_snapshot_rows_match_the_writer_schema():
    result = run_charging_des([_station(2)], [_ev("a", 0.0, 30.0)])
    schema = SCHEMAS["snapshot"]
    expected = set(schema.names) - {"run_id"}

    for row in result.snapshots:
        assert set(row) == expected
        assert row["entity_type"] == "station"
        assert row["state"] in STATION_SNAPSHOT_STATES

    table = pa.Table.from_pylist(
        [{"run_id": "t", **row} for row in result.snapshots], schema=schema
    )

    assert table.num_rows == len(result.snapshots)


def test_snapshot_carries_every_agreed_state():
    result = run_charging_des([_station(3)], [_ev("a", 0.0, 30.0)])

    at_zero = {r["state"]: r["value"] for r in result.snapshots if r["t_min"] == 0.0}

    assert set(at_zero) == set(STATION_SNAPSHOT_STATES)
    assert at_zero["chargers_total"] == 3.0
    assert at_zero["wait_min"] == 0.0


def test_snapshots_sit_on_an_exact_time_grid():
    """t_min 이 격자에서 밀리면 시간대별 집계에서 경계 행이 옆 칸으로 넘어간다."""

    evs = [_ev(f"ev{i:02d}", i * 7.0, 33.0) for i in range(12)]
    result = run_charging_des([_station(2)], evs, snapshot_every_min=0.1)

    times = sorted({r["t_min"] for r in result.snapshots})

    for k, t in enumerate(times):
        assert t == pytest.approx(k * 0.1, abs=1e-12)


def test_snapshot_lat_lon_comes_from_the_station():
    """설계 규칙 3 — 렌더링이 좌표를 다시 찾지 않아도 되게 같이 싣는다."""

    result = run_charging_des([_station(2)], [_ev("a", 0.0, 30.0)])

    assert {(r["lat"], r["lon"]) for r in result.snapshots} == {(36.5, 127.5)}


def test_snapshot_interval_must_be_positive():
    with pytest.raises(ValueError, match="스냅샷 간격"):
        run_charging_des([_station(2)], [_ev("a", 0.0, 30.0)], snapshot_every_min=0.0)


def test_queue_config_must_match_the_implementation():
    """설정에 적힌 규칙과 실제로 돈 규칙이 다르면 결과를 해석할 수 없다."""

    check_queue_config("FIFO", "MAX_POWER_IDLE")   # 현재 시나리오 값

    with pytest.raises(ValueError, match="discipline"):
        check_queue_config("LIFO", "MAX_POWER_IDLE")

    with pytest.raises(ValueError, match="charger_select"):
        check_queue_config("FIFO", "NEAREST")


def test_scenario_file_asks_for_the_implemented_rule():
    """config/scenario_seollal_down.yaml 이 실제로 구현된 규칙을 요구하는지 (하드코딩 아님)."""

    from evdt.config import ScenarioConfig
    from evdt.paths import CONFIG_DIR

    cfg = ScenarioConfig.from_yaml(CONFIG_DIR / "scenario_seollal_down.yaml")

    check_queue_config(cfg.queue.discipline, cfg.queue.charger_select)


# ---------------------------------------------------------------------------
# 큐 판단이 정말 queue_rule 에서 오는가
# ---------------------------------------------------------------------------


def test_higher_power_charger_is_used_first():
    """유휴 중 최고출력 — queue_rule 규칙 2 가 DES 를 통해서도 지켜진다."""

    station = StationSpec(
        station_id="s1",
        lat=36.5,
        lon=127.5,
        chargers=(Charger("slow#1", 50.0), Charger("fast#1", 350.0)),
    )

    (event,) = run_charging_des([station], [_ev("a", 0.0, 30.0, vmax_kw=50.0)]).charge_events

    assert event["charger_id"] == "fast#1"
    assert event["power_kw"] == 350.0


def test_sim_does_not_use_simpy_resource():
    """SimPy Resource 를 쓰면 그 안의 FIFO 가 두 번째 큐 규칙이 된다.

    우리 규칙은 "유휴 중 최고출력 / 전부 사용 중이면 최단 해제" 라서 Resource 의
    규칙과 다르고, 원장은 Resource 를 쓸 수 없으니 둘이 갈라진다 (설계 규칙 1).
    """

    source = (SRC / "world" / "sim.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "simpy"
    }

    assert used <= {"Environment"}, f"sim.py 가 SimPy 의 {sorted(used - {'Environment'})} 를 쓴다"


def test_sim_actually_calls_the_queue_rule():
    """시뮬레이터가 큐 규칙을 실제로 부르는지 (설계 규칙 1).

    "호출자가 sim.py 하나뿐" 으로 검사하지 않는다. Sprint 2 에 예약 원장이 두 번째
    호출자로 들어오면 정상 변경인데도 깨진다. 호출자 상한(sim·ledger 둘)은
    tests/test_queue_rule.py 가 지킨다.
    """

    callers = set()

    for py in sorted(SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "evdt.world.queue_rule":
                callers.add(py.relative_to(SRC).as_posix())

    assert "world/sim.py" in callers, f"sim.py 가 큐 규칙을 부르지 않는다. 호출자: {sorted(callers)}"
