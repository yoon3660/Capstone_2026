"""Parquet writer — 스키마가 실행마다 흔들리지 않는지."""

from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq
import pytest

from evdt.io.writers import SCHEMAS, ParquetRunWriter


def test_snapshot_schema_is_the_contract() -> None:
    """설계 규칙 4 — 이 컬럼 구성이 시뮬레이터와 렌더러 사이의 계약이다."""
    names = [n for n in SCHEMAS["snapshot"].names if n != "run_id"]
    assert names == ["t_min", "entity_type", "entity_id", "lat", "lon", "state", "value"]


def test_unknown_column_is_rejected(tmp_path) -> None:
    with ParquetRunWriter(tmp_path, "r1") as w:
        with pytest.raises(KeyError, match="스키마에 없는 컬럼"):
            w.append("charge_event", {"ev_id": "e1", "waiting_time": 3.0})


def test_unknown_table_is_rejected(tmp_path) -> None:
    with ParquetRunWriter(tmp_path, "r1") as w:
        with pytest.raises(KeyError, match="알 수 없는 테이블"):
            w.append("charge_events", {"ev_id": "e1"})


def test_types_stay_fixed_even_when_column_is_all_null(tmp_path) -> None:
    """charger_id 를 한 번도 안 채워도 string 으로 남아야 한다."""
    with ParquetRunWriter(tmp_path, "r1") as w:
        for i in range(5):
            w.append("charge_event", {"ev_id": f"e{i}", "wait_min": float(i)})
    schema = pq.read_schema(tmp_path / "charge_event.parquet")
    assert str(schema.field("charger_id").type) == "string"
    assert str(schema.field("wait_min").type) == "double"


def test_batching_across_multiple_flushes(tmp_path) -> None:
    with ParquetRunWriter(tmp_path, "r1", batch_size=10) as w:
        for i in range(25):
            w.snapshot(float(i), "station", "s1", 37.0, 127.0, "queue_len", float(i))
        counts = w.close()
    assert counts["snapshot"] == 25
    df = pd.read_parquet(tmp_path / "snapshot.parquet")
    assert len(df) == 25
    assert df["run_id"].unique().tolist() == ["r1"]


def test_untouched_tables_produce_no_file(tmp_path) -> None:
    with ParquetRunWriter(tmp_path, "r1") as w:
        w.append("charge_event", {"ev_id": "e1"})
    assert (tmp_path / "charge_event.parquet").is_file()
    assert not (tmp_path / "solver_log.parquet").exists()
