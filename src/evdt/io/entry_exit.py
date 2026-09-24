"""중간 진입·진출 추정 (이슈 #54).

    콘존 시간대별 교통량 → "어디서 몇 대가 붙고 몇 대가 빠지나"

구간 교통량이 상류 콘존보다 늘었으면 그 사이에서 차가 **붙은** 것이고, 줄었으면
**빠진** 것이다. 그게 전부다. 실측 OD (#61~#63) 없이 지금 가진 자료만으로 할 수 있는
가장 단순한 추정이다.

## 무엇을 고치려는 것인가

지금 목적지는 `demand_profile.through_share` — 교통량 ÷ 진입량의 **누적 최솟값** — 이
떨어지는 만큼만 생긴다. 누적 최솟값은 한 번 내려가면 못 올라가므로, 도시 근처에서
교통량이 다시 늘어나는 것을 **구조적으로 볼 수 없다.** 설 최대일 실측으로 재보면

    하행  411 km 중 326 km (79%) 에 목적지가 하나도 안 생긴다
    상행  411 km 중 399 km (97%)

상행은 구서 쪽 진입이 하루 50,548대인데 서울 근처 구간이 104,985대를 싣고 있다
(진입량의 1.75배). 귀경 교통의 대부분은 부산이 아니라 대구·대전·천안에서 붙는 차인데,
지금 모델은 전원 구서 출발로 돌고 있다.

## 이 방법이 말할 수 있는 것과 없는 것

**말할 수 있는 것** — 구간마다 차가 늘었는지 줄었는지, 얼마나.
설 기간 10일을 보면 경계의 **73~74% 가 모든 날에 같은 부호**다. 날마다 양은 달라도
(변동계수 0.3~0.4) 방향은 같다. 검지기 잡음이 아니라 구조적인 것이다.

**말할 수 없는 것 세 가지. 반드시 문서와 발표에 적는다.**

1. **한 IC 의 진입과 진출을 가를 수 없다.** 교통량 차이는 둘의 **차이(net)** 뿐이다.
   20대가 붙고 50대가 빠졌으면 "30대가 빠졌다" 로만 보인다. 진입·진출 둘 다 과소평가된다.
2. **분기점(JC)과 IC 를 가를 수 없다.** 다른 고속도로로 갈아타는 차와 일반 IC 로 빠지는
   차가 같아 보인다. 그 구분은 #62 가 실측 OD 로 한다.
3. **결측 콘존이 있다** (하행 20% · 상행 23%). 빠진 구간을 건너뛰고 재므로, 그 사이에서
   일어난 진입·진출이 **한 경계에 몰아서** 잡힌다. `gap_km` 로 얼마나 건너뛰었는지 남긴다.

## 결과를 누가 쓰나

한 표를 셋이 같이 쓴다. 따로 만들면 EV 와 배경 교통이 서로 다른 수요에서 나와,
CTM 이 만든 정체와 EV 의 도착이 어긋난다.

    EV 생성기   진입 지점 (entry_veh 에 비례) · 목적지 (exit_share 로 하류로 걸으며 추첨)
    CTM 램프    ramp_demand_veh = entry_veh · exit_ratio = exit_share
    검증        시뮬레이션이 만든 구간 통과 대수를 실측과 비교
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from evdt.io.demand_profile import _complete_zones, _slice, peak_date

#: 진출 비율의 상한. 한 경계에서 모두 빠지는 일은 없다고 본다 (검지기 오차 방어).
MAX_EXIT_SHARE = 0.95


def entry_exit_profile(
    traffic: pd.DataFrame,
    *,
    period: str,
    direction: str,
    date: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """시간대별 진입·진출 추정.

    한 행 = (시간, 경계). 경계는 **측정된 두 콘존 사이**다.

    | 컬럼 | 뜻 |
    |---|---|
    | `hour` | 0~23 |
    | `offset_km` | 경계 위치 = 하류 콘존의 시작점 |
    | `gap_km` | 상류 콘존 끝에서 이 경계까지 (결측으로 건너뛴 거리) |
    | `volume_veh` | 이 경계에 도착하는 흐름 (상류 콘존의 교통량) |
    | `entry_veh` | 여기서 붙은 대수 (net) |
    | `exit_veh` | 여기서 빠진 대수 (net) |
    | `exit_share` | `exit_veh / volume_veh` — 지나가는 차가 여기서 빠질 확률 |

    코리도 진입점(offset 0)은 여기 들어 있지 않다. `corridor_entry_hourly` 가 준다.
    """

    day = pd.Timestamp(date) if date is not None else peak_date(traffic, period=period, direction=direction)
    d = _complete_zones(_slice(traffic, period, direction))
    d = d[d["date"] == day]

    if d.empty:
        raise ValueError(f"{direction} {period} {day:%Y-%m-%d} 에 쓸 수 있는 콘존이 없다")

    zones = (
        d.groupby(["offset_km_start", "offset_km_end"], as_index=False)
        .size()
        .sort_values("offset_km_start")
        .reset_index(drop=True)
    )
    hourly = (
        d.groupby(["hour", "offset_km_start"])["volume_veh"].sum()
        .unstack("offset_km_start")
        .reindex(columns=zones["offset_km_start"])
        .reindex(range(24))
        .fillna(0.0)
    )

    volume = hourly.to_numpy()                       # (시간, 콘존)
    delta = np.diff(volume, axis=1)                  # (시간, 경계)
    upstream = volume[:, :-1]

    # 경계는 하류 콘존이 시작하는 곳. 결측으로 건너뛴 거리를 같이 남긴다.
    boundary_km = zones["offset_km_start"].to_numpy()[1:]
    gap_km = boundary_km - zones["offset_km_end"].to_numpy()[:-1]

    entry = np.maximum(delta, 0.0)
    exit_veh = np.maximum(-delta, 0.0)
    exit_share = np.divide(
        exit_veh, upstream,
        out=np.zeros_like(exit_veh), where=upstream > 0,
    ).clip(0.0, MAX_EXIT_SHARE)

    hours = np.repeat(hourly.index.to_numpy(), len(boundary_km))

    return pd.DataFrame({
        "hour": hours.astype(int),
        "offset_km": np.tile(boundary_km, len(hourly)),
        "gap_km": np.tile(gap_km, len(hourly)),
        "volume_veh": upstream.ravel(),
        "entry_veh": entry.ravel(),
        "exit_veh": exit_veh.ravel(),
        "exit_share": exit_share.ravel(),
    })


def corridor_entry_hourly(
    traffic: pd.DataFrame,
    *,
    period: str,
    direction: str,
    date: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """코리도 시작점의 시간대별 진입. 컬럼 (hour, offset_km, entry_veh).

    첫 콘존의 교통량이 그대로 코리도 진입이다 (그 위에서 들어온 차는 우리 범위 밖).
    """

    day = pd.Timestamp(date) if date is not None else peak_date(traffic, period=period, direction=direction)
    d = _complete_zones(_slice(traffic, period, direction))
    d = d[d["date"] == day]
    first = d["offset_km_start"].min()
    head = d[d["offset_km_start"] == first]

    hourly = head.groupby("hour")["volume_veh"].sum().reindex(range(24), fill_value=0.0)

    return pd.DataFrame({
        "hour": hourly.index.astype(int),
        "offset_km": float(first),
        "entry_veh": hourly.to_numpy(),
    })


def entry_points(profile: pd.DataFrame, entry: pd.DataFrame) -> pd.DataFrame:
    """코리도 진입 + 중간 진입을 한 표로. EV 생성기가 진입 지점을 뽑을 때 쓴다."""

    mid = (profile[profile["entry_veh"] > 0][["hour", "offset_km", "entry_veh"]])

    return (
        pd.concat([entry[["hour", "offset_km", "entry_veh"]], mid], ignore_index=True)
        .sort_values(["hour", "offset_km"])
        .reset_index(drop=True)
    )


def sample_entry_offsets(
    n: int,
    hour: int,
    points: pd.DataFrame,
    rng: np.random.Generator,
) -> np.ndarray:
    """그 시간에 들어오는 차 n 대의 진입 지점. entry_veh 에 비례해 뽑는다."""

    rows = points[points["hour"] == hour]

    if rows.empty or rows["entry_veh"].sum() <= 0:
        raise ValueError(f"{hour}시에 진입 지점이 없다")

    weights = rows["entry_veh"].to_numpy(dtype=float)

    return rng.choice(rows["offset_km"].to_numpy(dtype=float), size=n, p=weights / weights.sum())


def sample_exit_offsets(
    entry_offsets: np.ndarray,
    hour: int,
    profile: pd.DataFrame,
    rng: np.random.Generator,
    *,
    corridor_end_km: float,
) -> np.ndarray:
    """진입 지점마다 목적지 하나. 하류 경계를 훑으며 `exit_share` 로 빠진다.

    끝까지 살아남으면 코리도 끝이 목적지다. 이 방식은 "지나가는 차가 각 IC 에서
    빠질 확률" 이라는 실측 비율을 그대로 쓰므로, 진입 지점이 어디든 같은 규칙이 적용된다.
    """

    rows = profile[profile["hour"] == hour].sort_values("offset_km")
    boundaries = rows["offset_km"].to_numpy(dtype=float)
    shares = rows["exit_share"].to_numpy(dtype=float)

    entry_offsets = np.asarray(entry_offsets, dtype=float)
    out = np.full(entry_offsets.shape, float(corridor_end_km))
    alive = np.ones(entry_offsets.shape, dtype=bool)

    for km, share in zip(boundaries, shares, strict=True):
        if share <= 0:
            continue
        # 아직 안 빠졌고 이 경계가 진입 지점보다 하류인 차만 후보
        candidate = alive & (km > entry_offsets)
        leaving = candidate & (rng.random(entry_offsets.shape) < share)
        out[leaving] = km
        alive &= ~leaving

    return out
