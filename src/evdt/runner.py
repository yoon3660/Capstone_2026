"""엔드투엔드 러너 (이슈 #30) — config → EV → UE → DES → Parquet → KPI, 그리고 시드 반복.

    run_ue_once(cfg, seed=7)                      run 하나. run_id 를 돌려준다
    run_experiment(cfg, seeds=range(1, 21))       시드 여러 개. 하나라도 실패하면 멈춘다
    summarize_kpis(run_ids)                       KPI 별 평균 · 표준편차 · 95% 신뢰구간
    corridor_grid(run_ids, metric)                히트맵 재료 (휴게소 × 시각, 시드 전체를 합친 값)

왜 scripts/ 가 아니라 패키지에 두는가
    CI 에는 실측 데이터가 없다. "시드 3개 축소판이 끝까지 돈다" 를 테스트하려면 러너가
    DB 경로 · runs 폴더 · 수요 파일 위치를 인자로 받아야 하고, 테스트가 임포트할 수 있어야
    한다. scripts/run_ue.py · sweep_ue.py · run_experiment.py 는 이 모듈을 부르는 얇은 CLI 다.

계층
    world · engine · io · viz 를 모두 쓰는 맨 위 층이다. 아무도 이 모듈을 임포트하지 않는다
    (scripts 와 테스트만). 그래서 계층 규칙(tests/test_import_boundaries.py)과 부딪히지 않는다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from evdt.config import ScenarioConfig
from evdt.engine.ue import UESettings, des_arrivals, solve_and_log
from evdt.engine.ue_demand import ChargeRule, DemandBuild, build_trip_demands
from evdt.io.db import get_conn
from evdt.io.demand_profile import sample_dest_offsets
from evdt.io.event_log import load_sql, log_sim_result
from evdt.io.loaders import duck_connect, load_run_table
from evdt.io.run_registry import RunContext, make_run_id
from evdt.io.stations import read_station_chargers
from evdt.io.synthetic_ev import generate_evs
from evdt.io.vehicles import curve_segments, load_from_db, temp_table
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path
from evdt.viz.plots import (
    QUEUE_BIN_LABELS,
    QUEUE_BINS,
    plot_corridor_heatmap,
    plot_ue_gap,
)
from evdt.world.charging import temp_factors
from evdt.world.sim import check_queue_config, run_charging_des, station_specs

#: 병목 기준 (설계문서 §8.1): 휴게소×시간 평균 대기가 이 이상이면 병목 슬롯
BOTTLENECK_WAIT_MIN = 30.0

#: 목적지 난수는 EV 생성 난수와 다른 흐름을 쓴다 (generate_evs 가 seed 를 그대로 쓴다)
DEST_STREAM = 1

#: 신뢰구간 수준과 최소 시드 수 (이슈 #30 완료조건)
CI_LEVEL = 0.95
MIN_SEEDS = 20

Log = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def load_config(
    path: str | Path,
    *,
    soc: str | None = None,
    demand_multiplier: float | None = None,
    root: Path = PROJECT_ROOT,
) -> ScenarioConfig:
    """config 를 읽고, 준 값만 바꾼 실험(variant)으로 만든다. scenario_id 에 __soc-high__dm2 가 붙는다."""

    cfg = ScenarioConfig.from_yaml(root / path)
    tags: list[str] = []
    overrides: dict = {}

    if soc is not None:
        if not cfg.vehicles.soc_profiles:
            raise ValueError(f"{path} 에 vehicles.soc_profiles 가 없어서 soc 를 바꿀 수 없다")
        tags.append(f"soc-{soc}")
        overrides["vehicles.departure_soc"] = soc

    if demand_multiplier is not None:
        tags.append(f"dm{demand_multiplier:g}")
        overrides["demand.demand_multiplier"] = float(demand_multiplier)

    return cfg.variant("__".join(tags), overrides) if tags else cfg


def with_seed(cfg: ScenarioConfig, seed: int) -> ScenarioConfig:
    return dataclasses.replace(cfg, vehicles=dataclasses.replace(cfg.vehicles, seed=int(seed)))


def departure_soc_mean(cfg: ScenarioConfig) -> float:
    b = cfg.vehicles.soc_beta
    return b.lo + b.a / (b.a + b.b) * (b.hi - b.lo)


# ---------------------------------------------------------------------------
# run 하나
# ---------------------------------------------------------------------------


def build_demand(cfg: ScenarioConfig, stations, vclasses, curves, temps, corridor_end_km: float,
                 *, root: Path = PROJECT_ROOT) -> tuple[DemandBuild, float, float]:
    """교통량 프로파일 → EV → 충전 계획 선택지."""

    missing = cfg.missing_inputs(root)

    if missing:
        raise FileNotFoundError(
            f"수요 프로파일이 없다: {missing}\n먼저 실행할 것:  python scripts/build_demand_profile.py"
        )

    volume = pd.read_csv(root / cfg.demand.volume_profile)
    evs = generate_evs(volume, cfg, dest_offset_km=corridor_end_km)

    if cfg.demand.through_profile:
        rng = np.random.default_rng([cfg.vehicles.seed, DEST_STREAM])
        through = pd.read_csv(root / cfg.demand.through_profile)
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


def run_ue_once(
    cfg: ScenarioConfig,
    *,
    seed: int | None = None,
    overwrite: bool = False,
    db_path: Path | None = None,
    runs_dir: Path | None = None,
    root: Path = PROJECT_ROOT,
    gap_plot: bool = True,
    log: Log = print,
) -> str:
    """UE 한 번. run_id 를 돌려준다. 수렴 실패면 UENotConverged (run 은 FAILED 로 남는다)."""

    if cfg.policy.stage != "UE":
        raise ValueError(f"UE 시나리오가 아니다: policy.stage={cfg.policy.stage}")

    if seed is not None:
        cfg = with_seed(cfg, seed)

    check_queue_config(cfg.queue.discipline, cfg.queue.charger_select)
    db = Path(db_path) if db_path is not None else default_db_path()
    runs = Path(runs_dir) if runs_dir is not None else RUNS_DIR

    with get_conn(db, readonly=True) as conn:
        station_rows, charger_rows = read_station_chargers(conn, corridor_id=cfg.corridor_id)
        vclasses, curves, temps = load_from_db(conn)
        corridor_end_km = float(conn.execute(
            "SELECT length_km FROM corridor WHERE corridor_id = ?", (cfg.corridor_id,)
        ).fetchone()[0])

    specs = station_specs(station_rows, charger_rows)
    chargers = {s.station_id: s.chargers for s in specs}
    stations = [r for r in station_rows if r["station_id"] in chargers]

    built, range_factor, cpf = build_demand(cfg, stations, vclasses, curves, temps, corridor_end_km, root=root)
    soc_mean = departure_soc_mean(cfg)

    log(f"\n{cfg.scenario_id}  seed={cfg.vehicles.seed}  기온 {cfg.environment.temp_c}°C "
        f"(주행 {range_factor:.2f} · 충전출력 {cpf:.2f})")
    log(f"휴게소 {len(stations)}곳 · 충전기 {sum(len(c) for c in chargers.values())}기 · "
        f"코리도 {corridor_end_km:.1f} km")
    log(f"출발 SoC {cfg.vehicles.departure_soc or '(soc_beta)'}: 평균 {soc_mean:.0%} · "
        f"수요 배율 ×{cfg.demand.demand_multiplier:g}")
    log(f"진입 EV {built.n_ev:,}대 → 충전 필요 {len(built.trips):,}대 "
        f"(충전 없이 도착 {built.n_no_charge:,} · {cfg.policy.ue.max_stops}회 안에 불가 {built.n_infeasible:,})")

    ue = cfg.policy.ue
    settings = UESettings(
        max_iter=ue.max_iter, gap_tol=ue.gap_tol, min_gain_min=ue.min_gain_min,
        speed_kmh=cfg.demand.cruise_speed_kmh,
    )
    params = {
        "departure_soc": cfg.vehicles.departure_soc,
        "departure_soc_mean": round(soc_mean, 4),
        "demand_multiplier": cfg.demand.demand_multiplier,
    }

    with RunContext.open(cfg, seed=cfg.vehicles.seed, db_path=db, runs_dir=runs,
                         overwrite=overwrite, params=params) as run:
        log(f"run_id : {run.run_id}")
        run.kpis({
            "n_ev": (built.n_ev, "count"),
            "n_ev_charging": (len(built.trips), "count"),
            "n_ev_no_charge": (built.n_no_charge, "count"),
            "n_ev_infeasible": (built.n_infeasible, "count"),
            "departure_soc_mean": (soc_mean, "ratio"),
        })

        # 수렴 못 하면 gap 이력을 남기고 예외 → RunContext 가 run 을 FAILED 로 기록한다
        result = solve_and_log(built.trips, chargers, settings, run.writer)

        for h in result.history:
            log(f"  반복 {h.iteration:>2}  gap {h.rel_gap:8.4%}  총 체류 {h.total_dwell_min / 60:9,.0f} 시간  "
                f"개선 가능 {h.n_improvable:>5}대  바꾼 차 {h.n_switched:>5}대")

        sim = run_charging_des(specs, des_arrivals(built.trips, result),
                               snapshot_every_min=float(cfg.output.snapshot_every_min))
        log_sim_result(run.writer, sim.charge_events, sim.snapshots,
                       write_snapshots=cfg.output.write_snapshots)

        waits = np.array([e["wait_min"] for e in sim.charge_events]) if sim.charge_events else np.zeros(1)
        run.kpis({
            "ue_iterations": (result.history[-1].iteration, "count"),
            "ue_final_gap": (result.final_gap, "ratio"),
            "n_charge_visits": (len(sim.charge_events), "count"),
            "wait_mean_min": (float(waits.mean()), "min"),
            "wait_p95_min": (float(np.percentile(waits, 95)), "min"),
            "wait_max_min": (float(waits.max()), "min"),
            "dwell_total_h": (sum(e["dwell_min"] for e in sim.charge_events) / 60.0, "h"),
        })
        run_id = run.run_id

    if gap_plot:
        plot_ue_gap(load_run_table(run_id, "solver_log", runs), runs / run_id / "ue_gap.png", gap_tol=ue.gap_tol)

    _record_concentration_kpis(run_id, db, runs, log)
    return run_id


def _record_concentration_kpis(run_id: str, db: Path, runs: Path, log: Log) -> None:
    """쏠림 지표 (docs/UE_equilibrium.md §7). Parquet 가 닫힌 뒤 DuckDB 로 계산한다."""

    con = duck_connect(db_path=db, run_ids=[run_id], runs_dir=runs)

    if "charge_event" not in {r[0] for r in con.sql("SHOW TABLES").fetchall()}:
        n_slots, ratio_max, worst = 0, 0.0, 0.0
    else:
        hourly = f"({load_sql('station_hourly_wait')})"
        n_slots = con.sql(f"SELECT COUNT(*) FROM {hourly} WHERE mean_wait_min >= {BOTTLENECK_WAIT_MIN}").fetchone()[0]
        ratio_max, worst = con.sql(
            """
            WITH ch AS (SELECT station_id, SUM(n_units) AS n_ch FROM charger WHERE is_active = 1 GROUP BY 1),
                 ev AS (SELECT station_id, COUNT(*) AS n, AVG(wait_min) AS w FROM charge_event GROUP BY 1),
                 j  AS (SELECT (ev.n / SUM(ev.n) OVER ()) / (ch.n_ch / SUM(ch.n_ch) OVER ()) AS ratio, ev.w
                        FROM ev JOIN ch USING (station_id))
            SELECT MAX(ratio), MAX(w) FROM j
            """
        ).fetchone()

    with get_conn(db) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO run_kpi (run_id, metric, value, unit) VALUES (?, ?, ?, ?)",
            [
                (run_id, "bottleneck_slots", float(n_slots), "count"),
                (run_id, "share_ratio_max", float(ratio_max or 0.0), "ratio"),
                (run_id, "wait_worst_station_min", float(worst or 0.0), "min"),
            ],
        )

    log(f"병목 슬롯 {n_slots} · 몰림비 {float(ratio_max or 0):.2f}")


# ---------------------------------------------------------------------------
# 시드 반복
# ---------------------------------------------------------------------------


class ExperimentFailed(RuntimeError):
    """시드 중 하나라도 실패했다. 요약·히트맵은 만들지 않는다."""


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    run_ids: tuple[str, ...]
    summary: pd.DataFrame          # summarize_kpis 결과
    out_dir: Path
    heatmaps: tuple[Path, ...] = ()


def parse_seeds(text: str) -> list[int]:
    """'1-20', '1,2,5', '1-3,10' → 정수 목록 (순서 유지, 중복 제거)."""

    seeds: list[int] = []

    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if hi < lo:
                raise ValueError(f"시드 범위가 거꾸로다: {part}")
            seeds.extend(range(lo, hi + 1))
        else:
            seeds.append(int(part))

    out = list(dict.fromkeys(seeds))

    if not out:
        raise ValueError(f"시드가 없다: {text!r}")
    if any(s < 0 for s in out):
        raise ValueError(f"시드는 0 이상이어야 한다: {text!r}")

    return out


def experiment_id(cfg: ScenarioConfig, seeds: Sequence[int]) -> str:
    """시나리오 + 시드 목록에서 정해지는 이름. 같은 조건이면 같은 폴더."""

    pct = int(round(cfg.policy.participation * 100))
    ordered = sorted(seeds)

    if ordered == list(range(ordered[0], ordered[-1] + 1)):
        tag = f"s{ordered[0]:04d}-{ordered[-1]:04d}"
    else:
        tag = f"n{len(ordered)}-" + hashlib.sha1(",".join(map(str, ordered)).encode()).hexdigest()[:8]

    return f"{cfg.scenario_id}__{cfg.policy.stage}__p{pct:03d}__{tag}"


def run_status(db: Path, run_id: str) -> str | None:
    with get_conn(db, readonly=True) as conn:
        row = conn.execute("SELECT status FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else str(row[0])


def run_experiment(
    cfg: ScenarioConfig,
    seeds: Iterable[int],
    *,
    reuse_done: bool = True,
    min_seeds: int = MIN_SEEDS,
    heatmaps: Sequence[str] = ("wait", "queue"),
    db_path: Path | None = None,
    runs_dir: Path | None = None,
    root: Path = PROJECT_ROOT,
    log: Log = print,
) -> ExperimentResult:
    """시드마다 run 을 등록·실행하고, 전부 성공하면 KPI 신뢰구간과 히트맵을 만든다.

    실패한 run 이 하나라도 있으면 거기서 멈추고 ExperimentFailed. 요약도 히트맵도 쓰지 않는다 —
    19개로 낸 신뢰구간을 20개로 낸 것처럼 쓰는 일을 막는다. 실패한 run 은 FAILED 로 남는다.

    reuse_done: 이미 DONE 인 run 은 다시 돌리지 않는다 (같은 config·시드면 결과가 같다).
                False 면 전부 새로 돌린다 (코드를 고친 뒤에는 이쪽).
    """

    seeds = list(dict.fromkeys(int(s) for s in seeds))

    if len(seeds) < min_seeds:
        raise ValueError(f"시드가 {len(seeds)}개다. 신뢰구간에는 {min_seeds}개 이상이 필요하다")

    db = Path(db_path) if db_path is not None else default_db_path()
    runs = Path(runs_dir) if runs_dir is not None else RUNS_DIR
    run_ids: list[str] = []

    for i, seed in enumerate(seeds, 1):
        run_id = make_run_id(cfg.scenario_id, cfg.policy.stage, seed, cfg.policy.participation)

        if reuse_done and run_status(db, run_id) == "DONE" and (runs / run_id / "meta.json").is_file():
            log(f"[{i}/{len(seeds)}] seed {seed}: 이미 DONE → 재사용  {run_id}")
            run_ids.append(run_id)
            continue

        log(f"[{i}/{len(seeds)}] seed {seed}: 실행")

        try:
            got = run_ue_once(cfg, seed=seed, overwrite=True, db_path=db, runs_dir=runs, root=root,
                              gap_plot=False, log=_quiet)
        except Exception as exc:  # noqa: BLE001 — 무엇이 실패했든 결과를 내면 안 된다
            raise ExperimentFailed(
                f"seed {seed} 실패 ({type(exc).__name__}: {exc}).\n"
                f"  {i - 1}/{len(seeds)} 개만 끝났다. 요약·히트맵을 만들지 않고 멈춘다.\n"
                f"  run {run_id} 는 FAILED 로 남아 있다."
            ) from exc

        assert got == run_id, (got, run_id)
        run_ids.append(run_id)

    summary = summarize_kpis(run_ids, db_path=db)
    exp_id = experiment_id(cfg, seeds)
    out = runs / "experiments" / exp_id
    out.mkdir(parents=True, exist_ok=True)

    summary.to_csv(out / "kpi_ci.csv", index=False, encoding="utf-8")
    (out / "kpi_ci.md").write_text(summary_markdown(summary, cfg, seeds), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({
        "experiment_id": exp_id,
        "scenario_id": cfg.scenario_id,
        "config_hash": cfg.config_hash,
        "seeds": seeds,
        "run_ids": run_ids,
        "ci_level": CI_LEVEL,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    pngs = [
        plot_experiment_heatmap(cfg, run_ids, metric=m, path=out / f"heatmap_{m}.png",
                                n_seeds=len(seeds), db_path=db, runs_dir=runs)
        for m in heatmaps
        if m != "queue" or cfg.output.write_snapshots
    ]

    return ExperimentResult(exp_id, tuple(run_ids), summary, out, tuple(pngs))


def plot_experiment_heatmap(
    cfg: ScenarioConfig,
    run_ids: Sequence[str],
    *,
    metric: str,
    path: Path,
    n_seeds: int,
    db_path: Path | None = None,
    runs_dir: Path | None = None,
) -> Path:
    """시드 전체를 합친 휴게소 × 시각 히트맵 한 장. 세로축 = offset_km."""

    grid = corridor_grid(run_ids, metric=metric, db_path=db_path, runs_dir=runs_dir)
    stations = corridor_stations(cfg.corridor_id, db_path=db_path)
    what = "실제로 기다린 평균 (분)" if metric == "wait" else "줄 선 차 수 (대, 5분마다 찍은 값의 평균)"
    kw = {} if metric == "wait" else {"bins": QUEUE_BINS, "bin_labels": QUEUE_BIN_LABELS, "value_label": "큐 길이 (대)"}

    return plot_corridor_heatmap(
        [(f"{cfg.label}", grid)],
        stations,
        path,
        title=f"휴게소 × 시간대 — {'대기시간' if metric == 'wait' else '큐 길이'} (시드 {n_seeds}개 합산)",
        subtitle=f"{cfg.scenario_id} · 색 = {what} · 세로축은 실제 기점거리",
        **kw,
    )


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------

#: 표에 싣는 순서와 한국어 이름. 여기 없는 KPI 는 뒤에 이름 그대로 붙는다
KPI_LABELS: dict[str, str] = {
    "wait_mean_min": "평균 대기 (분)",
    "wait_p95_min": "P95 대기 (분)",
    "wait_max_min": "최대 대기 (분)",
    "wait_worst_station_min": "최악 휴게소 평균 대기 (분)",
    "bottleneck_slots": "병목 슬롯 (칸)",
    "share_ratio_max": "몰림비 (수요몫÷충전기몫 최대)",
    "dwell_total_h": "총 체류 (시간)",
    "n_ev_charging": "충전 필요 EV (대)",
    "n_charge_visits": "충전 정차 (회)",
    "ue_final_gap": "UE 마지막 gap",
    "ue_iterations": "UE 반복 수",
}


def summarize_kpis(run_ids: Sequence[str], *, db_path: Path | None = None) -> pd.DataFrame:
    """KPI 별 n · 평균 · 표준편차 · 95% 신뢰구간 (t 분포). 시드 사이 변동을 잰다.

    신뢰구간 = 평균 ± t(0.975, n−1) × 표준편차 / √n.
    run 하나의 KPI 가 표본 하나다. 시드가 다르면 EV 집합이 다르므로 독립 표본으로 본다.
    """

    from scipy import stats

    db = Path(db_path) if db_path is not None else default_db_path()

    with get_conn(db, readonly=True) as conn:
        kpi = pd.read_sql_query(
            f"SELECT run_id, metric, value, unit FROM run_kpi WHERE run_id IN ({','.join('?' * len(run_ids))})",  # noqa: S608
            conn, params=list(run_ids),
        )

    rows = []

    for metric, g in kpi.groupby("metric"):
        values = g["value"].to_numpy(dtype=float)
        n = len(values)
        mean = float(values.mean())
        sd = float(values.std(ddof=1)) if n > 1 else math.nan
        half = float(stats.t.ppf(0.5 + CI_LEVEL / 2, n - 1) * sd / math.sqrt(n)) if n > 1 else math.nan
        rows.append({
            "metric": metric,
            "label": KPI_LABELS.get(metric, metric),
            "unit": g["unit"].iloc[0],
            "n": n,
            "mean": mean,
            "sd": sd,
            "ci_low": mean - half,
            "ci_high": mean + half,
            "min": float(values.min()),
            "max": float(values.max()),
        })

    order = {m: i for i, m in enumerate(KPI_LABELS)}
    out = pd.DataFrame(rows)

    if out.empty:
        return out

    return out.sort_values("metric", key=lambda s: s.map(lambda m: order.get(m, len(order)))).reset_index(drop=True)


def summary_markdown(summary: pd.DataFrame, cfg: ScenarioConfig, seeds: Sequence[int]) -> str:
    """발표 슬라이드에 바로 붙이는 신뢰구간 표."""

    lines = [
        f"# {cfg.label}",
        "",
        f"시나리오 `{cfg.scenario_id}` · 시드 {len(seeds)}개 ({min(seeds)}–{max(seeds)}) · "
        f"출발 SoC {cfg.vehicles.departure_soc or '-'} · 수요 ×{cfg.demand.demand_multiplier:g} · "
        f"{int(CI_LEVEL * 100)}% 신뢰구간 (t 분포)",
        "",
        "| KPI | 평균 | 95% 신뢰구간 | 표준편차 | 최소 – 최대 |",
        "|---|---:|---|---:|---|",
    ]

    for r in summary.itertuples():
        f = (lambda x: f"{x:.4f}") if r.metric == "ue_final_gap" else (lambda x: f"{x:,.1f}")
        lines.append(f"| {r.label} | {f(r.mean)} | {f(r.ci_low)} – {f(r.ci_high)} | {f(r.sd)} | "
                     f"{f(r.min)} – {f(r.max)} |")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 히트맵 재료
# ---------------------------------------------------------------------------


def corridor_grid(
    run_ids: Sequence[str],
    *,
    metric: str = "wait",
    db_path: Path | None = None,
    runs_dir: Path | None = None,
) -> pd.DataFrame:
    """시드 전체를 합친 휴게소 × 시각 값. 컬럼 (station_id, hour, value, n).

    metric="wait"  : 그 시각에 도착한 차들이 **실제로 기다린** 평균 (분). 시드 전체 차를 한데 모은 평균
                     (시드별 평균의 평균이 아니다 — 차가 적은 시드의 칸이 과대대표되지 않게).
    metric="queue" : 스냅샷 queue_len (줄 선 차 수) 의 시각별 평균. 5분마다 찍힌 값을 시드 전체로 평균.
    """

    con = duck_connect(db_path=db_path or default_db_path(), run_ids=list(run_ids), runs_dir=runs_dir)

    if metric == "wait":
        return con.sql(
            f"""
            SELECT station_id, hour, SUM(n_ev * mean_wait_min) / SUM(n_ev) AS value, SUM(n_ev) AS n
            FROM ({load_sql('station_hourly_wait')}) GROUP BY ALL ORDER BY ALL
            """  # noqa: S608
        ).df()

    if metric == "queue":
        return con.sql(
            """
            SELECT entity_id AS station_id, CAST(FLOOR(t_min / 60) AS INTEGER) AS hour,
                   AVG(value) AS value, COUNT(*) AS n
            FROM snapshot WHERE entity_type = 'station' AND state = 'queue_len'
            GROUP BY ALL ORDER BY ALL
            """
        ).df()

    raise ValueError(f"metric 은 wait 또는 queue: {metric!r}")


def corridor_stations(corridor_id: str, *, db_path: Path | None = None) -> pd.DataFrame:
    """히트맵 세로축: 충전기가 있는 휴게소의 station_id · name · offset_km (DB 의 기점거리 그대로)."""

    with get_conn(db_path or default_db_path(), readonly=True) as conn:
        return pd.read_sql_query(
            """
            SELECT s.station_id, s.name, s.offset_km FROM station s
            WHERE s.corridor_id = ?
              AND EXISTS (SELECT 1 FROM charger c WHERE c.station_id = s.station_id AND c.is_active = 1)
            ORDER BY s.offset_km
            """,
            conn, params=(corridor_id,),
        )


def station_table(run_id: str, corridor_id: str, *, db_path: Path | None = None,
                  runs_dir: Path | None = None) -> pd.DataFrame:
    """휴게소별 충전기 몫 대비 수요 몫과 대기 (run 하나)."""

    con = duck_connect(db_path=db_path or default_db_path(), run_ids=[run_id], runs_dir=runs_dir)
    hourly = f"({load_sql('station_hourly_wait')})"
    return con.sql(
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
        WHERE s.corridor_id = '{corridor_id}'
        ORDER BY s.offset_km
        """  # noqa: S608
    ).df()
