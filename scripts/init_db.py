"""SQLite 마스터 DB 를 만든다 (T-02).

    python scripts/init_db.py                 # 기본 경로 (프로젝트 루트/evdt.db)
    python scripts/init_db.py --db my.db
    python scripts/init_db.py --seed-corridor # 경부선 상·하행 corridor 2개도 함께 넣는다

여러 번 실행해도 안전하다. 기존 데이터는 지우지 않는다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)

from evdt.io.db import get_conn, init_db, row_counts  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None, help="DB 경로 (기본: evdt.db)")
    ap.add_argument("--seed-corridor", action="store_true", help="경부선 corridor 2개 삽입")
    args = ap.parse_args()

    db = args.db or default_db_path()
    path = init_db(db)
    print(f"[OK] 스키마 적용: {path}")

    if args.seed_corridor:
        from seed_corridor import seed  # 같은 폴더

        seed(path)

    with get_conn(path, readonly=True) as conn:
        for table, n in row_counts(conn).items():
            print(f"     {table:<18} {n:>8,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
