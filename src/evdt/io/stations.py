"""충전소·충전기 조회.

시뮬레이터가 쓸 행을 그대로 돌려준다. 여기서 world 의 자료형(Charger 등)으로
바꾸지 않는다 — io 는 world 를 임포트하지 않는다 (tests/test_import_boundaries.py).
변환은 world/sim.py 의 station_specs 가 한다.
"""

from evdt.io.db import get_conn
from evdt.paths import default_db_path

#: scripts/smoke_run.py 가 넣는 가짜 데이터의 source. 진짜 코리도에 있으면 안 된다.
SMOKE_SOURCE = "smoke"

#: 가짜 휴게소는 이 코리도에만 둔다. 진짜 코리도(gyeongbu_up/down)와 섞이면 셀 분할·
#: 충전소 탐색·배정이 전부 가짜 휴게소를 진짜로 알고 쓴다.
SMOKE_CORRIDOR_ID = "smoke_down"


def require_no_smoke(conn, corridor_id: str) -> None:
    """진짜 코리도에 가짜 휴게소가 섞여 있으면 멈춘다.

    왜 조용히 걸러내지 않는가
        예전 smoke_run.py 는 가짜 휴게소 3곳을 **gyeongbu_down 에 직접** 넣었다.
        그 뒤 셀 분할이 그걸 앵커로 쓰면서 원인을 알 수 없는 셀 경계가 생겼고,
        찾는 데 오래 걸렸다. 한 곳에서만 걸러내면 다른 읽는 곳은 여전히 오염된 채로
        돈다. 오염 자체를 드러내고 지우게 하는 것이 맞다.
    """

    if corridor_id == SMOKE_CORRIDOR_ID:
        return

    rows = conn.execute(
        "SELECT station_id, name, offset_km FROM station WHERE corridor_id = ? AND source = ?",
        (corridor_id, SMOKE_SOURCE),
    ).fetchall()

    if rows:
        listed = ", ".join(f"{r[1]}({r[2]:.3f} km)" for r in rows)
        raise RuntimeError(
            f"{corridor_id} 에 smoke_run.py 가 넣은 가짜 휴게소 {len(rows)}곳이 섞여 있습니다: "
            f"{listed}\n지우려면:  python scripts/smoke_clean.py"
        )


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
        require_no_smoke(conn, corridor_id)
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

def read_station_chargers(
    conn,
    *,
    corridor_id: str | None = None,
    station_ids: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """(station 행, charger 행) 을 읽는다.

    **충전기 대수는 코드가 아니라 여기서 온다.** charger 한 행은 같은 사양
    `n_units` 기를 뜻한다 (schema.sql §4). 대수를 코드에 박으면 휴게소마다 다른
    값이 하나로 뭉개지고, 대기시간이 통째로 틀린다.

    is_active = 0 인 행도 그대로 돌려준다. 무엇을 뺄지는 읽는 쪽이 정한다
    (world/sim.py expand_chargers).
    """

    where = []
    params: list[str] = []

    if corridor_id is not None:
        require_no_smoke(conn, corridor_id)
        where.append("corridor_id = ?")
        params.append(corridor_id)

    if station_ids is not None:
        if not station_ids:
            return [], []
        where.append(f"station_id IN ({','.join('?' * len(station_ids))})")
        params.extend(station_ids)

    clause = f" WHERE {' AND '.join(where)}" if where else ""

    stations = [
        dict(row)
        for row in conn.execute(
            "SELECT station_id, corridor_id, name, direction, offset_km, lat, lon, n_parking"
            f" FROM station{clause} ORDER BY offset_km, station_id",
            params,
        )
    ]

    ids = [s["station_id"] for s in stations]

    if not ids:
        return [], []

    chargers = [
        dict(row)
        for row in conn.execute(
            "SELECT charger_id, station_id, power_kw, connector_type, n_units, is_active"
            f" FROM charger WHERE station_id IN ({','.join('?' * len(ids))})"
            " ORDER BY station_id, power_kw DESC, charger_id",
            ids,
        )
    ]

    return stations, chargers
