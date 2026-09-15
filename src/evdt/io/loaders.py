"""DuckDB 로 SQLite 마스터 + Parquet 로그를 한 번에 질의 (T-04).

왜 DuckDB 인가
    결과 분석을 pandas merge 로 하면 코드가 금방 읽기 어려워진다. 서버를 띄우지
    않고 SQL 한 줄로 "휴게소별 시간대별 평균 대기" 를 뽑을 수 있으면 분석 속도가
    달라진다 (T-17 완료 조건).

왜 sqlite 확장을 쓰지 않는가
    duckdb 의 sqlite_scanner 는 첫 사용 시 인터넷에서 확장을 내려받는다.
    학교 네트워크나 오프라인 환경에서 조용히 실패한다. 그래서 마스터 테이블은
    pandas 로 읽어 DuckDB 에 등록(register)한다. 크기가 작아서 비용이 없다.

사용법
    from evdt.io.loaders import duck_connect

    con = duck_connect(run_ids=["seollal_2026_down_base__UE__p100__s0007"])
    con.sql(\"\"\"
        SELECT s.name, CAST(e.t_arrive_min / 60 AS INT) AS hour,
               AVG(e.wait_min) AS avg_wait
        FROM charge_event e JOIN station s USING (station_id)
        GROUP BY 1, 2 ORDER BY 3 DESC
    \"\"\").show()
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from evdt.io.db import get_conn, table_names
from evdt.io.writers import TABLES
from evdt.paths import RUNS_DIR, default_db_path

if TYPE_CHECKING:  # pragma: no cover
    import duckdb


def run_parquet_path(run_id: str, table: str, runs_dir: Path | None = None) -> Path:
    return (runs_dir or RUNS_DIR) / run_id / f"{table}.parquet"


def load_run_table(run_id: str, table: str, runs_dir: Path | None = None) -> pd.DataFrame:
    """한 run 의 한 테이블을 DataFrame 으로. 파일이 없으면 빈 DataFrame."""
    path = run_parquet_path(run_id, table, runs_dir)
    if not path.is_file():
        return pd.DataFrame()
    return pd.read_parquet(path)


def list_runs(runs_dir: Path | None = None) -> list[str]:
    base = runs_dir or RUNS_DIR
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and (p / "meta.json").is_file())


def duck_connect(
    *,
    db_path: str | Path | None = None,
    run_ids: list[str] | None = None,
    runs_dir: Path | None = None,
) -> duckdb.DuckDBPyConnection:
    """SQLite 마스터 테이블과 지정한 run 들의 Parquet 를 모두 뷰로 올린 커넥션.

    run_ids 를 주지 않으면 runs/ 아래 모든 실행을 올린다. 여러 run 을 올리면
    각 테이블에 run_id 컬럼이 있으므로 스테이지 간 비교를 SQL 로 바로 할 수 있다.
    """
    import duckdb  # 지연 임포트: 시뮬레이션만 돌릴 때는 필요 없다

    con = duckdb.connect()

    # 1) SQLite 마스터 → pandas → DuckDB 뷰
    sqlite_path = Path(db_path) if db_path is not None else default_db_path()
    if sqlite_path.exists():
        with get_conn(sqlite_path, readonly=True) as sconn:
            for name in table_names(sconn):
                df = pd.read_sql_query(f'SELECT * FROM "{name}"', sconn)  # noqa: S608
                con.register(f"_master_{name}", df)
                con.execute(f'CREATE OR REPLACE VIEW "{name}" AS SELECT * FROM "_master_{name}"')

    # 2) Parquet 로그 → DuckDB 뷰
    ids = run_ids if run_ids is not None else list_runs(runs_dir)
    for table in TABLES:
        files = [str(p) for rid in ids if (p := run_parquet_path(rid, table, runs_dir)).is_file()]
        if not files:
            continue
        listing = ", ".join(f"'{f}'" for f in files)
        con.execute(
            f'CREATE OR REPLACE VIEW "{table}" AS '  # noqa: S608
            f"SELECT * FROM read_parquet([{listing}], union_by_name = true)"
        )
    return con
