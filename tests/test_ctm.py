"""CTM 단위 테스트 (이슈 #56).

실제 셀(`cell` 테이블)은 표준노드링크 SHP 가 있는 기기에서만 만들 수 있어서, 여기서는
합성 셀로 검증한다. 물리는 셀이 진짜인지와 무관하다 — 차가 보존되는지, 정체가
이론대로 퍼지는지는 균일한 셀에서 더 정확하게 잴 수 있다.
"""

from __future__ import annotations

import numpy as np
import pytest

from evdt.world.ctm import (
    CellArrays,
    CFLViolation,
    FundamentalDiagramError,
    check_cfl,
    density_veh_km_lane,
    receiving,
    sending,
    speed_kmh,
    step,
)

V_FREE = 100.0
W_BACK = 20.0
K_JAM = 150.0
DT_MIN = 0.2
CELL_KM = 1.0


def q_per_lane(v_free: float = V_FREE, w_back: float = W_BACK, k_jam: float = K_JAM) -> float:
    """삼각형 기본도의 차로당 용량 (설계문서 §9.5)."""
    return v_free * w_back * k_jam / (v_free + w_back)


def make_cells(
    lanes: list[int] | np.ndarray,
    *,
    length_km: float = CELL_KM,
    v_free: float = V_FREE,
    w_back: float = W_BACK,
    k_jam: float = K_JAM,
) -> CellArrays:
    """차로수만 주면 나머지는 기본도에서 따라 나오는 균일 셀."""
    lane_array = np.asarray(lanes, dtype=float)
    ones = np.ones_like(lane_array)

    return CellArrays(
        length_km=ones * length_km,
        lanes=lane_array,
        v_free_kmh=ones * v_free,
        w_back_kmh=ones * w_back,
        k_jam_veh_km_lane=ones * k_jam,
        q_max_veh_h=lane_array * q_per_lane(v_free, w_back, k_jam),
    )


# ---------------------------------------------------------------------------
# 만들 때 막는 것 — 설계문서 §9.5 와 CFL
# ---------------------------------------------------------------------------


def test_inconsistent_q_max_is_refused() -> None:
    """q_max 를 마음대로 넣으면 거부한다. 이걸 놓치면 정체가 아예 안 생긴다."""
    cells = make_cells([4, 4])
    broken = dict(
        length_km=cells.length_km,
        lanes=cells.lanes,
        v_free_kmh=cells.v_free_kmh,
        w_back_kmh=cells.w_back_kmh,
        k_jam_veh_km_lane=cells.k_jam_veh_km_lane,
        q_max_veh_h=cells.q_max_veh_h * 2.0,
    )

    with pytest.raises(FundamentalDiagramError, match="삼각형 기본도"):
        CellArrays(**broken)


def test_q_max_within_one_percent_is_accepted() -> None:
    """DB 의 CHECK 와 같은 1% 허용오차. 저장하며 반올림된 값이 거부되면 안 된다."""
    cells = make_cells([4, 4])

    CellArrays(
        length_km=cells.length_km,
        lanes=cells.lanes,
        v_free_kmh=cells.v_free_kmh,
        w_back_kmh=cells.w_back_kmh,
        k_jam_veh_km_lane=cells.k_jam_veh_km_lane,
        q_max_veh_h=np.round(cells.q_max_veh_h, 1),
    )


def test_cfl_violation_stops_before_running() -> None:
    """한 스텝에 차가 셀을 건너뛰면 멈춘다. 몇 번 셀인지 말해 준다."""
    cells = make_cells([4, 4, 4], length_km=0.1)   # CFL 하한 = 100 × 0.2/60 = 0.333 km

    with pytest.raises(CFLViolation, match="CFL 하한") as info:
        check_cfl(cells, DT_MIN)

    assert "셀 0" in str(info.value)
    assert "3개" in str(info.value)   # 어긴 셀 수를 알려준다


def test_cfl_passes_at_the_exact_floor() -> None:
    """하한과 정확히 같은 길이는 통과한다 (부동소수점 때문에 떨어지면 안 된다)."""
    floor_km = V_FREE * DT_MIN / 60.0

    check_cfl(make_cells([4], length_km=floor_km), DT_MIN)


# ---------------------------------------------------------------------------
# 보내는 양 · 받는 양
# ---------------------------------------------------------------------------


def test_sending_is_capped_by_capacity() -> None:
    """차가 아무리 많아도 한 스텝에 용량보다 많이 나가지 않는다."""
    cells = make_cells([4])
    capacity_per_step = cells.q_max_veh_h[0] * DT_MIN / 60.0

    crowded = sending(np.array([cells.jam_veh[0] / 2]), cells, DT_MIN)

    assert crowded[0] == pytest.approx(capacity_per_step)


def test_sending_is_free_flow_when_nearly_empty() -> None:
    """한산하면 자유속도로 지나가는 몫이 그대로 나간다."""
    cells = make_cells([4])
    n = np.array([10.0])

    assert sending(n, cells, DT_MIN)[0] == pytest.approx(10.0 * V_FREE * DT_MIN / 60.0 / CELL_KM)


def test_receiving_is_zero_when_jammed() -> None:
    """꽉 찬 셀은 한 대도 못 받는다. 이것이 정체가 상류로 번지는 원리다."""
    cells = make_cells([4])

    assert receiving(cells.jam_veh, cells, DT_MIN)[0] == pytest.approx(0.0)


def test_receiving_is_capped_by_capacity_when_empty() -> None:
    """텅 빈 셀도 용량보다 많이 받지는 못한다."""
    cells = make_cells([4])
    capacity_per_step = cells.q_max_veh_h[0] * DT_MIN / 60.0

    assert receiving(np.zeros(1), cells, DT_MIN)[0] == pytest.approx(capacity_per_step)


# ---------------------------------------------------------------------------
# 차량 보존 — 매 스텝
# ---------------------------------------------------------------------------


def _conservation_error(result, before: np.ndarray) -> float:
    """들어온 차 − 나간 차 − 늘어난 차. 0 이어야 한다."""
    entered = result.boundary_flow[0] + result.ramp_in_veh.sum()
    left = result.boundary_flow[-1] + result.ramp_out_veh.sum()

    return float(entered - left - (result.n_veh.sum() - before.sum()))


def test_vehicles_are_conserved_every_step() -> None:
    """유입·램프·정체가 모두 섞인 상태에서 매 스텝 보존된다."""
    cells = make_cells([4, 4, 3, 2, 4, 4])
    n = np.zeros(len(cells))

    ramp_demand = np.array([0.0, 5.0, 0.0, 0.0, 3.0, 0.0])
    exit_ratio = np.array([0.0, 0.0, 0.1, 0.0, 0.0, 0.0])

    for _ in range(600):
        result = step(
            n,
            cells,
            DT_MIN,
            inflow_veh=25.0,
            ramp_demand_veh=ramp_demand,
            exit_ratio=exit_ratio,
        )

        assert _conservation_error(result, n) == pytest.approx(0.0, abs=1e-9)

        n = result.n_veh


def test_cells_never_exceed_jam_or_go_negative() -> None:
    """어떤 셀도 정체 밀도를 넘거나 음수가 되지 않는다."""
    cells = make_cells([4, 4, 1, 4])
    n = np.zeros(len(cells))

    for _ in range(2000):
        n = step(n, cells, DT_MIN, inflow_veh=40.0).n_veh

        assert np.all(n >= -1e-9)
        assert np.all(n <= cells.jam_veh + 1e-9)


def test_blocked_inflow_is_reported() -> None:
    """첫 셀이 꽉 차면 들어오려던 차가 실제로는 덜 들어간다."""
    cells = make_cells([4, 4])
    jammed = cells.jam_veh.copy()

    result = step(jammed, cells, DT_MIN, inflow_veh=50.0)

    assert result.boundary_flow[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 자유류 통행시간
# ---------------------------------------------------------------------------


def test_light_traffic_travels_at_free_flow_speed() -> None:
    """막히지 않으면 셀 속도가 자유속도다 (지금의 고정 80 km/h 를 대체할 근거)."""
    cells = make_cells([4] * 20)
    n = np.zeros(len(cells))
    inflow = 3.0   # 용량(대/스텝)의 한참 아래

    for _ in range(3000):
        result = step(n, cells, DT_MIN, inflow_veh=inflow)
        n = result.n_veh

    speeds = speed_kmh(n, result.boundary_flow[1:], cells, DT_MIN)

    assert np.allclose(speeds, V_FREE, rtol=1e-6)


# ---------------------------------------------------------------------------
# 충격파 — 해석해와 비교
# ---------------------------------------------------------------------------


def test_queue_spreads_upstream_at_the_analytic_shock_speed() -> None:
    """병목 뒤 정체가 Rankine-Hugoniot 충격파 속도로 상류로 번진다.

        w_shock = (q_하류 − q_상류) / (k_하류 − k_상류)

    상류는 자유류(q_in, k1 = q_in / v_free), 하류는 병목 용량으로 막힌 상태
    (q_bn, k2 = K_jam − q_bn / w_back) 다. 삼각형 기본도에서 정체 구간의 밀도는
    용량에서 바로 나오므로, 정체가 번지는 속도도 숫자로 정해진다.
    """
    upstream_lanes, bottleneck_lanes = 4, 2
    n_upstream = 60
    cells = make_cells([upstream_lanes] * n_upstream + [bottleneck_lanes] * 10)

    q_bottleneck = bottleneck_lanes * q_per_lane()            # 대/h
    q_in = 0.92 * upstream_lanes * q_per_lane()               # 병목 용량보다 크다

    k_free = q_in / V_FREE                                     # 대/km (전 차로)
    k_queue = upstream_lanes * K_JAM - q_bottleneck / W_BACK
    analytic_kmh = (q_bottleneck - q_in) / (k_queue - k_free)

    assert analytic_kmh < 0   # 상류로 번진다

    inflow_per_step = q_in * DT_MIN / 60.0
    n = np.zeros(len(cells))
    critical = (k_free + k_queue) / 2 / upstream_lanes         # 대/km/차로

    # 정체 앞머리(상류 쪽 끝)가 어디인지 스텝마다 기록한다
    front_km: list[tuple[float, float]] = []

    for stepno in range(1, 12001):
        n = step(n, cells, DT_MIN, inflow_veh=inflow_per_step).n_veh
        queued = np.flatnonzero(density_veh_km_lane(n, cells)[:n_upstream] > critical)

        if queued.size:
            front_km.append((stepno * DT_MIN, float(queued[0]) * CELL_KM))

    # 정체가 상류 구간 안에서만 자라는 동안을 본다 (끝에 닿으면 더 못 자란다)
    inside = [(t, x) for t, x in front_km if 5.0 < x < n_upstream * CELL_KM - 5.0]

    assert len(inside) > 100, "충격파를 잴 구간이 충분히 잡히지 않았다"

    times = np.array([t / 60.0 for t, _ in inside])            # 시간
    positions = np.array([x for _, x in inside])
    measured_kmh = np.polyfit(times, positions, 1)[0]

    # 실측 오차 0.5% (셀 1 km 양자화를 897개 점으로 평균한 결과). 3% 면 넉넉하다
    assert measured_kmh == pytest.approx(analytic_kmh, rel=0.03)


def test_no_queue_when_demand_is_below_bottleneck_capacity() -> None:
    """병목 용량보다 적게 넣으면 정체가 생기지 않는다 (위 테스트의 대조군)."""
    cells = make_cells([4] * 30 + [2] * 10)
    q_in = 0.8 * 2 * q_per_lane()
    n = np.zeros(len(cells))

    for _ in range(6000):
        n = step(n, cells, DT_MIN, inflow_veh=q_in * DT_MIN / 60.0).n_veh

    assert density_veh_km_lane(n, cells).max() < K_JAM / 2


# ---------------------------------------------------------------------------
# 램프
# ---------------------------------------------------------------------------


def test_on_ramp_shares_space_when_mainline_is_congested() -> None:
    """본선이 막혀도 진입 램프가 완전히 굶지 않는다 (우선순위 몫은 받는다)."""
    cells = make_cells([4, 4, 1])
    ramp = np.array([0.0, 4.0, 0.0])
    n = np.zeros(len(cells))

    got = []
    for _ in range(4000):
        result = step(n, cells, DT_MIN, inflow_veh=30.0, ramp_demand_veh=ramp)
        n = result.n_veh
        got.append(result.ramp_in_veh[1])

    assert 0.0 < np.mean(got[-100:]) <= 4.0


def test_off_ramp_takes_its_share_of_the_flow() -> None:
    """진출 비율만큼 빠져나간다. 본선과 합치면 셀에서 나간 총량이다."""
    cells = make_cells([4, 4, 4])
    n = np.array([100.0, 0.0, 0.0])

    result = step(n, cells, DT_MIN, exit_ratio=np.array([0.25, 0.0, 0.0]))

    leaving = result.ramp_out_veh[0] + result.boundary_flow[1]

    assert result.ramp_out_veh[0] == pytest.approx(0.25 * leaving)
    assert leaving == pytest.approx(sending(n, cells, DT_MIN)[0])


def test_off_ramp_is_blocked_with_the_mainline() -> None:
    """본선이 막히면 진출 차량도 같이 막힌다 (FIFO). 한 줄에 서 있기 때문이다."""
    cells = make_cells([4, 4])
    n = np.array([100.0, 0.0])
    jammed = np.array([100.0, cells.jam_veh[1]])
    exit_ratio = np.array([0.25, 0.0])

    free = step(n, cells, DT_MIN, exit_ratio=exit_ratio)
    blocked = step(jammed, cells, DT_MIN, exit_ratio=exit_ratio)

    assert blocked.ramp_out_veh[0] < free.ramp_out_veh[0]
    assert blocked.ramp_out_veh[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 속도 — 하루치가 1분 안에 돌아야 한다 (#56 완료조건)
# ---------------------------------------------------------------------------


def test_one_day_of_the_down_corridor_is_fast_enough() -> None:
    """하행 규모(384 셀)로 하루(7,200 스텝)를 돈다.

    완료조건은 1분이다. 여기서는 기기 차이를 감안해 30초로 잡는다 — 재는 목적이
    "느려지지 않았나" 이지 정확한 초를 고정하는 것이 아니다. 로컬 실측은 2.6초였다.
    """
    import time

    rng = np.random.default_rng(0)
    cells = make_cells(rng.choice([3, 4, 5], size=384))
    check_cfl(cells, DT_MIN)

    ramp = np.zeros(len(cells))
    ramp[::20] = 2.0
    exit_ratio = np.zeros(len(cells))
    exit_ratio[::17] = 0.05

    n = np.zeros(len(cells))
    started = time.perf_counter()

    for _ in range(7200):
        n = step(
            n, cells, DT_MIN,
            inflow_veh=25.0,
            ramp_demand_veh=ramp,
            exit_ratio=exit_ratio,
        ).n_veh

    assert time.perf_counter() - started < 30.0
