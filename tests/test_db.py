"""T-02 완료 조건: 10개 테이블 확인, 더미 1행 삽입·조회.

여기에 더해 "조용히 틀리는" 세 가지를 회귀 테스트로 막는다:
  - FK 가 꺼진 커넥션
  - upsert 가 자식 행을 지우는 것
  - q_max 가 기본도와 어긋난 셀 (§9.5)
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from evdt.io.db import (
    EXPECTED_TABLES,
    SchemaError,
    get_conn,
    init_db,
    read_table,
    table_names,
    upsert_df,
    validate_master,
)

CELL_SQL = (
    "INSERT INTO cell (cell_id, corridor_id, seq, offset_km_start, offset_km_end, "
    "length_km, lanes, v_free_kmh, w_back_kmh, k_jam_veh_km_lane, q_max_veh_h, "
    "lat_start, lon_start, lat_end, lon_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def test_schema_creates_expected_tables(db_path) -> None:
    with get_conn(db_path, readonly=True) as conn:
        found = set(table_names(conn))
    assert set(EXPECTED_TABLES).issubset(found)


def test_init_db_is_idempotent(db_path) -> None:
    init_db(db_path)
    init_db(db_path)
    with get_conn(db_path, readonly=True) as conn:
        assert set(EXPECTED_TABLES).issubset(set(table_names(conn)))


def test_insert_and_read_back(seeded_db) -> None:
    with get_conn(seeded_db, readonly=True) as conn:
        stations = read_table(conn, "station", where="direction = ?", params=("DOWN",))
        cap = read_table(conn, "v_station_capacity")
    assert len(stations) == 1
    assert stations.iloc[0]["name"] == "안성휴게소"
    assert int(cap.iloc[0]["n_chargers"]) == 4


def test_foreign_keys_are_enforced(seeded_db) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with get_conn(seeded_db) as conn:
            conn.execute(
                "INSERT INTO charger (charger_id, station_id, power_kw) VALUES (?,?,?)",
                ("orphan", "no_such_station", 100.0),
            )


def test_upsert_updates_without_cascading_delete(seeded_db) -> None:
    """INSERT OR REPLACE 였다면 station 갱신이 charger 를 지웠을 것이다."""
    with get_conn(seeded_db) as conn:
        upsert_df(conn, "station", pd.DataFrame([{
            "station_id": "st_anseong", "corridor_id": "gyeongbu_down",
            "name": "안성휴게소(수정)", "direction": "DOWN", "offset_km": 63.5,
            "lat": 37.0075, "lon": 127.27, "source": "test",
        }]))
    with get_conn(seeded_db, readonly=True) as conn:
        st = read_table(conn, "station")
        ch = read_table(conn, "charger")
    assert st.iloc[0]["name"] == "안성휴게소(수정)"
    assert st.iloc[0]["offset_km"] == pytest.approx(63.5)
    assert len(ch) == 1, "station 을 갱신했더니 charger 가 사라졌다"


def test_unknown_column_fails_loudly(seeded_db) -> None:
    with pytest.raises(SchemaError, match="없는 컬럼"):
        with get_conn(seeded_db) as conn:
            upsert_df(conn, "station", pd.DataFrame([{"station_id": "x", "typo_col": 1}]))


@pytest.mark.parametrize(
    ("q_max", "should_pass"),
    [(12000.0, True), (12050.0, True), (9999.0, False), (20000.0, False)],
)
def test_ctm_fundamental_diagram_consistency(seeded_db, q_max, should_pass) -> None:
    """q_max = lanes * v*w*k/(v+w). 어기면 정체가 아예 생기지 않는다 (§9.5)."""
    row = ("c1", "gyeongbu_down", 0, 0.0, 0.5, 0.5, 4, 100.0, 20.0, 180.0, q_max,
           37.0, 127.0, 37.01, 127.01)
    if should_pass:
        with get_conn(seeded_db) as conn:
            conn.execute(CELL_SQL, row)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            with get_conn(seeded_db) as conn:
                conn.execute(CELL_SQL, row)


def test_validate_master_flags_bad_share(seeded_db) -> None:
    with get_conn(seeded_db) as conn:
        upsert_df(conn, "vehicle_class", pd.DataFrame([
            {"vclass_id": "a", "name": "A", "battery_kwh": 77.0, "vmax_kw": 220.0,
             "consumption_kwh_km": 0.18, "share": 0.4},
            {"vclass_id": "b", "name": "B", "battery_kwh": 64.0, "vmax_kw": 100.0,
             "consumption_kwh_km": 0.16, "share": 0.4},
        ]))
    with get_conn(seeded_db, readonly=True) as conn:
        problems = validate_master(conn)
    assert any("share" in p for p in problems)


def test_validate_master_passes_on_empty_db(db_path) -> None:
    with get_conn(db_path, readonly=True) as conn:
        assert validate_master(conn) == []
