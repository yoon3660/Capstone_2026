from __future__ import annotations

import hashlib
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from evdt.config import ScenarioConfig
from evdt.io.synthetic_ev import (
    generate_evs,
    hourly_ev_counts,
)
from evdt.paths import CONFIG_DIR

DEST_OFFSET_KM = 415.0


def _config() -> ScenarioConfig:
    """테스트용 설 하행 시나리오 config."""
    return ScenarioConfig.from_yaml(
        CONFIG_DIR / "scenario_seollal_down.yaml"
    )


def _traffic() -> pd.DataFrame:
    """테스트용 3시간 교통량."""
    return pd.DataFrame(
        {
            "hour": [0, 1, 2],
            "volume_veh": [1000.0, 2000.0, 3000.0],
        }
    )


def _with_method(
    cfg: ScenarioConfig,
    method: str,
    *,
    seed: int | None = None,
) -> ScenarioConfig:
    """EV 대수 생성 방식과 seed만 바꾼 테스트 config."""

    vehicles = cfg.vehicles

    if seed is not None:
        vehicles = replace(
            cfg.vehicles,
            seed=seed,
        )

    return replace(
        cfg,
        demand=replace(
            cfg.demand,
            ev_count_method=method,
        ),
        vehicles=vehicles,
    )


def _generate(
    cfg: ScenarioConfig | None = None,
    traffic: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """반복되는 generate_evs 호출을 한 곳에 모은다."""
    return generate_evs(
        traffic if traffic is not None else _traffic(),
        cfg if cfg is not None else _config(),
        dest_offset_km=DEST_OFFSET_KM,
    )


def _df_hash(df: pd.DataFrame) -> str:
    """DataFrame 내용을 SHA-256 해시로 만든다."""
    values = pd.util.hash_pandas_object(
        df,
        index=True,
    ).values.tobytes()

    return hashlib.sha256(values).hexdigest()


def _sample_first_hour_counts(
    method: str,
    n_seeds: int = 1000,
) -> np.ndarray:
    """
    첫 시간대의 EV 대수를 여러 seed에서 반복 생성한다.

    UE 전체를 반복 실행하는 것이 아니라
    시간대별 대수 생성 함수만 호출한다.
    """
    base_cfg = _config()

    traffic = (
        _traffic()
        .iloc[[0]]
        .reset_index(drop=True)
    )

    samples: list[int] = []

    for seed in range(n_seeds):
        cfg = _with_method(
            base_cfg,
            method,
            seed=seed,
        )

        counts = hourly_ev_counts(
            traffic,
            cfg,
        )

        samples.append(
            int(counts.loc[0, "n_ev"])
        )

    return np.asarray(samples)


@pytest.mark.parametrize(
    "method",
    [
        "fixed",
        "poisson",
        "binomial",
        "beta_binomial",
        "poisson_lognormal",
    ],
)
def test_same_seed_produces_identical_evs(
    method: str,
) -> None:
    """같은 method와 seed면 완전히 같은 차량 집합이 생성된다."""
    cfg = _with_method(
        _config(),
        method,
    )

    first = _generate(cfg=cfg)
    second = _generate(cfg=cfg)

    assert_frame_equal(first, second)
    assert _df_hash(first) == _df_hash(second)


def test_fixed_hourly_ev_count_matches_expected() -> None:
    """Fixed 방식은 기대 EV 대수를 반올림한 값과 정확히 일치한다."""
    cfg = _with_method(
        _config(),
        "fixed",
    )

    traffic = _traffic()

    counts = hourly_ev_counts(
        traffic,
        cfg,
    )

    expected = np.rint(
        traffic["volume_veh"].to_numpy()
        * cfg.demand.demand_multiplier
        * cfg.demand.ev_share
    ).astype(int)

    np.testing.assert_array_equal(
        counts["n_ev"].to_numpy(),
        expected,
    )


def test_poisson_mean_and_variance_match_theory() -> None:
    """Poisson 방식은 여러 seed에서 평균과 분산이 λ에 가까워야 한다."""
    cfg = _config()
    traffic = _traffic()

    samples = _sample_first_hour_counts(
        "poisson"
    )

    lambda_ = (
        traffic.loc[0, "volume_veh"]
        * cfg.demand.demand_multiplier
        * cfg.demand.ev_share
    )

    sample_mean = float(np.mean(samples))
    sample_variance = float(
        np.var(samples, ddof=1)
    )

    assert np.isclose(
        sample_mean,
        lambda_,
        rtol=0.05,
    )

    assert np.isclose(
        sample_variance,
        lambda_,
        rtol=0.15,
    )


def test_binomial_mean_and_variance_match_theory() -> None:
    """Binomial 방식은 여러 seed에서 평균 np, 분산 np(1-p)에 가까워야 한다."""
    cfg = _config()
    traffic = _traffic()

    samples = _sample_first_hour_counts(
        "binomial"
    )

    n = int(
        np.rint(
            traffic.loc[0, "volume_veh"]
            * cfg.demand.demand_multiplier
        )
    )

    p = cfg.demand.ev_share

    expected_mean = n * p
    expected_variance = n * p * (1.0 - p)

    sample_mean = float(np.mean(samples))
    sample_variance = float(
        np.var(samples, ddof=1)
    )

    assert np.isclose(
        sample_mean,
        expected_mean,
        rtol=0.05,
    )

    assert np.isclose(
        sample_variance,
        expected_variance,
        rtol=0.15,
    )

def test_beta_binomial_mean_and_variance_match_theory() -> None:
    """
    Beta-Binomial 방식은
    평균 np,
    분산 np(1-p) * (n + concentration) / (concentration + 1)
    에 가까워야 한다.
    """
    cfg = _config()
    traffic = _traffic()

    samples = _sample_first_hour_counts(
        "beta_binomial"
    )

    n = int(
        np.rint(
            traffic.loc[0, "volume_veh"]
            * cfg.demand.demand_multiplier
        )
    )

    p = cfg.demand.ev_share
    concentration = (
        cfg.demand.beta_binomial_concentration
    )

    expected_mean = n * p

    expected_variance = (
        n
        * p
        * (1.0 - p)
        * (n + concentration)
        / (concentration + 1.0)
    )

    sample_mean = float(
        np.mean(samples)
    )

    sample_variance = float(
        np.var(samples, ddof=1)
    )

    assert np.isclose(
        sample_mean,
        expected_mean,
        rtol=0.05,
    )

    assert np.isclose(
        sample_variance,
        expected_variance,
        rtol=0.15,
    )


def test_poisson_lognormal_mean_and_variance_match_theory() -> None:
    """
    Poisson-Lognormal 방식은 평균 mu를 유지하면서
    일반 Poisson보다 큰 분산을 가져야 한다.

    Var(N) =
        mu + mu^2 * (exp(sigma^2) - 1)
    """
    cfg = _config()
    traffic = _traffic()

    samples = _sample_first_hour_counts(
        "poisson_lognormal"
    )

    mu = (
        traffic.loc[0, "volume_veh"]
        * cfg.demand.demand_multiplier
        * cfg.demand.ev_share
    )

    sigma = (
        cfg.demand.poisson_lognormal_sigma
    )

    expected_mean = mu

    expected_variance = (
        mu
        + mu**2
        * (np.exp(sigma**2) - 1.0)
    )

    sample_mean = float(
        np.mean(samples)
    )

    sample_variance = float(
        np.var(samples, ddof=1)
    )

    assert np.isclose(
        sample_mean,
        expected_mean,
        rtol=0.05,
    )

    assert np.isclose(
        sample_variance,
        expected_variance,
        rtol=0.15,
    )


def test_initial_soc_is_inside_config_range() -> None:
    """생성된 모든 초기 SoC가 config의 [lo, hi] 범위 안에 있다."""
    cfg = _config()
    evs = _generate(cfg=cfg)

    assert not evs.empty

    assert evs["initial_soc"].between(
        cfg.vehicles.soc_beta.lo,
        cfg.vehicles.soc_beta.hi,
        inclusive="both",
    ).all()


@pytest.mark.parametrize(
    "method",
    [
        "fixed",
        "poisson",
        "binomial",
        "beta_binomial",
        "poisson_lognormal",
    ],
)
def test_zero_ev_share_generates_zero_evs(
    method: str,
) -> None:
    """EV 비율이 0이면 어떤 생성 방식에서도 차량이 생성되지 않는다."""
    cfg = _config()

    zero_cfg = replace(
        cfg,
        demand=replace(
            cfg.demand,
            ev_share=0.0,
            ev_count_method=method,
        ),
    )

    evs = _generate(
        cfg=zero_cfg,
    )

    assert evs.empty

    assert evs.columns.tolist() == [
        "ev_id",
        "vclass_id",
        "entry_time_min",
        "initial_soc",
        "entry_offset_km",
        "dest_offset_km",
    ]


def test_destination_offset_is_assigned() -> None:
    """생성된 모든 차량에 전달한 목적지 offset이 기록된다."""
    evs = _generate()

    assert not evs.empty
    assert (
        evs["dest_offset_km"]
        == DEST_OFFSET_KM
    ).all()

# ---------------------------------------------------------------------------
# 진입 SoC 분포 (#55)
#
# 진입 SoC 는 **아무도 관측하지 않는 양**이라 가정할 수밖에 없다. 그래서 여기서 지키는
# 것은 "값이 맞나" 가 아니라 **분포가 선언한 대로 나오나** 와 **가드레일이 조용히
# 분포를 대체하지 않나** 두 가지다.
# ---------------------------------------------------------------------------
import math  # noqa: E402

from evdt.config import SocBeta, SocLogNormal  # noqa: E402
from evdt.io.synthetic_ev import sample_initial_soc  # noqa: E402

HOLIDAY = SocLogNormal(mu=math.log(0.70), sigma=0.35, lo=0.33, hi=1.00)


def test_lognormal_soc_stays_inside_the_truncation_window() -> None:
    """절삭 구간 밖은 한 대도 나오지 않는다 — 바닥이 곧 하한이다."""
    s, _ = sample_initial_soc(HOLIDAY, np.random.default_rng(7), 50_000)

    assert s.min() >= HOLIDAY.lo
    assert s.max() <= HOLIDAY.hi


def test_lognormal_analytic_mean_matches_the_samples() -> None:
    """절삭 로그정규 평균의 닫힌 형태가 맞다.

    `departure_soc_mean` KPI 가 이 값을 쓴다. 틀리면 기준선이 조용히 어긋난다.
    """
    s, _ = sample_initial_soc(HOLIDAY, np.random.default_rng(11), 200_000)

    assert HOLIDAY.mean() == pytest.approx(float(s.mean()), abs=0.003)


def test_the_floor_is_a_tripwire_not_the_distribution() -> None:
    """바닥에 걸리는 차가 꼬리 수준이어야 한다.

    **이것이 이 티켓의 핵심 안전장치다.** 바닥이 많은 차를 건드리면 "로그정규를 썼다"
    는 말이 의미를 잃는다 — 분포가 아니라 바닥이 결과를 정하게 된다. 그때는 바닥이
    아니라 **mu 를 다시 봐야 한다**.
    """
    _, floored = sample_initial_soc(HOLIDAY, np.random.default_rng(13), 100_000)

    assert floored / 100_000 < 0.05


def test_the_low_band_stays_a_tail_not_a_pile() -> None:
    """바닥 바로 위 구간(33~40%)이 소수여야 한다.

    바닥을 40% 에서 33% 로 내리면서 생긴 조건이다. 그 구간에 차가 몰려 있으면 "바닥을
    내려 너그럽게 잡았다" 가 아니라 **분포가 바닥에 눌려 있다**는 뜻이고, 클리핑을
    피하려고 절삭을 쓴 의미가 없어진다.

    말이 안 되는 선은 40% 다 (그 정도면 분포가 아니라 바닥이 결과를 정한다). 지금
    설계값은 4.6% 라, 15% 를 경계로 두면 실질적인 제약이면서 여유가 3배 남는다.
    """
    s, _ = sample_initial_soc(HOLIDAY, np.random.default_rng(23), 200_000)
    band = float(((s >= HOLIDAY.lo) & (s < 0.40)).mean())

    assert band < 0.15


def test_the_tail_reaches_lower_than_the_centre_moves() -> None:
    """바닥을 내린 효과는 **중심보다 꼬리에 크게** 나타나야 한다.

    바닥 40%·sigma 0.25 → 바닥 33%·sigma 0.35 로 바꾸면서 중앙값은 68.4% → 65.8%
    (−2.6%p) 인데 p5 는 47.3% → 40.3% (−7.0%p) 다. 꼬리가 중심보다 2배 넘게 움직였다.

    ⚠ `mu` 는 **절삭 전** 중앙값(70%)이다. 상단 1.0 절삭이 sigma 와 함께 커지므로
    실제 중앙값은 그보다 낮게 나온다 — `mu` 를 중앙값으로 읽으면 안 된다.
    """
    s, _ = sample_initial_soc(HOLIDAY, np.random.default_rng(29), 200_000)
    median, p5 = float(np.median(s)), float(np.percentile(s, 5))

    assert median == pytest.approx(0.658, abs=0.01)
    assert p5 == pytest.approx(0.403, abs=0.01)
    assert (0.684 - median) < (0.473 - p5)      # 중심보다 꼬리가 더 움직였다


def test_beta_profile_reports_no_floored_cars() -> None:
    """Beta 는 lo 에서 시작하는 분포라 절삭이 없다. 가드레일 수는 0 이어야 한다."""
    _, floored = sample_initial_soc(SocBeta(2.0, 5.0, 0.10, 0.95),
                                    np.random.default_rng(17), 10_000)

    assert floored == 0


def test_same_seed_gives_the_same_soc() -> None:
    """같은 시드 → 같은 SoC. 역변환이라 뽑는 난수 개수도 n 으로 고정된다."""
    a, _ = sample_initial_soc(HOLIDAY, np.random.default_rng(3), 1_000)
    b, _ = sample_initial_soc(HOLIDAY, np.random.default_rng(3), 1_000)

    assert np.array_equal(a, b)


def test_lognormal_profile_loads_from_the_scenario_file() -> None:
    """config 에 선언한 holiday 프로파일이 로그정규로 읽힌다 (양방향)."""
    for name in ("scenario_seollal_down.yaml", "scenario_seollal_up.yaml"):
        cfg = ScenarioConfig.from_yaml(CONFIG_DIR / name)
        holiday = next(p.beta for p in cfg.vehicles.soc_profiles if p.name == "holiday")

        assert isinstance(holiday, SocLogNormal)
        assert holiday.lo == 0.33
