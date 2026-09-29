from __future__ import annotations

import numpy as np
import pandas as pd

from evdt.config import ScenarioConfig
from evdt.io.vehicles import read_vehicle_classes


def sample_fixed_counts(
    expected_ev: np.ndarray,
) -> np.ndarray:
    """Expected EV counts are rounded deterministically."""
    return np.rint(expected_ev).astype(int)


def sample_poisson_counts(
    expected_ev: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample hourly EV counts from a Poisson distribution."""
    return rng.poisson(expected_ev).astype(int)


def sample_binomial_counts(
    traffic_volume: np.ndarray,
    demand_multiplier: float,
    ev_share: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Treat each vehicle as an EV with probability ev_share."""
    n_vehicles = np.rint(
        traffic_volume * demand_multiplier
    ).astype(int)

    return rng.binomial(
        n=n_vehicles,
        p=ev_share,
    ).astype(int)

def sample_beta_binomial_counts(
    traffic_volume: np.ndarray,
    demand_multiplier: float,
    ev_share: float,
    concentration: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    시간대별 EV 비율 자체가 변한다고 가정한다.

    p_h ~ Beta(alpha, beta)
    N_h ~ Binomial(n_h, p_h)

    alpha = ev_share * concentration
    beta = (1 - ev_share) * concentration

    concentration이 클수록 p_h가 ev_share 근처에 모인다.
    """

    if concentration <= 0:
        raise ValueError(
            "beta_binomial_concentration은 0보다 커야 합니다"
        )

    n_vehicles = np.rint(
        traffic_volume * demand_multiplier
    ).astype(int)

    if ev_share == 0:
        return np.zeros_like(n_vehicles)

    if ev_share == 1:
        return n_vehicles

    alpha = ev_share * concentration
    beta = (1.0 - ev_share) * concentration

    sampled_p = rng.beta(
        alpha,
        beta,
        size=len(n_vehicles),
    )

    return rng.binomial(
        n=n_vehicles,
        p=sampled_p,
    ).astype(int)


def sample_poisson_lognormal_counts(
    expected_ev: np.ndarray,
    sigma: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    Poisson의 도착 강도 lambda 자체가 변한다고 가정한다.

    factor ~ LogNormal(-sigma^2 / 2, sigma^2)
    lambda_h' = lambda_h * factor

    E[factor] = 1 이므로 장기 평균은 기존 expected_ev와 같다.
    """

    if sigma < 0:
        raise ValueError(
            "poisson_lognormal_sigma는 0 이상이어야 합니다"
        )

    if sigma == 0:
        return rng.poisson(
            expected_ev
        ).astype(int)

    factor = np.exp(
        rng.normal(
            loc=-0.5 * sigma**2,
            scale=sigma,
            size=len(expected_ev),
        )
    )

    sampled_lambda = expected_ev * factor

    return rng.poisson(
        sampled_lambda
    ).astype(int)

def sample_ev_counts(
    traffic_volume: np.ndarray,
    cfg: ScenarioConfig,
    rng: np.random.Generator,
) -> np.ndarray:
    """설정된 방법에 따라 시간대별 EV 대수를 생성한다."""

    expected_ev = (
        traffic_volume
        * cfg.demand.demand_multiplier
        * cfg.demand.ev_share
    )

    method = cfg.demand.ev_count_method

    if method == "fixed":
        return sample_fixed_counts(
            expected_ev
        )

    if method == "poisson":
        return sample_poisson_counts(
            expected_ev,
            rng,
        )

    if method == "binomial":
        return sample_binomial_counts(
            traffic_volume=traffic_volume,
            demand_multiplier=cfg.demand.demand_multiplier,
            ev_share=cfg.demand.ev_share,
            rng=rng,
        )

    if method == "beta_binomial":
        return sample_beta_binomial_counts(
            traffic_volume=traffic_volume,
            demand_multiplier=cfg.demand.demand_multiplier,
            ev_share=cfg.demand.ev_share,
            concentration=cfg.demand.beta_binomial_concentration,
            rng=rng,
        )

    if method == "poisson_lognormal":
        return sample_poisson_lognormal_counts(
            expected_ev=expected_ev,
            sigma=cfg.demand.poisson_lognormal_sigma,
            rng=rng,
        )

    raise ValueError(
        f"지원하지 않는 ev_count_method 입니다: {method}"
    )


def hourly_ev_counts(
    traffic: pd.DataFrame,
    cfg: ScenarioConfig,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Calculate hourly expected and sampled EV counts."""

    df = traffic.copy()

    df["expected_ev"] = (
        df["volume_veh"]
        * cfg.demand.demand_multiplier
        * cfg.demand.ev_share
    )

    if rng is None:
        rng = np.random.default_rng(
            cfg.vehicles.seed
        )

    df["n_ev"] = sample_ev_counts(
        traffic_volume=df["volume_veh"].to_numpy(),
        cfg=cfg,
        rng=rng,
    )

    return df


def generate_evs(
    traffic: pd.DataFrame,
    cfg: ScenarioConfig,
    dest_offset_km: float,
) -> pd.DataFrame:
    """Generate synthetic EVs from an hourly traffic profile."""

    if dest_offset_km <= 0:
        raise ValueError(
            "dest_offset_km 은 0보다 커야 합니다"
        )

    # One RNG is shared by count sampling and EV attribute sampling.
    rng = np.random.default_rng(
        cfg.vehicles.seed
    )

    counts = hourly_ev_counts(
        traffic,
        cfg,
        rng=rng,
    )

    vehicle_classes, _ = read_vehicle_classes()

    vclass_ids = [
        row["vclass_id"]
        for row in vehicle_classes
    ]

    vclass_shares = [
        row["share"]
        for row in vehicle_classes
    ]

    soc_cfg = cfg.vehicles.soc_beta

    rows: list[dict] = []
    ev_seq = 1

    for row in counts.itertuples(index=False):
        n_ev = int(row.n_ev)

        if n_ev == 0:
            continue

        entry_times = (
            row.hour * 60
            + rng.uniform(
                0.0,
                60.0,
                size=n_ev,
            )
        )

        sampled_classes = rng.choice(
            vclass_ids,
            size=n_ev,
            p=vclass_shares,
        )

        beta_samples = rng.beta(
            soc_cfg.a,
            soc_cfg.b,
            size=n_ev,
        )

        initial_socs = (
            soc_cfg.lo
            + beta_samples
            * (soc_cfg.hi - soc_cfg.lo)
        )

        for i in range(n_ev):
            rows.append(
                {
                    "ev_id": f"EV{ev_seq:07d}",
                    "vclass_id": sampled_classes[i],
                    "entry_time_min": float(
                        entry_times[i]
                    ),
                    "initial_soc": float(
                        initial_socs[i]
                    ),
                    "entry_offset_km": 0.0, # placeholder -> #54
                    "dest_offset_km": float(dest_offset_km),
                }
            )

            ev_seq += 1

    result = pd.DataFrame(
        rows,
        columns=[
            "ev_id",
            "vclass_id",
            "entry_time_min",
            "initial_soc",
            "entry_offset_km",
            "dest_offset_km",
        ],
    )

    if result.empty:
        return result

    result = (
        result
        .sort_values("entry_time_min")
        .reset_index(drop=True)
    )

    result["ev_id"] = [
        f"EV{i:07d}"
        for i in range(1, len(result) + 1)
    ]

    return result[
        [
            "ev_id",
            "vclass_id",
            "entry_time_min",
            "initial_soc",
            "entry_offset_km",
            "dest_offset_km",
        ]
    ]