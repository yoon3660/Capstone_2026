"""통행량 → 수요 프로파일: 언제 들어오고 어디까지 가는가 (UE 수요 입력).

    traffic_gyeongbu.parquet (콘존 × 날짜 × 시)
        → entry_hourly_volume   진입 콘존의 시간대별 교통량   (언제 들어오는가)
        → through_share         각 지점을 지나가는 비율       (어디까지 가는가)

왜 목적지가 필요한가
    쏠림은 "누가 어느 휴게소에 닿을 수 있는가" 에서 시작한다. 전원이 부산까지
    간다고 두면 모든 차가 같은 휴게소 후보를 갖고 같은 곳에서 충전이 필요해진다.
    실제로는 절반 가까이가 천안 전에 빠진다 (설 2026 하행 실측, 아래 참조).

지나가는 비율을 교통량으로 잡는 방법
    서울 진입 교통량을 1 로 두고 콘존별 일교통량을 나눈다. 도시 근처에서는 중간
    진입 차량 때문에 교통량이 다시 늘어나므로 **누적 최솟값**을 쓴다 — "서울에서
    출발해 여기까지 온 차" 의 상한이다. 중간 진입 차량은 모델에 없다 (전원 서울 출발).

    그래서 최솟값이 한 번 바닥을 친 뒤로는 비율이 평평하다 (황간 부근 이후 약 19%).
    그 차들이 대구에서 내리는지 부산까지 가는지는 교통량만으로는 모른다.
    → 평평한 구간의 차는 코리도 끝까지 간다고 둔다. 장거리 비중을 조금 크게
      잡는 보수적 방향이다 (충전 수요가 늘어난다).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


def _slice(traffic: pd.DataFrame, period: str, direction: str) -> pd.DataFrame:
    d = traffic[(traffic["period"] == period) & (traffic["direction"] == direction)]

    if d.empty:
        raise ValueError(f"교통량이 없다: period={period!r} direction={direction!r}")

    return d


def _complete_zones(d: pd.DataFrame) -> pd.DataFrame:
    """결측 없이 교통량이 있는 콘존만 (결측 콘존은 0 으로 합산돼 비율을 망친다)."""

    ok = d.groupby("conzone_id")["volume_veh"].agg(lambda s: s.notna().all() and (s > 0).all())
    return d[d["conzone_id"].isin(ok[ok].index)]


def entry_zone(traffic: pd.DataFrame, *, period: str, direction: str) -> str:
    """진입 콘존 = 결측 없는 콘존 중 offset 이 가장 작은 곳."""

    d = _complete_zones(_slice(traffic, period, direction))
    return str(d.sort_values("offset_km")["conzone_id"].iloc[0])


def peak_date(traffic: pd.DataFrame, *, period: str, direction: str) -> pd.Timestamp:
    """진입 콘존의 일교통량이 가장 많은 날 (귀성 최대일)."""

    d = _slice(traffic, period, direction)
    zone = entry_zone(traffic, period=period, direction=direction)
    daily = d[d["conzone_id"] == zone].groupby("date")["volume_veh"].sum()
    return pd.Timestamp(daily.idxmax())


def entry_hourly_volume(
    traffic: pd.DataFrame,
    *,
    period: str,
    direction: str,
    date: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """진입 콘존의 시간대별 교통량. 컬럼 (hour, volume_veh) — synthetic_ev.generate_evs 입력."""

    d = _slice(traffic, period, direction)
    day = pd.Timestamp(date) if date is not None else peak_date(traffic, period=period, direction=direction)
    zone = entry_zone(traffic, period=period, direction=direction)
    x = d[(d["conzone_id"] == zone) & (d["date"] == day)]

    if len(x) != 24:
        raise ValueError(f"{day.date()} {zone} 의 시간대가 24개가 아니다: {len(x)}개")

    return x.sort_values("hour")[["hour", "volume_veh"]].reset_index(drop=True)


def through_share(
    traffic: pd.DataFrame,
    *,
    period: str,
    direction: str,
    date: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """각 지점을 지나가는 비율 (진입 = 1, 단조 비증가). 컬럼 (offset_km, share)."""

    d = _complete_zones(_slice(traffic, period, direction))
    day = pd.Timestamp(date) if date is not None else peak_date(traffic, period=period, direction=direction)
    daily = (
        d[d["date"] == day]
        .groupby("offset_km")["volume_veh"].sum()
        .sort_index()
    )
    share = (daily / daily.iloc[0]).cummin().clip(upper=1.0)

    return pd.DataFrame({"offset_km": share.index.astype(float), "share": share.to_numpy()})


def sample_dest_offsets(
    n: int,
    profile: pd.DataFrame,
    rng: np.random.Generator,
    *,
    corridor_end_km: float,
) -> np.ndarray:
    """지나가는 비율에서 목적지 offset 을 뽑는다.

    u ~ U(0,1) 에 대해 share 가 처음으로 u 아래로 내려가는 콘존 구간에서 내린다
    (구간 안에서는 균등). 끝까지 u 아래로 안 내려가면 코리도 끝.
    """

    offsets: Sequence[float] = profile["offset_km"].to_list()
    shares: Sequence[float] = profile["share"].to_list()
    u = rng.random(n)
    out = np.empty(n)

    for i, ui in enumerate(u):
        out[i] = corridor_end_km

        for k in range(1, len(offsets)):
            if shares[k] < ui:
                lo, hi = offsets[k - 1], offsets[k]
                out[i] = lo + rng.random() * (hi - lo)
                break

    return out
