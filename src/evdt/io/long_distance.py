"""장거리 통행을 따로 얹는다 (#54).

## 왜 필요한가 — 구간 교통량만으로는 통행거리가 정해지지 않는다

`entry_exit.py` 는 구간마다 몇 대가 붙고 빠졌는지를 낸다. 그런데 **같은 교통량
프로파일을 만드는 통행거리 분포는 무수히 많다.** 한쪽 끝은

    최대 churn   IC 마다 지나가는 차의 일정 비율이 빠진다.
                 통행이 짧아지고 완주 통행이 거의 0 이 된다 (entry_exit 의 기본)
    최대 through 빠지는 차와 붙는 차를 최대한 다른 차로 본다.
                 완주 통행이 자료가 허용하는 최대치가 된다

두 극단이 같은 구간 교통량을 낸다. 구간 교통량은 **흐름**을 정하지 **통행**을 정하지
않는다. 그래서 어느 쪽을 쓰는지가 결과를 바꾼다 — 그리고 그건 자료가 아니라 **가정**이다.

증거로, 설 최대일과 평시(3월) 최대일의 추정 통행거리가 거의 같게 나온다
(하행 평균 51 → 60 km). 진짜 귀성이라면 훨씬 길어져야 한다. 최대 churn 가정이
긴 꼬리를 만들 수 없기 때문이다.

## 자료가 허용하는 범위는 계산할 수 있다

완주 통행이 L 대라면 그 차들은 **모든 구간**을 지난다. 그러니 어떤 구간의 교통량도
L 보다 작을 수 없다.

    L ≤ min(구간 교통량)

설 최대일 실측으로는

    하행  진입 104,520 · 가장 적은 구간 14,700 (319 km) → 완주 상한 **14.1%**
    상행  진입  50,548 · 가장 적은 구간 28,100 ( 90 km) → 완주 상한 **55.6%**

(이 값이 `demand_profile.through_share` 의 마지막 값과 같은 것은 우연이 아니다.
누적 최솟값은 정의상 이 상한이다. 옛 방식은 **상한을 추정값으로 써 왔다.**)

한편 도로공사 TCS 실측은 서울→부산을 하루 12~74대로 준다 (진입의 0.07%).
그러니 진짜 값은 **0.07% 와 14.1% 사이 어딘가**이고, 자료만으로는 더 좁힐 수 없다.
#61~#63 의 실측 OD 가 좁혀 줄 일이다.

## 그래서 이 모듈이 하는 일

장거리 통행 비중을 **드러난 가정**으로 두고, 나머지는 실측 교통량에 맞춰 다시 맞춘다.

    1. 진입의 `share` 를 장거리 통행으로 뗀다 (목적지는 `min_trip_km` 이상)
    2. 그 차들이 각 구간에 얹는 교통량을 실측에서 뺀다
    3. 남은 교통량으로 `entry_exit` 추정을 다시 한다

**합치면 실측 구간 교통량이 그대로 나온다.** 장거리를 얼마나 섞든 자료와 모순되지
않는다는 뜻이다. 모순되는 값(잔차가 음수)은 거부하고 최대 허용치를 알려준다.

⚠ 여기서 만드는 장거리 통행은 **실측이 아니라 시나리오 파라미터**다. 결과 표와
발표에서 반드시 그렇게 표기한다. 과거에 가짜 휴게소를 진짜 코리도에 넣었다가
원인 모를 셀 경계를 만든 일이 있었다 (`docs/fake_data_audit.md`). 그래서 이 모듈은
DB 에 아무것도 쓰지 않고, 만든 값에 `source` 를 붙여 돌려주기만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: 만든 통행에 붙는 표시. 실측과 절대 섞이지 않게 한다.
SYNTHETIC_SOURCE = "assumed_long_distance"


class InfeasibleShare(ValueError):
    """실측 교통량이 그만큼의 장거리 통행을 담을 수 없다."""


@dataclass(frozen=True)
class LongDistanceSplit:
    """장거리 통행과, 그것을 뺀 나머지 교통량."""

    long_trips: pd.DataFrame       #: (hour, entry_offset_km, dest_offset_km, veh, source)
    residual_volume: pd.DataFrame  #: (hour, offset_km_start, volume_veh) — 실측 − 장거리
    share: float
    min_trip_km: float

    @property
    def n_long_veh(self) -> float:
        return float(self.long_trips["veh"].sum())


def max_feasible_share(
    volume: pd.DataFrame,
    *,
    min_trip_km: float,
    corridor_end_km: float,
) -> float:
    """진입 대비 장거리 통행의 최대 비중.

    `min_trip_km` 이상 가는 통행은 **길이 min_trip_km 인 어떤 창이든** 하나는
    통째로 지난다. 그러니 그런 창의 최소 교통량이 상한이다.
    """

    daily = volume.groupby("offset_km_start")["volume_veh"].sum().sort_index()
    x = daily.index.to_numpy(dtype=float)
    v = daily.to_numpy(dtype=float)
    head = v[0]

    if head <= 0:
        raise ValueError("진입 교통량이 0 이다")

    best = 0.0
    for start in x:
        if start + min_trip_km > corridor_end_km:
            break
        window = v[(x >= start) & (x < start + min_trip_km)]
        if window.size:
            best = max(best, float(window.min()))

    return min(best / head, 1.0)


def split(
    volume: pd.DataFrame,
    *,
    share: float,
    min_trip_km: float,
    corridor_end_km: float,
    cruise_kmh: float = 80.0,
    rng: np.random.Generator | None = None,
) -> LongDistanceSplit:
    """진입의 `share` 를 장거리 통행으로 떼고, 나머지 교통량을 돌려준다.

    volume: (hour, offset_km_start, volume_veh) — 하루치 실측 (한 방향, 한 날)

    장거리 통행은 코리도 진입점에서 출발해 `min_trip_km` 이상 간다. 목적지는
    **실제로 차가 많이 빠지는 곳**에 비례해 뽑는다 (구간 교통량이 줄어드는 곳).

    ⚠ **통행시간만큼 늦춰서 뺀다.** 8시에 출발한 차는 300 km 구간에 8시가 아니라
    12시쯤 나타난다. 같은 시각에 빼면 먼 구간의 아침 교통량에서 아직 오지도 않은
    차를 빼게 되고, 담을 수 있는 장거리 비중이 실제보다 좁게 나온다.
    """

    if not 0.0 <= share <= 1.0:
        raise ValueError(f"share 는 0 과 1 사이여야 한다: {share}")

    wide = (
        volume.groupby(["hour", "offset_km_start"])["volume_veh"].sum()
        .unstack("offset_km_start").sort_index(axis=1).fillna(0.0)
    )
    x = wide.columns.to_numpy(dtype=float)
    v = wide.to_numpy(dtype=float)                      # (시간, 구간)

    if share == 0.0:
        return LongDistanceSplit(
            long_trips=pd.DataFrame(
                columns=["hour", "entry_offset_km", "dest_offset_km", "veh", "source"]),
            residual_volume=volume.copy(),
            share=0.0,
            min_trip_km=min_trip_km,
        )

    # 목적지 후보와 가중치: 교통량이 줄어드는 곳 = 실제로 빠지는 곳
    drop = np.maximum(-np.diff(v.sum(axis=0)), 0.0)
    far = x[1:] >= min_trip_km
    candidates = np.r_[x[1:][far], corridor_end_km]
    weights = np.r_[drop[far], v.sum(axis=0)[-1]]

    if weights.sum() <= 0:
        raise InfeasibleShare(f"{min_trip_km} km 이상 갈 목적지가 없다")

    weights = weights / weights.sum()
    rng = rng or np.random.default_rng(0)

    rows = []
    covered = np.zeros_like(v)                           # 장거리 통행이 각 구간에 얹는 양

    for h_index, hour in enumerate(wide.index):
        n_long = v[h_index, 0] * share

        if n_long <= 0:
            continue

        for dest, weight in zip(candidates, weights, strict=True):
            veh = n_long * weight
            if veh <= 0:
                continue
            rows.append({
                "hour": int(hour),
                "entry_offset_km": float(x[0]),
                "dest_offset_km": float(dest),
                "veh": float(veh),
                "source": SYNTHETIC_SOURCE,
            })
            # 구간 s 에 닿는 시각 = 출발 + 주행시간. 하루를 넘어가면 버린다
            # (전날 출발분이 들어오는 것과 상쇄된다고 본다)
            reaches = x < dest
            arrive = h_index + np.floor((x - x[0]) / cruise_kmh).astype(int)
            for section in np.flatnonzero(reaches):
                slot = arrive[section]
                if 0 <= slot < covered.shape[0]:
                    covered[slot, section] += veh

    residual = v - covered
    worst = residual.min()

    if worst < -1e-6:
        limit = max_feasible_share(volume, min_trip_km=min_trip_km,
                                   corridor_end_km=corridor_end_km)
        raise InfeasibleShare(
            f"장거리 {share:.1%} 는 실측 교통량보다 많다 (구간 잔차 최소 {worst:,.0f}대).\n"
            f"이 min_trip_km({min_trip_km} km) 에서 자료가 허용하는 최대 비중은 {limit:.1%} 다"
        )

    residual_long = (
        pd.DataFrame(np.maximum(residual, 0.0), index=wide.index, columns=wide.columns)
        .stack().rename("volume_veh").reset_index()
    )

    return LongDistanceSplit(
        long_trips=pd.DataFrame(rows),
        residual_volume=residual_long,
        share=share,
        min_trip_km=min_trip_km,
    )
