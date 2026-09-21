"""가짜 데이터로 파이프라인을 한 바퀴 돌려본다 (핸즈온 STEP 09).

verify_setup.py 는 임시 폴더에서 돌고 흔적을 지운다. 이 스크립트는 **진짜
프로젝트 폴더에** 결과를 남긴다. 그래야 runs/ 폴더와 DB 행을 눈으로 볼 수 있다.

    python scripts/smoke_run.py

휴게소와 도착 수요는 여전히 가짜다. 하지만 **대기시간은 진짜다** — T-16 의 DES 가
실제로 큐를 돌려서 나온 값이다 (예전에는 rng.gauss 로 지어낸 숫자였다).
확인하려는 것은 배관이 이어져 있는가다:
    config → run 등록 → DB 에서 충전기 대수 → DES → Parquet → KPI → DuckDB

지우려면 폴더 하나와 DB 행 하나만 지우면 된다 (마지막에 명령을 출력한다).
"""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd  # noqa: E402
from _bootstrap import ROOT  # noqa: E402  (src 경로와 콘솔 인코딩을 먼저 준비한다)

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.io.db import get_conn, read_table, upsert_df  # noqa: E402
from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.io.run_registry import RunContext  # noqa: E402
from evdt.io.stations import read_station_chargers  # noqa: E402
from evdt.io.vehicles import curve_segments, load_from_db, temp_table  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.world.charging import temp_factors  # noqa: E402
from evdt.world.sim import (  # noqa: E402
    EVArrival,
    check_queue_config,
    run_charging_des,
    station_specs,
)

SMOKE_SEED = 9999

#: 가짜 수요 대수. 이 3곳의 하루 처리 가능 대수는 약 600대(충전기 12기 × 24시간,
#: -5°C 에서 한 대 28분)인데, 60% 를 안성으로 몰고 오전 9시에 집중시키기 때문에
#: 600을 넣으면 큐가 발산해서 대기가 30시간까지 간다. 예전에는 숫자를 지어냈기
#: 때문에 이 모순이 드러나지 않았다. 시나리오 종료(24시) 안에 끝나는 값으로 잡는다.
N_EV = 150

#: 도착 시각 분포 (분). 오전 9시 전후로 몰리는 가짜 프로파일.
PEAK_MIN = 540.0
PEAK_SD_MIN = 150.0
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


def build_arrivals(cfg, rng, stations, vclasses, curves, charge_power_factor):
    """가짜 수요를 만든다. 차량 제원과 충전곡선은 DB 의 진짜 값을 쓴다.

    어느 휴게소로 갈지는 여기서 **주사위로 정한다.** 그게 정책(T-18)이 할 일이고
    아직 없기 때문이다. 안성에 일부러 쏠리게 해서 대기가 생기는 모습을 본다.
    """

    ids = [v["vclass_id"] for v in vclasses]
    shares = [v["share"] for v in vclasses]
    by_id = {v["vclass_id"]: v for v in vclasses}
    curve_by_id = {vid: tuple(curve_segments(curves, vid)) for vid in ids}
    beta = cfg.vehicles.soc_beta
    target = cfg.vehicles.target_soc_cap

    arrivals = []

    for i in range(N_EV):
        # 60% 는 첫 번째 휴게소로 보낸다 (쏠림을 일부러 만든다)
        spec = stations[0] if rng.random() < 0.6 else rng.choice(stations[1:])
        vclass_id = rng.choices(ids, weights=shares, k=1)[0]
        vclass = by_id[vclass_id]

        soc_in = beta.lo + rng.betavariate(beta.a, beta.b) * (beta.hi - beta.lo)

        # 목표까지 채울 것이 없으면 애초에 충전하러 오지 않는다
        if soc_in >= target:
            continue

        arrivals.append(
            EVArrival(
                ev_id=f"smoke{i:05d}",
                vclass_id=vclass_id,
                station_id=spec.station_id,
                t_arrive_min=max(0.0, rng.gauss(PEAK_MIN, PEAK_SD_MIN)),
                soc_in=soc_in,
                soc_target=target,
                battery_kwh=float(vclass["battery_kwh"]),
                vmax_kw=float(vclass["vmax_kw"]),
                curve=curve_by_id[vclass_id],
                cold_factor=charge_power_factor,
            )
        )

    return arrivals


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

    # 충전기 대수는 DB 에서 읽는다. 코드에 박으면 휴게소마다 다른 값이 뭉개진다.
    with get_conn(db, readonly=True) as conn:
        station_rows, charger_rows = read_station_chargers(
            conn, station_ids=[sid for sid, *_ in STATIONS]
        )
        vclasses, curves, temps = load_from_db(conn)

    stations = station_specs(station_rows, charger_rows)
    check_queue_config(cfg.queue.discipline, cfg.queue.charger_select)

    _, charge_power_factor = temp_factors(cfg.environment.temp_c, temp_table(temps))
    arrivals = build_arrivals(cfg, rng, stations, vclasses, curves, charge_power_factor)

    print(f"휴게소 {len(stations)}곳 / 충전기 {sum(len(s.chargers) for s in stations)}기 "
          f"(DB charger.n_units)")
    print(f"도착 {len(arrivals)}대  ·  기온 {cfg.environment.temp_c}°C "
          f"(충전출력 계수 {charge_power_factor:.2f})")

    result = run_charging_des(
        stations, arrivals, snapshot_every_min=float(cfg.output.snapshot_every_min)
    )

    with RunContext.open(cfg, seed=SMOKE_SEED, db_path=db, overwrite=True) as run:
        print(f"run_id   : {run.run_id}")
        print(f"출력 폴더 : {run.output_dir}")

        run.writer.append_many("charge_event", list(result.charge_events))

        if cfg.output.write_snapshots:
            run.writer.append_many("snapshot", list(result.snapshots))

        last_end_min = max(e["t_end_min"] for e in result.charge_events)

        if last_end_min > cfg.time.end_min:
            print(
                f"\n  ⚠ 마지막 충전이 {last_end_min / 60:.1f}시에 끝난다 "
                f"(시나리오는 {cfg.time.end_min / 60:.0f}시까지). "
                "수요가 충전기 용량을 넘었다는 뜻이다 — N_EV 를 줄이거나 충전기를 늘릴 것."
            )

        waits = sorted(e["wait_min"] for e in result.charge_events)
        run.kpi("wait_p95_min", waits[int(len(waits) * 0.95)], "min")
        run.kpi("wait_mean_min", sum(waits) / len(waits), "min")
        run.kpi("wait_max_min", waits[-1], "min")
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
