from __future__ import annotations

import time

import pandas as pd

from _bootstrap import ROOT  # noqa: F401

from evdt.runner import (
    ExperimentFailed,
    load_config,
    parse_seeds,
    run_experiment,
)


METHODS = (
    "fixed",
    "poisson",
    "binomial",
    "beta_binomial",
    "poisson_lognormal",
)

GROUPS = {
    "fixed": "baseline",
    "poisson": "official",
    "binomial": "alternative",
    "beta_binomial": "experimental",
    "poisson_lognormal": "experimental",
}

SEEDS = "1-20"


def main() -> None:
    # #53 기존 비교 조건과 동일:
    # 경부 하행 · 출발 SoC high · 수요 ×3
    base_cfg = load_config(
        "config/scenario_seollal_down.yaml",
        soc="high",
        demand_multiplier=3.0,
    )

    seeds = parse_seeds(SEEDS)

    out_dir = (
        ROOT
        / "runs"
        / "experiments"
        / "ev_count_method_comparison_high_dm3"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[pd.DataFrame] = []
    runtime_rows: list[dict] = []

    print("=== EV count method UE comparison ===")
    print(f"methods : {len(METHODS)}")
    print(f"seeds   : {len(seeds)}")
    print(f"runs    : {len(METHODS) * len(seeds)}")
    print()

    for idx, method in enumerate(METHODS, 1):
        print("=" * 70)
        print(f"[{idx}/{len(METHODS)}] {method}")
        print("=" * 70)

        cfg = base_cfg.variant(
            f"evcount-{method}",
            {
                "demand.ev_count_method": method,
            },
        )

        started = time.perf_counter()

        try:
            result = run_experiment(
                cfg,
                seeds,
                reuse_done=True,
                min_seeds=20,

                # 지금 목적은 KPI 신뢰구간 비교.
                # 히트맵은 나중에 필요할 때 따로.
                heatmaps=(),
            )

        except ExperimentFailed as exc:
            elapsed = time.perf_counter() - started

            print(f"[FAILED] {method}")
            print(exc)

            runtime_rows.append(
                {
                    "method": method,
                    "group": GROUPS[method],
                    "status": "FAILED",
                    "runtime_sec": elapsed,
                }
            )
            continue

        elapsed = time.perf_counter() - started

        summary = result.summary.copy()
        summary.insert(0, "method", method)
        summary.insert(1, "group", GROUPS[method])

        summaries.append(summary)

        runtime_rows.append(
            {
                "method": method,
                "group": GROUPS[method],
                "status": "DONE",
                "runtime_sec": elapsed,
                "experiment_id": result.experiment_id,
            }
        )

        print()
        print(
            summary[
                [
                    "label",
                    "mean",
                    "sd",
                    "ci_low",
                    "ci_high",
                    "min",
                    "max",
                ]
            ].to_string(index=False)
        )
        print(f"\n{method} 완료: {elapsed:.1f}s\n")

    if summaries:
        combined = pd.concat(
            summaries,
            ignore_index=True,
        )

        combined.to_csv(
            out_dir / "combined_kpi_ci.csv",
            index=False,
            encoding="utf-8-sig",
        )

        print("\n=== 전체 KPI 비교 ===")

        print(
            combined[
                [
                    "method",
                    "group",
                    "label",
                    "mean",
                    "sd",
                    "ci_low",
                    "ci_high",
                ]
            ].to_string(index=False)
        )

    runtime_df = pd.DataFrame(runtime_rows)

    runtime_df.to_csv(
        out_dir / "method_runtime.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print("\n결과 폴더:")
    print(out_dir)


if __name__ == "__main__":
    main()