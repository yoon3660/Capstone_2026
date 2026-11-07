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

CELL_SQL = """
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
"""


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
    row = (
        "c1",
        "gyeongbu_down",
        0,
        0.0,
        0.5,
        0.5,
        4,
        "measured",
        100.0,
        20.0,
        180.0,
        q_max,
        37.0,
        127.0,
        37.01,
        127.01,
    )
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


def test_existing_db_gets_new_columns(tmp_path) -> None:
    """이미 만들어진 DB 에도 새 컬럼이 들어간다 (#24 lanes_source).

    schema.sql 은 CREATE TABLE IF NOT EXISTS 라서, 컬럼만 추가된 경우 기존 DB 는
    그대로 남는다. 그런데 init_db 는 테이블 존재만 보고 성공이라고 말한다.
    그러면 사람마다 스키마가 갈라지고 적재가 엉뚱한 데서 터진다.
    """
    from evdt.io.db import MIGRATIONS, column_names, init_db
    from evdt.paths import SCHEMA_PATH

    # lanes_source 가 없던 시절의 스키마를 만든다
    old_schema = "\n".join(
        line
        for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines()
        if "lanes_source" not in line
    )
    old_schema_path = tmp_path / "old_schema.sql"
    old_schema_path.write_text(old_schema, encoding="utf-8")

    db = tmp_path / "old.db"
    with get_conn(db) as conn:
        conn.executescript(old_schema)

    with get_conn(db) as conn:
        assert "lanes_source" not in column_names(conn, "cell")

    # init_db 가 빠진 컬럼을 채운다
    init_db(db)

    with get_conn(db) as conn:
        for table, column, _ in MIGRATIONS:
            assert column in column_names(conn, table)

        conn.execute(
            "INSERT INTO corridor (corridor_id, name, direction, origin_name, dest_name, "
            "length_km) VALUES ('c', '경부', 'DOWN', 'S', 'B', 400.0)"
        )
        conn.execute(
            CELL_SQL,
            ("c1", "c", 0, 0.0, 0.5, 0.5, 4, "measured",
             100.0, 20.0, 180.0, 4 * 100.0 * 20.0 * 180.0 / 120.0,
             37.0, 127.0, 37.01, 127.01),
        )

        # 새 컬럼은 measured/assumed 만 받는다
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                CELL_SQL,
                ("c2", "c", 1, 0.5, 1.0, 0.5, 4, "guessed",
                 100.0, 20.0, 180.0, 4 * 100.0 * 20.0 * 180.0 / 120.0,
                 37.0, 127.0, 37.01, 127.01),
            )


def test_cell_lanes_source_defaults_to_assumed(seeded_db) -> None:
    """lanes_source 를 빼고 넣어도 막히지 않는다. 대신 기본값은 'assumed' 다.

    NOT NULL 인데 기본값이 없으면 이 컬럼을 모르는 기존 INSERT 가 전부 깨진다
    (#24 머지 직후 verify_setup 이 그렇게 실패했다). 기본값은 "모르는 값" 쪽이어야
    한다 — 실측하지 않은 차로수를 measured 로 기록하면 안 된다.
    """
    with get_conn(seeded_db) as conn:
        conn.execute(
            "INSERT INTO cell (cell_id, corridor_id, seq, offset_km_start, offset_km_end, "
            "length_km, lanes, v_free_kmh, w_back_kmh, k_jam_veh_km_lane, q_max_veh_h, "
            "lat_start, lon_start, lat_end, lon_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("c_default", "gyeongbu_down", 0, 0.0, 0.5, 0.5, 4,
             100.0, 20.0, 180.0, 12000.0, 37.0, 127.0, 37.01, 127.01),
        )
        source = conn.execute(
            "SELECT lanes_source FROM cell WHERE cell_id = 'c_default'"
        ).fetchone()[0]

    assert source == "assumed"


def test_an_existing_db_gets_a_new_stage_in_the_check(tmp_path) -> None:
    """★ 기존 DB 의 `run.stage` CHECK 에 **빠진 스테이지가 채워진다** (#143).

    `schema.sql` 은 `CREATE TABLE IF NOT EXISTS` 다. 그래서 이미 있는 DB 의 CHECK 는
    영원히 안 바뀐다 — 스테이지를 더하면 새 DB 에서는 되고 **기존 DB 에서는 런이
    끝나는 순간** 터진다.

        sqlite3.IntegrityError: CHECK constraint failed: stage IN (...)

    #143 에서 `S3_EXACT` 를 더하면서 실제로 그랬고, 9분짜리 런을 버렸다. #138 의
    `MEC` 도 같은 길을 간다.

    그리고 이 마이그레이션은 **테이블을 다시 만든다.** 그래서 잃을 수 있는 것이
    많다 — 이 테스트가 지키는 것이 그 목록이다.
    """
    import json

    from evdt.config import VALID_STAGES
    from evdt.io.db import init_db
    from evdt.paths import SCHEMA_PATH

    # S3_EXACT 가 없던 시절의 스키마를 만든다
    old = SCHEMA_PATH.read_text(encoding="utf-8").replace(
        "                       'S3_EXACT',         -- S3 그룹을 전수 열거 (#143, 진단)\n", "")
    assert "'S3_EXACT'" not in old, "옛 스키마를 못 만들었다 — 주석 줄이 바뀌었나"

    db = tmp_path / "old.db"
    with get_conn(db) as conn:
        conn.executescript(old)
        conn.execute(
            "INSERT INTO corridor (corridor_id, name, direction, origin_name, dest_name, "
            "length_km) VALUES ('c', '경부', 'DOWN', 'S', 'B', 400.0)")
        conn.execute(
            "INSERT INTO scenario (scenario_id, corridor_id, label, day_type, sim_start_min, "
            "sim_end_min, ev_share, demand_multiplier, temp_c, config_path, config_yaml, "
            "config_hash) VALUES ('sc', 'c', 'n', 'SEOLLAL_PEAK', 0, 1440, 0.1, 1.0, 0.0, "
            "'p', 'y', 'h')")
        conn.execute(
            "INSERT INTO run (run_id, scenario_id, stage, seed, participation, "
            "code_version, output_dir, params_json, status) "
            "VALUES ('r1', 'sc', 'S3', 1, 1.0, 'abc', 'runs/r1', ?, 'DONE')",
            (json.dumps({"s3_window": 4}),))
        conn.execute(
            "INSERT INTO run_kpi (run_id, metric, value, unit) "
            "VALUES ('r1', 'dwell_total_h', 6288.27, 'h')")

    with get_conn(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO run (run_id, scenario_id, stage, seed, participation, "
                "code_version, output_dir) VALUES ('r2','sc','S3_EXACT',2,1.0,'a','b')")

    init_db(db)

    with get_conn(db) as conn:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='run'").fetchone()[0]

        # ① 빠진 스테이지가 다 들어왔다
        for st in VALID_STAGES:
            assert f"'{st}'" in ddl, f"{st} 가 CHECK 에 없다"

        # ② **데이터가 그대로다.** run_kpi 는 CASCADE 로 매달려 있어서, 외래키를 켠
        #    채로 run 을 지우면 전부 사라진다
        assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM run_kpi").fetchone()[0] == 1
        row = conn.execute(
            "SELECT params_json, status, code_version FROM run WHERE run_id='r1'").fetchone()
        assert json.loads(row[0])["s3_window"] == 4
        assert (row[1], row[2]) == ("DONE", "abc")

        # ③ 칸·CHECK·UNIQUE 가 보존됐다. 손으로 적은 DDL 로 다시 만들면 여기서
        #    조용히 사라진다 — 실제로 첫 구현이 participation 을 INTEGER 로 적었다
        for keep in ("code_dirty", "output_dir", "error_message",
                     "participation  REAL", "CHECK (seed >= 0)",
                     "UNIQUE (scenario_id, stage, seed, participation)"):
            assert keep in ddl, f"{keep} 가 사라졌다"

        # ④ 인덱스도 다시 만들어졌다
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name='run' AND type='index'")}
        assert "idx_run_scenario_stage" in idx

        assert not conn.execute("PRAGMA foreign_key_check").fetchall()

        # ⑤ 이제 새 스테이지가 들어가고, **엉뚱한 값은 여전히 막힌다**
        conn.execute(
            "INSERT INTO run (run_id, scenario_id, stage, seed, participation, "
            "code_version, output_dir) VALUES ('r2','sc','S3_EXACT',2,1.0,'a','b')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO run (run_id, scenario_id, stage, seed, participation, "
                "code_version, output_dir) VALUES ('r3','sc','NOPE',3,1.0,'a','b')")


def test_the_stage_migration_is_idempotent(seeded_db) -> None:
    """이미 맞는 DB 에는 **아무것도 하지 않는다.** 매번 테이블을 다시 만들면 안 된다."""
    from evdt.io.db import migrate_stage_check

    with get_conn(seeded_db) as conn:
        assert migrate_stage_check(conn) is False
