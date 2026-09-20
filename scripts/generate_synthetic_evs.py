from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from evdt.config import ScenarioConfig
from evdt.io.route import GyeongbuRoute
from evdt.io.run_registry import RunContext
from evdt.io.synthetic_ev import generate_evs
from evdt.paths import DATA_PROCESSED_DIR, PROJECT_ROOT

TRAFFIC_PATH = DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="T-07 교통량으로 시드 고정 합성 EV를 생성한다."
    )

    parser.add_argument(
        "--scenario",
        required=True,
        help="시나리오 YAML 경로",
    )
    parser.add_argument(
        "--period",
        required=True,
        help="traffic_gyeongbu.parquet의 period 값",
    )
    parser.add_argument(
        "--date",
        required=True,
        help="생성 대상 날짜 (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--entry-conzone",
        required=True,
        help="corridor 진입 수요로 사용할 VDS conzone_id",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="동일한 run_id가 이미 있으면 기존 실행을 덮어쓴다.",
    )

    return parser.parse_args()


def load_volume_profile(
    *,
    period: str,
    date: str,
    direction: str,
    entry_conzone: str,
) -> pd.DataFrame:
    """T-07 정리본에서 한 시나리오의 24시간 진입 교통량을 뽑는다."""

    if not TRAFFIC_PATH.exists():
        raise FileNotFoundError(
            f"T-07 교통량 파일이 없습니다: {TRAFFIC_PATH}"
        )

    traffic = pd.read_parquet(TRAFFIC_PATH)

    target_date = pd.Timestamp(date)

    profile = traffic[
        (traffic["period"] == period)
        & (traffic["date"] == target_date)
        & (traffic["direction"] == direction)
        & (traffic["conzone_id"] == entry_conzone)
    ][
        [
            "hour",
            "volume_veh",
        ]
    ].sort_values("hour")

    if len(profile) != 24:
        raise ValueError(
            f"24시간 데이터가 필요하지만 {len(profile)}행입니다: "
            f"{period=} {date=} {direction=} {entry_conzone=}"
        )

    if profile["hour"].nunique() != 24:
        raise ValueError("hour 0~23이 유일하게 한 행씩 존재해야 합니다")

    if profile["volume_veh"].isna().any():
        missing_hours = profile.loc[
            profile["volume_veh"].isna(),
            "hour",
        ].tolist()

        raise ValueError(
            f"진입 교통량에 결측이 있습니다: hour={missing_hours}"
        )

    if (profile["volume_veh"] < 0).any():
        raise ValueError("교통량은 음수가 될 수 없습니다")

    return profile.reset_index(drop=True)


def resolve_project_path(path_text: str) -> Path:
    path = Path(path_text)

    if path.is_absolute():
        return path

    return PROJECT_ROOT / path

def save_validation_plot(
    profile: pd.DataFrame,
    evs: pd.DataFrame,
    ev_share: float,
    demand_multiplier: float,
    output_path: Path,
) -> None:
    """관측 교통량 기반 기대 EV 수와 실제 합성 EV 수를 시간대별로 비교한다."""

    expected = profile.copy()
    expected["expected_ev"] = (
        expected["volume_veh"]
        * demand_multiplier
        * ev_share
    )

    generated = (
        evs.assign(
            hour=(evs["entry_time_min"] // 60).astype(int)
        )
        .groupby("hour")
        .size()
        .reindex(range(24), fill_value=0)
    )

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.plot(
        expected["hour"],
        expected["expected_ev"],
        marker="o",
        label="Observed traffic × EV share",
    )

    ax.bar(
        generated.index,
        generated.values,
        alpha=0.45,
        label="Generated synthetic EVs",
    )

    ax.set_xlabel("Hour")
    ax.set_ylabel("EV count")
    ax.set_title("Observed demand vs synthetic EV arrivals")
    ax.set_xticks(range(24))
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fig.savefig(
        output_path,
        dpi=150,
    )

    plt.close(fig)

def main() -> None:
    args = parse_args()

    cfg = ScenarioConfig.from_yaml(args.scenario)

    profile = load_volume_profile(
        period=args.period,
        date=args.date,
        direction=cfg.direction,
        entry_conzone=args.entry_conzone,
    )

    profile_path = resolve_project_path(
        cfg.demand.volume_profile
    )

    route = GyeongbuRoute.load()

    # TCS OD가 들어오기 전까지 corridor 종점을 임시 목적지로 사용한다.
    dest_offset_km = route.length_km

    generation_params = {
        "synthetic_ev": {
            "traffic_source": str(TRAFFIC_PATH.relative_to(PROJECT_ROOT)),
            "period": args.period,
            "date": args.date,
            "direction": cfg.direction,
            "entry_conzone_id": args.entry_conzone,
            "demand_multiplier": cfg.demand.demand_multiplier,
            "ev_share": cfg.demand.ev_share,
            "seed": cfg.vehicles.seed,
            "soc_beta": {
                "a": cfg.vehicles.soc_beta.a,
                "b": cfg.vehicles.soc_beta.b,
                "lo": cfg.vehicles.soc_beta.lo,
                "hi": cfg.vehicles.soc_beta.hi,
            },
            "count_method": "round(volume_veh * demand_multiplier * ev_share)",
            "arrival_method": "uniform_within_hour_conditioned_on_count",
            "destination_method": "corridor_end_until_tcs_od",
            "dest_offset_km": dest_offset_km,
        }
    }

    with RunContext.open(
        cfg,
        seed=cfg.vehicles.seed,
        params=generation_params,
        overwrite=args.overwrite,
    ) as run:
        profile_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        profile.to_csv(
            profile_path,
            index=False,
        )

        evs = generate_evs(
            profile,
            cfg,
            dest_offset_km=dest_offset_km,
        )

        ev_path = (
            DATA_PROCESSED_DIR
            / f"synthetic_evs_{cfg.scenario_id}.parquet"
        )

        evs.to_parquet(
            ev_path,
            index=False,
        )

        plot_path = run.output_dir / "synthetic_ev_hourly_validation.png"

        save_validation_plot(
            profile=profile,
            evs=evs,
            ev_share=cfg.demand.ev_share,
            demand_multiplier=cfg.demand.demand_multiplier,
            output_path=plot_path,
        )

        run.kpi(
            "synthetic_ev_count",
            len(evs),
            "veh",
        )

        print("=== 합성 EV 생성 완료 ===")
        print(f"run_id          : {run.run_id}")
        print(f"scenario        : {cfg.scenario_id}")
        print(f"direction       : {cfg.direction}")
        print(f"period          : {args.period}")
        print(f"date            : {args.date}")
        print(f"entry conzone   : {args.entry_conzone}")
        print(f"EV share        : {cfg.demand.ev_share}")
        print(f"seed            : {cfg.vehicles.seed}")
        print(f"destination km  : {dest_offset_km:.3f}")
        print(f"EV count        : {len(evs)}")
        print(f"volume profile  : {profile_path}")
        print(f"synthetic EVs   : {ev_path}")
        print(f"run output      : {run.output_dir}")
        print(f"validation plot : {plot_path}")

if __name__ == "__main__":
    main()