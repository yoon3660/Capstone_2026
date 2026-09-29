from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from evdt.config import ScenarioConfig
from evdt.io.synthetic_ev import hourly_ev_counts

N_SEEDS = 20
METHODS = (
    "fixed",
    "poisson",
    "binomial",
    "beta_binomial",
    "poisson_lognormal",
)
PEAK_HOUR = 18


def main() -> None:
    cfg = ScenarioConfig.from_yaml(
        "config/scenario_seollal_down.yaml"
    )

    traffic_path = Path(cfg.demand.volume_profile)
    traffic = pd.read_csv(traffic_path)

    results: list[dict] = []

    for method in METHODS:
        total_counts: list[int] = []
        peak_counts: list[int] = []

        started = time.perf_counter()

        for seed in range(N_SEEDS):
            test_cfg = replace(
                cfg,
                demand=replace(
                    cfg.demand,
                    ev_count_method=method,
                ),
                vehicles=replace(
                    cfg.vehicles,
                    seed=seed,
                ),
            )

            counts = hourly_ev_counts(
                traffic,
                test_cfg,
            )

            total_counts.append(
                int(counts["n_ev"].sum())
            )

            peak_row = counts[
                counts["hour"] == PEAK_HOUR
            ]

            peak_counts.append(
                int(peak_row["n_ev"].iloc[0])
            )

        elapsed = time.perf_counter() - started

        results.append(
            {
                "method": method,
                "total_mean": np.mean(total_counts),
                "total_variance": np.var(
                    total_counts,
                    ddof=1,
                ),
                "18h_mean": np.mean(peak_counts),
                "18h_variance": np.var(
                    peak_counts,
                    ddof=1,
                ),
                "min_total": np.min(total_counts),
                "max_total": np.max(total_counts),
                "runtime_sec": elapsed,
            }
        )

    result_df = pd.DataFrame(results)

    print()
    print(
        f"=== EV count method comparison "
        f"({N_SEEDS} seeds) ==="
    )

    print(
        result_df.to_string(
            index=False,
            float_format=lambda x: f"{x:.2f}",
        )
    )


if __name__ == "__main__":
    main()