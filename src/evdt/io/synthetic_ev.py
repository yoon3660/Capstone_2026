from __future__ import annotations

import numpy as np
import pandas as pd

from evdt.config import ScenarioConfig
from evdt.io.vehicles import read_vehicle_classes


def hourly_ev_counts(
    traffic: pd.DataFrame,
    cfg: ScenarioConfig,
) -> pd.DataFrame:
    """시간대별 교통량에서 합성 EV 생성 대수를 계산한다."""

    df = traffic.copy()

    df["expected_ev"] = (
        df["volume_veh"]
        * cfg.demand.demand_multiplier
        * cfg.demand.ev_share
    )

    df["n_ev"] = df["expected_ev"].round().astype(int)

    return df

def generate_evs(
    traffic: pd.DataFrame,
    cfg: ScenarioConfig,
    dest_offset_km: float,
) -> pd.DataFrame:
    """시간대별 교통량 프로파일에서 합성 EV 집합을 생성한다."""

    if dest_offset_km <= 0:
        raise ValueError("dest_offset_km 은 0보다 커야 합니다")

    counts = hourly_ev_counts(traffic, cfg)

    vehicle_classes, _ = read_vehicle_classes()

    vclass_ids = [row["vclass_id"] for row in vehicle_classes]
    vclass_shares = [row["share"] for row in vehicle_classes]

    rng = np.random.default_rng(cfg.vehicles.seed)

    soc_cfg = cfg.vehicles.soc_beta

    rows: list[dict] = []
    ev_seq = 1

    for row in counts.itertuples(index=False):
        n_ev = int(row.n_ev)

        if n_ev == 0:
            continue

        entry_times = row.hour * 60 + rng.uniform(
            0.0,
            60.0,
            size=n_ev,
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
            + beta_samples * (soc_cfg.hi - soc_cfg.lo)
        )

        for i in range(n_ev):
            rows.append(
                {
                    "ev_id": f"EV{ev_seq:07d}",
                    "vclass_id": sampled_classes[i],
                    "entry_time_min": float(entry_times[i]),
                    "initial_soc": float(initial_socs[i]),
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
            "dest_offset_km",
        ],
    )

    if result.empty:
        return result

    result = result.sort_values("entry_time_min").reset_index(drop=True)

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
            "dest_offset_km",
        ]
    ]