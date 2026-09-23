"""경부선 노선 좌표계 — 위경도를 offset_km 로 바꾼다.

도로중심선(T-09a, #23) 으로 만든 노선 폴리라인(구서IC → 양재IC, 415.058 km) 위에
임의의 점을 투영해 이정을 구한다. 휴게소·VDS 구간·CTM 셀이 모두 같은
좌표계를 쓰게 하려는 것이 목적이다 (설계 규칙 3).

    UP   offset_km = m          (구서IC 기점)
    DOWN offset_km = L - m      (양재IC 기점)

⚠ 노선은 하나뿐이어야 한다 (#51)
    예전에는 IC·휴게소 좌표를 직선으로 이은 노선(점 91개, 392.978 km)이 있었다. 굽은
    도로를 짧게 재서 남쪽 휴게소가 최대 21 km 앞당겨졌다. #23 에서 중심선으로 바꿨지만
    파일을 다시 만들지 않은 기기는 옛 노선을 계속 썼고, 읽는 쪽이 검사하지 않아 아무도
    몰랐다. 그래서 load() 가 노선의 출처와 길이를 검사하고, 틀리면 멈춘다.

노선은 scripts/build_route.py 가 한 번 만들어 data/processed/gyeongbu_route.json
에 저장하고, 휴게소 적재(load_chargers)와 교통량·속도 정리(build_traffic)가
모두 이 파일을 읽는다. 폴리라인은 넣는 점 집합에 따라 총연장이 1~2km 씩
달라지므로, 각자 만들면 휴게소와 VDS 구간이 서로 다른 좌표계에 놓인다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import cos, radians, sqrt
from pathlib import Path

from evdt.paths import DATA_PROCESSED_DIR

ROUTE_PATH = DATA_PROCESSED_DIR / "gyeongbu_route.json"

# 투영점이 노선에서 이만큼 넘게 떨어지면 노선 위의 점이 아니다.
MAX_OFF_ROUTE_KM = 2.0

#: 노선의 출처. build_route.py 가 meta 에 적는다
CENTERLINE_SOURCE = "centerline"

#: 중심선 기준 구서IC~양재IC 길이 (docs/T09a_centerline.md §4). 중심선 원본이 바뀌면 여기도 바꾼다
EXPECTED_ROUTE_KM = 415.058
ROUTE_LENGTH_TOL_KM = 0.01

REBUILD_STEPS = (
    "python scripts/load_centerline.py data/raw/ETC_S0_07_04_345774.csv\n"
    "    python scripts/build_route.py\n"
    "    python scripts/seed_corridor.py\n"
    "    python scripts/load_chargers.py\n"
    "    python scripts/build_traffic.py\n"
    "    python scripts/build_demand_profile.py   (하행·상행 모두)\n"
    "  (셀을 쓰면 build_lane_profile.py → estimate_flow_params.py → seed_cells.py --replace 도)"
)


class RouteOutdatedError(RuntimeError):
    """노선 파일이 중심선 기준이 아니다. 옛 노선으로 계산한 거리는 전부 틀린다."""


def _local_xy(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
    """작은 범위의 등거리 근사 (km). 선분 하나(10km 내외) 안에서 쓰기에 충분하다."""

    return lon * 111.320 * cos(radians(ref_lat)), lat * 110.574


@dataclass(frozen=True)
class GyeongbuRoute:
    points: tuple[tuple[float, float], ...]   # 구서IC → 양재IC 순서
    mileposts: tuple[float, ...]              # 각 점의 누적거리 (km)

    @property
    def length_km(self) -> float:
        return self.mileposts[-1]

    def save(self, path: Path = ROUTE_PATH, **meta: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "meta": meta,
                    "length_km": self.length_km,
                    "points": [list(p) for p in self.points],
                    "mileposts": list(self.mileposts),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path = ROUTE_PATH, *, validate: bool | None = None) -> GyeongbuRoute:
        """노선 파일을 읽는다. 프로젝트 노선(ROUTE_PATH)이면 중심선 기준인지 검사한다.

        validate: None 이면 ROUTE_PATH 일 때만 검사한다 (테스트의 임시 노선은 검사하지 않는다).
        """

        if not path.exists():
            raise FileNotFoundError(
                f"노선 파일이 없습니다: {path}\n"
                "먼저 실행할 것:  python scripts/build_route.py"
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        route = cls(
            points=tuple((lat, lon) for lat, lon in data["points"]),
            mileposts=tuple(data["mileposts"]),
        )

        if validate if validate is not None else path.resolve() == ROUTE_PATH.resolve():
            check_centerline_route(route, data.get("meta") or {}, path)

        return route

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """(m, 노선까지 거리 km). m 은 구서IC 에서의 누적거리."""

        best: tuple[float, float] | None = None

        for k in range(len(self.points) - 1):
            (lat_a, lon_a), (lat_b, lon_b) = self.points[k], self.points[k + 1]
            ref = (lat_a + lat_b) / 2
            ax, ay = _local_xy(lat_a, lon_a, ref)
            bx, by = _local_xy(lat_b, lon_b, ref)
            px, py = _local_xy(lat, lon, ref)

            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            cx, cy = ax + t * dx, ay + t * dy
            dist = sqrt((px - cx) ** 2 + (py - cy) ** 2)

            m = self.mileposts[k] + t * (self.mileposts[k + 1] - self.mileposts[k])

            if best is None or dist < best[1]:
                best = (m, dist)

        assert best is not None
        return best

    def offset_of(self, lat: float, lon: float, direction: str) -> float:
        """위경도를 방향별 offset_km 로. 노선에서 벗어난 점이면 멈춘다."""

        m, dist = self.project(lat, lon)

        if dist > MAX_OFF_ROUTE_KM:
            raise ValueError(
                f"노선에서 {dist:.1f}km 떨어진 점입니다: ({lat}, {lon})"
            )

        return self.to_direction(m, direction)

    def to_direction(self, m: float, direction: str) -> float:
        if direction == "UP":
            return m
        if direction == "DOWN":
            return self.length_km - m
        raise ValueError(f"알 수 없는 방향: {direction}")


def check_centerline_route(route: GyeongbuRoute, meta: dict, path: Path) -> None:
    """중심선으로 만든 노선인지, 길이가 기준과 같은지. 아니면 다시 만드는 순서를 알려주고 멈춘다."""

    problems = []

    if meta.get("source") != CENTERLINE_SOURCE:
        problems.append(f"출처가 중심선이 아니다 (meta.source={meta.get('source')!r}, 점 {len(route.points)}개)")

    if abs(route.length_km - EXPECTED_ROUTE_KM) > ROUTE_LENGTH_TOL_KM:
        problems.append(f"길이 {route.length_km:.3f} km ≠ 기준 {EXPECTED_ROUTE_KM} km")

    if problems:
        raise RouteOutdatedError(
            f"노선 파일이 도로중심선 기준이 아니다: {path}\n  - " + "\n  - ".join(problems) + "\n"
            "옛 노선으로 계산한 휴게소·콘존·셀 거리는 전부 틀린다 (#51). 다시 만들 것:\n    "
            + REBUILD_STEPS
        )
