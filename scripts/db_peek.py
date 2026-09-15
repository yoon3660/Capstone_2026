"""DB 안을 눈으로 확인한다 — PyCharm Database 도구 창 없이도.

Database 도구 창은 PyCharm Pro 기능이라 무료 티어에서는 안 보일 수 있다.
이 스크립트는 파이썬만으로 같은 일을 한다.

    python scripts/db_peek.py                       # 테이블 목록 + 행 수
    python scripts/db_peek.py station               # station 테이블 상위 20행
    python scripts/db_peek.py station --limit 100
    python scripts/db_peek.py --sql "SELECT name, offset_km FROM station ORDER BY offset_km"
    python scripts/db_peek.py --schema station      # 컬럼 정의와 제약

원시 SQL 은 읽기 전용 커넥션으로 돌아간다. 실수로 데이터를 지울 일은 없다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402

from evdt.io.db import get_conn, read_table, row_counts  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("table", nargs="?", help="들여다볼 테이블 이름")
    ap.add_argument("--db", type=Path, default=None, help="DB 경로 (기본: evdt.db)")
    ap.add_argument("--limit", type=int, default=20, help="출력할 행 수 (기본 20)")
    ap.add_argument("--sql", default=None, help="직접 실행할 SELECT 문")
    ap.add_argument("--schema", default=None, metavar="TABLE", help="테이블의 컬럼 정의 출력")
    args = ap.parse_args()

    db = args.db or default_db_path()
    if not db.exists():
        print(f"DB 가 없다: {db}")
        print("먼저 실행할 것:  python scripts/init_db.py --seed-corridor")
        return 1

    pd.set_option("display.max_columns", 50)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 40)

    with get_conn(db, readonly=True) as conn:
        if args.schema:
            print(f"\n=== {args.schema} 컬럼 정의 ===")
            info = pd.read_sql_query(f'PRAGMA table_info("{args.schema}")', conn)
            if info.empty:
                print(f"그런 테이블이 없다: {args.schema}")
                return 1
            print(info[["name", "type", "notnull", "dflt_value", "pk"]].to_string(index=False))
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (args.schema,)
            ).fetchone()
            if ddl and ddl[0]:
                print(f"\n--- CREATE 문 (CHECK 제약 포함) ---\n{ddl[0]}")
            return 0

        if args.sql:
            if not args.sql.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA")):
                print("읽기 전용이다. SELECT / WITH / PRAGMA 만 쓸 수 있다.")
                return 1
            print(pd.read_sql_query(args.sql, conn).to_string(index=False))
            return 0

        if args.table:
            df = read_table(conn, args.table)
            print(f"\n=== {args.table}  ({len(df):,} rows) ===")
            print(df.head(args.limit).to_string(index=False) if len(df) else "(비어 있음)")
            if len(df) > args.limit:
                print(f"... 그 외 {len(df) - args.limit:,}행")
            return 0

        print(f"\n=== {db} ===")
        counts = row_counts(conn)
        width = max(len(t) for t in counts) if counts else 10
        for table, n in counts.items():
            mark = " " if n else "·"   # · = 아직 안 채운 테이블
            print(f" {mark} {table:<{width}}  {n:>8,} rows")
        print("\n한 테이블을 보려면:  python scripts/db_peek.py <테이블명>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
