"""인내 분포 (#146) — 콜센터 문헌에서 가져온 것.

검사할 것은 셋이다.

1. **모든 가족이 같은 중앙값을 낸다** — 아니면 가족을 바꿀 때 모양과 수준이 섞인다
2. **점질량이면 예전과 비트 단위로 같다** — 저장된 run 이 전부 무효가 되면 안 된다
3. **옛 손잡이(`escape_cost_sigma`)가 그대로 돈다** — #78 의 결과를 지킨다
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from evdt.engine.patience import PATIENCE_KINDS, POINT, Patience

N = 200_000
SEED = 7

#: (kind, shape)
FAMILIES = [("lognormal", 0.8), ("weibull", 0.7), ("weibull", 2.0),
            ("exponential", 0.0), ("uniform", 0.6)]


def _draw(kind, shape, median=120.0, n=N):
    return Patience(median_min=median, kind=kind, shape=shape).draw(
        n, np.random.default_rng(SEED))


# ---------------------------------------------------------------------------
# 중앙값으로 맞춘다 — 가족을 바꿔도 수준이 안 움직인다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,shape", FAMILIES)
def test_every_family_has_the_same_median(kind, shape) -> None:
    """**이것이 이 모듈의 존재 이유다.** 수준이 같아야 모양만 비교된다."""
    got = float(np.median(_draw(kind, shape)))

    assert got == pytest.approx(120.0, rel=0.02), f"{kind}({shape}) 중앙값 {got:.1f}"


@pytest.mark.parametrize("kind,shape", FAMILIES)
def test_theory_mean_matches_the_draw(kind, shape) -> None:
    """`mean_min()` 은 총 사회적 비용을 어림할 때 쓴다 — 틀리면 조용히 틀린다."""
    spec = Patience(median_min=120.0, kind=kind, shape=shape)
    got = float(_draw(kind, shape).mean())

    assert got == pytest.approx(spec.mean_min(), rel=0.02)


def test_the_mean_is_not_the_median() -> None:
    """오른쪽 꼬리가 있는 가족은 평균 > 중앙값이다.

    문서에 적힌 '120분' 은 **중앙값**이다. 평균으로 읽으면 총 사회적 비용을
    과소평가한다.
    """
    spec = Patience(median_min=120.0, kind="lognormal", shape=0.8)

    assert spec.mean_min() > 120.0
    assert spec.mean_min() == pytest.approx(120.0 * math.exp(0.8 ** 2 / 2))


def test_weibull_k1_is_exponential() -> None:
    a = _draw("weibull", 1.0)
    b = _draw("exponential", 0.0)

    assert np.allclose(a, b)


# ---------------------------------------------------------------------------
# 모양이 실제로 다르다
# ---------------------------------------------------------------------------


def test_decreasing_hazard_has_a_fatter_tail_than_increasing() -> None:
    """k<1 (매몰비용) 은 k>1 (조급함) 보다 **훨씬 오래 기다리는 사람**이 있다."""
    sunk = _draw("weibull", 0.7)
    impatient = _draw("weibull", 2.0)

    assert np.percentile(sunk, 95) > np.percentile(impatient, 95) * 2
    assert np.percentile(sunk, 5) < np.percentile(impatient, 5)


def test_uniform_is_bounded() -> None:
    """상한이 있다 — 행동으로는 어색하다는 것이 이 가족의 한계다."""
    d = _draw("uniform", 0.6)

    assert d.min() >= 120.0 * 0.4 - 1e-9
    assert d.max() <= 120.0 * 1.6 + 1e-9


@pytest.mark.parametrize("kind,shape", FAMILIES)
def test_patience_is_never_negative(kind, shape) -> None:
    assert _draw(kind, shape).min() >= 0.0


def test_hazard_shape_says_what_it_means() -> None:
    assert "매몰비용" in Patience(120.0, "weibull", 0.7).hazard_shape()
    assert "잘 터진다" in Patience(120.0, "weibull", 2.0).hazard_shape()
    assert "무기억" in Patience(120.0, "exponential").hazard_shape()


# ---------------------------------------------------------------------------
# 꺼짐이 진짜 꺼짐인가
# ---------------------------------------------------------------------------


def test_point_gives_everyone_the_same_value() -> None:
    d = Patience(median_min=120.0).draw(1000, np.random.default_rng(0))

    assert np.all(d == 120.0)
    assert not POINT.spread


def test_zero_median_never_spreads() -> None:
    """이탈 비용 0 = 이탈 선택지 없음. 거기에 분포를 걸면 안 된다."""
    spec = Patience(median_min=0.0, kind="weibull", shape=2.0)

    assert not spec.spread
    assert np.all(spec.draw(100, np.random.default_rng(0)) == 0.0)


def test_zero_shape_is_a_point_mass_for_shape_families() -> None:
    for kind in ("lognormal", "weibull", "uniform"):
        spec = Patience(median_min=120.0, kind=kind, shape=0.0)
        assert not spec.spread, kind


def test_the_draw_is_reproducible() -> None:
    a = Patience(120.0, "weibull", 1.5).draw(500, np.random.default_rng(3))
    b = Patience(120.0, "weibull", 1.5).draw(500, np.random.default_rng(3))

    assert np.allclose(a, b)


# ---------------------------------------------------------------------------
# 거부
# ---------------------------------------------------------------------------


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="모르는 인내 분포"):
        Patience(120.0, "gamma", 1.0)


def test_weibull_without_a_shape_falls_back_to_a_point_mass() -> None:
    """자료구조는 **일관되게** 떨어진다 — lognormal σ=0 과 같은 규칙이다."""
    assert not Patience(120.0, "weibull", 0.0).spread


def test_config_refuses_weibull_without_a_shape() -> None:
    """config 는 **시끄럽게 거부한다.** "와이블로 돌려 줘" 라고 적고 shape 를 안 준
    것은 오타이지 점질량을 원한 것이 아니다. 자료구조(관대)와 config(엄격)의
    역할이 다르다.
    """
    import yaml

    from evdt.config import ConfigError, ScenarioConfig
    from evdt.paths import CONFIG_DIR

    raw = yaml.safe_load(
        (CONFIG_DIR / "scenario_seollal_down.yaml").read_text(encoding="utf-8"))
    raw["demand"] = {**raw["demand"], "patience_kind": "weibull", "patience_shape": 0.0}

    with pytest.raises(ConfigError, match="shape"):
        ScenarioConfig.from_dict(raw, source="test")


def test_negative_shape_raises() -> None:
    with pytest.raises(ValueError):
        Patience(120.0, "weibull", -1.0)


def test_uniform_half_width_must_be_in_range() -> None:
    with pytest.raises(ValueError, match="반폭"):
        Patience(120.0, "uniform", 1.5)


def test_every_listed_kind_can_be_built() -> None:
    for kind in PATIENCE_KINDS:
        shape = 0.5 if kind in ("lognormal", "weibull", "uniform") else 0.0
        Patience(120.0, kind, shape).draw(10, np.random.default_rng(0))


# ---------------------------------------------------------------------------
# 옛 손잡이를 지킨다 — 저장된 run 이 전부 무효가 되면 안 된다
# ---------------------------------------------------------------------------


def test_old_sigma_knob_still_means_lognormal() -> None:
    from evdt.engine.ue_demand import ChargeRule

    rule = ChargeRule(range_factor=1.0, buffer_km=30.0, reserve_soc=0.2,
                      target_soc_cap=0.8, max_stops=2,
                      escape_cost_min=120.0, escape_cost_sigma=0.8)
    spec = rule.patience_spec()

    assert spec.kind == "lognormal"
    assert spec.shape == 0.8
    assert spec.median_min == 120.0


def test_no_knobs_is_a_point_mass() -> None:
    from evdt.engine.ue_demand import ChargeRule

    rule = ChargeRule(range_factor=1.0, buffer_km=30.0, reserve_soc=0.2,
                      target_soc_cap=0.8, max_stops=2, escape_cost_min=120.0)

    assert not rule.patience_spec().spread


def test_patience_wins_over_the_old_knob() -> None:
    """둘 다 주면 새 쪽이 이긴다. 조용히 섞이면 어느 쪽이 돌았는지 모른다."""
    from evdt.engine.ue_demand import ChargeRule

    rule = ChargeRule(range_factor=1.0, buffer_km=30.0, reserve_soc=0.2,
                      target_soc_cap=0.8, max_stops=2,
                      escape_cost_min=90.0, escape_cost_sigma=0.8,
                      patience=Patience(median_min=999.0, kind="weibull", shape=2.0))
    spec = rule.patience_spec()

    assert spec.kind == "weibull"
    # 중앙값은 **escape_cost_min 이 이긴다** — 두 곳에 적힌 값이 갈라지면 안 된다
    assert spec.median_min == 90.0


def test_the_run_records_which_patience_it_used() -> None:
    """기록에 없으면 "어느 인내로 돌았나" 를 나중에 알 수 없다 (#101 과 같은 고장)."""
    from evdt.runner import load_config, run_params

    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")

    assert "patience" in run_params(cfg)
