"""인내와 우회 비용을 가른다 (#147 후속).

`escape_cost_min` 하나가 두 일을 했다.

    ① 인내      얼마나 기다릴 수 있나        — **선호**. 사람마다 다르다
    ② 우회 비용  IC 로 빠지면 몇 분 걸리나    — **지리**. 위치가 정한다

고정 120분일 때는 두 해석이 우연히 맞았다 ("IC 로 빠져 시내에서 충전하고 오면
120분이니, 줄이 그보다 비싸면 나간다"). 분포로 바꾸면 깨진다 —
**인내 9분인 차 = 우회도 9분인 차** 가 되어 조급한 차에게 공짜 탈출구가 열린다.
weibull:0.7 에서 S0 가 수요의 18.9% 를 잃었고, 총비용이 51% 떨어진 것은 엔진이
좋아진 게 아니라 **문제가 작아진 것**이었다.

이 파일이 못 박는 것은 셋이다.

    1. 안 가른 config 는 **비트 단위로 그대로** 돈다
    2. 가른 뒤에는 인내 가족이 **발길질을 건드리지 않는다**  ← 핵심
    3. 추출이 **독립 스트림**에서 나온다 (같은 스트림이면 난수열이 밀려
       "차량 집합이 바뀐" 것이 "인내를 바꾼" 것으로 보고된다)
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from evdt.engine.patience import Patience
from evdt.engine.ue_demand import ChargeRule

#: `ChargeRule` 의 필수 칸. 이 테스트가 보는 것과 무관하므로 설 config 의 값을 쓴다
REQUIRED = {
    "range_factor": 1.0, "buffer_km": 20.0, "reserve_soc": 0.1,
    "target_soc_cap": 0.8, "max_stops": 2,
}


def rule(**kw) -> ChargeRule:
    return ChargeRule(**{**REQUIRED, "escape_cost_min": 120.0, **kw})


# ---------------------------------------------------------------------------
# 두 분포가 갈라지는 자리
# ---------------------------------------------------------------------------
def test_without_a_family_the_two_specs_are_the_same() -> None:
    """기본값에서는 둘이 같은 분포다 — 그래서 추출 하나를 같이 쓴다."""

    r = rule()

    assert r.patience_spec() == r.escape_cost_spec()
    assert not r.splits_patience()


def test_a_family_splits_them() -> None:
    r = rule(patience=Patience(median_min=120.0, kind="weibull", shape=0.7))

    assert r.patience_spec() != r.escape_cost_spec()
    assert r.splits_patience()
    assert r.patience_spec().kind == "weibull"
    assert r.escape_cost_spec().kind == "point", (
        "우회 비용이 인내 가족을 따라가면 가른 뜻이 없다")


def test_a_separate_median_splits_them() -> None:
    r = rule(patience_median_min=30.0)

    assert r.splits_patience()
    assert r.patience_spec().median_min == 30.0
    assert r.escape_cost_spec().median_min == 120.0, (
        "우회 비용의 중앙값은 escape_cost_min 이다")


def test_the_patience_median_falls_back_to_the_escape_cost() -> None:
    """안 주면 지금까지와 같다 — 한 값이다."""

    assert rule().patience_median == 120.0
    assert rule(patience_median_min=45.0).patience_median == 45.0


def test_the_escape_cost_spread_is_not_the_patience_family() -> None:
    """★ `escape_cost_sigma` 는 **UE 수렴**에 필요한 산포다.

    0 이면 "나갈까 말까" 경계에 질량이 몰려 집단이 뒤집히며 진동한다
    (escape_cost_min 60 에서 20시드 중 10개가 수렴 실패). 그래서 인내 가족과
    **따로** 살아 있어야 한다.

    전에는 `runner` 가 늘 `patience` 객체를 넘기는 바람에 `patience_spec()` 의
    sigma 분기가 죽어 있었고, `escape_cost_sigma` 가 조용히 무시됐다.
    """

    r = rule(escape_cost_sigma=0.8,
             patience=Patience(median_min=120.0, kind="weibull", shape=0.7))

    assert r.escape_cost_spec().kind == "lognormal"
    assert r.escape_cost_spec().shape == 0.8
    assert r.patience_spec().kind == "weibull"


# ---------------------------------------------------------------------------
# ★ 핵심 — 인내는 발길질을 건드리지 않는다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,shape", [
    ("weibull", 0.7), ("weibull", 2.0), ("exponential", 0.0), ("uniform", 0.75),
])
def test_the_patience_family_does_not_move_the_escape_costs(kind, shape) -> None:
    """가족을 바꿔도 **우회 비용 추출이 그대로다.**

    이게 이 분리의 전부다. 여기가 깨지면 "엔진이 좋아졌다" 와 "문제가 작아졌다"
    를 가를 수 없다.
    """
    n = 2_000
    base = rule(escape_cost_sigma=0.5)
    split = dataclasses.replace(
        base, patience=Patience(median_min=120.0, kind=kind, shape=shape))

    a = base.escape_cost_spec().draw(n, np.random.default_rng(7))
    b = split.escape_cost_spec().draw(n, np.random.default_rng(7))

    assert np.array_equal(a, b), f"{kind}:{shape} 가 우회 비용을 움직였다"


def test_the_patience_draw_does_not_advance_the_main_stream() -> None:
    """★ 인내는 **독립 스트림**에서 뽑는다 (`gen.spawn`).

    같은 generator 에서 한 번 더 뽑으면 난수열이 밀려 뒤에 오는 habit_soc·
    기회충전 추첨이 전부 달라진다. 그러면 "인내 가족을 바꿨다" 가 아니라
    "차량 집합을 바꿨다" 가 되고, 위 주장을 확인할 수 없다.

    실제로 그렇게 재 봤더니 발길질이 240 → 256 으로 움직였다 — 탈출구 때문이
    아니라 난수열이 밀려서였다. 독립 스트림으로 바꾸니 240 → 240 이 됐다.
    """

    gen = np.random.default_rng(11)
    first = gen.random(5)

    child = gen.spawn(1)[0]
    child.random(1_000)                      # 자식에서 많이 뽑아도

    after = gen.random(5)                    # 부모는 안 밀린다
    expect = np.random.default_rng(11)
    expect.random(5)

    assert np.array_equal(after, expect.random(5))
    assert not np.array_equal(first, after)  # 같은 값이 나오는 게 아니다


def test_the_draw_site_uses_spawn() -> None:
    """소스에 `spawn` 이 남아 있어야 한다 — 지우면 조용히 난수열이 밀린다."""
    import inspect

    from evdt.engine import ue_demand

    src = inspect.getsource(ue_demand.build_trip_demands)

    assert "spawn" in src, "인내를 같은 스트림에서 뽑고 있다 (#147 후속)"
    assert src.index("escape_cost_spec") < src.index("patience_spec"), (
        "우회 비용을 **먼저** 뽑아야 안 가른 config 가 비트 단위로 같다")


# ---------------------------------------------------------------------------
# 지리와 선호를 섞어 쓰지 않는가
# ---------------------------------------------------------------------------
def test_the_trip_carries_both_values() -> None:
    from evdt.engine.ue_demand import TripDemand

    assert "patience_min" in {f.name for f in dataclasses.fields(TripDemand)}
    assert "escape_cost_min" in {f.name for f in dataclasses.fields(TripDemand)}


def test_the_simulated_car_carries_both_values() -> None:
    from evdt.world.corridor_sim import SimEV

    names = {f.name for f in dataclasses.fields(SimEV)}

    assert {"patience_min", "escape_cost_min"} <= names


def test_renege_reads_patience_and_balking_reads_the_escape_cost() -> None:
    """어느 값을 어디서 보는지 못 박는다 — 섞이면 분리가 무의미하다."""
    import inspect

    from evdt.world import corridor_sim

    src = inspect.getsource(corridor_sim.run_corridor)

    assert "ev.patience_min or ev.escape_cost_min" in src, (
        "포기 판단이 patience_min 을 안 본다")
    assert "escape_cost_min=c.ev.escape_cost_min" in src, (
        "발길질 판단(EVState)이 escape_cost_min 을 안 본다")


def test_the_run_params_record_the_patience_median_separately() -> None:
    """갈라진 run 과 안 갈라진 run 이 `run_params` 에서 구분돼야 한다.

    전에는 `patience` 칸에 `escape_cost_min` 이 중앙값으로 들어가 있어서,
    인내 중앙값을 따로 준 run 과 안 준 run 이 같은 기록을 남겼다.
    """
    import inspect

    from evdt import runner

    src = inspect.getsource(runner)

    assert '"patience_median_min"' in src, (
        "run_params 가 인내 중앙값을 따로 안 적는다 — stale_reason 이 못 막는다")
