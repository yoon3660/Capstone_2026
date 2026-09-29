"""엔드투엔드 러너 · 시드 반복 · 신뢰구간 · 히트맵 (이슈 #30).

CI 에는 실측 데이터가 없다. 그래서 임시 DB 에 휴게소 3곳을 넣고, 작은 교통량·목적지 CSV 를
만들어 **실제 러너 코드를 끝까지** 돌린다 (시드 3개 축소판, 1분 이내).
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import pytest
import yaml

from evdt.config import ScenarioConfig
from evdt.io.db import get_conn, upsert_df
from evdt.io.vehicles import read_temp_efficiency, read_vehicle_classes
from evdt.runner import (
    ExperimentFailed,
    _record_concentration_kpis,
    experiment_id,
    parse_seeds,
    run_experiment,
    run_ue_once,
)
from evdt.viz.plots import STATION_BAND_KM, _station_bands

SEEDS = [1, 2, 3]


@pytest.fixture
def mini(seeded_db: Path, cfg: ScenarioConfig, tmp_path: Path):
    """휴게소 3곳 (62·110·150 km) · 교통량 조금 · 목적지 180 km 이내 — 몇 초면 도는 시나리오."""

    with get_conn(seeded_db) as conn:
        upsert_df(conn, "station", pd.DataFrame([
            {"station_id": "st_mid", "corridor_id": "gyeongbu_down", "name": "중간휴게소", "direction": "DOWN",
             "offset_km": 110.0, "lat": 36.6, "lon": 127.4, "source": "test"},
            {"station_id": "st_far", "corridor_id": "gyeongbu_down", "name": "먼휴게소", "direction": "DOWN",
             "offset_km": 150.0, "lat": 36.3, "lon": 127.6, "source": "test"},
        ]))
        upsert_df(conn, "charger", pd.DataFrame([
            {"charger_id": "ch_mid", "station_id": "st_mid", "power_kw": 100.0, "n_units": 2, "source": "test"},
            {"charger_id": "ch_far", "station_id": "st_far", "power_kw": 350.0, "n_units": 2, "source": "test"},
        ]))
        classes, curves = read_vehicle_classes()
        upsert_df(conn, "vehicle_class", pd.DataFrame(classes))
        upsert_df(conn, "charge_curve", pd.DataFrame(curves))
        upsert_df(conn, "temp_efficiency", pd.DataFrame(read_temp_efficiency()))

    volume = tmp_path / "volume.csv"
    pd.DataFrame({"hour": range(24), "volume_veh": [0] * 7 + [180] * 6 + [0] * 11}).to_csv(volume, index=False)
    through = tmp_path / "through.csv"
    pd.DataFrame({"offset_km": [3.0, 100.0, 170.0, 200.0], "share": [1.0, 0.8, 0.5, 0.0]}).to_csv(through, index=False)

    # 운영 config 가 가리키는 **모든** 입력을 tmp_path 로 바꿔야 한다.
    # 하나라도 놓치면 그 파일이 있는 기기에서는 통과하고 CI 에서만 깨진다
    # (data/processed 는 gitignore 다). 실제로 #54 에서 겪었다.
    entry_exit = tmp_path / "entry_exit.csv"
    pd.DataFrame([
        {"hour": h, "offset_km": km, "gap_km": 0.0,
         "volume_veh": vol, "entry_veh": entry, "exit_veh": vol * share,
         "exit_share": share}
        for h in range(24)
        for km, share, entry in ((50.0, 0.0, 60.0), (120.0, 0.3, 0.0), (180.0, 0.5, 0.0))
        for vol in [180.0 if 7 <= h < 13 else 0.0]
    ]).to_csv(entry_exit, index=False)

    data = yaml.safe_load(cfg.raw_yaml)
    data["scenario_id"] = "mini_test"
    data["demand"]["volume_profile"] = str(volume)
    data["demand"]["through_profile"] = str(through)
    data["demand"]["entry_exit_profile"] = str(entry_exit)
    mini_cfg = ScenarioConfig.from_dict(data, source="mini")

    # 놓친 입력이 있으면 여기서 잡는다 — CI 까지 가지 않는다
    assert mini_cfg.missing_inputs(tmp_path) == [], "테스트 config 가 저장소 밖 파일을 가리킨다"
    outside = [f for f in (mini_cfg.demand.volume_profile, mini_cfg.demand.through_profile,
                           mini_cfg.demand.entry_exit_profile)
               if f and not str(f).startswith(str(tmp_path))]
    assert not outside, f"tmp_path 밖을 가리키는 입력: {outside}"

    return mini_cfg, seeded_db, tmp_path / "runs"


def _kpis(db: Path, run_ids) -> dict:
    with get_conn(db, readonly=True) as conn:
        rows = conn.execute(
            f"SELECT run_id, metric, value FROM run_kpi WHERE run_id IN ({','.join('?' * len(run_ids))})",  # noqa: S608
            list(run_ids),
        ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


# ---------------------------------------------------------------------------
# 완료조건: 시드 3개 축소판이 끝까지 돈다 (CI, 1분 이내)
# ---------------------------------------------------------------------------


def test_three_seed_experiment_runs_end_to_end(mini):
    cfg, db, runs = mini
    started = time.perf_counter()

    result = run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, log=lambda _: None)

    assert time.perf_counter() - started < 60
    assert result.run_ids == tuple(f"mini_test__UE__p100__s{s:04d}" for s in SEEDS)   # make_run_id 규칙
    for name in ("kpi_ci.csv", "kpi_ci.md", "manifest.json", "heatmap_wait.png", "heatmap_queue.png"):
        assert (result.out_dir / name).is_file(), name

    s = result.summary.set_index("metric")
    assert (s["n"] == 3).all()
    assert (s["ci_low"] <= s["mean"]).all() and (s["mean"] <= s["ci_high"]).all()
    assert s.loc["n_ev_charging", "mean"] > 0          # 실제로 충전 수요가 있었다


def test_confidence_interval_is_the_t_interval(mini):
    """평균 ± t(0.975, n−1) × 표준편차 / √n."""
    from scipy import stats

    cfg, db, runs = mini
    result = run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, heatmaps=(), log=lambda _: None)
    row = result.summary.set_index("metric").loc["n_ev_charging"]

    values = [v for (rid, m), v in _kpis(db, result.run_ids).items() if m == "n_ev_charging"]
    half = stats.t.ppf(0.975, 2) * pd.Series(values).std(ddof=1) / 3 ** 0.5

    assert row["mean"] == pytest.approx(sum(values) / 3)
    assert row["ci_high"] - row["mean"] == pytest.approx(half)


# ---------------------------------------------------------------------------
# 완료조건: 같은 시드 목록 두 번 → 동일 KPI
# ---------------------------------------------------------------------------


def test_same_seeds_twice_give_identical_kpis(mini):
    cfg, db, runs = mini
    kw = dict(min_seeds=3, db_path=db, runs_dir=runs, heatmaps=(), log=lambda _: None)

    first = run_experiment(cfg, SEEDS, reuse_done=False, **kw)
    kpi_first = _kpis(db, first.run_ids)
    second = run_experiment(cfg, SEEDS, reuse_done=False, **kw)       # 전부 새로 돌린다

    assert _kpis(db, second.run_ids) == kpi_first
    pd.testing.assert_frame_equal(first.summary, second.summary)


def test_different_seeds_give_different_demand(mini):
    """시드가 EV 집합을 실제로 바꾼다 — 안 바뀌면 신뢰구간이 0 폭이 되고 의미가 없다."""
    cfg, db, runs = mini
    result = run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, heatmaps=(), log=lambda _: None)

    assert result.summary.set_index("metric").loc["dwell_total_h", "sd"] > 0


# ---------------------------------------------------------------------------
# 완료조건: run 테이블에 시드 수만큼 행
# ---------------------------------------------------------------------------


def test_run_table_has_one_row_per_seed(mini):
    cfg, db, runs = mini
    run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, heatmaps=(), log=lambda _: None)
    run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, heatmaps=(), log=lambda _: None)  # 재실행

    with get_conn(db, readonly=True) as conn:
        rows = conn.execute(
            "SELECT seed, status FROM run WHERE scenario_id = 'mini_test' ORDER BY seed"
        ).fetchall()

    assert [(r[0], r[1]) for r in rows] == [(s, "DONE") for s in SEEDS]


# ---------------------------------------------------------------------------
# 완료조건: 실패한 run 이 하나라도 있으면 결과를 내지 않고 멈춘다
# ---------------------------------------------------------------------------


def test_one_failed_seed_stops_without_results(mini, monkeypatch):
    import evdt.runner as runner

    cfg, db, runs = mini
    real = runner.run_ue_once

    def flaky(c, *, seed=None, **kw):
        if seed == 2:
            raise RuntimeError("일부러 실패")
        return real(c, seed=seed, **kw)

    monkeypatch.setattr(runner, "run_ue_once", flaky)

    with pytest.raises(ExperimentFailed, match="seed 2 실패"):
        run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, log=lambda _: None)

    assert not (runs / "experiments" / experiment_id(cfg, SEEDS)).exists()


def test_failed_run_is_not_reused(mini):
    """이전에 FAILED 로 남은 run 은 재사용하지 않고 다시 돌린다."""
    cfg, db, runs = mini
    run_id = run_ue_once(cfg, seed=1, db_path=db, runs_dir=runs, gap_plot=False, log=lambda _: None)

    with get_conn(db) as conn:
        conn.execute("UPDATE run SET status = 'FAILED' WHERE run_id = ?", (run_id,))

    run_experiment(cfg, SEEDS, min_seeds=3, db_path=db, runs_dir=runs, heatmaps=(), log=lambda _: None)

    with get_conn(db, readonly=True) as conn:
        assert conn.execute("SELECT status FROM run WHERE run_id = ?", (run_id,)).fetchone()[0] == "DONE"


def test_twenty_seeds_are_required_by_default(mini):
    cfg, db, runs = mini

    with pytest.raises(ValueError, match="20개 이상"):
        run_experiment(cfg, SEEDS, db_path=db, runs_dir=runs)


# ---------------------------------------------------------------------------
# 완료조건: 히트맵 세로축 = offset_km
# ---------------------------------------------------------------------------


def test_heatmap_rows_sit_at_their_real_offset():
    """같은 간격 줄로 늘어놓으면 2.5 km 떨어진 곳과 40 km 떨어진 곳이 같아 보인다."""
    offsets = [1.0, 27.0, 153.8, 156.3, 291.0]
    bands = _station_bands(offsets)

    for km, (lo, hi) in zip(offsets, bands, strict=True):
        assert lo <= km <= hi
        assert hi - lo <= STATION_BAND_KM
    for (_, hi), (lo, _) in zip(bands, bands[1:], strict=False):
        assert hi <= lo + 1e-9                          # 겹치지 않는다
    assert bands[3][0] == pytest.approx((153.8 + 156.3) / 2)   # 가까운 두 곳은 중간에서 자른다
    assert bands[4][0] - bands[3][1] > 100              # 먼 곳 사이는 비어 있다 (도로)


# ---------------------------------------------------------------------------
# 보조
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("1-3", [1, 2, 3]), ("1,2,5", [1, 2, 5]), ("1-2,10", [1, 2, 10]), ("3,3,1", [3, 1]),
])
def test_parse_seeds(text, expected):
    assert parse_seeds(text) == expected


def test_experiment_id_names_the_seed_range(cfg):
    assert experiment_id(cfg, range(1, 21)).endswith("__UE__p100__s0001-0020")
    assert "__n3-" in experiment_id(cfg, [1, 5, 9])


def test_concentration_kpis_survive_a_run_without_charging(mini):
    """아무도 충전하지 않는 run 에서도 쏠림 지표가 0 으로 남는다 (NULL 로 빠지지 않는다)."""
    cfg, db, runs = mini
    run_id = run_ue_once(cfg, seed=1, db_path=db, runs_dir=runs, gap_plot=False, log=lambda _: None)
    (runs / run_id / "charge_event.parquet").unlink()

    _record_concentration_kpis(run_id, db, runs, lambda _: None)

    assert _kpis(db, [run_id])[(run_id, "bottleneck_slots")] == 0.0
