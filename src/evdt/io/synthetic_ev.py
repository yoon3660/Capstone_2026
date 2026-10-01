from __future__ import annotations

import math

import numpy as np
import pandas as pd

from evdt.config import ScenarioConfig, SocBeta, SocDist
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


def _norm_cdf(z: float) -> float:
    """표준정규 CDF (스칼라). 절삭 구간의 확률질량을 구하는 데 쓴다."""

    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


#: Acklam 의 역정규 CDF 유리근사 계수. |오차| < 1.15e-9 로 우리 용도에는 충분하다.
#: scipy.special.ndtri 를 쓰지 않는 이유 — scipy 는 이 프로젝트의 선언된 의존성이 아니다
#: (지금 import 되는 것은 전이 의존성이라 팀원의 새 환경에서는 없을 수 있다).
_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
      3.754408661907416e00)


def _norm_ppf(p: np.ndarray) -> np.ndarray:
    """표준정규 분위수 (역 CDF), 벡터화. p 는 (0, 1) 안이어야 한다."""

    p = np.asarray(p, dtype=float)
    out = np.empty_like(p)
    lo_tail, hi_tail = p < 0.02425, p > 1.0 - 0.02425
    mid = ~(lo_tail | hi_tail)

    q = np.sqrt(-2.0 * np.log(np.where(lo_tail, p, 0.5)))
    out = np.where(
        lo_tail,
        ((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
         / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)),
        out,
    )
    q = np.sqrt(-2.0 * np.log(np.where(hi_tail, 1.0 - p, 0.5)))
    out = np.where(
        hi_tail,
        -((((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5])
          / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)),
        out,
    )
    q = np.where(mid, p, 0.5) - 0.5
    r = q * q
    out = np.where(
        mid,
        (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q
        / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0),
        out,
    )
    return out


def sample_initial_soc(
    dist: SocDist,
    rng: np.random.Generator,
    n: int,
) -> tuple[np.ndarray, int]:
    """진입 SoC 를 n 개 뽑는다. `(값, 바닥에 걸린 대수)` 를 돌려준다 (#55).

    **절삭이지 클리핑이 아니다.** `max(soc, lo)` 로 깔면 lo 에 뾰족한 덩어리가 생겨
    그 자체가 인공물이 된다. 역변환으로 [lo, hi] 안에서만 뽑아 재정규화한다.

    두 번째 값은 "절삭이 없었다면 lo 아래였을 대수" 다 — 가드레일이 몇 대를 건드렸나.
    **이 수가 크면 가드레일이 잘 도는 게 아니라 분포가 틀린 것이다.**
    """

    if n <= 0:
        return np.empty(0, dtype=float), 0

    if isinstance(dist, SocBeta):
        samples = dist.lo + rng.beta(dist.a, dist.b, size=n) * (dist.hi - dist.lo)
        return samples, 0

    # 로그정규: 역변환 절삭. ln(X) ~ Normal(mu, sigma) 이므로 정규의 CDF 구간에서 뽑는다.
    lo_z = (math.log(dist.lo) - dist.mu) / dist.sigma if dist.lo > 0 else -40.0
    hi_z = (math.log(dist.hi) - dist.mu) / dist.sigma
    lo_p, hi_p = _norm_cdf(lo_z), _norm_cdf(hi_z)
    if hi_p - lo_p < 1e-12:
        raise ValueError(
            f"로그정규 진입 SoC 분포에 [{dist.lo}, {dist.hi}] 구간의 질량이 거의 없다 "
            f"(mu={dist.mu}, sigma={dist.sigma}). mu 를 구간 안으로 옮겨라"
        )

    u = rng.uniform(lo_p, hi_p, size=n)
    samples = np.exp(dist.mu + dist.sigma * _norm_ppf(u))
    # 절삭했으므로 구간을 벗어날 수 없지만, 부동소수 끝자락은 막아 둔다
    samples = np.clip(samples, dist.lo, dist.hi)

    # 가드레일이 몇 대를 건드렸나 = 절삭하지 않았다면 lo 아래였을 비율 × n
    return samples, int(round(lo_p * n))


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
    floored_total = 0        # 가드레일이 건드린 대수 (#55). 호출자가 KPI 로 남긴다

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

        initial_socs, n_floored = sample_initial_soc(soc_cfg, rng, n_ev)
        floored_total += n_floored

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
        result.attrs["n_soc_floored"] = floored_total
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

    out = result[
        [
            "ev_id",
            "vclass_id",
            "entry_time_min",
            "initial_soc",
            "entry_offset_km",
            "dest_offset_km",
        ]
    ]
    # 가드레일이 몇 대를 건드렸나 (#55). 조용히 사라지지 않게 호출자가 KPI 로 남긴다.
    out.attrs["n_soc_floored"] = floored_total
    return out