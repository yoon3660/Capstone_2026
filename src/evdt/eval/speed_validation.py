"""CTM 속도 vs VDS 실측 속도 비교 지표 (T-08).

절댓값 오차보다 "막히는 곳에서 막히는가" 가 중요하다. 속도가 10km/h 틀린 건
넘어갈 수 있지만, 실제로 정체가 나는 구간·시간에 시뮬이 소통으로 나오면
충전 수요의 파도(정체 해소 직후 몰림)를 재현할 수 없다.

입력: 둘 다 (conzone_id, date, hour, speed_kmh) 를 가진 DataFrame.
      시뮬 쪽은 CTM 셀 속도를 콘존 구간(offset_km_start~end)으로 평균해서 맞춘다.

지표
    mae_kmh, corr                    속도 절댓값 오차, 상관계수
    confusion                        실측 정체 O/X × 시뮬 정체 O/X (시간 단위)
    onset_lag_h                      콘존·일자별 첫 정체 시각 차이 (시뮬 − 실측, 시간)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

KEY = ["conzone_id", "date", "hour"]


@dataclass(frozen=True)
class SpeedComparison:
    n: int
    mae_kmh: float
    corr: float
    confusion: dict[str, int]      # tp, fp, fn, tn  (positive = 정체)
    onset_lag_h: pd.DataFrame      # conzone_id, date, obs_onset, sim_onset, lag_h

    @property
    def recall(self) -> float:
        """실제 정체 시간 중 시뮬도 정체로 잡은 비율."""
        c = self.confusion
        return c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else float("nan")

    @property
    def precision(self) -> float:
        """시뮬 정체 시간 중 실제로도 정체였던 비율."""
        c = self.confusion
        return c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else float("nan")


def compare_speeds(
    observed: pd.DataFrame,
    simulated: pd.DataFrame,
    congestion_kmh: float,
) -> SpeedComparison:
    """같은 콘존·같은 시각끼리 짝지어 비교한다. 한쪽이 비면 그 시간은 뺀다."""

    merged = observed[KEY + ["speed_kmh"]].merge(
        simulated[KEY + ["speed_kmh"]], on=KEY, suffixes=("_obs", "_sim")
    ).dropna(subset=["speed_kmh_obs", "speed_kmh_sim"])

    if merged.empty:
        raise ValueError("짝지어진 관측·시뮬 시간이 없습니다. 콘존 ID 와 날짜를 확인할 것")

    obs = merged["speed_kmh_obs"].to_numpy()
    sim = merged["speed_kmh_sim"].to_numpy()

    obs_jam = obs < congestion_kmh
    sim_jam = sim < congestion_kmh

    corr = float(np.corrcoef(obs, sim)[0, 1]) if len(merged) > 1 else float("nan")

    merged = merged.assign(obs_jam=obs_jam, sim_jam=sim_jam)
    onsets = []

    for (conzone_id, day), g in merged.groupby(["conzone_id", "date"]):
        obs_onset = g.loc[g["obs_jam"], "hour"].min()
        sim_onset = g.loc[g["sim_jam"], "hour"].min()
        if pd.isna(obs_onset) and pd.isna(sim_onset):
            continue
        onsets.append(
            {
                "conzone_id": conzone_id,
                "date": day,
                "obs_onset": obs_onset,
                "sim_onset": sim_onset,
                "lag_h": sim_onset - obs_onset,   # NaN = 한쪽만 정체
            }
        )

    return SpeedComparison(
        n=len(merged),
        mae_kmh=float(np.mean(np.abs(sim - obs))),
        corr=corr,
        confusion={
            "tp": int(np.sum(obs_jam & sim_jam)),
            "fp": int(np.sum(~obs_jam & sim_jam)),
            "fn": int(np.sum(obs_jam & ~sim_jam)),
            "tn": int(np.sum(~obs_jam & ~sim_jam)),
        },
        onset_lag_h=pd.DataFrame(
            onsets, columns=["conzone_id", "date", "obs_onset", "sim_onset", "lag_h"]
        ),
    )
