"""경부선 corridor 2개(상행·하행)를 넣는다.

상행·하행은 서로 다른 corridor 다 (설계문서 §1.2). 방향별 독립 인스턴스 2개로
돌리는 설계가 여기서 데이터로 확정된다.

    python scripts/seed_corridor.py

길이 416.0 km 는 서울TG~부산 본선 기준 개략값이다. T-06 에서 실제 휴게소
offset_km 를 계산할 때 기점 정의와 함께 확정하고, 그때 이 값을 갱신할 것.
갱신하면 station.offset_km 의 상한 검사(validate_master)도 같이 맞아야 한다.
"""

from __future__ import annotations

from pathlib import Path

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402

from evdt.io.db import get_conn, init_db, upsert_df  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402

GYEONGBU_LENGTH_KM = 416.0

CORRIDORS = [
    {
        "corridor_id": "gyeongbu_down",
        "name": "경부고속도로",
        "direction": "DOWN",
        "origin_name": "Seoul",
        "dest_name": "Busan",
        "length_km": GYEONGBU_LENGTH_KM,
        "note": "귀성 방향. offset_km 기점 = 서울TG",
    },
    {
        "corridor_id": "gyeongbu_up",
        "name": "경부고속도로",
        "direction": "UP",
        "origin_name": "Busan",
        "dest_name": "Seoul",
        "length_km": GYEONGBU_LENGTH_KM,
        "note": "귀경 방향. offset_km 기점 = 부산 기점 (하행과 좌표계가 반대)",
    },
]


def seed(db_path: Path | None = None) -> int:
    path = db_path or default_db_path()
    init_db(path)
    with get_conn(path) as conn:
        n = upsert_df(conn, "corridor", pd.DataFrame(CORRIDORS))
    print(f"[OK] corridor {len(CORRIDORS)}개 적재 ({path})")
    return n


if __name__ == "__main__":
    raise SystemExit(0 if seed() is not None else 1)
