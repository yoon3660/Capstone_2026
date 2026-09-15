"""T-04 완료 조건: 더미 실행이 run 에 등록되고 Parquet 생성, DuckDB 조회 성공."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from evdt.config import ScenarioConfig
from evdt.io.db import get_conn, read_table, upsert_df
from evdt.io.loaders import duck_connect
from evdt.io.run_registry import RunContext, RunExistsError, make_run_id, register_run


@pytest.fixture
def ready_db(seeded_db: Path) -> Path:
    """corridor 가 이미 들어 있는 DB (scenario FK 를 만족시키기 위해)."""
    return seeded_db


def test_run_id_is_deterministic() -> None:
    a = make_run_id("sc", "UE", 7, 1.0)
    b = make_run_id("sc", "UE", 7, 1.0)
    assert a == b == "sc__UE__p100__s0007"
    assert make_run_id("sc", "S4", 12, 0.6) == "sc__S4__p060__s0012"


def test_register_creates_row_and_directory(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    runs = tmp_path / "runs"
    handle = register_run(cfg, seed=7, db_path=ready_db, runs_dir=runs)

    assert handle.output_dir.is_dir()
    assert (handle.output_dir / "meta.json").is_file()
    assert (handle.output_dir / "config.yaml").read_text(encoding="utf-8") == cfg.raw_yaml

    with get_conn(ready_db, readonly=True) as conn:
        runs_df = read_table(conn, "run")
        scen_df = read_table(conn, "scenario")
    assert len(runs_df) == 1
    assert runs_df.iloc[0]["status"] == "RUNNING"
    assert runs_df.iloc[0]["code_version"]          # git SHA 또는 'nogit'
    assert scen_df.iloc[0]["config_yaml"] == cfg.raw_yaml


def test_duplicate_run_is_rejected(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    runs = tmp_path / "runs"
    register_run(cfg, seed=7, db_path=ready_db, runs_dir=runs)
    with pytest.raises(RunExistsError):
        register_run(cfg, seed=7, db_path=ready_db, runs_dir=runs)
    # 다른 시드는 문제없이 통과
    register_run(cfg, seed=8, db_path=ready_db, runs_dir=runs)
    # 명시적 덮어쓰기는 허용
    register_run(cfg, seed=7, db_path=ready_db, runs_dir=runs, overwrite=True)


def test_missing_corridor_gives_actionable_error(cfg: ScenarioConfig, db_path, tmp_path) -> None:
    with pytest.raises(RunExistsError, match="seed_corridor"):
        register_run(cfg, seed=1, db_path=db_path, runs_dir=tmp_path / "runs")


def test_context_marks_done_and_writes_parquet(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    runs = tmp_path / "runs"
    with RunContext.open(cfg, seed=7, db_path=ready_db, runs_dir=runs) as run:
        for i in range(30):
            run.writer.append("charge_event", {
                "ev_id": f"ev{i}", "station_id": "st_anseong",
                "t_arrive_min": float(i), "wait_min": float(i % 5),
                "dwell_min": float(i % 5) + 20.0,
            })
            run.writer.snapshot(float(i), "station", "st_anseong", 37.0, 127.0, "queue_len", 1.0)
        run.kpi("wait_p95_min", 18.3, "min")
        run_id = run.run_id

    assert (runs / run_id / "charge_event.parquet").is_file()
    assert (runs / run_id / "snapshot.parquet").is_file()

    with get_conn(ready_db, readonly=True) as conn:
        row = read_table(conn, "run", where="run_id = ?", params=(run_id,)).iloc[0]
        kpi = read_table(conn, "run_kpi", where="run_id = ?", params=(run_id,))
    assert row["status"] == "DONE"
    assert row["finished_at"]
    assert kpi.iloc[0]["metric"] == "wait_p95_min"


def test_context_marks_failed_on_exception(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    runs = tmp_path / "runs"
    with pytest.raises(ZeroDivisionError):
        with RunContext.open(cfg, seed=9, db_path=ready_db, runs_dir=runs) as run:
            run_id = run.run_id
            raise ZeroDivisionError("의도적 실패")

    with get_conn(ready_db, readonly=True) as conn:
        row = read_table(conn, "run", where="run_id = ?", params=(run_id,)).iloc[0]
    assert row["status"] == "FAILED"
    assert "ZeroDivisionError" in row["error_message"]


def test_duckdb_joins_sqlite_master_with_parquet(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    runs = tmp_path / "runs"
    with RunContext.open(cfg, seed=7, db_path=ready_db, runs_dir=runs) as run:
        for i in range(60):
            run.writer.append("charge_event", {
                "ev_id": f"ev{i}", "station_id": "st_anseong",
                "t_arrive_min": float(i * 5), "wait_min": float(i % 11),
            })
        run_id = run.run_id

    con = duck_connect(db_path=ready_db, run_ids=[run_id], runs_dir=runs)
    rows = con.sql(
        """
        SELECT s.name, CAST(e.t_arrive_min / 60 AS INTEGER) AS hour,
               AVG(e.wait_min) AS avg_wait
        FROM charge_event e JOIN station s USING (station_id)
        GROUP BY 1, 2 ORDER BY 2
        """
    ).fetchall()
    assert rows
    assert rows[0][0] == "안성휴게소"


def test_kpi_upsert_is_idempotent(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    handle = register_run(cfg, seed=7, db_path=ready_db, runs_dir=tmp_path / "runs")
    handle.kpi("total_social_cost_krw", 100.0, "KRW")
    handle.kpi("total_social_cost_krw", 250.0, "KRW")
    with get_conn(ready_db, readonly=True) as conn:
        kpi = read_table(conn, "run_kpi")
    assert len(kpi) == 1
    assert kpi.iloc[0]["value"] == pytest.approx(250.0)


def test_scenario_upsert_does_not_duplicate(cfg: ScenarioConfig, ready_db, tmp_path) -> None:
    register_run(cfg, seed=1, db_path=ready_db, runs_dir=tmp_path / "runs")
    register_run(cfg, seed=2, db_path=ready_db, runs_dir=tmp_path / "runs")
    with get_conn(ready_db, readonly=True) as conn:
        assert len(read_table(conn, "scenario")) == 1
        assert len(read_table(conn, "run")) == 2


def test_upsert_df_handles_numpy_scalars(ready_db) -> None:
    """pandas 가 만든 numpy 타입이 sqlite3 에 그대로 들어가면 터진다."""
    df = pd.DataFrame({
        "vclass_id": ["x"], "name": ["X"], "battery_kwh": [77.4],
        "vmax_kw": [233], "consumption_kwh_km": [0.18], "share": [1.0],
    })
    with get_conn(ready_db) as conn:
        upsert_df(conn, "vehicle_class", df)
        out = read_table(conn, "vehicle_class")
    assert out.iloc[0]["vmax_kw"] == pytest.approx(233.0)
