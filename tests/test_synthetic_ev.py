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