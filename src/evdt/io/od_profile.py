"""실측 TCS OD 로 목적지를 뽑는다 — `P(목적지 | 진입지점)` (#99).

## 무엇을 고치나

지금까지 목적지는 `entry_exit.sample_exit_offsets` 가 뽑았다. 하류 경계를 훑으며
`exit_share` 확률로 빠지는 **위험률 모델**이고, docstring 이 가정을 직접 적어 뒀다:

> *"진입 지점이 어디든 같은 규칙이 적용된다"*

**실측으로 재 보니 틀렸고, 틀린 방향이 기점마다 다르다** (설·하행):

    진입        실측 평균   현재 모델    p50 실측 → 모델
    서울 (33%)    36.4      61.8 km    16.8 → 63.1  (3.8배 길다)
    천안          53.1      40.8 km    34.9 → 20.7  (너무 짧다)
    대전          54.5      74.5 km    23.3 → 38.8

전역 곡선 하나로 모든 기점을 동시에 맞출 수 없다 — **기점·목적지 결합이 빠진** 것이다.
서울이 전체 물량의 33% 라 총합 편향을 서울 오차가 끌고 간다. #97 에서 본
*"통행이 실측보다 61~71% 길다"* 의 뿌리가 여기다.

## ⚠ OD 에는 시간이 없다

    tcs_od_gyeongbu.parquet: period · date · start/end_office · direction · volume_veh · offsets
                             ^^^^ hour 가 없다 (일자별 집계)

**그래서 OD 는 "어디로 가나" 만 준다. "언제 타나" 는 그대로 VDS 가 준다**
(`volume_profile` · `entry_exit_profile` 의 시간대별 진입 대수). 흐리면 하루가
평평해지고 **명절 피크가 사라져 쏠림 자체가 없어진다.**

대가도 있다. 위험률 모델은 `exit_share` 가 시간대별이어서 **목적지가 시간에 따라
달라졌는데**, OD 는 일자별이라 그게 없어진다. 기점 결합을 얻고 시간 변화를 잃는
교환이고, 기점 쪽 오차가 3.8배였으므로 그쪽이 크다. **일자별이라는 것은 자료의
한계이지 설계 선택이 아니다.**

## 좌표 관례 — 진입 0 km 와 서울TG

모델의 코리도 진입점은 **offset 0.0 km**(양재)인데 가장 가까운 OD 기점은
**서울TG 12.941 km** 다. 12.94 km 떨어져 있고 **물량이 가장 크다**(하행 104,520대).

이건 오차가 아니라 **같은 진입을 다르게 부르는 것**이다 — TCS 는 요금소 거래만 보므로
코리도 시작점이라는 개념이 없다. 그래서 `od_profile` 을 켜면 **진입 위치를 OD 기점으로
맞춘다.** 그러면 통행거리가 실측과 같은 자(尺)로 재진다.

**다만 조용히 맞추지 않는다.** 맞춤 거리를 찍고, `max_snap_km` 를 넘으면 거부한다 —
먼 곳에 붙으면 그 차는 **다른 기점의 목적지 분포**를 쓰게 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

#: 진입 지점을 OD 기점에 맞출 때 허용하는 최대 거리.
#:
#: 실측으로 재 본 최악의 경우가 둘이다 (설 연휴):
#:   하행  0.000 km(양재) → 서울TG 12.941 km   물량 104,520대 — **코리도 진입점**이다.
#:         TCS 는 요금소 거래만 보므로 "코리도 시작" 이라는 개념이 없다. 같은 진입을
#:         다르게 부르는 것이라 맞추는 게 맞다
#:   상행  408.243 km → 391.216 km (17.027)     물량 560대 (0.1%) — 작지만 멀다
#:
#: 물량 가중 평균은 하행 4.46 · 상행 2.56 km 다. 18.0 은 위 둘을 담되 그보다 멀면
#: **다른 기점의 목적지 분포를 쓰는 셈**이라 거부하는 선이다.
#: ⚠ 이 값을 올릴 때는 **무엇이 걸렸는지 물량과 함께** 적을 것. 숫자만 올리면
#:   엉뚱한 기점에 붙은 차를 못 본다.
MAX_SNAP_KM = 18.0

REQUIRED = ("period", "direction", "start_offset_km", "end_offset_km", "volume_veh")


@dataclass(frozen=True)
class OdDestinations:
    """`P(목적지 | 기점)` 표. 기점마다 목적지 후보와 누적확률을 들고 있다.

    `origins` 는 **정렬되어 있다** — 진입 지점을 맞출 때 이진 탐색을 쓴다.
    """

    #: OD 기점 offset (정렬)
    origins: np.ndarray
    #: origins[i] 의 목적지 후보
    dests: tuple[np.ndarray, ...]
    #: origins[i] 의 누적확률 (마지막이 1.0)
    cdfs: tuple[np.ndarray, ...]
    period: str
    direction: str

    def snap(self, entry_offsets: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """진입 지점마다 가장 가까운 OD 기점의 색인과 그 거리."""

        entry = np.asarray(entry_offsets, dtype=float)
        # searchsorted 로 좌우 후보만 보고 고른다 (기점이 41개라 전수도 되지만,
        # 진입 대수가 수만이라 거리 행렬을 만들면 메모리를 쓴다)
        right = np.searchsorted(self.origins, entry)
        left = np.clip(right - 1, 0, len(self.origins) - 1)
        right = np.clip(right, 0, len(self.origins) - 1)
        d_left = np.abs(entry - self.origins[left])
        d_right = np.abs(entry - self.origins[right])
        take_left = d_left <= d_right
        idx = np.where(take_left, left, right)
        return idx, np.where(take_left, d_left, d_right)

    def sample(
        self,
        entry_offsets: np.ndarray,
        rng: np.random.Generator,
        *,
        max_snap_km: float = MAX_SNAP_KM,
    ) -> tuple[np.ndarray, np.ndarray]:
        """진입 지점을 OD 기점에 맞추고 목적지를 뽑는다.

        돌려주는 것은 **(맞춘 진입 offset, 목적지 offset)** 둘이다. 진입도 돌려주는
        이유는 맞춘 뒤의 위치를 써야 통행거리가 실측과 같은 자로 재지기 때문이다.
        """

        entry = np.asarray(entry_offsets, dtype=float)
        idx, snap_km = self.snap(entry)

        if len(snap_km) and snap_km.max() > max_snap_km:
            worst = int(np.argmax(snap_km))
            raise ValueError(
                f"\n[중단] 진입 지점을 OD 기점에 맞출 수 없다 "
                f"(최대 {snap_km.max():.2f} km > 한계 {max_snap_km:.2f} km).\n"
                f"  진입 {entry[worst]:.3f} km → 가장 가까운 OD 기점 "
                f"{self.origins[idx[worst]]:.3f} km\n"
                f"  그 차는 **다른 기점의 목적지 분포**를 쓰게 된다. OD 기간·방향이"
                f" 맞는지, 아니면 max_snap_km 를 올릴 근거가 있는지 확인하라 (#99)."
            )

        snapped = self.origins[idx]
        out = np.empty(len(entry), dtype=float)
        u = rng.random(len(entry))

        # 기점이 같은 것끼리 묶어서 한 번에 뽑는다
        for i in np.unique(idx):
            pick = idx == i
            where = np.searchsorted(self.cdfs[i], u[pick], side="right")
            out[pick] = self.dests[i][np.clip(where, 0, len(self.dests[i]) - 1)]

        return snapped, out

    def describe_snap(self, entry_offsets: np.ndarray, weights: np.ndarray | None = None) -> str:
        """맞춤 거리 한 줄 요약. 조용히 맞추지 않기 위해 호출자가 찍는다."""

        _, snap_km = self.snap(entry_offsets)
        if not len(snap_km):
            return "맞춘 진입 지점 없음"
        w = None if weights is None else np.asarray(weights, dtype=float)
        avg = float(np.average(snap_km, weights=w))
        return (
            f"OD 기점 맞춤: 중위 {np.median(snap_km):.3f} km · "
            f"평균 {avg:.3f} · 최대 {snap_km.max():.3f} "
            f"(한계 {MAX_SNAP_KM:.1f})"
        )


def load_od_profile(path: str | Path, period: str, direction: str, *,
                    accept_subset: bool = False) -> OdDestinations:
    """OD parquet 에서 이 기간·방향의 `P(목적지 | 기점)` 를 만든다.

    **기간·방향은 `validate_trip_length.od_subset` 과 같은 대응을 써야 한다** — 두
    곳이 갈라지면 검증은 설 연휴를 보고 모델은 평시를 쓰게 된다 (#97 에서 그랬다).

    날짜는 **합친다.** 시나리오는 하루지만 하루치 OD 는 표본이 얇다. 10일을 합쳐
    기간 전체의 비율로 쓴다.
    """

    od = pd.read_parquet(path)
    missing = [c for c in REQUIRED if c not in od.columns]
    if missing:
        raise ValueError(
            f"\n[중단] {path} 에 필요한 열이 없다: {missing}\n"
            f"  있는 열: {list(od.columns)}"
        )

    # ⚠ 우리가 **안 쓰는 열**이 있으면 조용히 버리지 않는다 (#62 hotfix).
    #
    # #62 의 산출물은 `departure_hour` (시간대) 와 `source` (측정/추정/예측) 를 들고
    # 온다. 이 함수는 날짜·시간을 전부 합쳐 **일별 측정 OD 처럼** 다루므로, 그냥
    # 넣으면 시간 정보가 사라지고 추정 통행이 섞인다 — **에러 없이.**
    #
    # 실제로 그랬다: #62 결과를 그대로 먹이니 평균 통행거리가 40.50 → 39.52 km 로
    # 조용히 바뀌었다. 쓸 거면 **알고 쓰라고** 요구한다.
    richer = [c for c in ("departure_hour", "source") if c in od.columns]
    if richer and not accept_subset:
        raise ValueError(
            f"\n[중단] 이 OD 는 {richer} 를 들고 있는데 이 함수는 그걸 쓰지 않는다.\n"
            "  날짜·시간을 합치고 period 로 걸러 **일부만** 쓰게 된다.\n"
            "  그래도 되면 accept_subset=True 로 명시하라 — 무엇이 빠지는지 찍어 준다."
        )

    whole = float(od["volume_veh"].sum())

    for col, want in (("period", period), ("direction", direction)):
        have = set(od[col].dropna().astype(str).unique())
        if want not in have:
            raise ValueError(
                f"\n[중단] OD 의 {col} 에 {want!r} 가 없다. 있는 값: {sorted(have)}"
            )
        # ⚠ NaN 은 `== want` 에서 조용히 떨어진다. 떨어뜨리기 전에 **센다** —
        #   #62 의 첫 산출물은 period 가 **보정일에서만** 비어 있어서, 이 필터가
        #   "실제로 적합한 날은 버리고 예측한 날만 남기는" 결과를 냈다
        blank = float(od.loc[od[col].isna(), "volume_veh"].sum())
        if blank > 0 and not accept_subset:
            raise ValueError(
                f"\n[중단] OD 의 {col} 이 비어 있는 행이 있다 ({blank:,.0f}대).\n"
                f"  {col}=={want!r} 로 거르면 그 행들은 **말없이 사라진다.**\n"
                "  어느 기간인지 모르는 통행을 섞을 수는 없다 — 생성 쪽을 고치거나"
                " accept_subset=True 로 명시하라."
            )
        od = od[od[col].astype(str) == want]

    if od.empty:
        raise ValueError(f"\n[중단] {period}·{direction} 에 해당하는 OD 행이 없다.")

    kept = float(od["volume_veh"].sum())
    if accept_subset and whole > 0:
        print(f"  [od_profile] {period}·{direction} 로 {kept:,.0f} / {whole:,.0f}대 "
              f"({kept / whole:.0%}) 를 쓴다"
              + (f" · 안 쓰는 열 {richer}" if richer else ""))

    agg = (
        od.groupby(["start_offset_km", "end_offset_km"], as_index=False)["volume_veh"]
        .sum()
    )
    agg = agg[agg["volume_veh"] > 0]

    # 기점과 종점이 같은 행은 통행이 아니다 (같은 영업소 재진입)
    agg = agg[agg["start_offset_km"] != agg["end_offset_km"]]

    if agg.empty:
        raise ValueError(f"\n[중단] {period}·{direction} OD 에 유효한 통행이 없다.")

    origins: list[float] = []
    dests: list[np.ndarray] = []
    cdfs: list[np.ndarray] = []

    for origin, grp in agg.groupby("start_offset_km"):
        grp = grp.sort_values("end_offset_km")
        v = grp["volume_veh"].to_numpy(dtype=float)
        origins.append(float(origin))
        dests.append(grp["end_offset_km"].to_numpy(dtype=float))
        cdfs.append(np.cumsum(v) / v.sum())

    order = np.argsort(origins)
    return OdDestinations(
        origins=np.asarray(origins, dtype=float)[order],
        dests=tuple(dests[i] for i in order),
        cdfs=tuple(cdfs[i] for i in order),
        period=period,
        direction=direction,
    )


#: `demand.period` (수집 라벨) → OD 의 period (분석 라벨).
#: `scripts/validate_tcs_od.py::PERIOD_ALIAS` 와 **같은 대응**이다 — 두 곳이 갈라지면
#: 검증은 설 연휴를 보고 모델은 평시를 쓴다. #97 이 바로 그 실패였다.
PERIOD_ALIAS = {
    "seollal2026": "holiday",
    "base202603": "normal",
}


def od_subset_for(cfg) -> tuple[str, str]:
    """시나리오 config → OD 의 (period, direction).

    **이름 문자열로 짐작하지 않는다.** `scenario_id` 는 `seollal_2026_down_base` 라서
    "base" 가 들어 있고, 그걸로 기간을 고르면 **설 연휴 시나리오가 평시 OD 를 쓴다.**
    그래서 선언된 값만 본다 — `demand.period` 와 `corridor_id`.

    `variant()` 가 `scenario_id` 뒤에 태그를 붙여도 이 둘은 안 바뀐다.
    """

    period_raw = str(cfg.demand.period)
    if period_raw not in PERIOD_ALIAS:
        raise ValueError(
            f"\n[중단] demand.period={period_raw!r} 에 대응하는 OD period 를 모른다.\n"
            f"  아는 값: {sorted(PERIOD_ALIAS)}\n"
            "  기간을 새로 추가했으면 PERIOD_ALIAS 에 같이 넣어라 (#99)."
        )

    corridor = str(cfg.corridor_id)
    if corridor.endswith("_up"):
        direction = "UP"
    elif corridor.endswith("_down"):
        direction = "DOWN"
    else:
        raise ValueError(
            f"\n[중단] corridor_id={corridor!r} 에서 방향을 못 읽는다 "
            "(_up / _down 으로 끝나야 한다)."
        )

    return PERIOD_ALIAS[period_raw], direction
