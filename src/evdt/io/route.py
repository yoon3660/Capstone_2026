"""경부선 노선 좌표계 — 위경도를 offset_km 로 바꾼다.

T-06 의 route_mileposts 로 만든 노선 폴리라인(구서IC → 양재IC) 위에
임의의 점을 투영해 이정을 구한다. 휴게소·VDS 구간·CTM 셀이 모두 같은
좌표계를 쓰게 하려는 것이 목적이다 (설계 규칙 3).

    UP   offset_km = m          (구서IC 기점)
    DOWN offset_km = L - m      (양재IC 기점)

T-09a 의 offset_of() 가 이 역할을 맡기로 되어 있었으나 아직 없어서
T-07 에서 먼저 만든다. 셀 분할도 이 함수를 쓰면 된다.

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

from evdt.io.charger_ingest import route_mileposts
from evdt.paths import DATA_PROCESSED_DIR

ROUTE_PATH = DATA_PROCESSED_DIR / "gyeongbu_route.json"

# 투영점이 노선에서 이만큼 넘게 떨어지면 노선 위의 점이 아니다.
MAX_OFF_ROUTE_KM = 2.0


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

    @classmethod
    def build(
        cls,
        origins: dict[str, tuple[float, float]],
        points: set[tuple[float, float]],
        waypoints: list[dict] | None = None,
    ) -> GyeongbuRoute:
        """charger_ingest.route_mileposts 와 같은 입력으로 만든다."""

        milepost, _ = route_mileposts(origins, points, waypoints)
        ordered = sorted(milepost.items(), key=lambda item: item[1])

        return cls(
            points=tuple(p for p, _ in ordered),
            mileposts=tuple(m for _, m in ordered),
        )

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
    def load(cls, path: Path = ROUTE_PATH) -> GyeongbuRoute:
        if not path.exists():
            raise FileNotFoundError(
                f"노선 파일이 없습니다: {path}\n"
                "먼저 실행할 것:  python scripts/build_route.py"
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            points=tuple((lat, lon) for lat, lon in data["points"]),
            mileposts=tuple(data["mileposts"]),
        )

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
