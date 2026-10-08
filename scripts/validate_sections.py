"""우리 수요가 **실제 도로 위 대수**와 맞는지 본다 (#100).

    python scripts/validate_sections.py --config config/scenario_seollal_down.yaml
    python scripts/validate_sections.py --config config/scenario_seollal_down.yaml --od

## 통행거리가 맞아도 구간 대수는 틀릴 수 있다

#99 는 **통행거리 분포**를 실측 OD 와 맞췄다 (MAPE 53 → 8%). 그건 *"얼마나 멀리
가는가"* 의 모양이다. 구간 대수는 다른 질문이다 — **그 통행들이 실제로 어디를
지나가는가.**

진입 지점이 틀리면 거리 분포는 맞으면서 **차가 엉뚱한 구간을 지난다.** 그리고 지금
우리는 **진입은 VDS, 목적지는 OD** 로 쓰고 있어서, 두 자료가 기점에서 다른 말을 하면
그 불일치가 여기서 드러난다.

## 어떻게 재나

진입 지점·시각은 `entry_exit_profile` 이 **전체 차량** 기준으로 준다 (EV 만이 아니다).
각 (시각, 진입지점) 의 대수를 목적지 분포로 쪼개면 OD 표가 되고, 일정 속도를 가정하면
**어느 콘존을 언제 지나는지**가 정해진다.

**표본을 뽑지 않고 기댓값을 쓴다.** 난수를 쓰면 시드마다 답이 달라져서 두 모델의
차이와 잡음을 못 가른다.

`check_od_by_date.passage` 와 **같은 통과 규칙**을 쓴다 (#62 검토에서 쓴 것과 같다).

## ⚠ 읽을 때

- **워밍업**: 창 시작 직후는 앞 시간대에서 들어온 차가 빠져 과소집계된다. 뺀다.
- **VDS 공백**: 자료가 없는 콘존·시간은 **점수에서 빼고 몇 칸인지 적는다.**
  0 으로 세면 모델이 과대추정한 것처럼 보인다 (#97 에서 215행이 그랬다).
- MAPE 는 분모가 작은 칸(새벽)에서 부풀려진다. **WAPE 를 같이 낸다.**
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.entry_exit import (  # noqa: E402
    corridor_entry_hourly_from_profile,
    sample_exit_offsets,
)
from evdt.io.od_profile import load_od_profile, od_subset_for  # noqa: E402
from evdt.paths import DATA_PROCESSED_DIR, PROJECT_ROOT  # noqa: E402
from evdt.runner import load_config  # noqa: E402

OD_PATH = "data/processed/tcs_od_gyeongbu.parquet"


def zone_positions(direction: str) -> pd.DataFrame:
    z = pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet")
    z = z[z["direction"].astype(str) == direction].copy()
    z["position_km"] = (z["offset_km_start"] + z["offset_km_end"]) / 2.0
    return (z[["conzone_id", "position_km"]]
            .drop_duplicates("conzone_id").sort_values("position_km"))


def hazard_destinations(origins: np.ndarray, hour: int, profile: pd.DataFrame,
                        end_km: float, draws: int = 400) -> dict[float, np.ndarray]:
    """`exit_share` 위험률 모델의 목적지 분포 (기댓값으로 쓰려고 많이 뽑아 평균).

    이 모델은 닫힌 형태로 쓸 수도 있지만, **`sample_exit_offsets` 를 그대로 불러야**
    모델과 검증이 같은 규칙을 쓴다. 뽑는 수를 늘려 잡음을 줄인다.
    """
    rng = np.random.default_rng(12345 + hour)
    out: dict[float, np.ndarray] = {}
    for o in origins:
        d = sample_exit_offsets(np.full(draws, o), hour, profile, rng, corridor_end_km=end_km)
        vals, counts = np.unique(d, return_counts=True)
        out[float(o)] = np.vstack([vals, counts / counts.sum()])
    return out


def build_od(cfg, use_od: bool) -> tuple[pd.DataFrame, str]:
    """(진입 offset, 목적지 offset, 시각, 대수) 표. **전체 차량** 기준."""

    profile = pd.read_csv(PROJECT_ROOT / cfg.demand.entry_exit_profile)
    volume = pd.read_csv(PROJECT_ROOT / cfg.demand.volume_profile)
    points = corridor_entry_hourly_from_profile(profile, volume)
    end_km = float(profile["offset_km"].max())

    rows = []
    if use_od:
        period, direction = od_subset_for(cfg)
        od = load_od_profile(PROJECT_ROOT / OD_PATH, period, direction)
        label = f"OD 수요 ({period}·{direction})"
        for hour, g in points.groupby("hour"):
            origins = g["offset_km"].to_numpy(dtype=float)
            veh = g["entry_veh"].to_numpy(dtype=float)
            idx, _ = od.snap(origins)
            for i, n in zip(idx, veh, strict=True):
                if n <= 0:
                    continue
                probs = np.diff(np.concatenate([[0.0], od.cdfs[i]]))
                rows.append(pd.DataFrame({
                    "start_offset_km": od.origins[i], "end_offset_km": od.dests[i],
                    "hour": int(hour), "veh": n * probs,
                }))
    else:
        label = "옛 수요 (exit_share 위험률)"
        for hour, g in points.groupby("hour"):
            origins = np.unique(g["offset_km"].to_numpy(dtype=float))
            table = hazard_destinations(origins, int(hour), profile, end_km)
            for o, n in zip(g["offset_km"], g["entry_veh"], strict=True):
                if n <= 0:
                    continue
                vals, probs = table[float(o)]
                rows.append(pd.DataFrame({
                    "start_offset_km": float(o), "end_offset_km": vals,
                    "hour": int(hour), "veh": n * probs,
                }))

    out = pd.concat(rows, ignore_index=True)
    return out[out["end_offset_km"] > out["start_offset_km"]], label


def passage(od: pd.DataFrame, zones: pd.DataFrame, speed_kmh: float) -> pd.DataFrame:
    pos = zones["position_km"].to_numpy(dtype=float)
    zid = zones["conzone_id"].to_numpy()
    start = od["start_offset_km"].to_numpy(dtype=float)
    end = od["end_offset_km"].to_numpy(dtype=float)
    veh = od["veh"].to_numpy(dtype=float)
    t0 = od["hour"].to_numpy(dtype=int)

    out = np.zeros((len(pos), 24 + 24), dtype=float)
    for j, p in enumerate(pos):
        hit = (start <= p) & (p < end)
        if not hit.any():
            continue
        lag = (p - start[hit]) / speed_kmh
        whole = np.floor(lag).astype(int)
        frac = lag - whole
        arrive = np.clip(t0[hit] + whole, 0, 47)
        np.add.at(out[j], arrive, veh[hit] * (1 - frac))
        np.add.at(out[j], np.clip(arrive + 1, 0, 47), veh[hit] * frac)

    # 하루를 순환으로 본다 — 23시에 떠나 자정을 넘긴 차는 0시 칸으로 돌아온다.
    # 하루치 프로파일 하나를 반복 재생하는 것이 우리 수요의 정의다
    wrapped = out[:, :24] + out[:, 24:]
    flat = pd.DataFrame(wrapped, index=zid, columns=range(24))
    return flat.stack().rename("model_veh").rename_axis(["conzone_id", "hour"]).reset_index()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--od", action="store_true", help="목적지를 실측 OD 에서 (#99)")
    ap.add_argument("--warmup-hours", type=int, default=5)
    args = ap.parse_args()

    cfg = load_config(args.config)
    direction = "UP" if str(cfg.corridor_id).endswith("_up") else "DOWN"
    zones = zone_positions(direction)

    od, label = build_od(cfg, args.od)
    model = passage(od, zones, float(cfg.demand.cruise_speed_kmh))

    vds = pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet")
    vds = vds[(vds["direction"].astype(str) == direction)
              & (vds["period"].astype(str) == str(cfg.demand.period))]
    vds = (vds.groupby(["conzone_id", "hour"])["volume_veh"].mean()
           .rename("vds_veh").reset_index())

    cmp = model.merge(vds, on=["conzone_id", "hour"], how="left")
    cells = len(cmp)
    # ⚠ 자료 없는 칸은 **빼고 몇 칸인지 적는다.** 0 으로 세면 과대추정으로 보인다
    gap = cmp["vds_veh"].isna() | (cmp["vds_veh"] <= 0)
    gap_zones = sorted(cmp.loc[gap, "conzone_id"].unique())
    cmp = cmp[~gap]
    cmp = cmp[cmp["hour"] >= args.warmup_hours]

    err = (cmp["model_veh"] - cmp["vds_veh"]).abs()
    mape = (err / cmp["vds_veh"]).mean() * 100
    wape = err.sum() / cmp["vds_veh"].sum() * 100
    ratio = cmp["model_veh"].sum() / cmp["vds_veh"].sum()

    print(f"\n{cfg.scenario_id}  {direction}  —  {label}")
    print(f"수요: {cfg.demand_label}")
    print(f"속도 {cfg.demand.cruise_speed_kmh:g} km/h · 워밍업 {args.warmup_hours}시간 제외")
    print(f"\n  점수에 쓴 칸 {len(cmp):,} / {cells:,}")
    print(f"  VDS 자료 없음 {int(gap.sum()):,}칸 · 콘존 {len(gap_zones)}곳")
    print(f"\n  MAPE   {mape:6.1f}%")
    print(f"  WAPE   {wape:6.1f}%   (물량가중 — 새벽 작은 칸이 MAPE 를 부풀린다)")
    print(f"  총량배율 {ratio:5.2f}   (1.0 이면 하루 통과 대수가 맞는다)")

    by_hour = cmp.assign(ape=err / cmp["vds_veh"] * 100).groupby("hour")["ape"].mean()
    print("\n  시간대별 MAPE%")
    for h in sorted(by_hour.index):
        print(f"    {h:2d}시 {by_hour[h]:6.1f}  {'#' * int(min(by_hour[h], 120) / 4)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
