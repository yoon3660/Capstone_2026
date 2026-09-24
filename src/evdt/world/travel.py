"""통행시간 — "언제 출발해 어디서 어디까지 가면 몇 분 걸리나" (#56).

지금까지는 모든 차가 고정 속도(80 km/h)로 달렸다. 설 연휴의 핵심은 **도로가 막혀서
휴게소 도착이 늦어지고 몰리는 것**인데, 고정 속도로는 도착 시각이 틀리고 쏠림의
시간대가 어긋난다. CTM 이 낸 셀 속도로 통행시간을 계산하면 그게 들어온다.

두 구현이 같은 인터페이스를 쓴다 (`TravelTime`)

    ConstantSpeed    예전 방식. 시나리오 config 에서 고르면 그대로 재현된다
    CellSpeedField   CTM 이 낸 (시간 × 셀) 속도 격자

부르는 쪽(engine/ue.py)은 둘을 구분하지 않는다. 그래야 "CTM 을 켠 것 말고는 모두
같다" 는 비교가 된다.

왜 캐시가 필요한가
    UE 는 차마다 계획마다 통행시간을 여러 번 묻는다 (수만 대 × 계획 여러 개 ×
    반복). 물을 때마다 396개 셀을 하나씩 지나가면 파이썬에서 수억 번이 된다.
    그런데 **출발 지점은 언제나 셀 경계**다 — 휴게소는 반드시 셀 경계에 놓이고
    (T-17 규칙 2), 코리도 진입점도 경계다. 그래서 (출발 경계, 출발 시간칸) 마다
    하류 모든 경계까지의 누적 시간을 한 번만 계산해 두면 그 뒤로는 뺄셈이다.

막힌 셀
    속도가 0 이면 통행시간이 무한이 된다. 완전 정지는 CTM 에서 한 칸이 꽉 찼다는
    뜻이지 "영원히 못 간다" 가 아니므로 하한(MIN_SPEED_KMH)을 둔다. 이 하한에
    걸리는 칸이 많으면 결과를 믿으면 안 된다 — `jammed_share` 로 얼마나 걸렸는지
    셀 수 있게 해 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

#: 셀 속도가 이보다 낮아도 이 값으로 본다. 0 이면 통행시간이 무한이 된다.
MIN_SPEED_KMH = 1.0

EPS = 1e-9


class TravelTime(Protocol):
    """a 지점에서 b 지점까지, depart_min 에 출발했을 때 걸리는 분."""

    def minutes(self, from_km: float, to_km: float, depart_min: float) -> float: ...


@dataclass(frozen=True)
class ConstantSpeed:
    """고정 속도. 출발 시각과 무관하다 (예전 방식, 재현용)."""

    speed_kmh: float

    def minutes(self, from_km: float, to_km: float, depart_min: float = 0.0) -> float:
        return max(to_km - from_km, 0.0) / self.speed_kmh * 60.0


@dataclass
class CellSpeedField:
    """CTM 이 낸 (시간칸 × 셀) 속도 격자 위에서의 통행시간.

    edges_km    셀 경계. 길이 = 셀 수 + 1, 오름차순
    speeds_kmh  (시간칸, 셀) 격자
    bucket_min  시간칸 하나의 길이(분). cell_state 집계 간격과 같게 두면 격자를
                그대로 쓸 수 있다
    """

    edges_km: np.ndarray
    speeds_kmh: np.ndarray
    bucket_min: float = 5.0
    _cumulative: dict[tuple[int, int], np.ndarray] = field(
        default_factory=dict, repr=False, compare=False
    )
    _floored: int = field(default=0, repr=False, compare=False)
    _sampled: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.edges_km = np.asarray(self.edges_km, dtype=float)
        self.speeds_kmh = np.atleast_2d(np.asarray(self.speeds_kmh, dtype=float))

        n_cells = self.edges_km.size - 1

        if n_cells < 1:
            raise ValueError("셀 경계가 2개 이상이어야 한다")
        if np.any(np.diff(self.edges_km) <= 0):
            raise ValueError("셀 경계는 오름차순이어야 한다")
        if self.speeds_kmh.shape[1] != n_cells:
            raise ValueError(
                f"속도 격자의 셀 수가 경계와 맞지 않는다: {self.speeds_kmh.shape[1]} vs {n_cells}"
            )
        if self.bucket_min <= 0:
            raise ValueError("bucket_min 은 0보다 커야 한다")
        if not np.all(np.isfinite(self.speeds_kmh)):
            raise ValueError("속도 격자에 유한하지 않은 값이 있다")

    @property
    def length_km(self) -> float:
        return float(self.edges_km[-1])

    @property
    def jammed_share(self) -> float:
        """물어본 칸 중 속도 하한에 걸린 비율. 크면 통행시간을 믿으면 안 된다."""

        return self._floored / self._sampled if self._sampled else 0.0

    def _bucket(self, t_min: float) -> int:
        return int(np.clip(t_min // self.bucket_min, 0, self.speeds_kmh.shape[0] - 1))

    def _speed(self, bucket: int, cell: int) -> float:
        raw = self.speeds_kmh[bucket, cell]
        self._sampled += 1

        if raw < MIN_SPEED_KMH:
            self._floored += 1
            return MIN_SPEED_KMH

        return float(raw)

    def _cell_of(self, offset_km: float) -> int:
        """그 지점을 품은 셀. 경계 위의 점은 **하류 쪽** 셀로 본다 (진행 방향)."""

        return int(np.clip(np.searchsorted(self.edges_km, offset_km, side="right") - 1,
                           0, self.edges_km.size - 2))

    def _cum_from(self, boundary: int, bucket: int) -> np.ndarray:
        """경계 `boundary` 에서 시간칸 `bucket` 에 출발했을 때 각 하류 경계까지 누적 분."""

        key = (boundary, bucket)
        cached = self._cumulative.get(key)

        if cached is not None:
            return cached

        n_buckets = self.speeds_kmh.shape[0]
        cum = np.zeros(self.edges_km.size)
        elapsed = 0.0

        for cell in range(boundary, self.edges_km.size - 1):
            now = min(bucket + int(elapsed // self.bucket_min), n_buckets - 1)
            span = self.edges_km[cell + 1] - self.edges_km[cell]
            elapsed += span / self._speed(now, cell) * 60.0
            cum[cell + 1] = elapsed

        self._cumulative[key] = cum

        return cum

    def minutes(self, from_km: float, to_km: float, depart_min: float) -> float:
        """from_km 에서 depart_min 에 출발해 to_km 에 닿을 때까지의 분."""

        start = float(np.clip(from_km, 0.0, self.length_km))
        end = float(np.clip(to_km, 0.0, self.length_km))

        if end <= start + EPS:
            return 0.0

        now = float(depart_min)
        cell = self._cell_of(start)

        # 1. 출발점이 셀 한가운데면 그 셀의 남은 부분을 먼저 지난다
        if start > self.edges_km[cell] + EPS:
            reach = min(self.edges_km[cell + 1], end)
            now += (reach - start) / self._speed(self._bucket(now), cell) * 60.0

            if reach >= end - EPS:
                return now - depart_min

            cell += 1

        # 2. 여기서부터는 경계에서 경계로. 캐시가 받는다
        cum = self._cum_from(cell, self._bucket(now))
        last = self._cell_of(end)
        now += cum[last] - cum[cell]

        # 3. 도착점이 셀 한가운데면 마지막 조각
        if end > self.edges_km[last] + EPS:
            now += (end - self.edges_km[last]) / self._speed(self._bucket(now), last) * 60.0

        return now - depart_min


def speed_grid(
    speed_rows: list[np.ndarray],
) -> np.ndarray:
    """스텝마다 뽑아 둔 셀 속도를 (시간칸 × 셀) 격자로 쌓는다."""

    if not speed_rows:
        raise ValueError("속도 표본이 없다")

    return np.vstack([np.asarray(row, dtype=float) for row in speed_rows])
