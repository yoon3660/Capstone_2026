"""이벤트 로거와 Parquet 출력 (설계문서 T-17 · 이슈 #41).

완료 기준: DuckDB 로 "휴게소별 시간대별 평균 대기" 를 SQL 한 줄로 추출.
그 한 줄이 손으로 계산한 값과 같은지, 틀린 행이 로그에 못 들어가는지를 본다.
"""

from __future__ import annotations

import math

import duckdb
import pytest

from evdt.interfaces import SNAPSHOT_COLUMNS, SNAPSHOT_STATES
from evdt.io.event_log import (
    SQL_DIR,
    EventLogError,
    charge_event_problems,
    check_charge_events,
    check_snapshots,
    load_sql,
    log_sim_result,
    snapshot_row_problems,
)
from evdt.io.loaders import duck_connect
from evdt.io.writers import SCHEMAS, ParquetRunWriter
from evdt.world.sim import (
    STATION_SNAPSHOT_STATES,
    EVArrival,
    StationSpec,
    expand_chargers,
    run_charging_des,
)

FLAT_CURVE = ((0.0, 1.0, 10_000.0),)
DELTA_SOC = 0.5


def _ev(ev_id: str, t_arrive_min: float, service_min: float, *, station_id: str = "s1") -> EVArrival:
    vmax_kw = 100.0
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


def _station(n_units: int, *, station_id: str = "s1") -> StationSpec:
    return StationSpec(
        station_id=station_id,
        lat=36.5,
        lon=127.5,
        chargers=expand_chargers(
            [{"charger_id": f"{station_id}_dc", "power_kw": 200.0, "n_units": n_units}]
        ),
    )


def _write_run(runs_dir, run_id: str, result, **kw) -> dict[str, int]:
    with ParquetRunWriter(runs_dir / run_id, run_id) as w:
        return log_sim_result(w, result.charge_events, result.snapshots, **kw)


def _duck(tmp_path, *run_ids: str) -> duckdb.DuckDBPyConnection:
    # SQLite 가 없는 경로를 준다 — 이 쿼리는 Parquet 만으로 돌아야 한다
    return duck_connect(db_path=tmp_path / "none.db", run_ids=list(run_ids), runs_dir=tmp_path)


#: 한 충전기 · 30분 충전 · 도착 10, 20, 59.9, 60.0 분.
#:   10.0 → 즉시 시작, 40 에 끝      대기 0
#:   20.0 → 40 에 시작, 70 에 끝     대기 20
#:   59.9 → 70 에 시작, 100 에 끝    대기 10.1   ← 0시 칸 (반올림하면 1시로 넘어간다)
#:   60.0 → 100 에 시작, 130 에 끝   대기 40     ← 1시 칸
HAND_ARRIVALS = (("a", 10.0), ("b", 20.0), ("c", 59.9), ("d", 60.0))
HAND_HOUR0_MEAN = (0.0 + 20.0 + 10.1) / 3
HAND_HOUR1_MEAN = 40.0


def _hand_result():
    return run_charging_des([_station(1)], [_ev(i, t, 30.0) for i, t in HAND_ARRIVALS])


# ---------------------------------------------------------------------------
# 계약은 한 곳에
# ---------------------------------------------------------------------------


def test_writer_snapshot_schema_follows_the_interfaces_contract():
    names = tuple(n for n in SCHEMAS["snapshot"].names if n != "run_id")
    assert names == SNAPSHOT_COLUMNS


def test_sim_station_states_are_the_interfaces_contract():
    """sim 이 따로 목록을 들면 로거와 렌더러가 보는 목록과 갈라진다."""
    assert STATION_SNAPSHOT_STATES is SNAPSHOT_STATES["station"]


def test_des_output_passes_the_logger_checks():
    evs = [_ev(f"ev{i:02d}", i * 3.0, 25.0, station_id=f"s{i % 2}") for i in range(20)]
    result = run_charging_des([_station(2, station_id="s0"), _station(1, station_id="s1")], evs)

    check_charge_events(result.charge_events)
    check_snapshots(result.snapshots)


# ---------------------------------------------------------------------------
# 스냅샷 검사
# ---------------------------------------------------------------------------


def _snap(**over) -> dict:
    row = {
        "t_min": 5.0, "entity_type": "station", "entity_id": "s1",
        "lat": 36.5, "lon": 127.5, "state": "wait_min", "value": 3.0,
    }
    row.update(over)
    return row


def test_valid_snapshot_row_has_no_problems():
    assert snapshot_row_problems(_snap()) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"state": "wait_mins"}, "state='wait_mins'"),          # 오타 — 조용히 저장되면 렌더러에서 빈 칸
        ({"entity_type": "stations"}, "entity_type='stations'"),
        ({"entity_type": "cell", "state": "density"}, "entity_type='cell'"),  # 아직 계약 없음
        ({"lat": 127.5, "lon": 36.5}, "lat=127.5"),              # 뒤바뀐 좌표
        ({"lat": None}, "lat=None"),
        ({"lon": math.nan}, "lon=nan"),
        ({"value": math.inf}, "value=inf"),
        ({"t_min": -1.0}, "t_min=-1.0"),
        ({"entity_id": ""}, "entity_id"),
    ],
)
def test_bad_snapshot_rows_are_named(over, needle):
    problems = snapshot_row_problems(_snap(**over))
    assert any(needle in p for p in problems), problems


def test_snapshot_with_extra_column_is_rejected():
    assert snapshot_row_problems({**_snap(), "speed": 1.0})


# ---------------------------------------------------------------------------
# 충전 이벤트 검사
# ---------------------------------------------------------------------------


def _event(**over) -> dict:
    row = dict(_hand_result().charge_events[1])   # b: 20 도착, 40 시작, 70 종료
    row.update(over)
    return row


def test_valid_charge_event_has_no_problems():
    assert charge_event_problems(_event()) == []


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"t_start_min": 10.0, "wait_min": -10.0, "charge_min": 60.0}, "도착(20.0) 전에"),
        ({"wait_min": 0.0}, "wait_min=0.0"),                    # 음수·0 대기로 평균이 내려간다
        ({"charge_min": 1.0}, "charge_min=1.0"),
        ({"dwell_min": 1.0}, "dwell_min"),
        ({"t_end_min": 40.0}, "종료(40.0)"),
        ({"soc_out": 0.1}, "SoC"),
        ({"power_kw": None}, "power_kw"),
        ({"t_arrive_min": math.nan}, "유한값"),
        ({"station_id": ""}, "station_id"),
    ],
)
def test_bad_charge_events_are_named(over, needle):
    problems = charge_event_problems(_event(**over))
    assert any(needle in p for p in problems), problems


def test_duplicate_ev_is_rejected():
    rows = [_event(), _event()]
    with pytest.raises(EventLogError, match="두 번 충전"):
        check_charge_events(rows)


def test_error_message_is_capped():
    rows = [_event(ev_id=f"e{i}", wait_min=-1.0) for i in range(50)]
    with pytest.raises(EventLogError) as info:
        check_charge_events(rows)
    msg = str(info.value)
    assert "50행 중 50행" in msg
    assert "외 45행" in msg


# ---------------------------------------------------------------------------
# 쓰기 — 틀리면 한 줄도 안 쓴다
# ---------------------------------------------------------------------------


def test_nothing_is_written_when_any_snapshot_is_bad(tmp_path):
    result = _hand_result()
    snaps = list(result.snapshots)
    snaps[-1] = {**snaps[-1], "state": "wait_mins"}

    with ParquetRunWriter(tmp_path, "r") as w:
        with pytest.raises(EventLogError, match="snapshot"):
            log_sim_result(w, result.charge_events, snaps)
        assert w.counts["charge_event"] == 0      # 충전 이벤트는 멀쩡해도 쓰지 않았다

    assert not (tmp_path / "charge_event.parquet").exists()


def test_snapshots_are_checked_even_when_not_written(tmp_path):
    result = _hand_result()
    snaps = [{**r, "lat": r["lon"], "lon": r["lat"]} for r in result.snapshots]

    with ParquetRunWriter(tmp_path, "r") as w:
        with pytest.raises(EventLogError):
            log_sim_result(w, result.charge_events, snaps, write_snapshots=False)


def test_write_snapshots_false_skips_the_file(tmp_path):
    counts = _write_run(tmp_path, "r", _hand_result(), write_snapshots=False)

    assert counts == {"charge_event": 4, "snapshot": 0}
    assert (tmp_path / "r" / "charge_event.parquet").is_file()
    assert not (tmp_path / "r" / "snapshot.parquet").exists()


# ---------------------------------------------------------------------------
# 완료 기준 — SQL 한 줄
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["station_hourly_wait", "station_hourly_shown_wait"])
def test_saved_query_is_one_line(name):
    """주석을 빼면 한 줄. 분석하는 사람이 복사해서 바로 쓴다."""
    lines = [ln for ln in load_sql(name).splitlines() if ln.strip() and not ln.startswith("--")]
    assert len(lines) == 1


def test_station_hourly_wait_matches_hand_calculation(tmp_path):
    _write_run(tmp_path, "r1", _hand_result())

    rows = _duck(tmp_path, "r1").sql(load_sql("station_hourly_wait")).fetchall()

    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [("r1", "s1", 0, 3), ("r1", "s1", 1, 1)]
    assert rows[0][4] == pytest.approx(HAND_HOUR0_MEAN)
    assert rows[1][4] == pytest.approx(HAND_HOUR1_MEAN)


def test_hour_bucket_floors_instead_of_rounding():
    """DuckDB 의 CAST(59.9/60 AS INTEGER) 는 1 이다 — 반올림한다.

    예전 smoke_run 쿼리가 그렇게 해서 9:45 도착이 10시 칸에 들어갔다. 저장된 쿼리는 FLOOR.
    """
    assert duckdb.sql("SELECT CAST(59.9 / 60 AS INTEGER)").fetchone()[0] == 1
    assert "FLOOR(t_arrive_min / 60)" in load_sql("station_hourly_wait")


def test_query_keeps_runs_apart(tmp_path):
    """여러 run 을 한꺼번에 올려도 run_id 로 갈린다. 시드 10회를 한 쿼리로 본다."""
    _write_run(tmp_path, "r1", _hand_result())
    _write_run(
        tmp_path, "r2",
        run_charging_des([_station(4)], [_ev(i, t, 30.0) for i, t in HAND_ARRIVALS]),
    )

    rows = _duck(tmp_path, "r1", "r2").sql(load_sql("station_hourly_wait")).fetchall()
    by_run = {(r[0], r[2]): r[4] for r in rows}

    assert by_run[("r1", 0)] == pytest.approx(HAND_HOUR0_MEAN)
    assert by_run[("r2", 0)] == 0.0            # 4기면 아무도 안 기다린다
    assert by_run[("r2", 1)] == 0.0


def test_shown_wait_query_reads_the_snapshot_stream(tmp_path):
    """스냅샷은 5분 격자라 0시 칸에 12장. 차가 없는 시각도 0 으로 행이 있다."""
    result = _hand_result()
    _write_run(tmp_path, "r1", result)

    rows = _duck(tmp_path, "r1").sql(load_sql("station_hourly_shown_wait")).fetchall()
    hour0 = next(r for r in rows if r[2] == 0)

    shown = [
        r["value"] for r in result.snapshots
        if r["state"] == "wait_min" and r["t_min"] < 60.0
    ]
    assert hour0[3] == 12
    assert hour0[4] == pytest.approx(sum(shown) / len(shown))


def test_snapshot_parquet_carries_coordinates(tmp_path):
    """위경도가 스냅샷에 들어 있어야 렌더러가 master DB 없이 지도에 찍는다."""
    _write_run(tmp_path, "r1", _hand_result())

    lat, lon, n_null = _duck(tmp_path, "r1").sql(
        "SELECT MIN(lat), MIN(lon), COUNT(*) FILTER (WHERE lat IS NULL OR lon IS NULL) FROM snapshot"
    ).fetchone()

    assert (lat, lon, n_null) == (36.5, 127.5, 0)


def test_load_sql_lists_what_exists():
    with pytest.raises(FileNotFoundError, match="station_hourly_wait"):
        load_sql("no_such_query")
    assert (SQL_DIR / "station_hourly_wait.sql").is_file()
