"""smoke_run.py 가 남긴 가짜 데이터를 지운다.

    python scripts/smoke_clean.py

지우는 것: source='smoke' 인 휴게소·충전기, seed=9999 인 run 과 그 KPI,
그리고 runs/ 아래 해당 폴더. 진짜 데이터는 건드리지 않는다.
"""

from __future__ import annotations

import shutil

from _bootstrap import ROOT  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)

from evdt.io.db import get_conn  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402

SMOKE_SEED = 9999


def main() -> int:
    db = default_db_path()
    if not db.exists():
        print(f"DB 가 없다: {db}")
        return 0

    with get_conn(db) as conn:
        run_ids = [
            r[0] for r in conn.execute(
                "SELECT run_id FROM run WHERE seed = ?", (SMOKE_SEED,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM run WHERE seed = ?", (SMOKE_SEED,))   # run_kpi 는 CASCADE
        conn.execute("DELETE FROM charger WHERE source = 'smoke'")
        conn.execute("DELETE FROM station WHERE source = 'smoke'")

    for run_id in run_ids:
        target = ROOT / "runs" / run_id
        if target.exists():
            shutil.rmtree(target)
        print(f"  삭제: runs/{run_id}/")

    print(f"run {len(run_ids)}건, 가짜 휴게소·충전기 정리 완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
