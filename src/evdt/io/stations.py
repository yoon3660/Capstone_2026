"""주행 경로상 충전소 조회."""

from evdt.io.db import get_conn
from evdt.paths import default_db_path


def find_stations_on_route(
    corridor_id: str,
    current_offset_km: float,
    destination_offset_km: float,
) -> list[dict]:
    """같은 주행 방향에서 현재 위치와 목적지 사이의 충전소를 조회한다."""

    if corridor_id not in ("gyeongbu_up", "gyeongbu_down"):
        raise ValueError(f"알 수 없는 corridor_id: {corridor_id}")

    if current_offset_km < 0 or destination_offset_km < 0:
        raise ValueError("offset은 음수일 수 없습니다.")

    if destination_offset_km < current_offset_km:
        raise ValueError("목적지는 현재 위치보다 앞에 있어야 합니다.")

    if destination_offset_km == current_offset_km:
        return []

    with get_conn(default_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT station_id, corridor_id, offset_km
            FROM station
            WHERE corridor_id = ?
              AND offset_km > ?
              AND offset_km < ?
            ORDER BY offset_km
            """,
            (
                corridor_id,
                current_offset_km,
                destination_offset_km,
            ),
        ).fetchall()

    return [
        {
            "station_id": row[0],
            "corridor_id": row[1],
            "offset_km": row[2],
        }
        for row in rows
    ]