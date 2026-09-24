"""CTM 셀 조회 (#56).

`cell` 테이블의 한 방향을 seq 순서대로 돌려준다. 여기서 world 의 자료형
(`ctm.CellArrays`)으로 바꾸지 않는다 — io 는 world 를 임포트하지 않는다
(tests/test_import_boundaries.py). 변환은 `world.ctm.CellArrays.from_rows` 가 한다.

셀은 `scripts/seed_cells.py` 가 만든다. 셀이 없는 기기(표준노드링크 SHP 가 없는
기기)에서는 그냥 빈 목록이 아니라 **무엇을 실행해야 하는지**를 말하고 멈춘다.
빈 목록을 돌려주면 CTM 이 셀 0개로 조용히 돌아 아무 일도 일어나지 않는다.
"""

from __future__ import annotations

from evdt.io.db import get_conn
from evdt.paths import default_db_path

#: CellArrays 가 필요로 하는 순서. 여기와 world.ctm 이 같은 순서를 본다.
CELL_COLUMNS: tuple[str, ...] = (
    "length_km",
    "lanes",
    "v_free_kmh",
    "w_back_kmh",
    "k_jam_veh_km_lane",
    "q_max_veh_h",
)

#: 추가로 같이 읽는 것 (기록·스냅샷에 쓴다)
CELL_META_COLUMNS: tuple[str, ...] = (
    "cell_id",
    "seq",
    "offset_km_start",
    "offset_km_end",
    "lat_start",
    "lon_start",
)


def read_cells(corridor_id: str, *, db_path=None) -> list[dict]:
    """한 방향의 셀을 seq 순서대로. 없으면 만드는 방법을 알려주고 멈춘다."""

    columns = CELL_META_COLUMNS + CELL_COLUMNS

    with get_conn(db_path or default_db_path(), readonly=True) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM cell WHERE corridor_id = ? ORDER BY seq",
            (corridor_id,),
        ).fetchall()

    if not rows:
        raise LookupError(
            f"{corridor_id} 에 CTM 셀이 없습니다.\n"
            "만드는 순서 (표준노드링크 SHP 가 필요하다 — data/raw/README.md):\n"
            "    python scripts/audit_lane_mapping.py\n"
            "    python scripts/build_lane_profile.py\n"
            "    python scripts/seed_cells.py --write\n"
            "    python scripts/check_cells.py"
        )

    return [dict(zip(columns, row, strict=True)) for row in rows]
