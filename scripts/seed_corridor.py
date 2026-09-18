"""경부선 corridor 2개(상행·하행)를 넣는다.

상행·하행은 서로 다른 corridor 다 (설계문서 §1.2). 방향별 독립 인스턴스 2개로
돌리는 설계가 여기서 데이터로 확정된다.

    python scripts/seed_corridor.py

기점은 T-06 의 offset_km 와 같다. 하행 = 양재IC, 상행 = 구서IC.
길이 391.0 km 는 구서IC ~ 양재IC 를 도로공사 IC/JCT + 휴게소 좌표로 이은
누적거리다 (charger_ingest.route_mileposts, 2026-09 도로공사 목록 기준).
선분 근사라 실제 도로보다 몇 % 짧다. load_chargers.py 가 매번 이 값을
다시 계산해 출력하므로, 크게 달라지면 여기를 갱신할 것.
갱신하면 station.offset_km 의 상한 검사(validate_master)도 같이 맞아야 한다.
"""

from __future__ import annotations

from pathlib import Path

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402

from evdt.io.db import get_conn, init_db, upsert_df  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402

GYEONGBU_LENGTH_KM = 391.0

CORRIDORS = [
    {
        "corridor_id": "gyeongbu_down",
        "name": "경부고속도로",
        "direction": "DOWN",
        "origin_name": "Seoul",
        "dest_name": "Busan",
        "length_km": GYEONGBU_LENGTH_KM,
        "note": "귀성 방향. offset_km 기점 = 양재IC (0010I00045)",
    },
    {
        "corridor_id": "gyeongbu_up",
        "name": "경부고속도로",
        "direction": "UP",
        "origin_name": "Busan",
        "dest_name": "Seoul",
        "length_km": GYEONGBU_LENGTH_KM,
        "note": "귀경 방향. offset_km 기점 = 구서IC (0010I00001). 하행과 좌표계가 반대",
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
