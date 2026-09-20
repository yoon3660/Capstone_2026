"""차로 프로파일의 판단 규칙 (#24 debug/lanes).

scripts/build_lane_profile.py 의 그래프 탐색(본선 링크 선택)은 표준노드링크 원본이
있어야 돌지만, 아래 규칙들은 DataFrame 만 있으면 검사할 수 있다. 그래서 스크립트가
아니라 여기에 둔다 — 차로수는 CTM 용량에 그대로 곱해지는 값이라 규칙이 조용히
바뀌면 안 된다 (tests/test_lane_profile.py).

여기 있는 규칙
    1. 연결로(램프) 제외      select_mainline_candidates
    2. 관측 교통량 교차검증    check_lanes_against_observed
    3. 최소 셀 길이 병합       merge_short_segments / max_dt_min
    그리고 프로파일 정리·검증  merge_adjacent_lane_segments / validate_lane_profile
"""

from __future__ import annotations

import math

import pandas as pd

EPS = 1e-9

#: 표준노드링크 CONNECT: '0' 이 본선, 그 외는 연결로(램프·분기)다.
MAINLINE_CONNECT = "0"

#: 최종 parquet 스키마
PROFILE_COLUMNS = [
    "offset_km_start",
    "offset_km_end",
    "lanes",
    "direction",
    "lanes_source",
]

#: 차로수 허용 범위. 경부 본선은 편도 2차로 아래로 내려가지 않는다.
MIN_MAINLINE_LANES = 2
MAX_MAINLINE_LANES = 6


def normalize_intervals(df: pd.DataFrame) -> pd.DataFrame:
    """링크를 이정축 기준 [작은 값, 큰 값] 구간으로 정규화한다."""

    result = df.copy()
    result["offset_km_start"] = result[["m_start", "m_end"]].min(axis=1).astype(float)
    result["offset_km_end"] = result[["m_start", "m_end"]].max(axis=1).astype(float)

    length = result["offset_km_end"] - result["offset_km_start"]
    return result[length > 1e-6].copy().reset_index(drop=True)


def covered_ranges(
    intervals: list[tuple[float, float]],
    *,
    eps: float = EPS,
) -> list[tuple[float, float]]:
    """겹치거나 맞닿은 구간을 합쳐 덮인 범위 목록을 만든다."""

    if not intervals:
        return []

    merged: list[list[float]] = []

    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + eps:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [(a, b) for a, b in merged]


def uncovered_ranges(
    intervals: list[tuple[float, float]],
    span: tuple[float, float],
    *,
    eps: float = EPS,
) -> list[tuple[float, float]]:
    """span 안에서 intervals 가 덮지 못한 구간."""

    span_start, span_end = span
    gaps: list[tuple[float, float]] = []
    cursor = span_start

    for start, end in covered_ranges(intervals, eps=eps):
        if start > cursor + eps:
            gaps.append((cursor, min(start, span_end)))
        cursor = max(cursor, end)

        if cursor >= span_end - eps:
            break

    if cursor < span_end - eps:
        gaps.append((cursor, span_end))

    return [(a, b) for a, b in gaps if b - a > eps]


def select_mainline_candidates(
    candidates: pd.DataFrame,
    *,
    mainline_connect: str = MAINLINE_CONNECT,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """연결로(램프)를 본선 후보에서 뺀다. 단, 본선만으로 안 덮이는 구간은 램프로 메운다.

    왜 필요한가
        표준노드링크에서 램프도 ROAD_NAME='경부고속도로' 를 달고 있고, 램프는 보통
        1~2차로다. 램프가 본선 후보로 뽑히면 그 구간 용량이 1/4 로 줄어 CTM 에
        **없던 병목**이 생긴다. 정체가 쏠림을 만드는 구조라, 이 오류 하나가
        "명절에 어디로 쏠리는가" 라는 결론을 통째로 오염시킨다.

    왜 전역 필터가 아닌가
        일부 구간은 본선이 CONNECT≠'0' 으로 코딩돼 있어서 전부 빼면 본선이 끊긴다.
        그래서 **본선 링크가 덮지 못한 이정 구간에 걸치는 램프만** 되살린다.
        되살린 링크에서 나온 차로수는 lanes_source='assumed' 로 표시한다.

    반환: (후보 DataFrame, 되살린 램프 요약 DataFrame)
    """

    work = normalize_intervals(candidates)

    if work.empty:
        return work, pd.DataFrame(columns=["link_id", "lanes", "offset_km_start", "offset_km_end"])

    connect = work["connect"].astype(str).str.strip()
    is_mainline = connect == str(mainline_connect)

    mainline = work[is_mainline].copy()
    ramps = work[~is_mainline].copy()

    mainline["lanes_source"] = "measured"

    if ramps.empty:
        return mainline.reset_index(drop=True), ramps

    span = (
        float(work["offset_km_start"].min()),
        float(work["offset_km_end"].max()),
    )
    gaps = uncovered_ranges(
        list(zip(mainline["offset_km_start"], mainline["offset_km_end"], strict=True)),
        span,
    )

    if not gaps:
        return mainline.reset_index(drop=True), ramps.iloc[0:0]

    def fills_gap(row: pd.Series) -> bool:
        return any(
            row["offset_km_start"] < gap_end - EPS and row["offset_km_end"] > gap_start + EPS
            for gap_start, gap_end in gaps
        )

    needed = ramps[ramps.apply(fills_gap, axis=1)].copy()
    needed["lanes_source"] = "assumed"

    selected = pd.concat([mainline, needed], ignore_index=True)
    return selected.reset_index(drop=True), needed


def merge_adjacent_lane_segments(profile: pd.DataFrame) -> pd.DataFrame:
    """이정축에서 맞닿고 lanes/direction/lanes_source 가 같은 구간을 병합한다."""

    if profile.empty:
        return profile.copy()

    ordered = profile.sort_values(
        ["direction", "offset_km_start", "offset_km_end"]
    ).reset_index(drop=True)

    merged: list[dict] = []

    for _, row in ordered.iterrows():
        current = row.to_dict()

        if not merged:
            merged.append(current)
            continue

        previous = merged[-1]
        same = (
            previous["direction"] == current["direction"]
            and int(previous["lanes"]) == int(current["lanes"])
            and previous.get("lanes_source") == current.get("lanes_source")
        )
        touching = abs(
            float(previous["offset_km_end"]) - float(current["offset_km_start"])
        ) < EPS

        if same and touching:
            previous["offset_km_end"] = current["offset_km_end"]
        else:
            merged.append(current)

    result = pd.DataFrame(merged)
    result["lanes"] = result["lanes"].astype(int)
    return result


def merge_short_segments(profile: pd.DataFrame, min_length_km: float) -> pd.DataFrame:
    """min_length_km 보다 짧은 구간을 이웃에 흡수시킨다.

    왜 필요한가
        차로수 변경점은 CTM 셀 경계(앵커)가 된다. 24.7m 짜리 구간이 그대로 남으면
        그 길이의 셀이 생기고, CFL 조건 때문에 Δt 가 1초 미만이어야 한다 (max_dt_min).
        그런 해상도로는 하루를 돌릴 수 없다.

    어떻게 흡수하는가
        짧은 구간은 **차로수가 더 적은 쪽 이웃**에 붙인다. 용량을 크게 잡는 쪽으로
        틀리면 없어야 할 병목이 사라져 정체를 놓치기 때문이다. 흡수된 구간을 포함한
        결과 구간은 원본 그대로가 아니므로 lanes_source='assumed' 로 내린다.
    """

    if profile.empty:
        return profile.copy()

    if min_length_km <= 0:
        raise ValueError(f"min_length_km 는 0보다 커야 합니다: {min_length_km}")

    out: list[pd.DataFrame] = []

    for direction, part in profile.groupby("direction", sort=True):
        rows = (
            part.sort_values("offset_km_start")
            .reset_index(drop=True)
            .to_dict("records")
        )

        while len(rows) > 1:
            lengths = [r["offset_km_end"] - r["offset_km_start"] for r in rows]
            shortest = min(range(len(rows)), key=lambda i: lengths[i])

            if lengths[shortest] >= min_length_km - EPS:
                break

            left = rows[shortest - 1] if shortest > 0 else None
            right = rows[shortest + 1] if shortest + 1 < len(rows) else None

            # 차로수가 적은 쪽으로 붙인다. 한쪽밖에 없으면 그쪽.
            if left is None:
                target = right
            elif right is None:
                target = left
            else:
                target = min(left, right, key=lambda r: (int(r["lanes"]), -r["offset_km_start"]))

            target["offset_km_start"] = min(
                target["offset_km_start"], rows[shortest]["offset_km_start"]
            )
            target["offset_km_end"] = max(
                target["offset_km_end"], rows[shortest]["offset_km_end"]
            )
            target["lanes_source"] = "assumed"
            rows.pop(shortest)

        out.append(pd.DataFrame(rows).assign(direction=direction))

    return merge_adjacent_lane_segments(pd.concat(out, ignore_index=True))


def max_dt_min(v_free_kmh: float, cell_length_km: float) -> float:
    """CFL 조건이 허용하는 최대 시간간격(분).

    한 스텝에 차가 셀 하나를 넘어가면 CTM 이 성립하지 않는다.
        v_free * dt <= cell_length
    """

    if v_free_kmh <= 0 or cell_length_km <= 0:
        raise ValueError("v_free 와 셀 길이는 0보다 커야 합니다")

    return cell_length_km / v_free_kmh * 60.0


def lane_change_points(profile: pd.DataFrame) -> pd.DataFrame:
    """차로수가 바뀌는 지점. CTM 셀 분할의 강제 경계(앵커)가 된다."""

    rows: list[dict] = []

    for direction, part in profile.groupby("direction", sort=True):
        ordered = part.sort_values("offset_km_start").reset_index(drop=True)

        for previous, current in zip(
            ordered.to_dict("records"), ordered.to_dict("records")[1:], strict=False
        ):
            if int(previous["lanes"]) == int(current["lanes"]):
                continue

            rows.append(
                {
                    "direction": direction,
                    "offset_km": float(current["offset_km_start"]),
                    "lanes_from": int(previous["lanes"]),
                    "lanes_to": int(current["lanes"]),
                }
            )

    return pd.DataFrame(rows, columns=["direction", "offset_km", "lanes_from", "lanes_to"])


def check_lanes_against_observed(
    profile: pd.DataFrame,
    traffic: pd.DataFrame,
    q_max_veh_h_lane: float,
) -> pd.DataFrame:
    """실측 교통량으로 차로수를 반증한다. 물리적으로 불가능한 구간을 돌려준다.

    한 차로가 1시간에 q_max_veh_h_lane 대를 넘길 수 없다. 그러니 그 구간에서
    실제로 관측된 최대 시간교통량 Q 는 lanes × q_max 를 넘을 수 없다.
        필요 차로수 = ceil(Q / q_max)
    이보다 적게 잡혀 있으면 차로수(또는 링크 선택)가 틀린 것이다.

    traffic: build_traffic 정리본 (direction, offset_km, volume_veh)
    """

    if q_max_veh_h_lane <= 0:
        raise ValueError("q_max_veh_h_lane 는 0보다 커야 합니다")

    observed = traffic.dropna(subset=["volume_veh"])
    rows: list[dict] = []

    for _, segment in profile.iterrows():
        inside = observed[
            (observed["direction"] == segment["direction"])
            & (observed["offset_km"] >= segment["offset_km_start"])
            & (observed["offset_km"] < segment["offset_km_end"])
        ]

        if inside.empty:
            continue

        peak = float(inside["volume_veh"].max())
        required = math.ceil(peak / q_max_veh_h_lane - 1e-9)

        if required <= int(segment["lanes"]):
            continue

        rows.append(
            {
                "direction": segment["direction"],
                "offset_km_start": float(segment["offset_km_start"]),
                "offset_km_end": float(segment["offset_km_end"]),
                "lanes": int(segment["lanes"]),
                "observed_peak_veh_h": peak,
                "lanes_required": required,
            }
        )

    return pd.DataFrame(
        rows,
        columns=[
            "direction",
            "offset_km_start",
            "offset_km_end",
            "lanes",
            "observed_peak_veh_h",
            "lanes_required",
        ],
    )


def validate_lane_profile(profile: pd.DataFrame) -> None:
    """스키마·차로수 범위·gap/overlap 을 검사한다. 어긋나면 멈춘다."""

    if list(profile.columns) != PROFILE_COLUMNS:
        raise RuntimeError(f"최종 profile 컬럼이 다릅니다: {list(profile.columns)}")

    if profile.empty:
        raise RuntimeError("최종 lane profile이 비어 있습니다.")

    if profile.isna().any().any():
        raise RuntimeError("최종 lane profile에 NULL이 있습니다.")

    if not profile["lanes_source"].isin(["measured", "assumed"]).all():
        raise RuntimeError("lanes_source 는 measured/assumed 만 허용합니다.")

    out_of_range = profile[
        ~profile["lanes"].between(MIN_MAINLINE_LANES, MAX_MAINLINE_LANES)
    ]

    if not out_of_range.empty:
        raise RuntimeError(
            f"본선 차로수가 {MIN_MAINLINE_LANES}~{MAX_MAINLINE_LANES} 범위를 벗어납니다. "
            "연결로(램프)가 본선으로 뽑혔을 가능성이 큽니다.\n"
            + out_of_range.to_string(index=False)
        )

    for direction, part in profile.groupby("direction", sort=True):
        ordered = part.sort_values("offset_km_start").reset_index(drop=True)

        for previous, current in zip(
            ordered.to_dict("records"), ordered.to_dict("records")[1:], strict=False
        ):
            diff = float(current["offset_km_start"]) - float(previous["offset_km_end"])

            if diff > EPS:
                raise RuntimeError(
                    f"{direction}: {previous['offset_km_end']:.3f}~"
                    f"{current['offset_km_start']:.3f} km 에 gap 이 있습니다."
                )

            if diff < -EPS:
                raise RuntimeError(
                    f"{direction}: {current['offset_km_start']:.3f} km 부근에 overlap 이 있습니다."
                )
