"""CTM 하루 실행과 기록 (#56).

기록이 계약을 지키는지(스냅샷 로거가 받아 주는지), 5분 집계가 평균인지, 그리고
기록에 남은 속도와 통행시간이 보는 속도가 같은지를 본다. 마지막 것이 특히 중요하다 —
다르면 "이 차가 왜 이때 도착했나" 를 기록으로 따라갈 수 없다.
"""

from __future__ import annotations

import numpy as np
import pytest

from evdt.io.event_log import snapshot_row_problems
from evdt.io.writers import SCHEMAS
from evdt.world.ctm_run import RECORD_EVERY_MIN, run_day
from tests.test_ctm import DT_MIN, make_cells

CELL_KM = 1.0


def _cell_rows(n: int) -> list[dict]:
    """io.cells.read_cells 가 주는 모양의 행."""
    return [
        {
            "cell_id": f"gyeongbu_down_{i:04d}",
            "seq": i,
            "offset_km_start": i * CELL_KM,
            "offset_km_end": (i + 1) * CELL_KM,
            "lat_start": 36.0 + i * 0.01,
            "lon_start": 127.0 + i * 0.01,
        }
        for i in range(n)
    ]


def _run(n_cells: int = 6, horizon_min: float = 60.0, inflow: float = 20.0, **kw):
    cells = make_cells([4] * n_cells)
    return run_day(
        cells, _cell_rows(n_cells), DT_MIN,
        inflow_veh_per_step=lambda t: inflow,
        horizon_min=horizon_min,
        **kw,
    )


# ---------------------------------------------------------------------------
# 기록이 계약을 지킨다
# ---------------------------------------------------------------------------


def test_snapshot_rows_pass_the_logger() -> None:
    """로거가 거부하는 행이 하나도 없어야 한다 (설계 규칙 4)."""
    run = _run()

    assert run.snapshot_rows
    for row in run.snapshot_rows:
        assert snapshot_row_problems(row) == [], row


def test_snapshot_covers_all_four_states() -> None:
    run = _run()
    states = {row["state"] for row in run.snapshot_rows}

    assert states == {"speed_kmh", "density_veh_km", "flow_veh_h", "ev_count"}


def test_cell_state_rows_match_the_parquet_schema() -> None:
    """writers.SCHEMAS 의 컬럼과 정확히 같아야 한다 (run_id 는 writer 가 붙인다)."""
    expected = {f.name for f in SCHEMAS["cell_state"]} - {"run_id"}

    for row in _run().cell_state_rows:
        assert set(row) == expected


def test_snapshot_point_is_the_cell_start() -> None:
    """셀은 선분이라 점 하나로 줄인다. 끝점을 쓰면 이웃 셀과 겹쳐 보인다."""
    rows = _cell_rows(4)
    run = _run(n_cells=4)

    by_id = {r["cell_id"]: r for r in rows}
    for snap in run.snapshot_rows:
        assert snap["lat"] == pytest.approx(by_id[snap["entity_id"]]["lat_start"])


# ---------------------------------------------------------------------------
# 5분 집계
# ---------------------------------------------------------------------------


def test_records_every_five_minutes() -> None:
    """한 시간이면 12칸 × 셀 수."""
    n_cells = 6
    run = _run(n_cells=n_cells, horizon_min=60.0)

    windows = 60.0 / RECORD_EVERY_MIN

    assert len(run.cell_state_rows) == windows * n_cells
    assert len(run.snapshot_rows) == windows * n_cells * 4


def test_window_is_a_mean_not_the_last_step() -> None:
    """5분 중 한 스텝만 막힌 것과 내내 막힌 것이 달라야 한다.

    마지막 값만 쓰면 둘이 같아 보인다 — 막힌 순간이 칸의 끝이 아니면 아예 사라진다.
    """
    cells = make_cells([4] * 4)
    rows = _cell_rows(4)

    # 5분 칸 하나 안에서 유입이 크게 흔들린다
    def bursty(t_min: float) -> float:
        return 60.0 if (t_min % RECORD_EVERY_MIN) < DT_MIN else 0.0

    run = run_day(cells, rows, DT_MIN, inflow_veh_per_step=bursty, horizon_min=RECORD_EVERY_MIN)
    steady = run_day(cells, rows, DT_MIN, inflow_veh_per_step=lambda t: 60.0,
                     horizon_min=RECORD_EVERY_MIN)

    bursty_flow = run.cell_state_rows[0]["flow_veh_h"]
    steady_flow = steady.cell_state_rows[0]["flow_veh_h"]

    assert bursty_flow < steady_flow


def test_last_partial_window_is_still_recorded() -> None:
    """시간이 5분으로 안 나눠떨어져도 마지막 조각을 버리지 않는다."""
    run = _run(n_cells=3, horizon_min=7.0)

    times = sorted({row["t_min"] for row in run.cell_state_rows})

    assert times == [5.0, 7.0]


# ---------------------------------------------------------------------------
# 통행시간이 보는 속도 == 기록에 남은 속도
# ---------------------------------------------------------------------------


def test_speed_field_matches_the_recorded_cell_state() -> None:
    """속도 격자와 cell_state 가 같은 집계에서 나온다."""
    n_cells = 6
    run = _run(n_cells=n_cells, horizon_min=30.0)

    grid = run.speed_field.speeds_kmh
    by_time: dict[float, dict[str, float]] = {}

    for row in run.cell_state_rows:
        by_time.setdefault(row["t_min"], {})[row["cell_id"]] = row["speed_kmh"]

    for window, t_min in enumerate(sorted(by_time)):
        for cell in range(n_cells):
            recorded = by_time[t_min][f"gyeongbu_down_{cell:04d}"]
            assert grid[window, cell] == pytest.approx(recorded)


def test_speed_field_edges_span_the_corridor() -> None:
    run = _run(n_cells=6)

    assert run.speed_field.edges_km[0] == pytest.approx(0.0)
    assert run.speed_field.length_km == pytest.approx(6 * CELL_KM)


# ---------------------------------------------------------------------------
# 차량 보존 · 막는 것
# ---------------------------------------------------------------------------


def test_vehicles_are_conserved_over_the_whole_day() -> None:
    run = _run(n_cells=8, horizon_min=180.0, inflow=25.0)

    assert run.conservation_error_veh == pytest.approx(0.0, abs=1e-6)
    assert run.entered_veh > 0


def test_cfl_is_checked_before_the_first_step() -> None:
    """돌다가 틀린 값을 내는 것보다 안 도는 편이 낫다."""
    from evdt.world.ctm import CFLViolation

    cells = make_cells([4] * 3, length_km=0.1)

    with pytest.raises(CFLViolation):
        run_day(cells, _cell_rows(3), DT_MIN, inflow_veh_per_step=lambda t: 1.0, horizon_min=5.0)


def test_row_count_mismatch_is_refused() -> None:
    cells = make_cells([4] * 5)

    with pytest.raises(ValueError, match="셀 수가 다르다"):
        run_day(cells, _cell_rows(3), DT_MIN, inflow_veh_per_step=lambda t: 1.0, horizon_min=5.0)


def test_ev_count_is_recorded_when_given() -> None:
    """EV 수는 보여주기용이다 — CTM 물리에는 안 들어간다."""
    n_cells = 4
    run = _run(n_cells=n_cells, horizon_min=10.0,
               ev_count_per_cell=lambda t: np.arange(n_cells, dtype=float))

    ev_rows = [r for r in run.snapshot_rows if r["state"] == "ev_count"]
    last_cell = [r for r in ev_rows if r["entity_id"] == f"gyeongbu_down_{n_cells - 1:04d}"]

    assert all(r["value"] == pytest.approx(n_cells - 1) for r in last_cell)


# ---------------------------------------------------------------------------
# warmup — 빈 도로에서 0시에 시작하지 않는다 (#54)
# ---------------------------------------------------------------------------


def test_warmup_fills_the_corridor_before_recording() -> None:
    """실측 0시에는 전날 들어온 차가 이미 달리고 있다. 빈 채로 시작하면 새벽이 모자란다."""
    cold = _run(n_cells=8, horizon_min=120.0)
    warm = run_day(make_cells([4] * 8), _cell_rows(8), DT_MIN,
                   inflow_veh_per_step=lambda t: 20.0, horizon_min=120.0, warmup_min=60.0)

    assert cold.initial_veh == 0.0
    assert warm.initial_veh > 0.0

    first = lambda run: min(r["t_min"] for r in run.cell_state_rows)   # noqa: E731
    cold_flow = [r["flow_veh_h"] for r in cold.cell_state_rows if r["t_min"] == first(cold)]
    warm_flow = [r["flow_veh_h"] for r in warm.cell_state_rows if r["t_min"] == first(warm)]

    assert sum(warm_flow) > sum(cold_flow)


def test_conservation_still_holds_with_warmup() -> None:
    """시작부터 도로에 있던 차는 '들어온 차' 가 아니다. 안 빼면 보존이 warmup 만큼 틀린다."""
    warm = run_day(make_cells([4] * 8), _cell_rows(8), DT_MIN,
                   inflow_veh_per_step=lambda t: 25.0, horizon_min=120.0, warmup_min=30.0)

    assert warm.initial_veh > 0.0
    assert warm.conservation_error_veh == pytest.approx(0.0, abs=1e-6)


def test_warmup_records_nothing_extra() -> None:
    """감아 도는 구간은 기록에 남지 않는다."""
    warm = run_day(make_cells([4] * 4), _cell_rows(4), DT_MIN,
                   inflow_veh_per_step=lambda t: 10.0, horizon_min=30.0, warmup_min=60.0)

    times = sorted({r["t_min"] for r in warm.cell_state_rows})

    assert min(times) > 0.0
    assert max(times) == pytest.approx(30.0)
