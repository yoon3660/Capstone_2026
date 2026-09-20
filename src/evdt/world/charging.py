"""충전 점유시간 계산기 (T-14).

    (초기 SoC, 목표 SoC, 충전기 출력, 외기온) → 충전 소요시간(분)

순수 함수다. DB 도 파일도 읽지 않는다. 입력으로 받은 충전곡선과 온도 계수만 쓴다.
시뮬레이터(world/sim.py)와 예약 원장(engine/ledger.py)이 **같은 함수**를 부른다
(설계 규칙 1). 두 곳에 따로 구현하면 원장이 틀린 미래를 예측하고 한계 외부비용이
전부 오염된다.

계산 방법
    충전곡선을 SoC 구간별로 적분한다. 구간마다 실제 출력은
        min(구간 출력(차량), 차량 최대 수용출력, 충전기 출력) × 온도 계수
    이고, 그 구간에 넣어야 할 에너지를 출력으로 나눠 시간을 더한다.

    병목은 충전기와 차량 중 **작은 쪽**이다. 200kW 충전기에 350kW 차가 물리면
    200kW 가 상한이고, 50kW 충전기면 50kW 가 상한이다.

무엇을 넣지 않았는가
    - 충전 손실(배터리로 들어가는 에너지 < 충전기가 내보내는 에너지). 점유시간에는
      같은 방향으로 작용하지만 곡선 자체가 실측 시간에 맞춰져 있어 이중 반영이 된다.
    - 배터리 예열(preconditioning). 저온 계수가 평균적으로 흡수한다.
"""

from __future__ import annotations

from collections.abc import Sequence

#: 충전곡선 한 구간: (soc_from, soc_to, power_kw)
CurveSegment = tuple[float, float, float]


def temp_factors(
    temp_c: float,
    table: Sequence[tuple[float, float, float]],
) -> tuple[float, float]:
    """온도 → (전비 계수, 충전출력 계수). 표에 없는 온도는 선형보간한다.

    table: (temp_c, range_factor, charge_power_factor) 를 온도 오름차순으로.
    표의 범위를 벗어나면 양 끝 값을 쓴다 (외삽하지 않는다 — 근거가 없다).
    """

    if not table:
        raise ValueError("온도-효율 표가 비어 있습니다")

    points = sorted(table)

    if temp_c <= points[0][0]:
        return points[0][1], points[0][2]

    if temp_c >= points[-1][0]:
        return points[-1][1], points[-1][2]

    for (t0, r0, c0), (t1, r1, c1) in zip(points, points[1:], strict=False):
        if t0 <= temp_c <= t1:
            w = (temp_c - t0) / (t1 - t0)
            return r0 + w * (r1 - r0), c0 + w * (c1 - c0)

    raise AssertionError("도달할 수 없음")   # pragma: no cover


def charge_time_min(
    soc_from: float,
    soc_to: float,
    battery_kwh: float,
    vmax_kw: float,
    charger_kw: float,
    curve: Sequence[CurveSegment],
    charge_power_factor: float = 1.0,
) -> float:
    """SoC soc_from → soc_to 충전에 걸리는 시간(분).

    charge_power_factor 는 temp_factors 가 준 충전출력 계수다.
    soc_from == soc_to 면 0.0 (경계값).
    """

    if not 0.0 <= soc_from <= 1.0 or not 0.0 <= soc_to <= 1.0:
        raise ValueError(f"SoC 는 0~1 이어야 합니다: {soc_from} → {soc_to}")

    if soc_to < soc_from:
        raise ValueError(f"목표 SoC 가 초기 SoC 보다 낮습니다: {soc_from} → {soc_to}")

    if battery_kwh <= 0 or vmax_kw <= 0 or charger_kw <= 0:
        raise ValueError("배터리 용량과 출력은 0보다 커야 합니다")

    if charge_power_factor <= 0:
        raise ValueError(f"충전출력 계수가 0 이하입니다: {charge_power_factor}")

    if soc_to == soc_from:
        return 0.0

    covered = 0.0
    hours = 0.0

    for seg_from, seg_to, seg_power_kw in sorted(curve):
        lo = max(seg_from, soc_from)
        hi = min(seg_to, soc_to)

        if hi <= lo:
            continue

        power_kw = min(seg_power_kw, vmax_kw, charger_kw) * charge_power_factor

        if power_kw <= 0:
            raise ValueError(f"충전 출력이 0 이하입니다: {seg_from}~{seg_to}")

        hours += (hi - lo) * battery_kwh / power_kw
        covered += hi - lo

    if abs(covered - (soc_to - soc_from)) > 1e-9:
        raise ValueError(
            "충전곡선이 요청한 SoC 구간을 덮지 못합니다: "
            f"{soc_from}~{soc_to} 중 {covered:.3f} 만 덮임"
        )

    return hours * 60.0


def energy_for_distance_kwh(
    distance_km: float,
    consumption_kwh_km: float,
    range_factor: float = 1.0,
) -> float:
    """거리를 달리는 데 드는 전력량(kWh). 저온이면 range_factor 가 작아 더 많이 든다."""

    if distance_km < 0:
        raise ValueError(f"거리가 음수입니다: {distance_km}")

    if consumption_kwh_km <= 0 or range_factor <= 0:
        raise ValueError("전비와 전비 계수는 0보다 커야 합니다")

    return distance_km * consumption_kwh_km / range_factor
