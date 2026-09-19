"""경부선 corridor 2개(상행·하행)를 넣는다.

상행·하행은 서로 다른 corridor다 (설계문서 §1.2).
방향별 독립 인스턴스 2개로 구성한다.

    python scripts/seed_corridor.py

기점은 T-06의 offset_km와 같다. 하행 = 양재IC, 상행 = 구서IC.

도로 길이는 scripts/build_route.py가 생성한
data/processed/gyeongbu_route.json에서 읽는다.
따라서 seed_corridor.py 실행 전에 build_route.py를 실행해야 한다.

station.offset_km 상한 검사(validate_master)는
corridor.length_km를 기준으로 한다.
"""

from __future__ import annotations

from pathlib import Path

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402

from evdt.io.db import get_conn, init_db, upsert_df  # noqa: E402
from evdt.io.route import GyeongbuRoute  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402



CORRIDORS = [
    {
        "corridor_id": "gyeongbu_down",
        "name": "경부고속도로",
        "direction": "DOWN",
        "origin_name": "Seoul",
        "dest_name": "Busan",
        "note": "귀성 방향. offset_km 기점 = 양재IC (0010I00045)",
    },
    {
        "corridor_id": "gyeongbu_up",
        "name": "경부고속도로",
        "direction": "UP",
        "origin_name": "Busan",
        "dest_name": "Seoul",
        "note": "귀경 방향. offset_km 기점 = 구서IC (0010I00001). 하행과 좌표계가 반대",
    },
]


def seed(db_path: Path | None = None) -> int:
    path = db_path or default_db_path()

    # 공통 노선 파일에서 현재 기준 길이를 읽는다.
    route_length_km = round(GyeongbuRoute.load().length_km, 3)

    corridors = pd.DataFrame([
        {**corridor, "length_km": route_length_km}
        for corridor in CORRIDORS
    ])

    init_db(path)

    with get_conn(path) as conn:
        n = upsert_df(conn, "corridor", corridors)

    print(
        f"[OK] corridor {len(CORRIDORS)}개 적재 "
        f"(길이 {route_length_km}km, {path})"
    )

    return n


if __name__ == "__main__":
    raise SystemExit(0 if seed() is not None else 1)
