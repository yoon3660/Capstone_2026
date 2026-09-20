"""차종·충전곡선·온도효율 설정 파일을 DB 행으로 바꾼다 (T-10 / T-11).

    config/vehicles.yaml         → vehicle_class, charge_curve
    config/temp_efficiency.yaml  → temp_efficiency

값의 근거는 설정 파일 주석에 있다. 이 모듈은 읽고 모양을 검사할 뿐이다.
(충전시간 계산은 evdt.world.charging — io 는 world 를 임포트하지 않는다.)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from evdt.paths import CONFIG_DIR

VEHICLES_PATH = CONFIG_DIR / "vehicles.yaml"
TEMP_EFFICIENCY_PATH = CONFIG_DIR / "temp_efficiency.yaml"

SHARE_TOLERANCE = 1e-6


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"설정 파일이 없습니다: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError(f"최상위가 매핑이 아닙니다: {path}")

    return data


def read_vehicle_classes(path: Path = VEHICLES_PATH) -> tuple[list[dict], list[dict]]:
    """(vehicle_class 행, charge_curve 행). 모양이 어긋나면 즉시 멈춘다.

    로딩 시점에 검사하는 이유는 T-03 과 같다 — 잘못된 값은 시뮬레이션 3시간 뒤가
    아니라 지금 터져야 한다.
    """

    data = _load_yaml(path)
    classes = data.get("vehicle_classes")

    if not classes:
        raise ValueError(f"vehicle_classes 가 비어 있습니다: {path}")

    class_rows: list[dict] = []
    curve_rows: list[dict] = []
    seen: set[str] = set()

    for entry in classes:
        vclass_id = entry["vclass_id"]

        if vclass_id in seen:
            raise ValueError(f"vclass_id 가 중복됩니다: {vclass_id}")

        seen.add(vclass_id)

        for key in ("battery_kwh", "vmax_kw", "consumption_kwh_km"):
            if entry[key] <= 0:
                raise ValueError(f"{vclass_id}.{key} 는 0보다 커야 합니다: {entry[key]}")

        class_rows.append(
            {
                "vclass_id": vclass_id,
                "name": entry["name"],
                "battery_kwh": float(entry["battery_kwh"]),
                "vmax_kw": float(entry["vmax_kw"]),
                "consumption_kwh_km": float(entry["consumption_kwh_km"]),
                "share": float(entry["share"]),
                "source": entry["source"],
            }
        )
        curve_rows += _curve_rows(vclass_id, entry)

    total = sum(row["share"] for row in class_rows)

    if abs(total - 1.0) > SHARE_TOLERANCE:
        raise ValueError(f"share 합이 {total} 입니다 (1.0 이어야 함)")

    return class_rows, curve_rows


def _curve_rows(vclass_id: str, entry: dict) -> list[dict]:
    """충전곡선 구간을 절대 출력(kW)으로 바꾼다. power_frac 는 차량 최대 수용출력 대비 비율."""

    vmax_kw = float(entry["vmax_kw"])
    rows = []
    cursor = 0.0

    for soc_from, soc_to, power_frac in entry["curve"]:
        if abs(soc_from - cursor) > 1e-9:
            raise ValueError(
                f"{vclass_id} 충전곡선에 틈/겹침이 있습니다: {cursor} → {soc_from}"
            )

        if not 0.0 < power_frac <= 1.0:
            raise ValueError(f"{vclass_id} power_frac 가 0~1 밖입니다: {power_frac}")

        rows.append(
            {
                "vclass_id": vclass_id,
                "soc_from": float(soc_from),
                "soc_to": float(soc_to),
                "power_kw": round(vmax_kw * float(power_frac), 2),
            }
        )
        cursor = float(soc_to)

    if abs(cursor - 1.0) > 1e-9:
        raise ValueError(f"{vclass_id} 충전곡선이 SoC 1.0 에서 끝나지 않습니다: {cursor}")

    return rows


def read_temp_efficiency(path: Path = TEMP_EFFICIENCY_PATH) -> list[dict]:
    """temp_efficiency 행. 20 °C 기준점과 단조성을 여기서 확인한다."""

    data = _load_yaml(path)
    points = data.get("points")

    if not points:
        raise ValueError(f"points 가 비어 있습니다: {path}")

    rows = [
        {
            "temp_c": float(temp_c),
            "range_factor": float(range_factor),
            "charge_power_factor": float(charge_power_factor),
            "source": source,
        }
        for temp_c, range_factor, charge_power_factor, source in points
    ]
    rows.sort(key=lambda r: r["temp_c"])

    if len({r["temp_c"] for r in rows}) != len(rows):
        raise ValueError("온도가 중복됩니다")

    base = [r for r in rows if r["temp_c"] == 20.0]

    if not base:
        raise ValueError("기준점 20 °C 가 표에 없습니다")

    if base[0]["range_factor"] != 1.0 or base[0]["charge_power_factor"] != 1.0:
        raise ValueError("20 °C 의 두 계수는 기준점이므로 1.0 이어야 합니다")

    for required in (-10.0, 0.0, 20.0):
        if not any(r["temp_c"] == required for r in rows):
            raise ValueError(f"필수 온도 지점이 없습니다: {required} °C")

    # 20 °C 아래에서는 추울수록 두 계수가 모두 작아져야 한다.
    cold = [r for r in rows if r["temp_c"] <= 20.0]

    for a, b in zip(cold, cold[1:], strict=False):
        if a["range_factor"] > b["range_factor"]:
            raise ValueError(f"range_factor 가 단조가 아닙니다: {a['temp_c']} → {b['temp_c']}")
        if a["charge_power_factor"] > b["charge_power_factor"]:
            raise ValueError(
                f"charge_power_factor 가 단조가 아닙니다: {a['temp_c']} → {b['temp_c']}"
            )

    return rows


def curve_segments(curve_rows: list[dict], vclass_id: str) -> list[tuple[float, float, float]]:
    """charge_time_min 이 받는 (soc_from, soc_to, power_kw) 목록으로."""

    return sorted(
        (r["soc_from"], r["soc_to"], r["power_kw"])
        for r in curve_rows
        if r["vclass_id"] == vclass_id
    )


def temp_table(temp_rows: list[dict]) -> list[tuple[float, float, float]]:
    """temp_factors 가 받는 (temp_c, range_factor, charge_power_factor) 목록으로."""

    return sorted(
        (r["temp_c"], r["range_factor"], r["charge_power_factor"]) for r in temp_rows
    )


def load_from_db(conn) -> tuple[list[dict], list[dict], list[dict]]:
    """적재된 DB 에서 (vehicle_class, charge_curve, temp_efficiency) 행을 읽는다.

    시뮬레이터·예약 원장은 설정 파일이 아니라 DB 를 읽는다. 실행 결과의 근거가
    run 테이블과 같은 DB 안에 있어야 "이 숫자 어디서 나왔지" 에 답할 수 있다.
    """

    classes = [dict(r) for r in conn.execute("SELECT * FROM vehicle_class ORDER BY vclass_id")]
    curves = [
        dict(r)
        for r in conn.execute("SELECT * FROM charge_curve ORDER BY vclass_id, soc_from")
    ]
    temps = [dict(r) for r in conn.execute("SELECT * FROM temp_efficiency ORDER BY temp_c")]

    if not classes:
        raise RuntimeError(
            "vehicle_class 가 비어 있습니다. 먼저 실행할 것: python scripts/seed_vehicles.py"
        )

    return classes, curves, temps
