"""가짜 데이터로 파이프라인을 한 바퀴 돌려본다 (핸즈온 STEP 09).

verify_setup.py 는 임시 폴더에서 돌고 흔적을 지운다. 이 스크립트는 **진짜
프로젝트 폴더에** 결과를 남긴다. 그래야 runs/ 폴더와 DB 행을 눈으로 볼 수 있다.

    python scripts/smoke_run.py

휴게소와 충전기는 **진짜**다 — T-05/T-06 이 적재한 경부선 하행 휴게소를 도로공사
휴게소 코드(station.source_key)로 골라 쓴다. 도착 수요만 가짜다. 대기시간은 T-16 의
DES 가 실제로 큐를 돌려서 나온 값이다.

예전에는 가짜 휴게소 3곳을 만들어 **진짜 하행 코리도에 넣었다.** 그 좌표와 거리는
지어낸 값이었고(가짜 안성 62.0 km ↔ 진짜 53.5 km), 셀 분할이 그걸 진짜로 알고
앵커로 썼다. 가짜 데이터는 더 만들지 않는다.
확인하려는 것은 배관이 이어져 있는가다:
    config → run 등록 → DB 에서 충전기 대수 → DES → 로거 검사 → Parquet → KPI → DuckDB

지우려면 폴더 하나와 DB 행 하나만 지우면 된다 (마지막에 명령을 출력한다).
"""

from __future__ import annotations

import random
from pathlib import Path

from _bootstrap import ROOT  # noqa: E402  (src 경로와 콘솔 인코딩을 먼저 준비한다)

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.io.db import get_conn, read_table  # noqa: E402
from evdt.io.event_log import load_sql, log_sim_result  # noqa: E402
from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.io.run_registry import RunContext  # noqa: E402
from evdt.io.stations import SMOKE_SOURCE, read_station_chargers  # noqa: E402
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

#: 쓸 휴게소 (경부선 하행, 도로공사 휴게소 코드). 첫 번째로 수요를 몰아서 쏠림을 본다.
#: 이름·ID 가 아니라 코드로 고르는 이유: 이름은 표기가 바뀌고(서울만남의광장 ↔
#: 서울만남휴게소), station_id 는 적재 방식에 따라 만들어진 값이다. 코드는 공급처가 준다.
STATION_KEYS = (
    "A00005",   # 안성휴게소
    "A00034",   # 천안호두휴게소
    "A00103",   # 옥천휴게소
)
CORRIDOR_ID = "gyeongbu_down"

#: 가짜 수요 대수. 세 곳 충전기 합계와 −5°C 충전시간으로 정한다 (아래 capacity 경고 참조).
#: 예전 가짜 휴게소(12기)에 맞춰 둔 150 은 진짜 휴게소(28기)에는 너무 적다.
N_EV = 300

#: 도착 시각 분포 (분). 오전 9시 전후로 몰리는 가짜 프로파일.
PEAK_MIN = 540.0
PEAK_SD_MIN = 150.0


def remove_legacy_fakes(db: Path) -> None:
    """예전 smoke_run 이 남긴 가짜 휴게소·충전기를 지운다 (있으면).

    진짜 코리도를 읽는 코드는 이것들이 섞여 있으면 멈추도록 돼 있다
    (evdt.io.stations.require_no_smoke). 여기서 한 번 정리해 준다.
    """
    with get_conn(db) as conn:
        removed = conn.execute(
            "DELETE FROM station WHERE source = ?", (SMOKE_SOURCE,)
        ).rowcount
        conn.execute("DELETE FROM corridor WHERE corridor_id = 'smoke_down'")

    if removed:
        print(f"예전 가짜 휴게소 {removed}곳을 지웠다 (충전기는 CASCADE).")


def pick_real_stations(conn) -> list[str]:
    """STATION_KEYS 순서대로 진짜 station_id 를 돌려준다. 하나라도 없으면 멈춘다."""

    placeholders = ",".join("?" * len(STATION_KEYS))
    found = {
        row[0]: row[1]
        for row in conn.execute(
            f"SELECT source_key, station_id FROM station"
            f" WHERE corridor_id = ? AND source_key IN ({placeholders})",
            (CORRIDOR_ID, *STATION_KEYS),
        )
    }
    missing = [k for k in STATION_KEYS if k not in found]

    if missing:
        raise SystemExit(
            f"{CORRIDOR_ID} 에 휴게소 {missing} 이 없다. 먼저 실행할 것:\n"
            "    python scripts/load_chargers.py"
        )

    return [found[k] for k in STATION_KEYS]


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
        print(f"DB 가 없다: {db}\n먼저 실행할 것:  python scripts/init_db.py")
        return 1

    remove_legacy_fakes(db)

    cfg = ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
    rng = random.Random(SMOKE_SEED)

    print(f"\n시나리오 : {cfg.scenario_id}  ({cfg.label})")
    print(f"설정 해시 : {cfg.config_hash}")

    # 충전기 대수는 DB 에서 읽는다. 코드에 박으면 휴게소마다 다른 값이 뭉개진다.
    with get_conn(db, readonly=True) as conn:
        station_ids = pick_real_stations(conn)
        station_rows, charger_rows = read_station_chargers(conn, station_ids=station_ids)
        vclasses, curves, temps = load_from_db(conn)

    order = {sid: i for i, sid in enumerate(station_ids)}
    stations = sorted(station_specs(station_rows, charger_rows), key=lambda s: order[s.station_id])
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

        # 쓰기 전에 두 테이블을 모두 검사한다. 틀린 행이 하나라도 있으면 한 줄도 안 쓴다.
        log_sim_result(
            run.writer, result.charge_events, result.snapshots,
            write_snapshots=cfg.output.write_snapshots,
        )

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

    # 설계문서 T-17 완료 기준: 휴게소별 시간대별 평균 대기를 SQL 한 줄로 (src/evdt/sql/).
    # 저장된 쿼리를 그대로 감싸서 이름만 붙인다. 집계를 여기서 다시 쓰면 두 벌이 갈라진다.
    print("\n--- DuckDB 로 뽑은 휴게소×시간대 평균 대기 (상위 5) ---")
    con = duck_connect(db_path=db, run_ids=[run_id])
    print(con.sql(
        f"""
        SELECT s.name AS 휴게소, w.hour AS 시각, w.n_ev AS 대수,
               ROUND(w.mean_wait_min, 1) AS 평균대기분
        FROM ({load_sql("station_hourly_wait")}) w JOIN station s USING (station_id)
        ORDER BY 평균대기분 DESC LIMIT 5
        """  # noqa: S608
    ).df().to_string(index=False))

    print("\n--- 만들어진 파일 ---")
    for p in sorted((ROOT / "runs" / run_id).iterdir()):
        print(f"  runs/{run_id}/{p.name}  ({p.stat().st_size:,} bytes)")

    print("\n배관 이상 없음. 지우려면:")
    print("  python scripts/smoke_clean.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
