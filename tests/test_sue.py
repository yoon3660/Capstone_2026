"""SUE — 확률적 사용자 균형, MNL 모드 B (#173).

지키는 것
    - θ = ∞ 이면 UE 와 같다 (위임 경로 + 유한한 큰 θ 의 일반 경로 둘 다)
    - 같은 시드면 같은 결과 (ε 고정)
    - θ 를 줄이면 휴게소 분배가 퍼진다
    - 이탈 여부는 잡음 없는 비용으로 정한다 (한산하면 θ 와 무관하게 같은 차가 이탈)
"""

from __future__ import annotations

import math
import random

import pytest

from evdt.engine.sue import SueSettings, choose, solve_sue
from evdt.engine.ue import UESettings, solve_ue
from evdt.engine.ue_demand import Plan, PlannedStop, TripDemand
from evdt.world.sim import expand_chargers

FLAT = ((0.0, 1.0, 10_000.0),)
DSOC = 0.5
SPEED = 60.0   # 1 km = 1 분


def _chargers(n: int, kw: float = 200.0, sid: str = "A") -> tuple:
    return expand_chargers([{"charger_id": f"{sid}_dc", "power_kw": kw, "n_units": n}])


def _battery_for(minutes_at_200kw: float) -> float:
    return minutes_at_200kw * 200.0 / 60.0 / DSOC


def _trip(ev_id, entry, stops_by_plan, *, service_min=30.0, vmax=400.0, escape=False,
          escape_cost=0.0) -> TripDemand:
    plans = [Plan(tuple(PlannedStop(s, off, 0.25, 0.25 + DSOC) for s, off in stops))
             for stops in stops_by_plan]
    if escape:
        plans.append(Plan(()))
    return TripDemand(ev_id, "vc", entry, 0.0, 400.0, _battery_for(service_min), vmax, FLAT, 1.0,
                      tuple(plans), escape_cost_min=escape_cost)


def _congested(n=60, seed=3):
    """두 휴게소 + 두 곳 다 서는 계획 + 이탈. 줄이 생길 만큼 붐빈다."""
    rng = random.Random(seed)
    trips = [
        _trip(f"e{i:03d}", rng.uniform(0, 120),
              [[("A", 50.0)], [("B", 120.0)], [("A", 50.0), ("B", 120.0)]],
              service_min=rng.uniform(20, 40), escape=True, escape_cost=rng.uniform(60, 120))
        for i in range(n)
    ]
    # 두 휴게소의 출력을 다르게 둔다. 같으면 한산할 때 비용이 정확히 같아지고, 그 동률을
    # UE 는 "앞 계획" 으로, SUE 는 (θ 가 아무리 커도) ε 로 깬다 — 결과가 갈리는 건 그 몫뿐이다
    chargers = {"A": _chargers(3, kw=200.0, sid="A"), "B": _chargers(2, kw=260.0, sid="B")}
    return trips, chargers


def _settings(**kw) -> UESettings:
    kw.setdefault("max_iter", 200)
    kw.setdefault("gap_tol", 0.0)
    kw.setdefault("min_gain_min", 1e-6)
    return UESettings(speed_kmh=SPEED, **kw)


# ---------------------------------------------------------------------------
# θ = ∞ 이면 UE
# ---------------------------------------------------------------------------


def test_infinite_theta_is_ue():
    trips, ch = _congested()
    st = _settings(gap_tol=0.03, min_gain_min=1.0)
    ue = solve_ue(trips, ch, st)
    sue = solve_sue(trips, ch, st, SueSettings(theta=math.inf, seed=1))
    assert sue.choice == ue.choice
    assert sue.dwell_by_ev == ue.dwell_by_ev


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_huge_finite_theta_takes_general_path_and_matches_ue(seed):
    # 일반 경로(잡음을 더하는 반복)가 θ → ∞ 에서 UE 로 가는지 — 위임과 별개로 확인
    trips, ch = _congested(seed=seed)
    st = _settings(gap_tol=0.03, min_gain_min=1.0)
    ue = solve_ue(trips, ch, st)
    sue = solve_sue(trips, ch, st, SueSettings(theta=1e12, seed=seed))
    assert sue.choice == ue.choice
    assert [round(h.rel_gap, 12) for h in sue.history] == [round(h.rel_gap, 12) for h in ue.history]
    assert sue.dwell_by_ev == pytest.approx(ue.dwell_by_ev)


# ---------------------------------------------------------------------------
# 같은 시드면 같은 결과
# ---------------------------------------------------------------------------


def test_same_seed_same_result_different_seed_different_people():
    trips, ch = _congested()
    st = _settings(gap_tol=0.05, min_gain_min=1.0)
    a = solve_sue(trips, ch, st, SueSettings(theta=0.05, seed=7))
    b = solve_sue(list(reversed(trips)), ch, st, SueSettings(theta=0.05, seed=7))
    c = solve_sue(trips, ch, st, SueSettings(theta=0.05, seed=8))
    assert a.choice == b.choice and a.dwell_by_ev == b.dwell_by_ev
    assert a.choice != c.choice


# ---------------------------------------------------------------------------
# θ 를 줄이면 퍼진다
# ---------------------------------------------------------------------------


def _uncongested_two_power(n=200):
    # A 는 350 kW, B 는 100 kW. 충전기가 넉넉해 대기가 없다 → UE 는 전원 A
    trips = [_trip(f"e{i:03d}", i * 0.5, [[("A", 50.0)], [("B", 60.0)]], service_min=30.0)
             for i in range(n)]
    ch = {"A": _chargers(500, kw=350.0, sid="A"), "B": _chargers(500, kw=100.0, sid="B")}
    return trips, ch


def test_smaller_theta_spreads_choices():
    trips, ch = _uncongested_two_power()
    st = _settings(gap_tol=0.0, min_gain_min=1e-6)

    def share_b(theta):
        r = solve_sue(trips, ch, st, SueSettings(theta=theta, seed=1))
        return sum(1 for s in r.station_of(trips).values() if s == ("B",)) / len(trips)

    ue_b = share_b(math.inf)
    assert ue_b == 0.0
    shares = [share_b(t) for t in (1.0, 0.1, 0.02)]
    assert shares == sorted(shares), shares          # θ 가 작을수록 B 가 는다
    assert shares[-1] > 0.2


def test_logit_share_matches_formula_when_uncongested():
    # 대기가 없으면 각자의 선택이 서로 독립 → B 비중 ≈ 1 / (1 + exp(θ·ΔC))
    trips, ch = _uncongested_two_power(n=2000)
    st = _settings()
    theta = 0.05
    r = solve_sue(trips, ch, st, SueSettings(theta=theta, seed=4))
    # 충전시간: A 는 차의 vmax(400) 와 350 kW 중 작은 값, B 는 100 kW
    t_a = 30.0 * 200.0 / 350.0
    t_b = 30.0 * 200.0 / 100.0
    expected = 1.0 / (1.0 + math.exp(theta * (t_b - t_a)))
    got = sum(1 for s in r.station_of(trips).values() if s == ("B",)) / len(trips)
    assert got == pytest.approx(expected, abs=0.03)


# ---------------------------------------------------------------------------
# 이탈은 잡음 없이
# ---------------------------------------------------------------------------


def test_escape_set_does_not_depend_on_theta_when_uncongested():
    rng = random.Random(0)
    trips = [_trip(f"e{i:03d}", i * 1.0, [[("A", 50.0)], [("B", 60.0)]], service_min=30.0,
                   escape=True, escape_cost=rng.choice([5.0, 500.0])) for i in range(100)]
    ch = {"A": _chargers(500, kw=350.0, sid="A"), "B": _chargers(500, kw=100.0, sid="B")}
    st = _settings()

    def escaped(theta):
        r = solve_sue(trips, ch, st, SueSettings(theta=theta, seed=2))
        return {e for e, k in r.choice.items() if r and next(t for t in trips if t.ev_id == e).plans[k].is_escape}

    base = escaped(math.inf)
    assert base == {t.ev_id for t in trips if t.escape_cost_min == 5.0}
    for theta in (1.0, 0.05, 0.001):
        assert escaped(theta) == base


def test_choose_gain_uses_real_cost_when_escape_status_changes():
    t = _trip("x", 0.0, [[("A", 50.0)], [("B", 60.0)]], escape=True, escape_cost=50.0)
    sue = SueSettings(theta=0.05, seed=1)
    # 안쪽 최소 실제 비용 40 < 이탈 50 → 안으로. 지금 이탈(2)이면 이득은 실제 비용 차 10
    ch = choose(t, [40.0, 45.0, 50.0], 2, sue)
    assert ch.target in (0, 1) and ch.gain == pytest.approx(10.0)
    # 안쪽 최소 60 > 이탈 50 → 이탈. 지금 A(0)면 이득은 60 − 50
    ch = choose(t, [60.0, 70.0, 50.0], 0, sue)
    assert ch.target == 2 and ch.gain == pytest.approx(10.0)


def test_theta_must_be_positive():
    with pytest.raises(ValueError):
        SueSettings(theta=0.0)
