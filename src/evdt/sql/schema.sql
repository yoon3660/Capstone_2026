-- ===========================================================================
--  EV Charging Digital Twin — SQLite 정적 마스터 스키마
--  설계문서 §9.3 "재현 가능한 것은 Parquet, 재현의 근거는 SQLite"
--
--  이 파일에 들어오는 것   : 입력 마스터 데이터 + 실험의 신원(identity)
--  이 파일에 들어오지 않는 것: 시뮬레이션이 뱉는 대용량 이벤트 로그 → Parquet
--
--  적용:  python -m evdt.io.db init       (또는 scripts/init_db.py)
--  이 파일은 여러 번 실행해도 안전하다 (CREATE ... IF NOT EXISTS).
-- ===========================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;


-- ---------------------------------------------------------------------------
-- 1. corridor — 교통축. 상행/하행은 서로 다른 corridor 2개다 (§1.2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corridor (
    corridor_id   TEXT    PRIMARY KEY,                 -- 'gyeongbu_down'
    name          TEXT    NOT NULL,                    -- '경부고속도로'
    direction     TEXT    NOT NULL CHECK (direction IN ('UP', 'DOWN')),
    origin_name   TEXT    NOT NULL,                    -- 'Seoul'
    dest_name     TEXT    NOT NULL,                    -- 'Busan'
    length_km     REAL    NOT NULL CHECK (length_km > 0),
    note          TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, direction)
);


-- ---------------------------------------------------------------------------
-- 2. cell — CTM 셀 (T-09). Sprint 1 에서는 비어 있어도 된다.
--
--    ⚠ 삼각형 기본도에서 q_max 는 독립 파라미터가 아니다 (§9.5):
--         q_max_per_lane = v_free * w_back * k_jam_per_lane / (v_free + w_back)
--       이걸 어기면 정체가 아예 생기지 않는다. 그래서 DB 레벨 CHECK 로 못 박는다.
--       (허용 오차 1% — 단위 변환 반올림만 흡수한다.)
--
--    ⚠ lat/lon 을 지금 넣는 이유 (설계 규칙 3): 나중에 2.5D 렌더링을 붙일 때
--       좌표가 없으면 셀 분할을 처음부터 다시 해야 한다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cell (
    cell_id            TEXT    PRIMARY KEY,            -- 'gyeongbu_down_0042'
    corridor_id        TEXT    NOT NULL REFERENCES corridor(corridor_id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,               -- 진행 방향 순서 0,1,2...
    offset_km_start    REAL    NOT NULL CHECK (offset_km_start >= 0),
    offset_km_end      REAL    NOT NULL,
    length_km          REAL    NOT NULL CHECK (length_km > 0),
    lanes              INTEGER NOT NULL CHECK (lanes >= 1),

    v_free_kmh         REAL    NOT NULL CHECK (v_free_kmh > 0),
    w_back_kmh         REAL    NOT NULL CHECK (w_back_kmh > 0),
    k_jam_veh_km_lane  REAL    NOT NULL CHECK (k_jam_veh_km_lane > 0),  -- 차로당
    q_max_veh_h        REAL    NOT NULL CHECK (q_max_veh_h > 0),        -- 셀 전체(차로 합)

    lat_start          REAL    NOT NULL,
    lon_start          REAL    NOT NULL,
    lat_end            REAL    NOT NULL,
    lon_end            REAL    NOT NULL,

    UNIQUE (corridor_id, seq),
    CHECK (offset_km_end > offset_km_start),
    -- 기본도 일관성 검사
    CHECK (
        abs(
            q_max_veh_h
            - (lanes * v_free_kmh * w_back_kmh * k_jam_veh_km_lane)
              / (v_free_kmh + w_back_kmh)
        ) <= 0.01 * q_max_veh_h
    )
);
CREATE INDEX IF NOT EXISTS idx_cell_corridor_offset ON cell (corridor_id, offset_km_start);


-- ---------------------------------------------------------------------------
-- 3. station — 휴게소 충전소 (T-06).
--    offset_km : 시뮬레이션 좌표계 (기점 기준 선형 거리)
--    lat / lon : 렌더링 좌표계
--    둘 다 저장한다 (설계 규칙 3).
--    cell_id 는 T-09 에서 채워지므로 지금은 NULL 허용.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS station (
    station_id     TEXT    PRIMARY KEY,                -- 'gyeongbu_down_anseong'
    corridor_id    TEXT    NOT NULL REFERENCES corridor(corridor_id) ON DELETE CASCADE,
    cell_id        TEXT             REFERENCES cell(cell_id) ON DELETE SET NULL,
    name           TEXT    NOT NULL,                   -- '안성휴게소'
    direction      TEXT    NOT NULL CHECK (direction IN ('UP', 'DOWN')),
    offset_km      REAL    NOT NULL CHECK (offset_km >= 0),
    lat            REAL    NOT NULL CHECK (lat BETWEEN 33.0 AND 39.0),
    lon            REAL    NOT NULL CHECK (lon BETWEEN 124.0 AND 132.0),
    n_parking      INTEGER          CHECK (n_parking IS NULL OR n_parking >= 0),
    source         TEXT    NOT NULL DEFAULT 'unknown', -- 'moe_api' | 'manual' | ...
    source_key     TEXT,                               -- 원시 데이터의 식별자 (statId 등)
    collected_at   TEXT,                               -- 원시 수집 시각 (추적용)
    UNIQUE (corridor_id, name)
);
CREATE INDEX IF NOT EXISTS idx_station_corridor_offset ON station (corridor_id, offset_km);


-- ---------------------------------------------------------------------------
-- 4. charger — 충전기 (T-06). 한 행 = 같은 사양의 충전기 n_units 기.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS charger (
    charger_id      TEXT    PRIMARY KEY,               -- 'gyeongbu_down_anseong_200kw'
    station_id      TEXT    NOT NULL REFERENCES station(station_id) ON DELETE CASCADE,
    power_kw        REAL    NOT NULL CHECK (power_kw > 0),
    connector_type  TEXT    NOT NULL DEFAULT 'DC_COMBO',
    n_units         INTEGER NOT NULL DEFAULT 1 CHECK (n_units >= 1),
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    source          TEXT    NOT NULL DEFAULT 'unknown',
    source_key      TEXT,
    UNIQUE (station_id, power_kw, connector_type)
);
CREATE INDEX IF NOT EXISTS idx_charger_station ON charger (station_id);


-- ---------------------------------------------------------------------------
-- 5. vehicle_class — 차종 (T-10). share 합이 1.0 이어야 한다
--    (SQLite 는 테이블 전체 제약을 못 걸어서 Python 쪽 validate_master() 가 검사).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vehicle_class (
    vclass_id            TEXT PRIMARY KEY,             -- 'ioniq5_long'
    name                 TEXT NOT NULL,
    battery_kwh          REAL NOT NULL CHECK (battery_kwh > 0),
    vmax_kw              REAL NOT NULL CHECK (vmax_kw > 0),   -- 차량이 받을 수 있는 최대 DC 출력
    consumption_kwh_km   REAL NOT NULL CHECK (consumption_kwh_km > 0),
    share                REAL NOT NULL CHECK (share >= 0.0 AND share <= 1.0),
    source               TEXT NOT NULL DEFAULT 'unknown'
);


-- ---------------------------------------------------------------------------
-- 6. charge_curve — 구간별 충전곡선 (T-10). SoC 구간마다 받을 수 있는 출력.
--    구간은 [soc_from, soc_to) 로 해석한다. 차종별로 0.0~1.0 을 빈틈없이 덮어야 한다
--    (검사는 validate_master()).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS charge_curve (
    vclass_id   TEXT NOT NULL REFERENCES vehicle_class(vclass_id) ON DELETE CASCADE,
    soc_from    REAL NOT NULL CHECK (soc_from >= 0.0 AND soc_from < 1.0),
    soc_to      REAL NOT NULL CHECK (soc_to   >  0.0 AND soc_to  <= 1.0),
    power_kw    REAL NOT NULL CHECK (power_kw > 0),
    PRIMARY KEY (vclass_id, soc_from),
    CHECK (soc_to > soc_from)
);


-- ---------------------------------------------------------------------------
-- 7. temp_efficiency — 온도-효율 테이블 (T-11).
--    설을 고른 이유가 저온이므로 이 표가 명절 시나리오의 근거다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS temp_efficiency (
    temp_c              REAL NOT NULL PRIMARY KEY,
    range_factor        REAL NOT NULL CHECK (range_factor > 0 AND range_factor <= 1.2),
    charge_power_factor REAL NOT NULL CHECK (charge_power_factor > 0 AND charge_power_factor <= 1.2),
    source              TEXT NOT NULL DEFAULT 'unknown'
);


-- ---------------------------------------------------------------------------
-- 8. scenario — 실험 조건. config YAML 전문과 해시를 같이 박아둔다.
--    "이 숫자 어디서 나왔지" 에 답하려면 config 원문이 DB 안에 있어야 한다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenario (
    scenario_id       TEXT NOT NULL PRIMARY KEY,
    corridor_id       TEXT NOT NULL REFERENCES corridor(corridor_id),
    label             TEXT NOT NULL,
    day_type          TEXT NOT NULL CHECK (day_type IN ('SEOLLAL_PEAK', 'CHUSEOK_PEAK', 'WEEKEND_BASE', 'WEEKDAY_BASE')),
    sim_start_min     INTEGER NOT NULL CHECK (sim_start_min >= 0),
    sim_end_min       INTEGER NOT NULL,
    ev_share          REAL NOT NULL CHECK (ev_share >= 0.0 AND ev_share <= 1.0),
    demand_multiplier REAL NOT NULL CHECK (demand_multiplier > 0),
    temp_c            REAL NOT NULL,
    config_path       TEXT NOT NULL,
    config_yaml       TEXT NOT NULL,                   -- YAML 전문
    config_hash       TEXT NOT NULL,                   -- sha256(config_yaml)[:16]
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (sim_end_min > sim_start_min)
);
CREATE INDEX IF NOT EXISTS idx_scenario_hash ON scenario (config_hash);


-- ---------------------------------------------------------------------------
-- 9. run — 실행 레지스트리 (T-04). 결과 재현에 필요한 5개 필드:
--      scenario_id / stage / seed / participation / code_version
--    UNIQUE 제약이 중복 실험을 막는다 (설계문서 §9.3).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run (
    run_id         TEXT    PRIMARY KEY,
    scenario_id    TEXT    NOT NULL REFERENCES scenario(scenario_id) ON DELETE CASCADE,
    stage          TEXT    NOT NULL CHECK (stage IN (
                       'UE', 'S0', 'S1', 'S2', 'S3', 'S4',
                       'MEC',              -- 한계 외부비용 순차 배정 (S1 변형 / fallback)
                       'PAPER',            -- 기반 논문 재현 (사다리의 한 칸)
                       'PERFECT_ROLLING',  -- 완전 예측 롤링
                       'ORACLE'            -- 사후 최적해
                   )),
    seed           INTEGER NOT NULL CHECK (seed >= 0),
    participation  REAL    NOT NULL CHECK (participation >= 0.0 AND participation <= 1.0),

    code_version   TEXT    NOT NULL,                   -- git SHA (짧은 형식)
    code_dirty     INTEGER NOT NULL DEFAULT 0 CHECK (code_dirty IN (0, 1)),
    params_json    TEXT    NOT NULL DEFAULT '{}',      -- 스테이지별 추가 파라미터
    output_dir     TEXT    NOT NULL,                   -- 'runs/<run_id>'

    status         TEXT    NOT NULL DEFAULT 'RUNNING'
                           CHECK (status IN ('RUNNING', 'DONE', 'FAILED')),
    error_message  TEXT,
    started_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    hostname       TEXT,
    python_version TEXT,

    UNIQUE (scenario_id, stage, seed, participation)
);
CREATE INDEX IF NOT EXISTS idx_run_scenario_stage ON run (scenario_id, stage);


-- ---------------------------------------------------------------------------
-- 10. run_kpi — 실행별 집계 지표 (§8.1).
--     지표는 전부 "낮을수록 좋다" 로 통일한다.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_kpi (
    run_id  TEXT NOT NULL REFERENCES run(run_id) ON DELETE CASCADE,
    metric  TEXT NOT NULL,      -- 'total_social_cost_krw', 'wait_p95_min', ...
    value   REAL NOT NULL,
    unit    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, metric)
);


-- ---------------------------------------------------------------------------
-- 편의 뷰 — 충전소별 총 충전기 기수 / 총 출력
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_station_capacity AS
SELECT
    s.corridor_id,
    s.station_id,
    s.name,
    s.direction,
    s.offset_km,
    COALESCE(SUM(c.n_units), 0)                AS n_chargers,
    COALESCE(SUM(c.n_units * c.power_kw), 0.0) AS total_power_kw,
    MAX(c.power_kw)                            AS max_power_kw
FROM station s
LEFT JOIN charger c
       ON c.station_id = s.station_id AND c.is_active = 1
GROUP BY s.station_id;
