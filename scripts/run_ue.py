"""UE 균형 한 번 돌리기 — 진짜 휴게소·충전기·교통량으로.

    python scripts/build_demand_profile.py        # 처음 한 번 (수요 프로파일 CSV)
    python scripts/run_ue.py                      # config 의 시드
    python scripts/run_ue.py --seed 3 --overwrite

    config → 교통량으로 EV 생성 → 충전 계획 선택지 → UE 균형 → DES → Parquet → KPI

남는 것
    runs/<run_id>/charge_event.parquet   균형 상태의 충전 정차 (정차마다 한 행)
    runs/<run_id>/snapshot.parquet       휴게소 상태 시계열
    runs/<run_id>/solver_log.parquet     반복별 gap (iteration, rel_gap)
    runs/<run_id>/ue_gap.png             수렴 그래프
    run_kpi                              대기·체류·gap·수요 구성

수렴하지 못하면 gap 이력을 남긴 채 run 을 FAILED 로 기록하고 멈춘다.
"""

from __future__ import annotations

import argparse
import dataclasses

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.engine.ue import UESettings, des_arrivals, solve_and_log  # noqa: E402
from evdt.engine.ue_demand import ChargeRule, build_trip_demands  # noqa: E402
from evdt.io.db import get_conn  # noqa: E402
from evdt.io.demand_profile import sample_dest_offsets  # noqa: E402
from evdt.io.event_log import load_sql, log_sim_result  # noqa: E402
from evdt.io.loaders import duck_connect, load_run_table  # noqa: E402
from evdt.io.run_registry import RunContext  # noqa: E402
from evdt.io.stations import read_station_chargers  # noqa: E402
from evdt.io.synthetic_ev import generate_evs  # noqa: E402
from evdt.io.vehicles import curve_segments, load_from_db, temp_table  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.viz.plots import plot_ue_gap  # noqa: E402
from evdt.world.charging import temp_factors  # noqa: E402
from evdt.world.sim import check_queue_config, run_charging_des, station_specs  # noqa: E402

#: 병목 기준 (설계문서 §8.1): 휴게소×시간 평균 대기가 이 이상이면 병목 슬롯
BOTTLENECK_WAIT_MIN = 30.0

#: 목적지 난수는 EV 생성 난수와 다른 흐름을 쓴다 (generate_evs 가 seed 를 그대로 쓴다)
DEST_STREAM = 1


def build_demand(cfg: ScenarioConfig, stations, vclasses, curves, temps, corridor_end_km: float):
    missing = cfg.missing_inputs(ROOT)

    if missing:
        raise SystemExit(f"수요 프로파일이 없다: {missing}\n먼저 실행할 것:  python scripts/build_demand_profile.py")

    volume = pd.read_csv(ROOT / cfg.demand.volume_profile)
    evs = generate_evs(volume, cfg, dest_offset_km=corridor_end_km)

    if cfg.demand.through_profile:
        rng = np.random.default_rng([cfg.vehicles.seed, DEST_STREAM])
        through = pd.read_csv(ROOT / cfg.demand.through_profile)
        evs["dest_offset_km"] = sample_dest_offsets(len(evs), through, rng, corridor_end_km=corridor_end_km)

    range_factor, charge_power_factor = temp_factors(cfg.environment.temp_c, temp_table(temps))
    rule = ChargeRule(
        range_factor=range_factor,
        buffer_km=cfg.demand.safety_buffer_km,
        reserve_soc=cfg.demand.low_soc_threshold,
        target_soc_cap=cfg.vehicles.target_soc_cap,
        max_stops=cfg.policy.ue.max_stops,
    )
    built = build_trip_demands(
        evs.to_dict("records"),
        stations,
        {v["vclass_id"]: v for v in vclasses},
        {v["vclass_id"]: tuple(curve_segments(curves, v["vclass_id"])) for v in vclasses},
        rule=rule,
        charge_power_factor=charge_power_factor,
    )
    return built, range_factor, charge_power_factor


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seed", type=int, default=None, help="기본: config 의 vehicles.seed")
    ap.add_argument("--overwrite", action="store_true", help="같은 run_id 가 있으면 덮어쓴다")
    args = ap.parse_args()

    cfg = ScenarioConfig.from_yaml(ROOT / args.config)

    if cfg.policy.stage != "UE":
        raise SystemExit(f"UE 시나리오가 아니다: policy.stage={cfg.policy.stage}")

    if args.seed is not None:
        cfg = dataclasses.replace(cfg, vehicles=dataclasses.replace(cfg.vehicles, seed=args.seed))

    check_queue_config(cfg.queue.discipline, cfg.queue.charger_select)
    db = default_db_path()

    with get_conn(db, readonly=True) as conn:
        station_rows, charger_rows = read_station_chargers(conn, corridor_id=cfg.corridor_id)
        vclasses, curves, temps = load_from_db(conn)
        corridor_end_km = float(conn.execute(
            "SELECT length_km FROM corridor WHERE corridor_id = ?", (cfg.corridor_id,)
        ).fetchone()[0])

    specs = station_specs(station_rows, charger_rows)
    chargers = {s.station_id: s.chargers for s in specs}
    stations = [r for r in station_rows if r["station_id"] in chargers]

    built, range_factor, cpf = build_demand(cfg, stations, vclasses, curves, temps, corridor_end_km)
    n_stops = pd.Series([len(t.plans[0].stops) for t in built.trips]).value_counts().sort_index()

    print(f"\n{cfg.scenario_id}  seed={cfg.vehicles.seed}  기온 {cfg.environment.temp_c}°C "
          f"(주행 {range_factor:.2f} · 충전출력 {cpf:.2f})")
    print(f"휴게소 {len(stations)}곳 · 충전기 {sum(len(c) for c in chargers.values())}기 · "
          f"코리도 {corridor_end_km:.1f} km")
    print(f"진입 EV {built.n_ev:,}대 → 충전 필요 {len(built.trips):,}대 "
          f"(충전 없이 도착 {built.n_no_charge:,} · {cfg.policy.ue.max_stops}회 안에 불가 {built.n_infeasible:,})")
    print("  필요한 정차 수: " + ", ".join(f"{k}회 {v:,}대" for k, v in n_stops.items()))

    ue = cfg.policy.ue
    settings = UESettings(
        max_iter=ue.max_iter, gap_tol=ue.gap_tol, min_gain_min=ue.min_gain_min,
        speed_kmh=cfg.demand.cruise_speed_kmh,
    )

    with RunContext.open(cfg, seed=cfg.vehicles.seed, db_path=db, overwrite=args.overwrite) as run:
        print(f"run_id : {run.run_id}")
        run.kpis({
            "n_ev": (built.n_ev, "count"),
            "n_ev_charging": (len(built.trips), "count"),
            "n_ev_no_charge": (built.n_no_charge, "count"),
            "n_ev_infeasible": (built.n_infeasible, "count"),
        })

        # 수렴 못 하면 gap 이력을 남기고 예외 → RunContext 가 run 을 FAILED 로 기록한다
        result = solve_and_log(built.trips, chargers, settings, run.writer)

        for h in result.history:
            print(f"  반복 {h.iteration:>2}  gap {h.rel_gap:8.4%}  총 체류 {h.total_dwell_min / 60:9,.0f} 시간  "
                  f"개선 가능 {h.n_improvable:>5}대  바꾼 차 {h.n_switched:>5}대")

        sim = run_charging_des(
            specs, des_arrivals(built.trips, result),
            snapshot_every_min=float(cfg.output.snapshot_every_min),
        )
        log_sim_result(run.writer, sim.charge_events, sim.snapshots,
                       write_snapshots=cfg.output.write_snapshots)

        waits = np.array([e["wait_min"] for e in sim.charge_events])
        run.kpis({
            "ue_iterations": (result.history[-1].iteration, "count"),
            "ue_final_gap": (result.final_gap, "ratio"),
            "n_charge_visits": (len(waits), "count"),
            "wait_mean_min": (float(waits.mean()), "min"),
            "wait_p95_min": (float(np.percentile(waits, 95)), "min"),
            "wait_max_min": (float(waits.max()), "min"),
            "dwell_total_h": (sum(e["dwell_min"] for e in sim.charge_events) / 60.0, "h"),
        })
        run_id = run.run_id

    runs_dir = ROOT / "runs"
    png = plot_ue_gap(load_run_table(run_id, "solver_log", runs_dir), runs_dir / run_id / "ue_gap.png",
                      gap_tol=ue.gap_tol)

    con = duck_connect(db_path=db, run_ids=[run_id])
    hourly = f"({load_sql('station_hourly_wait')})"
    n_slots = con.sql(f"SELECT COUNT(*) FROM {hourly} WHERE mean_wait_min >= {BOTTLENECK_WAIT_MIN}").fetchone()[0]

    with get_conn(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_kpi (run_id, metric, value, unit) VALUES (?, ?, ?, ?)",
            (run_id, "bottleneck_slots", float(n_slots), "count"),
        )

    print("\n--- 휴게소별: 충전기 몫 대비 수요 몫, 대기 (쏠림) ---")
    print(con.sql(
        f"""
        WITH ch AS (SELECT station_id, SUM(n_units) AS n_ch FROM charger WHERE is_active = 1 GROUP BY 1),
             ev AS (SELECT station_id, COUNT(*) AS n, AVG(wait_min) AS w,
                           QUANTILE_CONT(wait_min, 0.95) AS w95 FROM charge_event GROUP BY 1),
             hr AS (SELECT station_id, MAX(mean_wait_min) AS worst FROM {hourly} GROUP BY 1)
        SELECT s.name AS 휴게소, ROUND(s.offset_km) AS km, ch.n_ch AS 충전기,
               ROUND(100.0 * ch.n_ch / SUM(ch.n_ch) OVER (), 1) AS 충전기몫,
               COALESCE(ev.n, 0) AS 정차,
               ROUND(100.0 * COALESCE(ev.n, 0) / SUM(COALESCE(ev.n, 0)) OVER (), 1) AS 수요몫,
               ROUND(ev.w, 1) AS 평균대기, ROUND(ev.w95, 1) AS P95대기, ROUND(hr.worst, 1) AS 최악시간대
        FROM station s JOIN ch USING (station_id)
             LEFT JOIN ev USING (station_id) LEFT JOIN hr USING (station_id)
        WHERE s.corridor_id = '{cfg.corridor_id}'
        ORDER BY s.offset_km
        """  # noqa: S608
    ).df().to_string(index=False))

    print(f"\n병목 슬롯 (휴게소×시간 평균 대기 ≥ {BOTTLENECK_WAIT_MIN:.0f}분): {n_slots}")
    print(f"수렴 그래프: {png.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
