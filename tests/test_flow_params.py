from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from evdt.io.flow_params import k_from_q, q_per_lane


W_BACK_KMH = 18.0
K_JAM_VEH_KM_LANE = 144.0

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "src" / "evdt" / "sql" / "schema.sql"


def _create_memory_db() -> sqlite3.Connection:
    """schema.sql을 적용한 인메모리 SQLite DB를 만든다."""
    conn = sqlite3.connect(":memory:")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    return conn


def _insert_test_corridor(conn: sqlite3.Connection) -> None:
    """cell 테스트에 필요한 최소 corridor 행을 넣는다."""
    conn.execute(
        """
        INSERT INTO corridor (
            corridor_id,
            name,
            direction,
            origin_name,
            dest_name,
            length_km
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "test_down",
            "경부고속도로",
            "DOWN",
            "서울",
            "부산",
            415.058,
        ),
    )


def test_q_and_k_are_inverse() -> None:
    """q_per_lane()과 k_from_q()가 0.1% 이내에서 서로 역함수인지 확인한다."""
    for v_free_kmh in (96.4, 98.1, 100.0):
        q_max = q_per_lane(
            v_free_kmh=v_free_kmh,
            w_back_kmh=W_BACK_KMH,
            k_jam_veh_km_lane=K_JAM_VEH_KM_LANE,
        )

        restored_k = k_from_q(
            v_free_kmh=v_free_kmh,
            w_back_kmh=W_BACK_KMH,
            q_max_veh_h_lane=q_max,
        )

        relative_error = (
            abs(restored_k - K_JAM_VEH_KM_LANE)
            / K_JAM_VEH_KM_LANE
        )

        assert relative_error <= 0.001


@pytest.mark.parametrize(
    "v_free_kmh",
    [
        96.4,   # DOWN VDS 실측 대표값
        98.1,   # UP VDS 실측 대표값
        100.0,  # 기준값
    ],
)
def test_recommended_q_per_lane_is_reasonable(
    v_free_kmh: float,
) -> None:
    """권장 파라미터의 차로당 q_max가 2,150~2,250 veh/h 범위인지 확인한다."""
    q_max = q_per_lane(
        v_free_kmh=v_free_kmh,
        w_back_kmh=W_BACK_KMH,
        k_jam_veh_km_lane=K_JAM_VEH_KM_LANE,
    )

    assert 2150.0 <= q_max <= 2250.0


def test_jam_spacing_is_physically_reasonable() -> None:
    """k_jam 기준 차량 1대당 공간이 6~8m 범위인지 확인한다."""
    spacing_m = 1000.0 / K_JAM_VEH_KM_LANE

    assert 6.0 <= spacing_m <= 8.0


def test_v_free_sensitivity_is_within_five_percent() -> None:
    """v_free를 90/100/110으로 바꿔도 q_max 변화가 기준 대비 5% 이내인지 확인한다."""
    q_base = q_per_lane(
        v_free_kmh=100.0,
        w_back_kmh=W_BACK_KMH,
        k_jam_veh_km_lane=K_JAM_VEH_KM_LANE,
    )

    for v_free_kmh in (90.0, 100.0, 110.0):
        q_max = q_per_lane(
            v_free_kmh=v_free_kmh,
            w_back_kmh=W_BACK_KMH,
            k_jam_veh_km_lane=K_JAM_VEH_KM_LANE,
        )

        relative_change = abs(q_max - q_base) / q_base

        assert relative_change <= 0.05


def test_recommended_cell_passes_database_check() -> None:
    """권장 파라미터로 계산한 cell 행이 DB CHECK 제약을 통과하는지 확인한다."""
    conn = _create_memory_db()

    try:
        _insert_test_corridor(conn)

        lanes = 4
        v_free_kmh = 96.4

        q_max_veh_h = lanes * q_per_lane(
            v_free_kmh=v_free_kmh,
            w_back_kmh=W_BACK_KMH,
            k_jam_veh_km_lane=K_JAM_VEH_KM_LANE,
        )

        conn.execute(
            """
            INSERT INTO cell (
                cell_id,
                corridor_id,
                seq,
                offset_km_start,
                offset_km_end,
                length_km,
                lanes,
                lanes_source,
                v_free_kmh,
                w_back_kmh,
                k_jam_veh_km_lane,
                q_max_veh_h,
                lat_start,
                lon_start,
                lat_end,
                lon_end
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "test_down_0000",
                "test_down",
                0,
                0.0,
                0.5,
                0.5,
                lanes,
                "measured",
                v_free_kmh,
                W_BACK_KMH,
                K_JAM_VEH_KM_LANE,
                q_max_veh_h,
                37.0,
                127.0,
                37.01,
                127.01,
            ),
        )

        row = conn.execute(
            """
            SELECT q_max_veh_h
            FROM cell
            WHERE cell_id = ?
            """,
            ("test_down_0000",),
        ).fetchone()

        assert row is not None
        assert row[0] == pytest.approx(q_max_veh_h)

    finally:
        conn.close()


def test_inconsistent_q_max_is_rejected_by_database() -> None:
    """삼각형 기본도와 맞지 않는 q_max가 DB CHECK에서 거부되는지 확인한다."""
    conn = _create_memory_db()

    try:
        _insert_test_corridor(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO cell (
                    cell_id,
                    corridor_id,
                    seq,
                    offset_km_start,
                    offset_km_end,
                    length_km,
                    lanes,
                    lanes_source,
                    v_free_kmh,
                    w_back_kmh,
                    k_jam_veh_km_lane,
                    q_max_veh_h,
                    lat_start,
                    lon_start,
                    lat_end,
                    lon_end
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "bad_q_max",
                    "test_down",
                    0,
                    0.0,
                    0.5,
                    0.5,
                    4,
                    "measured",
                    96.4,
                    W_BACK_KMH,
                    K_JAM_VEH_KM_LANE,
                    5000.0,  # 의도적으로 잘못된 q_max
                    37.0,
                    127.0,
                    37.01,
                    127.01,
                ),
            )

    finally:
        conn.close()


def test_invalid_lanes_source_is_rejected() -> None:
    """lanes_source가 measured/assumed 이외의 값이면 DB에서 거부되는지 확인한다."""
    conn = _create_memory_db()

    try:
        _insert_test_corridor(conn)

        q_max_veh_h = 4 * q_per_lane(
            v_free_kmh=96.4,
            w_back_kmh=W_BACK_KMH,
            k_jam_veh_km_lane=K_JAM_VEH_KM_LANE,
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO cell (
                    cell_id,
                    corridor_id,
                    seq,
                    offset_km_start,
                    offset_km_end,
                    length_km,
                    lanes,
                    lanes_source,
                    v_free_kmh,
                    w_back_kmh,
                    k_jam_veh_km_lane,
                    q_max_veh_h,
                    lat_start,
                    lon_start,
                    lat_end,
                    lon_end
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "bad_lane_source",
                    "test_down",
                    0,
                    0.0,
                    0.5,
                    0.5,
                    4,
                    "unknown",
                    96.4,
                    W_BACK_KMH,
                    K_JAM_VEH_KM_LANE,
                    q_max_veh_h,
                    37.0,
                    127.0,
                    37.01,
                    127.01,
                ),
            )

    finally:
        conn.close()