"""개발 환경 셀프 체크 (T-01 ~ T-04 완료 조건 자동 확인).

    python scripts/verify_setup.py

팀원이 클론 직후 딱 한 번 돌려보는 스크립트다. 여기서 전부 [OK] 가 뜨면
"내 PC 에서는 안 되는데" 를 물어볼 일이 없다.

실제 프로젝트 DB 를 건드리지 않는다. 전부 임시 폴더에서 돌고 끝나면 지운다.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path

import _bootstrap  # noqa: E402, F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)

ROOT = _bootstrap.ROOT

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str) -> Callable:
    def deco(fn: Callable[[], str]) -> Callable[[], str]:
        def wrapper() -> str:
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
                print(f"  [OK]   {name}" + (f"  — {detail}" if detail else ""))
                return detail
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"  [FAIL] {name}")
                print("         " + "\n         ".join(traceback.format_exc().splitlines()[-4:]))
                return ""

        return wrapper

    return deco


# ---------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="evdt_verify_"))
DB = TMP / "verify.db"
RUNS = TMP / "runs"


def main() -> int:
    print("\n=== evdt 개발 환경 셀프 체크 ===")
    print(f"프로젝트 루트 : {ROOT}")
    print(f"임시 작업 폴더: {TMP}\n")

    print("[A] 인터프리터와 의존성")
    t_python()
    t_imports()

    print("\n[B] T-01  패키지 골격과 의존 방향")
    t_package()
    t_import_boundary()

    print("\n[C] T-02  SQLite 스키마와 DB 접근")
    t_schema()
    t_crud()
    t_foreign_keys()
    t_qmax_check()

    print("\n[D] T-03  시나리오 config 로더")
    t_config_good()
    t_config_bad()

    print("\n[E] T-04  실행 레지스트리와 Parquet")
    t_run_register()
    t_run_duplicate()
    t_parquet()
    t_duckdb()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"실패 {len(failed)}건 / 전체 {len(RESULTS)}건")
        for n in failed:
            print(f"  - {n}")
        print("\n대부분은 의존성 설치 문제다. 먼저 이걸 확인할 것:")
        print("  pip install -r requirements-dev.txt")
        print("  pip install -e .")
        return 1
    print(f"전체 {len(RESULTS)}건 통과. Sprint 1 Epic A 완료 조건을 만족한다.")
    print("=" * 60)
    return 0


# --- A ---------------------------------------------------------------------
@check("Python 3.12 이상")
def t_python() -> str:
    if sys.version_info < (3, 12):  # noqa: UP036 — 구버전 인터프리터로 실행됐을 때를 잡는 검사다
        raise RuntimeError(f"3.12 이상이 필요하다 (현재 {sys.version.split()[0]})")
    return sys.version.split()[0]


@check("의존 패키지 임포트")
def t_imports() -> str:
    import duckdb
    import numpy
    import pandas
    import pyarrow
    import scipy
    import yaml

    versions = [
        f"numpy {numpy.__version__}",
        f"pandas {pandas.__version__}",
        f"scipy {scipy.__version__}",
        f"pyarrow {pyarrow.__version__}",
        f"duckdb {duckdb.__version__}",
        f"pyyaml {yaml.__version__}",
    ]
    # 아래 셋은 Sprint 1 후반~3 에서 쓴다. 없으면 경고만.
    optional = []
    for mod in ("simpy", "pulp", "matplotlib"):
        try:
            m = __import__(mod)
            versions.append(f"{mod} {getattr(m, '__version__', '?')}")
        except ImportError:
            optional.append(mod)
    if optional:
        versions.append(f"(미설치: {', '.join(optional)} — Sprint 1 후반에 필요)")
    return ", ".join(versions)


# --- B ---------------------------------------------------------------------
@check("import evdt")
def t_package() -> str:
    import evdt

    return f"v{evdt.__version__}, root={evdt.PROJECT_ROOT.name}"


@check("의존 방향: world 가 engine 을 임포트하지 않는다")
def t_import_boundary() -> str:
    import ast

    world = ROOT / "src" / "evdt" / "world"
    offenders = []
    for py in world.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for m in mods:
                if m == "evdt.engine" or m.startswith("evdt.engine."):
                    offenders.append(f"{py.relative_to(ROOT)}:{node.lineno}")
    if offenders:
        raise AssertionError("world 가 engine 을 임포트한다: " + ", ".join(offenders))
    return f"{len(list(world.rglob('*.py')))}개 파일 검사"


# --- C ---------------------------------------------------------------------
@check("schema.sql 적용, 10개 테이블 생성")
def t_schema() -> str:
    from evdt.io.db import EXPECTED_TABLES, get_conn, init_db, table_names

    init_db(DB)
    with get_conn(DB, readonly=True) as conn:
        found = table_names(conn)
    missing = set(EXPECTED_TABLES) - set(found)
    if missing:
        raise AssertionError(f"없는 테이블: {missing}")
    return f"{len(EXPECTED_TABLES)}개 테이블"


@check("더미 행 삽입·조회 (corridor → station → charger)")
def t_crud() -> str:
    import pandas as pd

    from evdt.io.db import get_conn, read_table, upsert_df

    with get_conn(DB) as conn:
        upsert_df(conn, "corridor", pd.DataFrame([{
            "corridor_id": "gyeongbu_down", "name": "경부고속도로", "direction": "DOWN",
            "origin_name": "Seoul", "dest_name": "Busan", "length_km": 416.0,
        }]))
        upsert_df(conn, "station", pd.DataFrame([{
            "station_id": "gyeongbu_down_anseong", "corridor_id": "gyeongbu_down",
            "name": "안성휴게소", "direction": "DOWN", "offset_km": 62.0,
            "lat": 37.0075, "lon": 127.2700, "n_parking": 320, "source": "dummy",
        }]))
        upsert_df(conn, "charger", pd.DataFrame([
            {"charger_id": "anseong_200", "station_id": "gyeongbu_down_anseong",
             "power_kw": 200.0, "n_units": 4, "source": "dummy"},
            {"charger_id": "anseong_100", "station_id": "gyeongbu_down_anseong",
             "power_kw": 100.0, "n_units": 2, "source": "dummy"},
        ]))
        cap = read_table(conn, "v_station_capacity")
    row = cap.iloc[0]
    if int(row["n_chargers"]) != 6:
        raise AssertionError(f"충전기 기수가 6이어야 하는데 {row['n_chargers']}")
    return f"{row['name']} 충전기 {int(row['n_chargers'])}기 / {row['total_power_kw']:.0f}kW"


@check("외래키 제약이 실제로 작동한다")
def t_foreign_keys() -> str:
    import sqlite3

    from evdt.io.db import get_conn

    try:
        with get_conn(DB) as conn:
            conn.execute(
                "INSERT INTO charger (charger_id, station_id, power_kw) VALUES (?, ?, ?)",
                ("orphan", "존재하지_않는_충전소", 100.0),
            )
    except sqlite3.IntegrityError:
        return "없는 station_id 삽입이 거부됨"
    raise AssertionError("FK 위반이 통과했다 — PRAGMA foreign_keys 가 꺼져 있다")


@check("CTM 기본도 일관성 CHECK (§9.5 q_max 함정)")
def t_qmax_check() -> str:
    import sqlite3

    from evdt.io.db import get_conn

    # v=100, w=20, k_jam=180/lane, lanes=4  →  q_max = 4*100*20*180/120 = 12000
    good = ("c_ok", "gyeongbu_down", 0, 0.0, 0.5, 0.5, 4, 100.0, 20.0, 180.0, 12000.0,
            37.0, 127.0, 37.01, 127.01)
    bad = ("c_bad", "gyeongbu_down", 1, 0.5, 1.0, 0.5, 4, 100.0, 20.0, 180.0, 9999.0,
           37.0, 127.0, 37.01, 127.01)
    sql = (
        "INSERT INTO cell (cell_id, corridor_id, seq, offset_km_start, offset_km_end, "
        "length_km, lanes, v_free_kmh, w_back_kmh, k_jam_veh_km_lane, q_max_veh_h, "
        "lat_start, lon_start, lat_end, lon_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    with get_conn(DB) as conn:
        conn.execute(sql, good)
    try:
        with get_conn(DB) as conn:
            conn.execute(sql, bad)
    except sqlite3.IntegrityError:
        return "일관된 셀은 통과, q_max 가 어긋난 셀은 거부됨"
    raise AssertionError("q_max 일관성 검사가 작동하지 않는다 — 정체가 안 생기는 버그로 이어진다")


# --- D ---------------------------------------------------------------------
@check("정상 config 3종 로딩")
def t_config_good() -> str:
    from evdt.config import ScenarioConfig

    names = []
    for p in sorted((ROOT / "config").glob("scenario_*.yaml")):
        cfg = ScenarioConfig.from_yaml(p)
        names.append(f"{cfg.scenario_id}({cfg.config_hash})")
    if len(names) < 3:
        raise AssertionError(f"config/ 에 시나리오가 {len(names)}개뿐이다")
    return ", ".join(names)


@check("고의로 망친 config 3종이 명확한 메시지로 실패")
def t_config_bad() -> str:
    from evdt.config import ConfigError, ScenarioConfig

    expectations = {
        "bad_missing_key.yaml": ["time", "ev_share"],
        "bad_out_of_range.yaml": ["ev_share", "participation", "end_min", "hi"],
        "bad_typo.yaml": ["day_type", "vehicles", "stage"],
    }
    summary = []
    for fname, needles in expectations.items():
        path = ROOT / "tests" / "fixtures" / fname
        try:
            ScenarioConfig.from_yaml(path)
        except ConfigError as exc:
            msg = str(exc)
            missed = [n for n in needles if n not in msg]
            if missed:
                raise AssertionError(f"{fname} 메시지에 {missed} 가 없다:\n{msg}") from exc
            n_lines = msg.count("\n  - ")
            summary.append(f"{fname}→{n_lines}건")
        else:
            raise AssertionError(f"{fname} 가 에러 없이 통과했다")
    return ", ".join(summary)


# --- E ---------------------------------------------------------------------
_HANDLE = {}


@check("run 등록, runs/<run_id>/ 생성, git SHA 기록")
def t_run_register() -> str:
    from evdt.config import ScenarioConfig
    from evdt.io.run_registry import register_run

    cfg = ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
    handle = register_run(cfg, seed=7, db_path=DB, runs_dir=RUNS)
    _HANDLE["h"] = handle
    for f in ("meta.json", "config.yaml"):
        if not (handle.output_dir / f).is_file():
            raise AssertionError(f"{f} 가 생성되지 않았다")
    return f"{handle.run_id}  (code={handle.code_version})"


@check("중복 실험 차단 (scenario, stage, seed, participation)")
def t_run_duplicate() -> str:
    from evdt.config import ScenarioConfig
    from evdt.io.run_registry import RunExistsError, register_run

    cfg = ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
    try:
        register_run(cfg, seed=7, db_path=DB, runs_dir=RUNS)
    except RunExistsError:
        handle = register_run(cfg, seed=7, db_path=DB, runs_dir=RUNS, overwrite=True)
        return f"거부 후 overwrite=True 로만 재등록 가능 ({handle.run_id})"
    raise AssertionError("같은 조건이 두 번 등록됐다 — UNIQUE 제약이 없다")


@check("Parquet writer (스냅샷 스키마 포함)")
def t_parquet() -> str:
    from evdt.io.writers import ParquetRunWriter

    handle = _HANDLE["h"]
    with ParquetRunWriter(handle.output_dir, handle.run_id) as w:
        for i in range(120):
            t_arr = float(i * 3)
            wait = float(i % 17)
            w.append("charge_event", {
                "ev_id": f"ev{i:04d}", "vclass_id": "ioniq5", "power_kw": 200.0,
                "station_id": "gyeongbu_down_anseong", "charger_id": "anseong_200",
                "t_arrive_min": t_arr, "t_start_min": t_arr + wait,
                "t_end_min": t_arr + wait + 25.0, "wait_min": wait,
                "charge_min": 25.0, "dwell_min": wait + 25.0,
                "soc_in": 0.25, "soc_out": 0.8, "soc_target": 0.8,
                "energy_kwh": 40.0, "cold_factor": 0.82, "was_reassigned": False,
            })
            w.snapshot(t_arr, "station", "gyeongbu_down_anseong",
                       37.0075, 127.27, "queue_len", float(i % 9))
        counts = w.close()
    if counts["charge_event"] != 120 or counts["snapshot"] != 120:
        raise AssertionError(f"기록 행 수가 맞지 않는다: {counts}")
    return f"charge_event {counts['charge_event']}행, snapshot {counts['snapshot']}행"


@check("DuckDB 로 SQLite + Parquet 조인 질의")
def t_duckdb() -> str:
    from evdt.io.loaders import duck_connect

    handle = _HANDLE["h"]
    con = duck_connect(db_path=DB, run_ids=[handle.run_id], runs_dir=RUNS)
    rows = con.sql(
        """
        SELECT s.name                                AS station,
               CAST(e.t_arrive_min / 60 AS INTEGER)  AS hour,
               COUNT(*)                              AS n,
               AVG(e.wait_min)                       AS avg_wait_min
        FROM charge_event e
        JOIN station s USING (station_id)
        GROUP BY 1, 2
        ORDER BY avg_wait_min DESC
        LIMIT 3
        """
    ).fetchall()
    if not rows:
        raise AssertionError("조인 결과가 비었다")
    top = rows[0]
    return f"휴게소×시간대 {len(rows)}행 중 최대 대기 {top[0]} {top[1]}시 {top[3]:.1f}분"


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
