"""포털 콘존(VDS 구간)을 경부선 이정(offset_km)에 붙인다 (T-07 / T-08).

콘존에는 좌표가 없고 "안성IC-오산IC" 같은 이름만 있다. 그래서
    1. 양 끝 IC 이름을 도로공사 IC 목록(locationinfoIc, 전국)의 좌표에 붙이고
    2. 그 좌표를 노선에 투영해 누적거리 m 을 구한다.
    3. 좌표가 없거나(서울TG, 서울산IC, 옥산JC), 노선 순서를 거스르는 점은
       앞뒤 끝점 사이를 콘존 순서로 선형 보간한다.

도로공사 IC 목록 주의점
    - 같은 이름이 두 번 있다 (영천·김천·추풍령·구서·경주). 좌표가 다른 경우도 있어서
      이웃 끝점과 가장 가까운 후보를 고른다.
    - 포털 표기가 다르다: "서영천Hi" = 서영천IC, "북구미하이패스IC" = 북구미IC 등.
    - IC 와 JC 는 이름이 같아도 다른 점이다 (판교IC / 판교JC).
"""

from __future__ import annotations

import re
from collections import defaultdict

from evdt.io.ex_portal import Conzone
from evdt.io.route import MAX_OFF_ROUTE_KM, GyeongbuRoute

# 정규화로 풀리지 않는 끝점. 값은 정규화 결과 키다.
ENDPOINT_ALIASES = {
    # 통도사에는 일반 IC 와 하이패스 IC 가 따로 있다. 하이패스 IC 는 도로공사
    # 목록에 좌표가 없어서, 통도사IC 에 붙이지 않고(길이 0 콘존이 생긴다) 보간한다.
    "통도사Hi": "통도사하이패스:IC",
}


def normalize_endpoint(name: str) -> str:
    """IC 이름을 "기본이름:종류" 로. 종류는 IC / JC / TG.

    종류를 지우면 판교IC 와 판교JC 가 같은 점이 되어 길이 0 콘존이 생긴다.
    하이패스IC 는 IC 로 본다. 도로공사 목록 "옥산하이패스IC" = 포털 "옥산IC",
    "서영천IC" = "서영천Hi", "북구미IC" = "북구미하이패스IC".
    """

    name = re.sub(r"\s+", "", name)

    if name in ENDPOINT_ALIASES:
        return ENDPOINT_ALIASES[name]

    kind = "IC"

    for pattern, found in (
        (r"(분기점|JCT|JC)$", "JC"),
        (r"(TG|T/G|톨게이트|요금소)$", "TG"),
        (r"(나들목|IC|Hi)$", "IC"),
    ):
        if re.search(pattern, name):
            kind = found
            name = re.sub(pattern, "", name)
            break

    name = re.sub(r"하이패스$", "", name)

    return f"{name}:{kind}"


def _ic_index(ics: list[dict]) -> dict[str, list[tuple[float, float]]]:
    index: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for ic in ics:
        try:
            point = (float(ic["yValue"]), float(ic["xValue"]))
        except (KeyError, TypeError, ValueError):
            continue

        key = normalize_endpoint(ic.get("icName", ""))

        if point not in index[key]:
            index[key].append(point)

    return index


def locate_conzones(
    conzones: list[Conzone],
    route: GyeongbuRoute,
    ics: list[dict],
) -> list[dict]:
    """한 방향 콘존 목록(진행 순서)을 이정에 붙인다.

    반환 행: conzone_id, name, direction, seq,
             offset_km_start, offset_km_end, offset_km (가운데),
             position_source ("ic" | "interpolated"), in_corridor
    """

    if not conzones:
        return []

    direction = conzones[0].direction
    index = _ic_index(ics)

    # 끝점 체인: P0, P1, ..., Pn  (콘존 i = P_i → P_{i+1})
    names = [conzones[0].endpoints[0]] + [c.endpoints[1] for c in conzones]

    # 앞 콘존의 끝과 뒤 콘존의 시작이 같아야 체인 보간이 맞다.
    for prev, cur in zip(conzones, conzones[1:], strict=False):
        if normalize_endpoint(prev.endpoints[1]) != normalize_endpoint(cur.endpoints[0]):
            raise RuntimeError(
                f"콘존 체인이 이어지지 않습니다: {prev.name} / {cur.name}"
            )

    # 1. 후보 좌표 → m (노선 위에 있는 후보만)
    candidates: list[list[float]] = []

    for name in names:
        ms = []
        for lat, lon in index.get(normalize_endpoint(name), []):
            m, dist = route.project(lat, lon)
            if dist <= MAX_OFF_ROUTE_KM:
                ms.append(m)
        candidates.append(ms)

    # 2. 후보가 하나뿐인 점을 먼저 확정하고, 여럿이면 가장 가까운 확정점 쪽 후보.
    #    확정점이 퍼져 나가도록 바뀌는 게 없을 때까지 반복한다.
    chosen: list[float | None] = [ms[0] if len(ms) == 1 else None for ms in candidates]

    changed = True
    while changed:
        changed = False
        for i, ms in enumerate(candidates):
            if len(ms) <= 1 or chosen[i] is not None:
                continue

            known_idx = [j for j, m in enumerate(chosen) if m is not None]
            if not known_idx:
                continue

            nearest = min(known_idx, key=lambda j: abs(j - i))
            chosen[i] = min(ms, key=lambda m: abs(m - chosen[nearest]))
            changed = True

    # 3. 진행 방향으로 m 이 단조여야 한다. 거스르는 점은 버리고 보간한다.
    #    DOWN 은 양재→구서 로 가므로 m 이 줄어든다.
    sign = 1.0 if direction == "UP" else -1.0
    known = [i for i, m in enumerate(chosen) if m is not None]

    changed = True
    while changed:
        changed = False
        for a, b, c in zip(known, known[1:], known[2:], strict=False):
            ma, mb, mc = chosen[a], chosen[b], chosen[c]
            if sign * (mb - ma) < 0 or sign * (mc - mb) < 0:
                # 가운데 점이 양쪽과 어긋나면 가운데를, 아니면 뒤쪽을 버린다
                drop = b if sign * (mc - ma) >= 0 else c
                chosen[drop] = None
                known.remove(drop)
                changed = True
                break

    source = ["ic" if m is not None else "interpolated" for m in chosen]

    # 4. 보간. 양 끝 바깥(코리도 밖)은 None 으로 남긴다.
    for i, m in enumerate(chosen):
        if m is not None:
            continue
        left = max((j for j in known if j < i), default=None)
        right = min((j for j in known if j > i), default=None)
        if left is None or right is None:
            continue
        t = (i - left) / (right - left)
        chosen[i] = chosen[left] + t * (chosen[right] - chosen[left])

    rows = []

    for i, conzone in enumerate(conzones):
        m_start, m_end = chosen[i], chosen[i + 1]

        if m_start is None or m_end is None:
            rows.append(
                {
                    "conzone_id": conzone.conzone_id,
                    "name": conzone.name,
                    "direction": direction,
                    "seq": conzone.seq,
                    "offset_km_start": None,
                    "offset_km_end": None,
                    "offset_km": None,
                    "position_source": "unknown",
                    "in_corridor": False,
                }
            )
            continue

        start = route.to_direction(m_start, direction)
        end = route.to_direction(m_end, direction)
        mid = (start + end) / 2

        rows.append(
            {
                "conzone_id": conzone.conzone_id,
                "name": conzone.name,
                "direction": direction,
                "seq": conzone.seq,
                "offset_km_start": round(start, 3),
                "offset_km_end": round(end, 3),
                "offset_km": round(mid, 3),
                "position_source": (
                    "ic" if source[i] == source[i + 1] == "ic" else "interpolated"
                ),
                "in_corridor": 0.0 <= mid <= route.length_km,
            }
        )

    return rows

