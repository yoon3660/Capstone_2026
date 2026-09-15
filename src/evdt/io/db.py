"""SQLite 마스터 DB 접근 (T-02).

왜 이 모듈이 필요한가
    티켓마다 각자 sqlite3.connect() 를 부르면 PRAGMA 설정이 제각각이 되고
    (foreign_keys 가 꺼진 커넥션에서는 FK 가 그냥 무시된다) 스키마가 금방
    갈라진다. 커넥션 생성은 이 파일의 get_conn() 하나로 고정한다.

사용법
    from evdt.io.db import get_conn, upsert_df, read_table

    with get_conn() as conn:
        upsert_df(conn, "station", df)
        out = read_table(conn, "station", where="direction = ?", params=("DOWN",))

CLI
    python -m evdt.io.db init       # 스키마 적용
    python -m evdt.io.db tables     # 테이블 목록과 행 수
    python -m evdt.io.db validate   # 마스터 데이터 정합성 검사
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from evdt.paths import SCHEMA_PATH, default_db_path

#: schema.sql 이 만들어야 하는 테이블. init_db() 가 이 목록으로 자기 검증한다.
EXPECTED_TABLES: tuple[str, ...] = (
    "corridor",
    "cell",
    "station",
    "charger",
    "vehicle_class",
    "charge_curve",
    "temp_efficiency",
    "scenario",
    "run",
    "run_kpi",
)


class SchemaError(RuntimeError):
    """스키마가 기대한 모양이 아닐 때."""


class MasterDataError(RuntimeError):
    """마스터 데이터가 물리적으로 말이 안 될 때."""


# ---------------------------------------------------------------------------
# 커넥션
# ---------------------------------------------------------------------------
@contextmanager
def get_conn(
    db_path: str | Path | None = None,
    *,
    readonly: bool = False,
) -> Iterator[sqlite3.Connection]:
    """SQLite 커넥션을 연다.

    - foreign_keys 를 반드시 켠다. 기본값이 OFF 라서 안 켜면 FK 가 장식이 된다.
    - 정상 종료 시 commit, 예외 발생 시 rollback.
    - row_factory 를 sqlite3.Row 로 두어 컬럼명 접근이 가능하다.
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"DB 가 없다: {path}  (먼저 `python -m evdt.io.db init`)")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------
def init_db(db_path: str | Path | None = None, *, schema_path: Path | None = None) -> Path:
    """schema.sql 을 적용하고, 기대한 테이블이 다 생겼는지 확인한다.

    여러 번 실행해도 안전하다 (CREATE ... IF NOT EXISTS).
    기존 데이터는 지우지 않는다.
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    sql_file = schema_path or SCHEMA_PATH
    if not sql_file.is_file():
        raise SchemaError(f"schema.sql 을 찾을 수 없다: {sql_file}")

    sql = sql_file.read_text(encoding="utf-8")
    with get_conn(path) as conn:
        conn.executescript(sql)
        found = set(table_names(conn))

    missing = [t for t in EXPECTED_TABLES if t not in found]
    if missing:
        raise SchemaError(
            "스키마 적용 후에도 없는 테이블: " + ", ".join(missing) + f"  (db={path})"
        )
    return path


def table_names(conn: sqlite3.Connection) -> list[str]:
    """사용자 테이블 목록 (sqlite 내부 테이블 제외)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """테이블의 PRIMARY KEY 컬럼 (선언 순서)."""
    info = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    if not info:
        raise SchemaError(f"테이블이 없다: {table}")
    pk = [(r["pk"], r["name"]) for r in info if r["pk"] > 0]
    return [name for _, name in sorted(pk)]


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    if not info:
        raise SchemaError(f"테이블이 없다: {table}")
    return [r["name"] for r in info]


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(t)}").fetchone()[0]
        for t in table_names(conn)
    }


# ---------------------------------------------------------------------------
# 읽기 / 쓰기
# ---------------------------------------------------------------------------
def read_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    columns: Sequence[str] | None = None,
    where: str | None = None,
    params: Sequence[Any] = (),
    order_by: str | None = None,
) -> pd.DataFrame:
    """테이블을 DataFrame 으로 읽는다.

    where 는 반드시 ? 플레이스홀더를 쓰고 값은 params 로 넘긴다.
    (문자열 포매팅으로 값을 끼워 넣지 말 것 — 따옴표 하나에 조용히 깨진다.)
    """
    cols = ", ".join(_quote_ident(c) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {_quote_ident(table)}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    return pd.read_sql_query(sql, conn, params=tuple(params))


def upsert_df(
    conn: sqlite3.Connection,
    table: str,
    df: pd.DataFrame,
    *,
    conflict_columns: Sequence[str] | None = None,
) -> int:
    """DataFrame 을 테이블에 upsert 한다. 삽입/갱신된 행 수를 반환.

    ⚠ INSERT OR REPLACE 를 쓰지 않는 이유
        REPLACE 는 기존 행을 DELETE 한 뒤 INSERT 한다. 그러면
        ON DELETE CASCADE 가 걸린 자식 행(예: station 을 갱신했는데 charger)이
        조용히 같이 지워진다. 그래서 ON CONFLICT ... DO UPDATE 를 쓴다.

    conflict_columns 를 주지 않으면 테이블의 PRIMARY KEY 를 쓴다.
    UNIQUE 제약 기준으로 upsert 하고 싶으면 명시적으로 넘길 것.
    """
    if df.empty:
        return 0

    valid = set(column_names(conn, table))
    unknown = [c for c in df.columns if c not in valid]
    if unknown:
        raise SchemaError(
            f"{table} 에 없는 컬럼: {unknown}\n  사용 가능: {sorted(valid)}"
        )

    keys = list(conflict_columns) if conflict_columns else primary_key_columns(conn, table)
    if not keys:
        raise SchemaError(f"{table} 에 PRIMARY KEY 가 없어 upsert 기준을 정할 수 없다")
    missing_keys = [k for k in keys if k not in df.columns]
    if missing_keys:
        raise SchemaError(f"{table} upsert 에 필요한 키 컬럼이 DataFrame 에 없다: {missing_keys}")

    cols = list(df.columns)
    updatable = [c for c in cols if c not in keys]

    placeholders = ", ".join("?" for _ in cols)
    col_sql = ", ".join(_quote_ident(c) for c in cols)
    conflict_sql = ", ".join(_quote_ident(c) for c in keys)

    if updatable:
        set_sql = ", ".join(f"{_quote_ident(c)} = excluded.{_quote_ident(c)}" for c in updatable)
        action = f"DO UPDATE SET {set_sql}"
    else:
        action = "DO NOTHING"

    sql = (
        f"INSERT INTO {_quote_ident(table)} ({col_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_sql}) {action}"
    )

    records = [tuple(_to_sqlite(v) for v in row) for row in df.itertuples(index=False, name=None)]
    cur = conn.executemany(sql, records)
    return cur.rowcount if cur.rowcount != -1 else len(records)


def _to_sqlite(value: Any) -> Any:
    """numpy/pandas 스칼라를 sqlite3 가 받는 파이썬 타입으로."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (bool,)):
        return int(value)
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return None
    item = getattr(value, "item", None)
    if callable(item) and not isinstance(value, (str, bytes)):
        try:
            return item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def _quote_ident(name: str) -> str:
    """식별자 인용. SQL 주입과 예약어 충돌을 동시에 막는다."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"식별자로 쓸 수 없는 이름: {name!r}")
    return f'"{name}"'


# ---------------------------------------------------------------------------
# 마스터 데이터 정합성 검사
# ---------------------------------------------------------------------------
def validate_master(conn: sqlite3.Connection, *, strict: bool = False) -> list[str]:
    """테이블 단위 CHECK 로는 못 거는 제약을 검사한다.

    비어 있는 테이블은 "아직 안 채운 것"으로 보고 통과시킨다
    (Sprint 1 초반에는 대부분 비어 있다).

    반환: 문제 설명 문자열 리스트. strict=True 면 문제가 있을 때 예외를 던진다.
    """
    problems: list[str] = []

    # 1) 차종 비중 합 = 1.0
    n_vc = conn.execute("SELECT COUNT(*) FROM vehicle_class").fetchone()[0]
    if n_vc:
        total = conn.execute("SELECT SUM(share) FROM vehicle_class").fetchone()[0] or 0.0
        if abs(total - 1.0) > 1e-6:
            problems.append(f"vehicle_class.share 합이 {total:.6f} 다 (1.0 이어야 함)")

    # 2) 충전곡선이 각 차종의 0.0~1.0 을 빈틈없이 덮는가
    for (vclass_id,) in conn.execute("SELECT vclass_id FROM vehicle_class").fetchall():
        segs = conn.execute(
            "SELECT soc_from, soc_to FROM charge_curve WHERE vclass_id = ? ORDER BY soc_from",
            (vclass_id,),
        ).fetchall()
        if not segs:
            problems.append(f"charge_curve 에 {vclass_id} 구간이 하나도 없다")
            continue
        if abs(segs[0]["soc_from"] - 0.0) > 1e-9:
            problems.append(f"{vclass_id} 충전곡선이 SoC 0.0 에서 시작하지 않는다")
        if abs(segs[-1]["soc_to"] - 1.0) > 1e-9:
            problems.append(f"{vclass_id} 충전곡선이 SoC 1.0 에서 끝나지 않는다")
        for a, b in zip(segs, segs[1:], strict=False):
            if abs(a["soc_to"] - b["soc_from"]) > 1e-9:
                problems.append(
                    f"{vclass_id} 충전곡선에 틈/겹침: {a['soc_to']} → {b['soc_from']}"
                )

    # 3) CTM 기본도 일관성 (DB CHECK 와 이중 확인 — 기존 DB 는 CHECK 를 통과한 적이 없을 수 있다)
    bad_cells = conn.execute(
        """
        SELECT cell_id, q_max_veh_h,
               (lanes * v_free_kmh * w_back_kmh * k_jam_veh_km_lane)
               / (v_free_kmh + w_back_kmh) AS q_implied
        FROM cell
        WHERE abs(q_max_veh_h
                  - (lanes * v_free_kmh * w_back_kmh * k_jam_veh_km_lane)
                    / (v_free_kmh + w_back_kmh)) > 0.01 * q_max_veh_h
        """
    ).fetchall()
    for r in bad_cells:
        problems.append(
            f"cell {r['cell_id']}: q_max={r['q_max_veh_h']:.1f} 인데 "
            f"기본도상 {r['q_implied']:.1f} 이어야 한다 (§9.5)"
        )

    # 4) 각 휴게소는 정확히 한 셀에 매핑된다 (셀을 채운 뒤에만 의미 있음)
    n_cells = conn.execute("SELECT COUNT(*) FROM cell").fetchone()[0]
    if n_cells:
        unmapped = conn.execute(
            "SELECT station_id FROM station WHERE cell_id IS NULL"
        ).fetchall()
        for r in unmapped:
            problems.append(f"station {r['station_id']} 이 어떤 셀에도 매핑되지 않았다")

    # 5) 충전소 offset 이 corridor 길이를 넘지 않는가
    over = conn.execute(
        """
        SELECT s.station_id, s.offset_km, c.length_km
        FROM station s JOIN corridor c ON c.corridor_id = s.corridor_id
        WHERE s.offset_km > c.length_km
        """
    ).fetchall()
    for r in over:
        problems.append(
            f"station {r['station_id']} offset_km={r['offset_km']} 이 "
            f"corridor 길이 {r['length_km']} 를 넘는다"
        )

    if strict and problems:
        raise MasterDataError("마스터 데이터 문제:\n  - " + "\n  - ".join(problems))
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "init"
    db = Path(argv[1]) if len(argv) > 1 else default_db_path()

    if cmd == "init":
        path = init_db(db)
        with get_conn(path) as conn:
            counts = row_counts(conn)
        print(f"OK  스키마 적용 완료: {path}")
        for t, n in counts.items():
            print(f"    {t:<18} {n:>8,} rows")
        return 0

    if cmd == "tables":
        with get_conn(db, readonly=True) as conn:
            for t, n in row_counts(conn).items():
                print(f"{t:<18} {n:>8,}")
        return 0

    if cmd == "validate":
        with get_conn(db, readonly=True) as conn:
            problems = validate_master(conn)
        if problems:
            print("문제 발견:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("OK  마스터 데이터 정합성 이상 없음")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
