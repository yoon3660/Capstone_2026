"""경부고속도로 본선 차로수 프로파일을 생성한다.

입력:
    data/processed/lane_mapping_audit.parquet

출력:
    data/processed/lanes_gyeongbu.parquet

처리 흐름:
1. INCREASE/DECREASE 링크를 UP/DOWN으로 분리한다.
2. 각 방향에서 0~415 km를 덮는 주 연결 컴포넌트를 선택한다.
3. #23 중심선 이정축에서 단일 후보/중복 후보 구간을 계산한다.
4. 중복 구간은 F_NODE -> T_NODE 연결성으로 본선 경로를 찾는다.
5. 복수 경로만 중심선 snap/길이 오차/속도/CONNECT를 보조 기준으로 선택한다.
6. 선택된 링크가 하나의 비분기 본선인지 검증한다.
7. 링크의 LANES를 연속 프로파일로 변환하고 같은 차로수 구간을 병합한다.
8. 최종 parquet을 저장하고 차로수 변경 지점을 출력한다.

주의:
- LANES는 본선 선택 기준으로 사용하지 않는다.
- CONNECT=1 링크를 일괄 제거하지 않는다.
- 비싼 SHP -> 중심선 매핑은 다시 수행하지 않고 audit 캐시만 사용한다.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd
import yaml

from evdt.io import flow_params as fp
from evdt.io.lane_profile import (
    MAX_MAINLINE_LANES,
    MIN_MAINLINE_LANES,
    PROFILE_COLUMNS,
    check_lanes_against_observed,
    lane_change_points,
    max_dt_min,
    merge_adjacent_lane_segments,
    merge_short_segments,
    normalize_intervals,
    select_mainline_candidates,
    validate_lane_profile,
)
from evdt.paths import CONFIG_DIR, DATA_PROCESSED_DIR

PROCESSED_DIR = DATA_PROCESSED_DIR
FLOW_PARAMS_PATH = CONFIG_DIR / "flow_params.yaml"
TRAFFIC_PATH = PROCESSED_DIR / "traffic_gyeongbu.parquet"
CACHE_PATH = PROCESSED_DIR / "lane_mapping_audit.parquet"
OUTPUT_PATH = PROCESSED_DIR / "lanes_gyeongbu.parquet"

DIRECTIONS = ("UP", "DOWN")
MOVEMENT_TO_DIRECTION = {
    "INCREASE": "UP",
    "DECREASE": "DOWN",
}

REQUIRED_COLUMNS = {
    "link_id",
    "f_node",
    "t_node",
    "lanes",
    "max_spd",
    "connect",
    "m_start",
    "m_end",
    "movement",
    "max_snap_m",
    "source_length_m",
}

EPS = 1e-9
MAX_PATHS = 100


# ---------------------------------------------------------------------------
# 입력 / 공통 그래프 유틸
# ---------------------------------------------------------------------------

def load_audit(path: Path = CACHE_PATH) -> pd.DataFrame:
    """audit parquet을 읽고 필수 컬럼을 검증한다."""
    if not path.exists():
        raise FileNotFoundError(f"audit 파일이 없습니다: {path}")

    audit = pd.read_parquet(path)

    missing = REQUIRED_COLUMNS - set(audit.columns)
    if missing:
        raise RuntimeError(
            "lane_mapping_audit.parquet에 필요한 컬럼이 없습니다: "
            f"{sorted(missing)}"
        )

    return audit


def prepare_direction_candidates(audit: pd.DataFrame) -> pd.DataFrame:
    """SAME을 제외하고 이동 방향을 UP/DOWN으로 변환한다."""
    moving = audit[audit["movement"].isin(MOVEMENT_TO_DIRECTION)].copy()
    moving["direction"] = moving["movement"].map(MOVEMENT_TO_DIRECTION)
    return moving


def find_components(df: pd.DataFrame) -> list[list[int]]:
    """F_NODE/T_NODE를 무방향으로 보고 연결 컴포넌트를 찾는다."""
    node_to_links: dict[str, list[int]] = defaultdict(list)

    for idx, row in df.iterrows():
        node_to_links[str(row["f_node"])].append(idx)
        node_to_links[str(row["t_node"])].append(idx)

    visited: set[int] = set()
    components: list[list[int]] = []

    for start_idx in df.index:
        if start_idx in visited:
            continue

        queue = deque([start_idx])
        visited.add(start_idx)
        component: list[int] = []

        while queue:
            link_idx = queue.popleft()
            component.append(link_idx)
            row = df.loc[link_idx]

            for node in (str(row["f_node"]), str(row["t_node"])):
                for next_idx in node_to_links[node]:
                    if next_idx not in visited:
                        visited.add(next_idx)
                        queue.append(next_idx)

        components.append(component)

    return components


def get_main_component(df: pd.DataFrame) -> pd.DataFrame:
    """가장 넓은 이정 범위를 덮는 연결 컴포넌트를 1차 후보로 선택한다."""
    work = df.reset_index(drop=True).copy()
    components = find_components(work)

    if not components:
        raise RuntimeError("연결 컴포넌트를 찾지 못했습니다.")

    def component_span(indexes: list[int]) -> float:
        comp = work.loc[indexes]
        m_min = min(float(comp["m_start"].min()), float(comp["m_end"].min()))
        m_max = max(float(comp["m_start"].max()), float(comp["m_end"].max()))
        return m_max - m_min

    best_indexes = max(components, key=component_span)
    return work.loc[best_indexes].copy().reset_index(drop=True)


# ---------------------------------------------------------------------------
# 이정 coverage / overlap cluster
# ---------------------------------------------------------------------------

def merge_same_candidate_segments(coverage: pd.DataFrame) -> pd.DataFrame:
    """서로 붙어 있고 후보 링크 집합이 같은 coverage 구간을 병합한다."""
    if coverage.empty:
        return coverage.copy()

    merged: list[dict] = []

    for _, row in coverage.iterrows():
        current = row.to_dict()

        if not merged:
            merged.append(current)
            continue

        previous = merged[-1]
        same_candidates = previous["link_ids"] == current["link_ids"]
        touching = abs(
            float(previous["offset_km_end"]) - float(current["offset_km_start"])
        ) < EPS

        if same_candidates and touching:
            previous["offset_km_end"] = current["offset_km_end"]
            previous["length_km"] = (
                float(previous["offset_km_end"])
                - float(previous["offset_km_start"])
            )
        else:
            merged.append(current)

    return pd.DataFrame(merged)


def build_interval_coverage(df: pd.DataFrame) -> pd.DataFrame:
    """#23 이정축을 따라 각 구간의 활성 후보 링크 집합을 계산한다."""
    intervals = normalize_intervals(df)

    if intervals.empty:
        raise RuntimeError("coverage를 만들 링크가 없습니다.")

    start_events: dict[float, list[str]] = defaultdict(list)
    end_events: dict[float, list[str]] = defaultdict(list)

    for _, row in intervals.iterrows():
        link_id = str(row["link_id"])
        start = float(row["offset_km_start"])
        end = float(row["offset_km_end"])
        start_events[start].append(link_id)
        end_events[end].append(link_id)

    breakpoints = sorted(set(start_events) | set(end_events))
    active: set[str] = set()
    rows: list[dict] = []

    for current, next_point in zip(breakpoints, breakpoints[1:], strict=False):
        for link_id in end_events.get(current, []):
            active.discard(link_id)

        for link_id in start_events.get(current, []):
            active.add(link_id)

        length_km = next_point - current
        if length_km <= EPS:
            continue

        rows.append(
            {
                "offset_km_start": current,
                "offset_km_end": next_point,
                "length_km": length_km,
                "candidate_count": len(active),
                "link_ids": tuple(sorted(active)),
            }
        )

    coverage = pd.DataFrame(rows)
    if coverage.empty:
        raise RuntimeError("coverage 결과가 비어 있습니다.")

    return merge_same_candidate_segments(coverage)


def build_overlap_clusters(coverage: pd.DataFrame) -> pd.DataFrame:
    """서로 맞닿은 candidate_count>1 구간을 하나의 overlap cluster로 묶는다."""
    overlap = (
        coverage[coverage["candidate_count"] > 1]
        .sort_values("offset_km_start")
        .reset_index(drop=True)
    )

    columns = [
        "cluster_id",
        "offset_km_start",
        "offset_km_end",
        "length_km",
        "segment_count",
        "max_candidate_count",
        "link_count",
        "link_ids",
    ]

    if overlap.empty:
        return pd.DataFrame(columns=columns)

    clusters: list[dict] = []
    start = float(overlap.iloc[0]["offset_km_start"])
    end = float(overlap.iloc[0]["offset_km_end"])
    link_ids = set(overlap.iloc[0]["link_ids"])
    segment_count = 1
    max_candidates = int(overlap.iloc[0]["candidate_count"])

    for i in range(1, len(overlap)):
        row = overlap.iloc[i]
        row_start = float(row["offset_km_start"])
        row_end = float(row["offset_km_end"])

        if abs(row_start - end) < EPS:
            end = max(end, row_end)
            link_ids.update(row["link_ids"])
            segment_count += 1
            max_candidates = max(max_candidates, int(row["candidate_count"]))
            continue

        clusters.append(
            {
                "offset_km_start": start,
                "offset_km_end": end,
                "length_km": end - start,
                "segment_count": segment_count,
                "max_candidate_count": max_candidates,
                "link_count": len(link_ids),
                "link_ids": tuple(sorted(link_ids)),
            }
        )

        start = row_start
        end = row_end
        link_ids = set(row["link_ids"])
        segment_count = 1
        max_candidates = int(row["candidate_count"])

    clusters.append(
        {
            "offset_km_start": start,
            "offset_km_end": end,
            "length_km": end - start,
            "segment_count": segment_count,
            "max_candidate_count": max_candidates,
            "link_count": len(link_ids),
            "link_ids": tuple(sorted(link_ids)),
        }
    )

    result = pd.DataFrame(clusters)
    result.insert(0, "cluster_id", range(1, len(result) + 1))
    return result[columns]


def build_cluster_context(
    coverage: pd.DataFrame,
    clusters: pd.DataFrame,
) -> pd.DataFrame:
    """각 overlap cluster 바로 앞/뒤의 단일 후보 링크를 찾는다."""
    rows: list[dict] = []

    for _, cluster in clusters.iterrows():
        start = float(cluster["offset_km_start"])
        end = float(cluster["offset_km_end"])

        left = coverage[
            (coverage["candidate_count"] == 1)
            & (coverage["offset_km_end"] <= start + EPS)
        ].sort_values("offset_km_end", ascending=False)

        right = coverage[
            (coverage["candidate_count"] == 1)
            & (coverage["offset_km_start"] >= end - EPS)
        ].sort_values("offset_km_start")

        left_link = (
            str(left.iloc[0]["link_ids"][0])
            if not left.empty and len(left.iloc[0]["link_ids"]) == 1
            else None
        )
        right_link = (
            str(right.iloc[0]["link_ids"][0])
            if not right.empty and len(right.iloc[0]["link_ids"]) == 1
            else None
        )

        rows.append(
            {
                "cluster_id": int(cluster["cluster_id"]),
                "left_link": left_link,
                "right_link": right_link,
            }
        )

    return pd.DataFrame(rows)


def print_coverage_summary(
    coverage: pd.DataFrame,
    clusters: pd.DataFrame,
    direction: str,
) -> None:
    """진단에 필요한 coverage 통계만 간단히 출력한다."""
    single = coverage[coverage["candidate_count"] == 1]
    overlap = coverage[coverage["candidate_count"] > 1]
    gap = coverage[coverage["candidate_count"] == 0]

    total = float(coverage["length_km"].sum())
    single_km = float(single["length_km"].sum())
    overlap_km = float(overlap["length_km"].sum())
    gap_km = float(gap["length_km"].sum())

    print()
    print(f"=== {direction} coverage ===")
    print(f"전체 길이: {total:.3f} km")
    print(f"단일 후보: {single_km:.3f} km ({single_km / total * 100:.2f}%)")
    print(f"중복 후보: {overlap_km:.3f} km ({overlap_km / total * 100:.2f}%)")
    print(f"빈 구간  : {gap_km:.3f} km ({gap_km / total * 100:.4f}%)")
    print(f"overlap cluster: {len(clusters)}개")

    if not overlap.empty:
        print(f"최대 동시 후보 수: {int(overlap['candidate_count'].max())}")

    if not gap.empty:
        raise RuntimeError(
            f"{direction}: 후보가 없는 이정 구간이 {len(gap)}개 있습니다."
        )


# ---------------------------------------------------------------------------
# overlap cluster 경로 선택
# ---------------------------------------------------------------------------

def find_local_paths(
    candidate_df: pd.DataFrame,
    start_node: str,
    end_node: str,
    limit: int = MAX_PATHS,
) -> list[list[str]]:
    """candidate 링크만 이용해 start_node -> end_node의 directed path를 찾는다."""
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for _, row in candidate_df.iterrows():
        adjacency[str(row["f_node"])].append(
            (str(row["t_node"]), str(row["link_id"]))
        )

    paths: list[list[str]] = []
    stack: list[tuple[str, list[str], set[str]]] = [
        (str(start_node), [], {str(start_node)})
    ]

    while stack and len(paths) < limit:
        node, path_links, visited_nodes = stack.pop()

        if node == str(end_node):
            paths.append(path_links)
            continue

        for next_node, link_id in adjacency.get(node, []):
            if next_node in visited_nodes:
                continue

            stack.append(
                (
                    next_node,
                    path_links + [link_id],
                    visited_nodes | {next_node},
                )
            )

    return paths


def summarize_path(links: pd.DataFrame, path: list[str]) -> dict:
    """복수 경로 비교용 지표를 계산한다. LANES는 사용하지 않는다."""
    selected = links[links["link_id_str"].isin(path)].copy()

    if selected.empty and path:
        raise RuntimeError(f"경로 링크를 찾지 못했습니다: {path}")

    source_km = pd.to_numeric(
        selected["source_length_m"], errors="coerce"
    ) / 1000.0

    projected_km = (
        pd.to_numeric(selected["m_end"], errors="coerce")
        - pd.to_numeric(selected["m_start"], errors="coerce")
    ).abs()

    snaps = pd.to_numeric(selected["max_snap_m"], errors="coerce")
    speeds = pd.to_numeric(selected["max_spd"], errors="coerce")

    return {
        "path": " -> ".join(path),
        "link_count": len(path),
        "source_km": float(source_km.sum()),
        "projected_km": float(projected_km.sum()),
        "length_error_km": float((source_km - projected_km).abs().sum()),
        "max_snap_m": float(snaps.max()) if not snaps.empty else 0.0,
        "mean_snap_m": float(snaps.mean()) if not snaps.empty else 0.0,
        "min_speed": float(speeds.min()) if not speeds.empty else float("inf"),
        "low_speed_count": int((speeds < 80).sum()),
        "connect_1_count": int(selected["connect"].astype(str).eq("1").sum()),
    }


def choose_best_path(links: pd.DataFrame, paths: list[list[str]]) -> list[str]:
    """복수 경로 중 #23 중심선과 가장 잘 맞는 경로를 선택한다."""
    if not paths:
        raise RuntimeError("선택할 경로가 없습니다.")

    def score(path: list[str]) -> tuple:
        s = summarize_path(links, path)
        return (
            s["max_snap_m"],
            s["length_error_km"],
            s["low_speed_count"],
            s["mean_snap_m"],
            s["connect_1_count"],
        )

    return min(paths, key=score)


def find_endpoint_paths(
    candidate_df: pd.DataFrame,
    known_node: str,
    direction: str,
    missing_side: str,
    limit: int = MAX_PATHS,
) -> list[list[str]]:
    """corridor 끝단 cluster에서 가능한 경로를 찾는다.

    빈 경로 []도 허용한다. 이미 확정된 flank 링크가 끝단까지
    덮는 경우 추가 링크가 필요하지 않을 수 있기 때문이다.
    """
    indegree: dict[str, int] = defaultdict(int)
    outdegree: dict[str, int] = defaultdict(int)
    nodes: set[str] = set()

    for _, row in candidate_df.iterrows():
        f_node = str(row["f_node"])
        t_node = str(row["t_node"])
        nodes.update((f_node, t_node))
        outdegree[f_node] += 1
        indegree[t_node] += 1

    source_nodes = [
        node for node in nodes
        if indegree[node] == 0 and outdegree[node] > 0
    ]
    sink_nodes = [
        node for node in nodes
        if outdegree[node] == 0 and indegree[node] > 0
    ]

    upstream_missing = (
        (direction == "UP" and missing_side == "LEFT")
        or (direction == "DOWN" and missing_side == "RIGHT")
    )

    paths: list[list[str]] = []
    endpoint_nodes = source_nodes if upstream_missing else sink_nodes

    for endpoint_node in endpoint_nodes:
        remaining = limit - len(paths)
        if remaining <= 0:
            break

        if upstream_missing:
            found = find_local_paths(
                candidate_df,
                endpoint_node,
                str(known_node),
                limit=remaining,
            )
        else:
            found = find_local_paths(
                candidate_df,
                str(known_node),
                endpoint_node,
                limit=remaining,
            )

        paths.extend(found)

    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    for path in paths:
        key = tuple(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)

    return unique[:limit]


def choose_endpoint_path(
    links: pd.DataFrame,
    paths: list[list[str]],
    interior_link_id: str,
    missing_side: str,
    cluster_start: float,
    cluster_end: float,
) -> list[str]:
    """실제 corridor 끝까지 닿는 endpoint 후보만 남긴 뒤 선택한다."""
    link_lookup = {
        str(row["link_id"]): row
        for _, row in links.iterrows()
    }

    valid_paths: list[list[str]] = []

    for path in paths:
        check_ids = list(path)
        if interior_link_id not in check_ids:
            check_ids.append(str(interior_link_id))

        rows = [
            link_lookup[link_id]
            for link_id in check_ids
            if link_id in link_lookup
        ]
        if not rows:
            continue

        starts = [
            min(float(row["m_start"]), float(row["m_end"]))
            for row in rows
        ]
        ends = [
            max(float(row["m_start"]), float(row["m_end"]))
            for row in rows
        ]

        if missing_side == "LEFT":
            reaches_boundary = min(starts) <= cluster_start + 1e-6
        elif missing_side == "RIGHT":
            reaches_boundary = max(ends) >= cluster_end - 1e-6
        else:
            raise ValueError(f"알 수 없는 missing_side: {missing_side}")

        if reaches_boundary:
            valid_paths.append(path)

    if not valid_paths:
        raise RuntimeError("corridor 끝까지 도달하는 endpoint 경로가 없습니다.")

    if [] in valid_paths:
        return []

    if len(valid_paths) == 1:
        return valid_paths[0]

    return choose_best_path(links, valid_paths)


def _regular_cluster_endpoints(
    direction: str,
    left_row: pd.Series,
    right_row: pd.Series,
) -> tuple[str, str]:
    if direction == "UP":
        return str(left_row["t_node"]), str(right_row["f_node"])
    if direction == "DOWN":
        return str(right_row["t_node"]), str(left_row["f_node"])
    raise ValueError(f"알 수 없는 direction: {direction}")


def _endpoint_known_node(
    direction: str,
    missing_side: str,
    left_row: pd.Series | None,
    right_row: pd.Series | None,
) -> str:
    if missing_side == "LEFT":
        if right_row is None:
            raise RuntimeError("RIGHT flank가 없습니다.")
        return (
            str(right_row["f_node"])
            if direction == "UP"
            else str(right_row["t_node"])
        )

    if left_row is None:
        raise RuntimeError("LEFT flank가 없습니다.")
    return (
        str(left_row["t_node"])
        if direction == "UP"
        else str(left_row["f_node"])
    )


def resolve_clusters(
    main_component: pd.DataFrame,
    coverage: pd.DataFrame,
    clusters: pd.DataFrame,
    direction: str,
) -> set[str]:
    """모든 overlap cluster에서 실제 본선 링크를 선택한다."""
    if clusters.empty:
        return set()

    context = build_cluster_context(coverage, clusters)
    links = main_component.copy()
    links["link_id_str"] = links["link_id"].astype(str)

    link_lookup = {
        str(row["link_id"]): row
        for _, row in links.iterrows()
    }
    cluster_lookup = {
        int(row["cluster_id"]): row
        for _, row in clusters.iterrows()
    }

    selected: set[str] = set()
    unique_count = 0
    multiple_count = 0
    endpoint_count = 0

    print()
    print(f"=== {direction} overlap cluster 선택 ===")

    for _, ctx in context.iterrows():
        cluster_id = int(ctx["cluster_id"])
        cluster = cluster_lookup[cluster_id]

        left_link = ctx["left_link"]
        right_link = ctx["right_link"]
        left_row = link_lookup.get(str(left_link)) if left_link is not None else None
        right_row = link_lookup.get(str(right_link)) if right_link is not None else None

        candidate_ids = {str(x) for x in cluster["link_ids"]}
        candidate_df = links[links["link_id_str"].isin(candidate_ids)].copy()

        if left_row is not None and right_row is not None:
            start_node, end_node = _regular_cluster_endpoints(
                direction,
                left_row,
                right_row,
            )
            paths = find_local_paths(
                candidate_df,
                start_node,
                end_node,
                limit=MAX_PATHS,
            )

            if not paths:
                raise RuntimeError(
                    f"{direction} cluster #{cluster_id}: 연결 경로가 없습니다."
                )

            if len(paths) == 1:
                chosen = paths[0]
                unique_count += 1
                reason = "UNIQUE"
            else:
                chosen = choose_best_path(links, paths)
                multiple_count += 1
                reason = f"MULTIPLE({len(paths)})"

        else:
            if left_row is None and right_row is None:
                raise RuntimeError(
                    f"{direction} cluster #{cluster_id}: 양쪽 flank가 모두 없습니다."
                )

            missing_side = "LEFT" if left_row is None else "RIGHT"
            known_node = _endpoint_known_node(
                direction,
                missing_side,
                left_row,
                right_row,
            )

            paths = find_endpoint_paths(
                candidate_df,
                known_node,
                direction,
                missing_side,
                limit=MAX_PATHS,
            )

            if not paths:
                raise RuntimeError(
                    f"{direction} cluster #{cluster_id}: "
                    "끝단 연결 후보를 찾지 못했습니다."
                )

            interior_link_id = (
                str(right_link) if missing_side == "LEFT" else str(left_link)
            )
            chosen = choose_endpoint_path(
                links=links,
                paths=paths,
                interior_link_id=interior_link_id,
                missing_side=missing_side,
                cluster_start=float(cluster["offset_km_start"]),
                cluster_end=float(cluster["offset_km_end"]),
            )

            endpoint_count += 1
            reason = f"ENDPOINT-{missing_side}({len(paths)})"

        selected.update(map(str, chosen))

        if reason != "UNIQUE":
            chosen_text = " -> ".join(chosen) if chosen else "(추가 링크 없음)"
            print(
                f"cluster #{cluster_id:<3} {reason:<18} → {chosen_text}"
            )

    print(
        f"요약: UNIQUE {unique_count}, "
        f"MULTIPLE {multiple_count}, ENDPOINT {endpoint_count}"
    )

    return selected


def build_selected_link_ids(
    main_component: pd.DataFrame,
    coverage: pd.DataFrame,
    clusters: pd.DataFrame,
    direction: str,
) -> set[str]:
    """단일 후보 링크와 cluster 선택 링크를 합쳐 본선 링크 집합을 만든다."""
    selected: set[str] = set()

    single = coverage[coverage["candidate_count"] == 1]
    for link_ids in single["link_ids"]:
        selected.update(map(str, link_ids))

    selected.update(
        resolve_clusters(
            main_component,
            coverage,
            clusters,
            direction,
        )
    )
    return selected


# ---------------------------------------------------------------------------
# 본선 검증 / 주행 순서 정렬
# ---------------------------------------------------------------------------

def select_rows_by_ids(
    main_component: pd.DataFrame,
    selected_ids: set[str],
) -> pd.DataFrame:
    return (
        main_component[
            main_component["link_id"].astype(str).isin(selected_ids)
        ]
        .copy()
        .reset_index(drop=True)
    )


def mainline_graph_stats(selected: pd.DataFrame) -> dict:
    indegree: dict[str, int] = defaultdict(int)
    outdegree: dict[str, int] = defaultdict(int)
    nodes: set[str] = set()

    for _, row in selected.iterrows():
        f_node = str(row["f_node"])
        t_node = str(row["t_node"])
        nodes.update((f_node, t_node))
        outdegree[f_node] += 1
        indegree[t_node] += 1

    sources = [
        node for node in nodes
        if indegree[node] == 0 and outdegree[node] > 0
    ]
    sinks = [
        node for node in nodes
        if outdegree[node] == 0 and indegree[node] > 0
    ]
    branch_nodes = [
        node for node in nodes
        if indegree[node] > 1 or outdegree[node] > 1
    ]

    return {
        "sources": sources,
        "sinks": sinks,
        "branch_nodes": branch_nodes,
    }


def validate_selected_links(
    selected: pd.DataFrame,
    direction: str,
) -> None:
    """선택된 링크가 하나의 비분기 본선이고 LANES가 1~6인지 검증한다."""
    if selected.empty:
        raise RuntimeError(f"{direction}: 선택된 본선 링크가 없습니다.")

    components = find_components(selected)
    stats = mainline_graph_stats(selected)

    m_min = min(
        float(selected["m_start"].min()),
        float(selected["m_end"].min()),
    )
    m_max = max(
        float(selected["m_start"].max()),
        float(selected["m_end"].max()),
    )

    lanes = pd.to_numeric(selected["lanes"], errors="coerce")
    # 본선은 편도 2차로 아래로 내려가지 않는다. 1차로면 연결로(램프)가 섞인 것이다.
    invalid_lanes = (
        lanes.isna() | (lanes < MIN_MAINLINE_LANES) | (lanes > MAX_MAINLINE_LANES)
    )

    print()
    print(f"=== {direction} 최종 본선 검증 ===")
    print(f"선택 링크 수: {len(selected)}")
    print(f"연결 컴포넌트 수: {len(components)}")
    print(f"source 노드 수: {len(stats['sources'])}")
    print(f"sink 노드 수: {len(stats['sinks'])}")
    print(f"분기 노드 수: {len(stats['branch_nodes'])}")
    print(f"이정 범위: {m_min:.3f} ~ {m_max:.3f} km")
    print(f"{MIN_MAINLINE_LANES}~{MAX_MAINLINE_LANES} 범위 밖 LANES: {int(invalid_lanes.sum())}")

    errors = []
    if len(components) != 1:
        errors.append(f"component={len(components)}")
    if len(stats["sources"]) != 1:
        errors.append(f"source={len(stats['sources'])}")
    if len(stats["sinks"]) != 1:
        errors.append(f"sink={len(stats['sinks'])}")
    if stats["branch_nodes"]:
        errors.append(f"branch={len(stats['branch_nodes'])}")
    if invalid_lanes.any():
        errors.append(f"invalid_lanes={int(invalid_lanes.sum())}")

    if errors:
        raise RuntimeError(
            f"{direction}: 최종 본선 검증 실패 ({', '.join(errors)})"
        )

    print("본선 연결성/LANES 검증 통과")


def order_selected_links(
    selected: pd.DataFrame,
    direction: str,
) -> pd.DataFrame:
    """선택된 비분기 본선을 F_NODE -> T_NODE 주행 순서로 정렬한다."""
    stats = mainline_graph_stats(selected)

    if (
        len(stats["sources"]) != 1
        or len(stats["sinks"]) != 1
        or stats["branch_nodes"]
    ):
        raise RuntimeError(
            f"{direction}: 정렬 전에 본선 그래프 검증이 필요합니다."
        )

    work = selected.copy()
    work["f_node_str"] = work["f_node"].astype(str)
    work["t_node_str"] = work["t_node"].astype(str)

    outgoing: dict[str, int] = {}
    for idx, row in work.iterrows():
        f_node = row["f_node_str"]
        if f_node in outgoing:
            raise RuntimeError(f"{direction}: 분기 F_NODE 발견: {f_node}")
        outgoing[f_node] = idx

    current = stats["sources"][0]
    ordered_indexes: list[int] = []
    visited: set[int] = set()

    while current in outgoing:
        idx = outgoing[current]
        if idx in visited:
            raise RuntimeError(f"{direction}: cycle이 발견되었습니다.")

        visited.add(idx)
        ordered_indexes.append(idx)
        current = str(work.loc[idx, "t_node_str"])

    if len(ordered_indexes) != len(work):
        raise RuntimeError(
            f"{direction}: 주행 순서 정렬 실패 "
            f"({len(ordered_indexes)}/{len(work)} links)"
        )

    return work.loc[ordered_indexes].copy().reset_index(drop=True)


# ---------------------------------------------------------------------------
# LANES profile 생성 / 검증 / 저장
# ---------------------------------------------------------------------------

def _shared_boundaries(
    ordered: pd.DataFrame,
    direction: str,
) -> list[float]:
    """연속 링크의 공유 노드 이정을 평균값으로 맞춰 단일 경계를 만든다."""
    boundaries = [float(ordered.iloc[0]["m_start"])]
    mismatches_m: list[float] = []

    for i in range(len(ordered) - 1):
        previous_end = float(ordered.iloc[i]["m_end"])
        next_start = float(ordered.iloc[i + 1]["m_start"])
        mismatches_m.append(abs(previous_end - next_start) * 1000.0)
        boundaries.append((previous_end + next_start) / 2.0)

    boundaries.append(float(ordered.iloc[-1]["m_end"]))

    if direction == "UP":
        monotonic = all(
            boundaries[i + 1] >= boundaries[i] - EPS
            for i in range(len(boundaries) - 1)
        )
    elif direction == "DOWN":
        monotonic = all(
            boundaries[i + 1] <= boundaries[i] + EPS
            for i in range(len(boundaries) - 1)
        )
    else:
        raise ValueError(f"알 수 없는 direction: {direction}")

    if not monotonic:
        raise RuntimeError(
            f"{direction}: 선택된 링크의 이정 진행 방향이 일관되지 않습니다."
        )

    if mismatches_m:
        s = pd.Series(mismatches_m, dtype=float)
        print()
        print(f"=== {direction} 공유 노드 이정 오차 ===")
        print(
            f"평균 {s.mean():.3f} m / "
            f"P95 {s.quantile(0.95):.3f} m / "
            f"최대 {s.max():.3f} m"
        )

    return boundaries


def build_lane_profile(
    selected: pd.DataFrame,
    direction: str,
) -> pd.DataFrame:
    """주행 순서의 본선 링크를 최종 lane profile로 변환한다."""
    ordered = order_selected_links(selected, direction)
    boundaries = _shared_boundaries(ordered, direction)

    rows: list[dict] = []

    for i, (_, link) in enumerate(ordered.iterrows()):
        a = float(boundaries[i])
        b = float(boundaries[i + 1])
        start = min(a, b)
        end = max(a, b)

        if end - start <= EPS:
            continue

        rows.append(
            {
                "offset_km_start": start,
                "offset_km_end": end,
                "lanes": int(link["lanes"]),
                "direction": direction,
                "lanes_source": str(link.get("lanes_source", "measured")),
            }
        )

    profile = pd.DataFrame(rows)
    return merge_adjacent_lane_segments(profile)


def save_lane_profile(
    profiles: list[pd.DataFrame],
    output_path: Path = OUTPUT_PATH,
) -> pd.DataFrame:
    """방향별 profile을 합쳐 검증 후 parquet으로 저장한다."""
    config = yaml.safe_load(FLOW_PARAMS_PATH.read_text(encoding="utf-8"))
    defaults = config["defaults"]
    min_cell_km = float(defaults["min_cell_length_km"])
    q_max_lane = fp.q_per_lane(
        v_free_kmh=float(defaults["v_free_kmh"]),
        w_back_kmh=float(defaults["w_back_kmh"]),
        k_jam_veh_km_lane=float(defaults["k_jam_veh_km_lane"]),
    )

    final = pd.concat(profiles, ignore_index=True)[PROFILE_COLUMNS]

    # 차로수 변경점은 CTM 셀 경계가 된다. 셀이 너무 짧으면 CFL 조건 때문에
    # Δt 가 1초 미만이어야 해서 하루를 돌릴 수 없다.
    before = len(final)
    final = merge_short_segments(final, min_cell_km)
    print()
    print(
        f"최소 셀 길이 {min_cell_km * 1000:.0f}m 병합: {before} -> {len(final)}개 구간 "
        f"(Δt 상한 {max_dt_min(float(defaults['v_free_kmh']), min_cell_km):.2f}분)"
    )

    direction_rank = {"UP": 0, "DOWN": 1}
    final["_direction_rank"] = final["direction"].map(direction_rank)
    final = (
        final.sort_values(["_direction_rank", "offset_km_start"])
        .drop(columns="_direction_rank")
        .reset_index(drop=True)
    )

    validate_lane_profile(final)

    # 실측 교통량으로 반증: 한 차로가 q_max 를 넘길 수 없다.
    if TRAFFIC_PATH.exists():
        violations = check_lanes_against_observed(
            final,
            pd.read_parquet(TRAFFIC_PATH),
            q_max_lane,
        )

        if not violations.empty:
            print(violations.to_string(index=False))
            raise RuntimeError(
                f"관측 교통량이 차로수와 모순되는 구간 {len(violations)}개. "
                "연결로(램프)가 본선으로 뽑혔는지 확인할 것."
            )

        print(f"관측 교통량 교차검증 통과 (차로당 q_max {q_max_lane:,.0f} 대/h)")
    else:
        print(f"관측 교통량 파일이 없어 교차검증을 건너뜀: {TRAFFIC_PATH}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.to_parquet(output_path, index=False)

    print()
    print(f"저장 완료: {output_path}")
    print(f"최종 profile 행 수: {len(final)}")

    return final


# ---------------------------------------------------------------------------
# 방향별 파이프라인 / main
# ---------------------------------------------------------------------------

def process_direction(
    moving: pd.DataFrame,
    direction: str,
) -> pd.DataFrame:
    """한 방향의 후보 -> 본선 -> lane profile 전체 파이프라인."""
    part = moving[moving["direction"] == direction].copy()
    if part.empty:
        raise RuntimeError(f"{direction}: 후보 링크가 없습니다.")

    # 연결로(램프)를 본선 후보에서 뺀다. 본선이 안 덮는 구간만 램프로 메운다.
    part, ramp_fallback = select_mainline_candidates(part)

    main_component = get_main_component(part)

    m_min = min(
        float(main_component["m_start"].min()),
        float(main_component["m_end"].min()),
    )
    m_max = max(
        float(main_component["m_start"].max()),
        float(main_component["m_end"].max()),
    )

    print()
    print("=" * 16 + f" {direction} " + "=" * 16)
    print(f"본선 후보 링크 수: {len(part)} (램프 보충 {len(ramp_fallback)}개)")

    if not ramp_fallback.empty:
        print("  본선이 덮지 못해 되살린 연결로 링크:")
        for row in ramp_fallback.itertuples():
            print(
                f"    {row.link_id} {row.offset_km_start:.3f}~{row.offset_km_end:.3f} km "
                f"lanes={row.lanes} connect={row.connect}"
            )

    print(f"메인 컴포넌트 링크 수: {len(main_component)}")
    print(f"이정 범위: {m_min:.3f} ~ {m_max:.3f} km")

    coverage = build_interval_coverage(main_component)
    clusters = build_overlap_clusters(coverage)
    print_coverage_summary(coverage, clusters, direction)

    selected_ids = build_selected_link_ids(
        main_component,
        coverage,
        clusters,
        direction,
    )
    selected = select_rows_by_ids(main_component, selected_ids)

    validate_selected_links(selected, direction)
    return build_lane_profile(selected, direction)


def main() -> int:
    print("=== lane_mapping_audit.parquet 읽기 ===")
    audit = load_audit()
    print(f"전체 캐시 링크 수: {len(audit)}")

    moving = prepare_direction_candidates(audit)

    print()
    print("=== 방향별 후보 링크 ===")
    print(
        moving["direction"]
        .value_counts()
        .reindex(DIRECTIONS)
        .to_string()
    )

    profiles = [
        process_direction(moving, direction)
        for direction in DIRECTIONS
    ]

    final_profile = save_lane_profile(profiles)

    changes = lane_change_points(final_profile)
    print()
    print("========== 차로수 변경 지점 ==========")

    for direction in DIRECTIONS:
        part = changes[changes["direction"] == direction]
        print()
        print(f"--- {direction} ({len(part)}개) ---")

        for row in part.itertuples():
            print(f"{row.offset_km:.3f} km : {row.lanes_from} → {row.lanes_to}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
