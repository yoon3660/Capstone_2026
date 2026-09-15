"""가짜 데이터로 파이프라인을 한 바퀴 돌려본다 (핸즈온 STEP 09).

verify_setup.py 는 임시 폴더에서 돌고 흔적을 지운다. 이 스크립트는 **진짜
프로젝트 폴더에** 결과를 남긴다. 그래야 runs/ 폴더와 DB 행을 눈으로 볼 수 있다.

    python scripts/smoke_run.py

여기서 만드는 숫자는 전부 가짜다. 시뮬레이터(T-16)가 아직 없기 때문이다.
확인하려는 것은 "숫자가 맞는가" 가 아니라 **배관이 이어져 있는가** 다:
    config → run 등록 → Parquet 기록 → KPI 저장 → DuckDB 조회

지우려면 폴더 하나와 DB 행 하나만 지우면 된다 (마지막에 명령을 출력한다).
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pandas as pd  # noqa: E402
from _bootstrap import ROOT  # noqa: E402  (src 경로와 콘솔 인코딩을 먼저 준비한다)

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.io.db import get_conn, read_table, upsert_df  # noqa: E402
from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.io.run_registry import RunContext  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402

SMOKE_SEED = 9999
STATIONS = [
    # (station_id, 이름, 기점 거리 km, 위도, 경도, 충전기 기수)
    ("smoke_anseong", "안성휴게소(가짜)", 62.0, 37.0075, 127.2700, 4),
    ("smoke_cheonan", "천안휴게소(가짜)", 92.0, 36.8200, 127.1400, 2),
    ("smoke_okcheon", "옥천휴게소(가짜)", 185.0, 36.3100, 127.5700, 6),
]


def ensure_smoke_stations(db: Path) -> None:
    """가짜 휴게소 3곳을 넣는다. T-06 이 실제 데이터를 넣으면 지우면 된다."""
    stations = pd.DataFrame([
        {
            "station_id": sid, "corridor_id": "gyeongbu_down", "name": name,
            "direction": "DOWN", "offset_km": km, "lat": lat, "lon": lon,
            "source": "smoke",
        }
        for sid, name, km, lat, lon, _ in STATIONS
    ])
    chargers = pd.DataFrame([
        {
            "charger_id": f"{sid}_200", "station_id": sid, "power_kw": 200.0,
            "n_units": units, "source": "smoke",
        }
        for sid, _, _, _, _, units in STATIONS
    ])
    with get_conn(db) as conn:
        if conn.execute("SELECT 1 FROM corridor WHERE corridor_id='gyeongbu_down'").fetchone() is None:
            raise SystemExit(
                "corridor 가 없다. 먼저 실행할 것:\n"
                "    python scripts/init_db.py --seed-corridor"
            )
        upsert_df(conn, "station", stations)
        upsert_df(conn, "charger", chargers)


def main() -> int:
    db = default_db_path()
    if not db.exists():
        print(f"DB 가 없다: {db}\n먼저 실행할 것:  python scripts/init_db.py --seed-corridor")
        return 1

    ensure_smoke_stations(db)

    cfg = ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
    rng = random.Random(SMOKE_SEED)

    print(f"\n시나리오 : {cfg.scenario_id}  ({cfg.label})")
    print(f"설정 해시 : {cfg.config_hash}")

    with RunContext.open(cfg, seed=SMOKE_SEED, db_path=db, overwrite=True) as run:
        print(f"run_id   : {run.run_id}")
        print(f"출력 폴더 : {run.output_dir}")

        waits: list[float] = []
        for i in range(600):
            # 오전 9시(540분) 근처에 도착이 몰리는 가짜 프로파일
            t_arr = max(0.0, rng.gauss(540, 150))
            sid, name, _, lat, lon, units = STATIONS[
                0 if rng.random() < 0.6 else rng.randrange(1, 3)   # 안성에 일부러 쏠리게
            ]
            # 도착이 몰릴수록 대기가 길어지는 모양만 흉내낸다
            peak = math.exp(-((t_arr - 540) ** 2) / (2 * 120**2))
            wait = max(0.0, rng.gauss(45 * peak / units * 4, 6))
            charge = rng.uniform(18, 35)
            waits.append(wait)

            run.writer.append("charge_event", {
                "ev_id": f"smoke{i:05d}", "vclass_id": "smoke_ev",
                "station_id": sid, "charger_id": f"{sid}_200", "power_kw": 200.0,
                "t_arrive_min": t_arr, "t_start_min": t_arr + wait,
                "t_end_min": t_arr + wait + charge,
                "wait_min": wait, "charge_min": charge, "dwell_min": wait + charge,
                "soc_in": rng.uniform(0.12, 0.45), "soc_out": 0.8, "soc_target": 0.8,
                "energy_kwh": rng.uniform(25, 55), "cold_factor": 0.82,
                "was_reassigned": False,
            })
            run.writer.snapshot(round(t_arr / 5) * 5.0, "station", sid, lat, lon,
                                "queue_len", float(rng.randint(0, 12)))

        waits.sort()
        run.kpi("wait_p95_min", waits[int(len(waits) * 0.95)], "min")
        run.kpi("wait_mean_min", sum(waits) / len(waits), "min")
        run.kpi("n_charge_events", float(len(waits)), "count")
        run_id = run.run_id

    # --- 확인 -------------------------------------------------------------
    print("\n--- DB 에 남은 것 ---")
    with get_conn(db, readonly=True) as conn:
        r = read_table(conn, "run", where="run_id = ?", params=(run_id,)).iloc[0]
        kpi = read_table(conn, "run_kpi", where="run_id = ?", params=(run_id,))
    print(f"  status={r['status']}  code_version={r['code_version']}  seed={r['seed']}")
    for _, k in kpi.iterrows():
        print(f"  {k['metric']:<18} {k['value']:>8.2f} {k['unit']}")

    print("\n--- DuckDB 로 뽑은 휴게소×시간대 평균 대기 (상위 5) ---")
    con = duck_connect(db_path=db, run_ids=[run_id])
    print(con.sql(
        """
        SELECT s.name                               AS 휴게소,
               CAST(e.t_arrive_min / 60 AS INTEGER) AS 시각,
               COUNT(*)                             AS 대수,
               ROUND(AVG(e.wait_min), 1)            AS 평균대기분
        FROM charge_event e JOIN station s USING (station_id)
        GROUP BY 1, 2 ORDER BY 평균대기분 DESC LIMIT 5
        """
    ).df().to_string(index=False))

    print("\n--- 만들어진 파일 ---")
    for p in sorted((ROOT / "runs" / run_id).iterdir()):
        print(f"  runs/{run_id}/{p.name}  ({p.stat().st_size:,} bytes)")

    print("\n배관 이상 없음. 지우려면:")
    print("  python scripts/smoke_clean.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
