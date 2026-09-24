"""UE 균형 (이슈 #29).

완료조건 중 문장 그대로는 지킬 수 없는 두 개(단조감소, 대기시간이 "같아진다")는
docs/UE_equilibrium.md §4 에 이유를 적고, 대신 지킬 수 있는 더 강한 조건을 검사한다.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from evdt.config import ScenarioConfig
from evdt.engine.ue import (
    UENotConverged,
    UESettings,
    _plan_cost,
    _roll,
    des_arrivals,
    evaluate,
    solve_and_log,
    solve_ue,
    solver_log_rows,
)
from evdt.engine.ue_demand import Plan, PlannedStop, TripDemand
from evdt.io.db import get_conn, read_table
from evdt.io.run_registry import RunContext
from evdt.io.writers import SCHEMAS
from evdt.world.sim import StationSpec, expand_chargers, run_charging_des
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
DSOC = 0.5
SPEED = 60.0   # 1 km = 1 분이라 도착 시각을 손으로 세기 쉽다


def _chargers(n_units: int, power_kw: float = 200.0, sid: str = "A") -> tuple:
    return expand_chargers([{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}])


def _battery_for(minutes_at_200kw: float) -> float:
    """200 kW 에서 DSOC 를 채우는 데 정확히 minutes 분 걸리는 배터리 (평탄 곡선)."""
    return minutes_at_200kw * 200.0 / 60.0 / DSOC


def _trip(ev_id: str, entry: float, stops_by_plan, *, service_min: float = 30.0, vmax: float = 200.0) -> TripDemand:
    """stops_by_plan: [[(station_id, offset_km), ...], ...] — 계획마다 정차 목록."""
    plans = tuple(
        Plan(tuple(PlannedStop(sid, off, 0.25, 0.25 + DSOC) for sid, off in stops))
        for stops in stops_by_plan
    )
    return TripDemand(ev_id, "vc", entry, 0.0, 400.0, _battery_for(service_min), vmax, FLAT, 1.0, plans)


def _settings(**kw) -> UESettings:
    return UESettings(speed_kmh=SPEED, **kw)


def _mean_wait(result, trips, sid: str, service_min: float = 30.0) -> float:
    w = [v.dwell_min - service_min for v in result.visits if v.station_id == sid]
    return sum(w) / len(w)


# ---------------------------------------------------------------------------
# 완료조건: 휴게소 1곳이면 전원 그곳
# ---------------------------------------------------------------------------


def test_single_station_everyone_goes_there():
    trips = [_trip(f"e{i:02d}", i * 2.0, [[("A", 50.0)]]) for i in range(20)]
    r = solve_ue(trips, {"A": _chargers(2)}, _settings())

    assert set(r.station_of(trips).values()) == {("A",)}
    assert r.converged
    assert r.final_gap == 0.0


# ---------------------------------------------------------------------------
# 완료조건: 같은 조건 휴게소 2곳이면 대기가 같아진다 (균형 조건)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [80, 81])
def test_two_identical_stations_balance(n):
    """정확히 같아지는 것은 대수가 짝수이고 도착이 대칭일 때뿐이다 (81대면 한 대 차이).

    그래서 두 가지를 본다:
      1) 균형 조건 그 자체 — 누구도 혼자 옮겨서 줄일 수 없다 (gap 0)
      2) 두 곳 평균 대기 차이가 "한 대가 옮겨갈 때 생기는 차이" 이하
         (충전 30분 ÷ 충전기 2기 = 15분)
    """
    trips = [_trip(f"e{i:03d}", i * 1.5, [[("A", 50.0)], [("B", 50.0)]]) for i in range(n)]
    ch = {"A": _chargers(2, sid="A"), "B": _chargers(2, sid="B")}
    r = solve_ue(trips, ch, _settings(gap_tol=1e-9))

    assert r.final_gap == 0.0
    assert abs(_mean_wait(r, trips, "A") - _mean_wait(r, trips, "B")) <= 30.0 / 2
    counts = pd.Series([s[0] for s in r.station_of(trips).values()]).value_counts()
    assert abs(counts["A"] - counts["B"]) <= 1


# ---------------------------------------------------------------------------
# 완료조건: gap — 반복별로 남고, 수렴 기준 아래로 내려간다
# ---------------------------------------------------------------------------


def _random_single_stop(seed: int, n: int = 150):
    rng = random.Random(seed)
    stations = [("A", 30.0), ("B", 70.0), ("C", 120.0)]
    trips = []
    for i in range(n):
        reach = rng.choice([1, 2, 3])  # 배터리가 적은 차는 앞쪽 휴게소만 닿는다
        trips.append(_trip(f"e{i:03d}", rng.uniform(0, 300), [[s] for s in stations[:reach]],
                           service_min=rng.uniform(20, 40)))
    ch = {"A": _chargers(3, 350.0, "A"), "B": _chargers(2, 200.0, "B"), "C": _chargers(2, 100.0, "C")}
    return trips, ch


def test_single_stop_demand_is_exact_equilibrium_after_one_pass():
    """FIFO 에서 내 대기는 먼저 온 차만 정한다 → 도착 순서대로 한 번 고르면 정확한 균형."""
    trips, ch = _random_single_stop(1)
    r = solve_ue(trips, ch, _settings(gap_tol=1e-9, min_gain_min=0.0))

    assert [h.iteration for h in r.history] == [0, 1]
    assert r.history[0].rel_gap > 0.05       # 대기를 모르는 선택은 균형에서 멀다
    assert r.final_gap == 0.0


def test_gap_history_starts_at_free_flow_and_ends_below_tolerance():
    trips, ch = _random_single_stop(2)
    r = solve_ue(trips, ch, _settings(gap_tol=0.03))

    gaps = [h.rel_gap for h in r.history]
    assert gaps[0] == max(gaps)
    assert gaps[-1] <= 0.03
    assert r.history[0].n_switched > 0


def test_solver_log_rows_fill_only_iteration_and_rel_gap():
    trips, ch = _random_single_stop(3)
    rows = solver_log_rows(solve_ue(trips, ch, _settings()))

    assert all(set(r) == {"iteration", "rel_gap"} for r in rows)
    assert {"iteration", "rel_gap"} <= set(SCHEMAS["solver_log"].names)
    assert [r["iteration"] for r in rows] == list(range(len(rows)))


# ---------------------------------------------------------------------------
# 완료조건: 수렴 실패는 조용히 넘어가지 않는다 → run FAILED
# ---------------------------------------------------------------------------


def _unbalanced():
    return (
        [_trip(f"e{i:03d}", i * 1.5, [[("A", 50.0)], [("B", 50.0)]]) for i in range(40)],
        {"A": _chargers(2, sid="A"), "B": _chargers(2, sid="B")},
    )


def test_not_converged_raises_with_history():
    trips, ch = _unbalanced()

    with pytest.raises(UENotConverged, match="수렴하지 않았다") as info:
        solve_ue(trips, ch, _settings(max_iter=0))

    assert not info.value.result.converged
    assert len(info.value.result.history) == 1


def test_failed_convergence_marks_run_failed_and_keeps_gap(cfg: ScenarioConfig, seeded_db, tmp_path):
    trips, ch = _unbalanced()
    runs = tmp_path / "runs"

    with pytest.raises(UENotConverged):
        with RunContext.open(cfg, seed=11, db_path=seeded_db, runs_dir=runs) as run:
            run_id = run.run_id
            solve_and_log(trips, ch, _settings(max_iter=0), run.writer)

    with get_conn(seeded_db, readonly=True) as conn:
        row = read_table(conn, "run", where="run_id = ?", params=(run_id,)).iloc[0]

    assert row["status"] == "FAILED"
    assert "UENotConverged" in row["error_message"]

    log = pd.read_parquet(runs / run_id / "solver_log.parquet")
    assert log["iteration"].tolist() == [0]
    assert log["rel_gap"].iloc[0] > 0.5
    assert log["mip_gap"].isna().all()      # MIP 칸은 비워 둔다


# ---------------------------------------------------------------------------
# 완료조건: 재현성
# ---------------------------------------------------------------------------


def test_same_input_same_result():
    trips, ch = _random_single_stop(4)
    a = solve_ue(trips, ch, _settings())
    b = solve_ue(list(reversed(trips)), ch, _settings())   # 입력 순서가 달라도

    assert a.choice == b.choice
    assert a.history == b.history
    assert a.visits == b.visits


# ---------------------------------------------------------------------------
# 비용이 queue_rule 과 DES 와 같다
# ---------------------------------------------------------------------------


def _multi_stop_case():
    rng = random.Random(5)
    trips = []
    for i in range(60):
        plans = [[("A", 40.0), ("C", 200.0)], [("B", 90.0), ("C", 200.0)], [("B", 90.0), ("D", 260.0)]]
        trips.append(_trip(f"e{i:03d}", rng.uniform(0, 120), plans, service_min=rng.uniform(15, 35)))
    ch = {s: _chargers(2, 200.0, s) for s in "ABCD"}
    return trips, ch


def test_des_reproduces_ue_dwell_including_second_stops():
    """UE 가 굴린 결과를 DES 에 그대로 넣으면 정차마다 체류시간이 같아야 한다."""
    trips, ch = _multi_stop_case()
    r = solve_ue(trips, ch, _settings(gap_tol=0.2))
    sim = run_charging_des(
        [StationSpec(s, 36.5, 127.5, c) for s, c in ch.items()], des_arrivals(trips, r)
    )
    des = {(e["ev_id"], e["stop_seq"]): e["dwell_min"] for e in sim.charge_events}

    assert len(des) == len(r.visits)
    assert any(v.stop_seq == 2 for v in r.visits)
    for v in r.visits:
        assert des[(v.ev_id, v.stop_seq)] == pytest.approx(v.dwell_min, abs=1e-9)


def test_second_stop_arrives_after_first_dwell_and_drive():
    trips = [_trip("e0", 0.0, [[("A", 40.0), ("C", 200.0)]])]
    r = solve_ue(trips, {"A": _chargers(1, sid="A"), "C": _chargers(1, sid="C")}, _settings())
    first, second = sorted(r.visits, key=lambda v: v.stop_seq)

    assert first.t_arrive_min == 40.0
    assert second.t_arrive_min == pytest.approx(40.0 + first.dwell_min + 160.0)


def test_unilateral_cost_is_exact_for_single_stop():
    """'혼자 바꾸면?' 을 실제로 한 대만 바꿔 굴린 결과와 비교한다 (한 번 서는 차)."""
    trips, ch = _random_single_stop(6, n=60)
    trips = sorted(trips, key=lambda t: t.ev_id)
    choice = {t.ev_id: 0 for t in trips}
    ledgers, _ = _roll(trips, choice, ch, ConstantSpeed(SPEED))

    for trip in trips[::7]:
        for k, plan in enumerate(trip.plans):
            predicted, _ = _plan_cost(trip, plan, ledgers, ConstantSpeed(SPEED), exclude_self=True)
            _, visits = _roll(trips, {**choice, trip.ev_id: k}, ch, ConstantSpeed(SPEED))
            actual = sum(v.dwell_min for v in visits if v.ev_id == trip.ev_id)
            assert predicted == pytest.approx(actual, abs=1e-9)


def test_evaluate_matches_the_definition_of_gap():
    trips, ch = _random_single_stop(7, n=40)
    ev = evaluate(sorted(trips, key=lambda t: t.ev_id), {t.ev_id: 0 for t in trips}, ch, ConstantSpeed(SPEED))
    excess = sum(ev.current[e] - ev.best_cost[e] for e in ev.current)

    assert ev.rel_gap == pytest.approx(excess / sum(ev.current.values()))
    assert all(ev.best_cost[e] <= ev.current[e] for e in ev.current)


# ---------------------------------------------------------------------------
# 쏠림 — 균형인데도 사회적으로는 손해 (UE ≠ SO)
# ---------------------------------------------------------------------------


def test_equilibrium_leaves_captive_drivers_waiting():
    """배터리가 적어 A 밖에 못 가는 차(captive)와 A·B 둘 다 가는 차(flexible).

    A 는 200 kW (30분), B 는 150 kW (40분). flexible 이 먼저 와서 더 빠른 A 를 고른다 —
    자기에게는 최선이다. 1분 뒤 온 captive 는 A 에서 29분을 기다린다.
    flexible 이 B 로 가면 자기는 10분 손해지만 captive 가 29분을 번다 (SO).
    UE 는 이 10분 손해를 스스로 감수하지 않는다 → 쏠림.
    """
    flexible = _trip("f0", 0.0, [[("A", 50.0)], [("B", 50.0)]])
    captive = _trip("c0", 1.0, [[("A", 50.0)]])
    ch = {"A": _chargers(1, 200.0, "A"), "B": _chargers(1, 150.0, "B")}

    r = solve_ue([flexible, captive], ch, _settings(gap_tol=1e-9))
    ue_total = sum(r.dwell_by_ev.values())

    assert r.station_of([flexible, captive]) == {"f0": ("A",), "c0": ("A",)}
    assert r.final_gap == 0.0                                  # 정말 균형이다
    assert r.dwell_by_ev["c0"] == pytest.approx(29.0 + 30.0)   # captive 가 기다린다

    so = evaluate([captive, flexible], {"f0": 1, "c0": 0}, ch, ConstantSpeed(SPEED))
    so_total = sum(so.current.values())

    assert so_total == pytest.approx(40.0 + 30.0)
    assert ue_total - so_total == pytest.approx(19.0)          # UE 가 남긴 사회적 손실


# ---------------------------------------------------------------------------
# CTM 통행시간으로 바꿔 끼우기 (#56)
# ---------------------------------------------------------------------------


def test_congested_travel_time_pushes_arrivals_later():
    """막힌 구간을 지나오면 휴게소 도착이 늦어진다.

    고정 80 km/h 로는 이게 아예 나오지 않는다. "도로가 막혀서 도착이 밀리고
    그래서 몰린다" 가 설 연휴의 핵심이라, 이 한 칸이 UE 와 CTM 을 잇는 자리다.
    """
    import numpy as np

    from evdt.world.travel import CellSpeedField

    edges = np.arange(0.0, 401.0, 10.0)          # 10 km 셀 40개
    speeds = np.full((288, edges.size - 1), SPEED)
    speeds[:, 1:3] = SPEED / 4                    # 10~30 km 가 1/4 속도

    trips = [_trip(f"e{i:02d}", i * 2.0, [[("A", 50.0)]]) for i in range(5)]

    free = solve_ue(trips, {"A": _chargers(2)}, _settings())
    jammed = solve_ue(trips, {"A": _chargers(2)}, _settings(travel=CellSpeedField(edges, speeds)))

    first_free = min(v.t_arrive_min for v in free.visits)
    first_jammed = min(v.t_arrive_min for v in jammed.visits)

    # 20 km 를 1/4 속도(15 km/h)로 지나면 20분이 80분이 된다 → 60분 늦다
    assert first_jammed - first_free == pytest.approx(60.0, abs=1e-6)


def test_uniform_travel_time_reproduces_the_fixed_speed_run():
    """전 구간이 같은 속도인 격자는 고정 속도와 완전히 같은 결과를 낸다.

    CTM 을 켠 것 말고는 모두 같다는 비교가 성립하려면 이 자리가 새는 곳이 없어야 한다.
    """
    import numpy as np

    from evdt.world.travel import CellSpeedField

    edges = np.arange(0.0, 401.0, 10.0)
    field = CellSpeedField(edges, np.full((288, edges.size - 1), SPEED))

    trips = [_trip(f"e{i:02d}", i * 3.0, [[("A", 40.0)], [("B", 90.0)]]) for i in range(8)]
    chargers = {"A": _chargers(1), "B": _chargers(1)}

    fixed = solve_ue(trips, chargers, _settings())
    gridded = solve_ue(trips, chargers, _settings(travel=field))

    assert fixed.station_of(trips) == gridded.station_of(trips)
    assert [v.t_arrive_min for v in fixed.visits] == pytest.approx(
        [v.t_arrive_min for v in gridded.visits]
    )
