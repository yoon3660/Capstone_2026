"""CTM 셀 분할의 기본 계산."""

import math


def split_anchor_intervals(
    anchors: list[float],
    min_cell_length_km: float,
) -> list[tuple[float, float]]:
    """인접한 앵커 사이를 최소 길이 이상인 셀로 균등 분할한다.

    반환값: [(셀 시작 offset, 셀 끝 offset), ...]
    """
    if min_cell_length_km <= 0:
        raise ValueError("최소 셀 길이는 0보다 커야 합니다.")

    if len(anchors) < 2:
        raise ValueError("앵커가 최소 2개 필요합니다.")

    anchors = sorted(set(float(x) for x in anchors))

    if not all(math.isfinite(x) for x in anchors):
        raise ValueError("앵커에 유효하지 않은 값이 있습니다.")

    cells = []

    for start, end in zip(anchors, anchors[1:], strict=False):
        gap = end - start

        # 짧은 앵커 구간은 임의로 처리하지 않고 병합 단계로 넘긴다.
        if gap < min_cell_length_km - 1e-9:
            raise ValueError(
                f"앵커 간격이 너무 짧습니다: "
                f"{start:.6f}~{end:.6f}km ({gap:.6f}km)"
            )

        # 예: 5.3km / 0.5km -> 10등분
        count = max(1, math.floor((gap + 1e-9) / min_cell_length_km))

        for i in range(count):
            cell_start = start + gap * i / count
            cell_end = end if i == count - 1 else start + gap * (i + 1) / count

            cells.append((cell_start, cell_end))

    return cells

def resolve_short_anchor_gaps(
    length_km: float,
    station_offsets: list[float],
    lane_change_offsets: list[float],
    min_cell_length_km: float,
) -> tuple[list[float], list[float]]:
    """휴게소 경계를 유지하면서 너무 가까운 차로 변경 경계를 제거한다.

    반환값:
        (최종 앵커 목록, 제거된 차로 변경 지점 목록)

    주의:
        차로수 보정은 이 함수에서 하지 않는다.
        제거된 경계가 포함된 셀의 차로수는 별도로 계산해야 한다.
    """
    if not math.isfinite(length_km) or length_km <= 0:
        raise ValueError("노선 길이가 올바르지 않습니다.")

    if not math.isfinite(min_cell_length_km) or min_cell_length_km <= 0:
        raise ValueError("최소 셀 길이가 올바르지 않습니다.")

    protected = {0.0, float(length_km)}

    for offset in station_offsets:
        offset = float(offset)

        if not math.isfinite(offset) or not 0 <= offset <= length_km:
            raise ValueError(f"휴게소 위치가 올바르지 않습니다: {offset}")

        protected.add(offset)

    changes = set()

    for offset in lane_change_offsets:
        offset = float(offset)

        if not math.isfinite(offset) or not 0 <= offset <= length_km:
            raise ValueError(f"차로 변경 위치가 올바르지 않습니다: {offset}")

        changes.add(offset)

    anchors = sorted(protected | changes)
    removed = []

    while True:
        short_index = None

        for i in range(len(anchors) - 1):
            gap = anchors[i + 1] - anchors[i]

            if gap < min_cell_length_km - 1e-9:
                short_index = i
                break

        if short_index is None:
            break

        left = anchors[short_index]
        right = anchors[short_index + 1]

        # 휴게소끼리 너무 가까우면 둘 다 제거할 수 없다.
        if left in protected and right in protected:
            raise ValueError(
                f"보호된 앵커 사이가 너무 짧습니다: "
                f"{left:.6f} ~ {right:.6f}km"
            )

        # 휴게소를 남기고 차로 변경 경계를 제거한다.
        if left in protected:
            target = right
        elif right in protected:
            target = left
        else:
            # 두 경계가 모두 차로 변경 지점인 상황은
            # 어느 쪽 차로수가 더 적은지 확인한 후 처리해야 한다.
            raise ValueError(
                f"차로 변경 경계끼리 너무 가깝습니다: "
                f"{left:.6f} ~ {right:.6f}km"
            )

        anchors.remove(target)
        removed.append(target)

    return anchors, removed

def assign_lanes_to_cells(
    cells: list[tuple[float, float]],
    lane_segments: list[dict],
) -> list[dict]:
    """각 셀과 겹치는 원본 구간 중 최소 차로수를 적용한다.

    lane_segments는 한 방향의 차로 프로파일만 전달한다.
    여러 차로수 구간에 걸치거나 추정값이 포함되면 assumed로 표시한다.
    """
    if not lane_segments:
        raise ValueError("차로 프로파일이 비어 있습니다.")

    result = []

    for start, end in cells:
        if not math.isfinite(start) or not math.isfinite(end) or start >= end:
            raise ValueError("셀 시작·끝 위치가 올바르지 않습니다.")

        overlapping = []
        covered_km = 0.0

        for segment in lane_segments:
            overlap = min(end, segment["offset_km_end"]) - max(
                start, segment["offset_km_start"]
            )

            if overlap > 1e-9:
                overlapping.append(segment)
                covered_km += overlap

        if not math.isclose(
            covered_km,
            end - start,
            rel_tol=0,
            abs_tol=1e-7,
        ):
            raise ValueError(
                f"차로 데이터가 셀 전체를 덮지 않습니다: {start}~{end}km"
            )

        lanes = min(int(segment["lanes"]) for segment in overlapping)

        same_lanes = all(
            int(segment["lanes"]) == lanes
            for segment in overlapping
        )

        all_measured = all(
            segment["lanes_source"] == "measured"
            for segment in overlapping
        )

        source = (
            "measured"
            if same_lanes and all_measured
            else "assumed"
        )

        result.append({
            "offset_km_start": start,
            "offset_km_end": end,
            "length_km": end - start,
            "lanes": lanes,
            "lanes_source": source,
        })

    return result