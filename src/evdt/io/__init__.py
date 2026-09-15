"""저장 계층.

    db.py            SQLite 마스터 (정적 입력 + 실험의 신원)
    run_registry.py  run 등록 / run_id 규칙 / git SHA 기록
    writers.py       Parquet 이벤트 로그 writer
    loaders.py       DuckDB 로 SQLite + Parquet 조인 질의

경계 규칙 (설계문서 §9.3):
    "재현 가능한 것은 Parquet, 재현의 근거는 SQLite."
"""

from evdt.io.db import get_conn, init_db, read_table, table_names, upsert_df
from evdt.io.loaders import duck_connect, load_run_table
from evdt.io.run_registry import RunContext, RunHandle, make_run_id, register_run
from evdt.io.writers import ParquetRunWriter

__all__ = [
    "get_conn",
    "init_db",
    "read_table",
    "table_names",
    "upsert_df",
    "RunContext",
    "RunHandle",
    "make_run_id",
    "register_run",
    "ParquetRunWriter",
    "duck_connect",
    "load_run_table",
]
