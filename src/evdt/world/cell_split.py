"""CTM 셀 분할 (T-17).

    (노선 길이, 휴게소 offset, 차로 프로파일) → 셀 목록 (offset, 길이, 차로수)

규칙
    1. 앵커 = 코리도 양 끝 + 휴게소 + 차로수 변경 지점.
       IC 는 앵커가 아니다 (서울 근처에서 간격이 너무 좁다). 좌표 보간에만 쓴다.
    2. 휴게소는 **반드시** 셀 경계에 둔다. 셀 한가운데 진출입로가 있으면 CTM 이
       그 유입·유출을 표현하지 못한다.
    3. 앵커 사이 간격 g 를 floor(g / L) 등분한다 (최소 1등분). L = 셀 목표 길이.
    4. 차로수 변경 지점은 이웃 앵커와 L 이상 떨어질 때만 앵커가 된다. 가까우면
       버리고, 그 셀의 차로수는 걸친 구간 중 **적은 쪽**을 쓴다.
    5. 휴게소끼리(또는 휴게소와 코리도 끝이) L 보다 가까우면 그 사이는 L 보다 짧은
       셀 **한 칸**이 된다. CFL 하한보다만 길면 허용한다.

셀 목표 길이와 CFL 하한은 다른 값이다
    CFL 하한   max(v_free, w_back) × dt  — 이보다 짧은 셀은 물리적으로 불가능하다.
               한 스텝에 차가 셀을 건너뛰고 정체파가 불가능한 속도로 퍼진다.
    목표 길이 L  계산량과 공간 해상도의 절충. 하한보다 충분히 커야 한다.

    예전에는 둘 다 `min_cell_length_km`(0.5 km) 하나로 읽었다. 그 값은 원래 차로
    프로파일 병합 임계값이었고, 그대로 셀 길이로 쓰니 셀이 전부 0.505 km 가 되어
    양방향 1,555 셀이 나왔다 (예상 432 의 3.6배). 그래서 키를 나눴다.

규칙 5 가 왜 필요한가
    서울만남의광장휴게소는 하행 기점(양재IC)에서 0.559 km 에 있다. 코리도 끝과
    휴게소가 둘 다 경계여야 하므로 0.559 km 셀이 생길 수밖에 없다. "모든 셀 ≥ L"
    을 고집하면 이 코리도는 분할 자체가 안 된다 (L = 0.8 부터 실패). 짧은 셀은
    CFL 만 지키면 물리적으로 문제없으므로 허용하고, 몇 개인지 보고한다.

순수 함수만 둔다. DB·파일을 읽지 않는다 (scripts/seed_cells.py 가 읽어서 넣는다).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

EPS = 1e-9


def cfl_min_cell_km(v_free_kmh: float, w_back_kmh: float, dt_min: float) -> float:
    """CFL 조건이 허용하는 가장 짧은 셀(km). 셀 = max(v_free, w_back) × dt 이상."""

    for name, value in (("v_free", v_free_kmh), ("w_back", w_back_kmh), ("dt", dt_min)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 는 0보다 커야 합니다: {value}")

    return max(v_free_kmh, w_back_kmh) * dt_min / 60.0


def lane_change_offsets(lane_segments: Sequence[dict]) -> list[float]:
    """한 방향 차로 프로파일에서 차로수가 바뀌는 지점."""

    ordered = sorted(lane_segments, key=lambda s: float(s["offset_km_start"]))

    return [
        float(current["offset_km_start"])
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if int(previous["lanes"]) != int(current["lanes"])
    ]


def place_anchors(
    length_km: float,
    station_offsets: Iterable[float],
    lane_change_offsets: Iterable[float],
    *,
    cell_length_km: float,
    cfl_floor_km: float,
) -> tuple[list[float], list[float]]:
    """셀 경계가 될 앵커를 정한다. 반환: (앵커, 버린 차로수 변경 지점).

    휴게소와 코리도 양 끝은 보호 앵커다 — 절대 버리지 않는다. 보호 앵커끼리
    CFL 하한보다 가까우면 셀을 만들 수 없으므로 멈춘다.

    차로수 변경 지점은 왼쪽에서 오른쪽으로 훑으며, 왼쪽 앵커와 오른쪽 보호 앵커 양쪽
    모두에서 L 이상 떨어질 때만 남긴다. 순서가 정해져 있어 결과가 결정적이다.
    """

    if not math.isfinite(length_km) or length_km <= 0:
        raise ValueError(f"노선 길이가 올바르지 않습니다: {length_km}")

    if cfl_floor_km <= 0 or cell_length_km < cfl_floor_km - EPS:
        raise ValueError(
            f"셀 목표 길이 {cell_length_km} km 가 CFL 하한 {cfl_floor_km:.3f} km 보다 짧습니다."
        )

    def checked(values: Iterable[float], what: str) -> set[float]:
        out = set()

        for value in values:
            value = float(value)

            if not math.isfinite(value) or not -EPS <= value <= length_km + EPS:
                raise ValueError(f"{what} 위치가 노선 밖입니다: {value} (노선 {length_km} km)")

            out.add(min(max(value, 0.0), length_km))

        return out

    protected = sorted({0.0, float(length_km)} | checked(station_offsets, "휴게소"))

    for left, right in zip(protected, protected[1:], strict=False):
        if right - left < cfl_floor_km - EPS:
            raise ValueError(
                f"보호 앵커(휴게소·코리도 끝) 사이 {left:.3f}~{right:.3f} km 가 "
                f"CFL 하한 {cfl_floor_km:.3f} km 보다 짧습니다. 셀을 만들 수 없습니다."
            )

    anchors = list(protected)
    dropped: list[float] = []

    for change in sorted(checked(lane_change_offsets, "차로수 변경") - set(protected)):
        left = max(a for a in anchors if a < change)
        right = min(p for p in protected if p > change)

        if change - left >= cell_length_km - EPS and right - change >= cell_length_km - EPS:
            anchors.append(change)
            anchors.sort()
        else:
            dropped.append(change)

    return anchors, dropped


def split_anchor_intervals(
    anchors: Sequence[float],
    *,
    cell_length_km: float,
    cfl_floor_km: float,
) -> list[tuple[float, float]]:
    """앵커 사이를 floor(g / L) 등분한다 (최소 1등분).

    g < L 인 간격은 한 칸으로 둔다 — place_anchors 가 그런 간격은 보호 앵커 사이에만
    남기므로, 휴게소를 경계에 두기 위한 짧은 셀이다. CFL 하한보다 짧으면 멈춘다.
    """

    points = sorted({float(a) for a in anchors})

    if len(points) < 2:
        raise ValueError("앵커가 최소 2개 필요합니다.")

    cells: list[tuple[float, float]] = []

    for start, end in zip(points, points[1:], strict=False):
        gap = end - start

        if gap < cfl_floor_km - EPS:
            raise ValueError(
                f"{start:.3f}~{end:.3f} km ({gap * 1000:.0f} m) 가 CFL 하한 "
                f"{cfl_floor_km * 1000:.0f} m 보다 짧습니다."
            )

        count = max(1, math.floor((gap + EPS) / cell_length_km))

        for i in range(count):
            cell_start = start + gap * i / count
            cell_end = end if i == count - 1 else start + gap * (i + 1) / count
            cells.append((cell_start, cell_end))

    return cells


def assign_lanes_to_cells(
    cells: Sequence[tuple[float, float]],
    lane_segments: Sequence[dict],
) -> list[dict]:
    """각 셀과 겹치는 차로 구간 중 **최소** 차로수를 적용한다.

    적은 쪽을 쓰는 이유: 차로수를 적게 잡은 실수는 실측 교통량으로 반증되지만,
    많게 잡은 실수는 아무것도 잡아내지 못한다 (debug/lanes). 여러 차로수에 걸치거나
    추정값이 섞이면 lanes_source='assumed' 로 내린다.
    """

    if not lane_segments:
        raise ValueError("차로 프로파일이 비어 있습니다.")

    result = []

    for start, end in cells:
        if not math.isfinite(start) or not math.isfinite(end) or start >= end:
            raise ValueError(f"셀 시작·끝 위치가 올바르지 않습니다: {start}~{end}")

        overlapping = []
        covered_km = 0.0

        for segment in lane_segments:
            overlap = min(end, segment["offset_km_end"]) - max(start, segment["offset_km_start"])

            if overlap > EPS:
                overlapping.append(segment)
                covered_km += overlap

        if not math.isclose(covered_km, end - start, rel_tol=0, abs_tol=1e-7):
            raise ValueError(f"차로 데이터가 셀 전체를 덮지 않습니다: {start}~{end} km")

        lanes = min(int(s["lanes"]) for s in overlapping)
        same_lanes = all(int(s["lanes"]) == lanes for s in overlapping)
        all_measured = all(s["lanes_source"] == "measured" for s in overlapping)

        result.append(
            {
                "offset_km_start": start,
                "offset_km_end": end,
                "length_km": end - start,
                "lanes": lanes,
                "lanes_source": "measured" if same_lanes and all_measured else "assumed",
            }
        )

    return result


def station_cell_index(cells: Sequence[dict], offset_km: float) -> int:
    """휴게소가 붙는 셀의 순번. **휴게소 지점에서 시작하는 셀**(하류 셀)이다.

    휴게소는 셀 경계에 있으므로 앞뒤 두 셀에 걸친다. 하나를 고르라면 하류 셀이다 —
    충전을 마친 차가 본선으로 **합류**하는 곳이 여기다. 휴게소로 **빠지는** 차는 그
    바로 앞 셀의 끝에서 나간다. CTM 을 연결할 때 이 규칙을 따를 것.

    휴게소가 코리도 끝에 있으면 하류 셀이 없으므로 끝나는 셀을 돌려준다.
    """

    for i, cell in enumerate(cells):
        if abs(float(cell["offset_km_start"]) - offset_km) < 1e-6:
            return i

    last = len(cells) - 1

    if last >= 0 and abs(float(cells[last]["offset_km_end"]) - offset_km) < 1e-6:
        return last

    raise ValueError(f"{offset_km} km 는 어떤 셀의 경계도 아닙니다.")


def build_direction_cells(
    length_km: float,
    station_offsets: Sequence[float],
    lane_segments: Sequence[dict],
    *,
    cell_length_km: float,
    cfl_floor_km: float,
) -> tuple[list[dict], list[float]]:
    """한 방향의 셀 전체. 반환: (셀 목록, 버린 차로수 변경 지점).

    scripts/seed_cells.py 가 부르는 입구. 여기서 만든 결과를 tests/test_cell_split.py
    가 완료조건대로 검사한다 — 실제 데이터 없이도 CI 에서 돈다.
    """

    anchors, dropped = place_anchors(
        length_km,
        station_offsets,
        lane_change_offsets(lane_segments),
        cell_length_km=cell_length_km,
        cfl_floor_km=cfl_floor_km,
    )
    intervals = split_anchor_intervals(
        anchors, cell_length_km=cell_length_km, cfl_floor_km=cfl_floor_km
    )

    return assign_lanes_to_cells(intervals, lane_segments), dropped
