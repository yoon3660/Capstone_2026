from bisect import bisect_right
from math import asin, cos, hypot, isfinite, radians, sin, sqrt


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """두 위경도 사이의 직선거리(km)를 계산한다."""
    lat1, lon1, lat2, lon2 = map(
        radians, (lat1, lon1, lat2, lon2)
    )

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )

    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(a)))


class Polyline:
    def __init__(self, points, offsets):
        self.points = points
        self.offsets = offsets

    @classmethod
    def from_points(cls, points, offsets=None):
        """좌표 목록과 선택적인 거리 목록으로 중심선을 생성한다."""
        points = [
            (float(lat), float(lon))
            for lat, lon in points
        ]

        if len(points) < 2:
            raise ValueError("좌표가 최소 2개 필요합니다.")

        for lat, lon in points:
            if (
                not isfinite(lat)
                or not isfinite(lon)
                or not 33 <= lat <= 39
                or not 124 <= lon <= 132
            ):
                raise ValueError("위경도 범위가 잘못되었습니다.")

        if offsets is None:
            # 기존 IC/JCT 폴리라인 방식: 좌표 간 거리 누적
            offsets = [0.0]

            for i in range(1, len(points)):
                distance = haversine_km(
                    *points[i - 1],
                    *points[i],
                )

                offsets.append(offsets[-1] + distance)
        else:
            # 공식 이정 또는 별도로 보정한 이정 사용
            offsets = [float(value) for value in offsets]

        if len(points) != len(offsets):
            raise ValueError("좌표와 이정의 개수가 다릅니다.")

        if not all(isfinite(value) for value in offsets):
            raise ValueError("유효하지 않은 이정값이 있습니다.")

        if any(
            offsets[i] <= offsets[i - 1]
            for i in range(1, len(offsets))
        ):
            raise ValueError("이정은 엄격하게 증가해야 합니다.")

        return cls(points, offsets)

    def point_at(self, offset_km):
        """주어진 이정에 해당하는 (위도, 경도)를 반환한다."""
        offset_km = float(offset_km)

        if (
            not isfinite(offset_km)
            or offset_km < self.offsets[0]
            or offset_km > self.offsets[-1]
        ):
            raise ValueError("이정이 중심선 범위를 벗어났습니다.")

        if offset_km == self.offsets[-1]:
            return self.points[-1]

        # 이정이 속한 구간 찾기
        i = bisect_right(self.offsets, offset_km) - 1

        start = self.offsets[i]
        end = self.offsets[i + 1]

        ratio = (offset_km - start) / (end - start)

        lat1, lon1 = self.points[i]
        lat2, lon2 = self.points[i + 1]

        lat = lat1 + ratio * (lat2 - lat1)
        lon = lon1 + ratio * (lon2 - lon1)

        return lat, lon

    def offset_of(self, lat, lon):
        """가장 가까운 중심선 위치의 (이정, 거리km)를 반환한다."""
        lat = float(lat)
        lon = float(lon)

        if not isfinite(lat) or not isfinite(lon):
            raise ValueError("유효하지 않은 좌표입니다.")

        best_distance = float("inf")
        best_offset = None

        # 중심선의 각 선분에 좌표를 투영한다.
        for i in range(len(self.points) - 1):
            lat1, lon1 = self.points[i]
            lat2, lon2 = self.points[i + 1]

            # 조회 지점을 원점으로 하는 국소 평면 좌표(km)
            scale_x = EARTH_RADIUS_KM * cos(radians(lat))
            scale_y = EARTH_RADIUS_KM

            ax = radians(lon1 - lon) * scale_x
            ay = radians(lat1 - lat) * scale_y

            bx = radians(lon2 - lon) * scale_x
            by = radians(lat2 - lat) * scale_y

            dx = bx - ax
            dy = by - ay

            length_sq = dx * dx + dy * dy

            if length_sq == 0:
                ratio = 0.0
            else:
                ratio = -(ax * dx + ay * dy) / length_sq
                ratio = max(0.0, min(1.0, ratio))

            nearest_x = ax + ratio * dx
            nearest_y = ay + ratio * dy

            distance = hypot(nearest_x, nearest_y)

            if distance < best_distance:
                best_distance = distance
                best_offset = (
                    self.offsets[i]
                    + ratio
                    * (self.offsets[i + 1] - self.offsets[i])
                )

        return best_offset, best_distance