"""수요 레이어 — 실측과 가정을 갈라 둔다 (#54).

## 왜 필요한가

2026 설 연휴를 재현한 수요 위에, 발표를 위해 **가정으로 얹는 것**이 생긴다.
EV 보급률을 미래 수준으로 올리거나, 장거리 통행 비중을 자료가 허용하는 범위 안에서
높이는 것 같은 일이다.

그 자체는 정당하다. 문제는 **얹은 것과 실측이 구분되지 않을 때**다. 몇 주 지나면
"이 히트맵이 재현이었나 가정이었나" 를 아무도 모른다. 발표에서 질문 하나에 무너진다.
예전에 가짜 휴게소를 진짜 코리도에 넣고 원인을 못 찾아 헤맨 적도 있다
(`docs/fake_data_audit.md`).

그래서 얹는 것을 **이름 붙은 레이어**로만 얹게 한다.

    demand:
      layers:
        - {kind: ev_adoption,   ev_share: 0.25}
        - {kind: long_distance, share: 0.10, min_trip_km: 200.0}

규칙 셋

    1. 레이어는 반드시 `kind` 를 가진다. 모르는 kind 는 로딩 시점에 거부한다
    2. 레이어는 모두 **가정**이다. 실측 레이어라는 것은 없다 — 실측은 레이어가 아니라
       바탕이다
    3. `label()` 한 줄이 run params 와 **모든 그림 부제**에 들어간다.
       그림만 보고도 무엇이 얹혔는지 알 수 있어야 한다
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 레이어가 없는 실행. 그림 부제에 이렇게 찍힌다.
MEASURED_ONLY = "2026 설 재현 (가정 레이어 없음)"


@dataclass(frozen=True, slots=True)
class EvAdoption:
    """EV 보급률을 미래 수준으로 올린다.

    2026 실측 가정은 `demand.ev_share` 다. 이 레이어는 그 값을 **덮어쓴다** —
    곱하지 않는다. "2030년에 25% 라면" 을 그대로 쓰기 위해서다.
    """

    ev_share: float

    kind = "ev_adoption"

    def label(self) -> str:
        return f"EV 보급률 {self.ev_share:.0%}"


@dataclass(frozen=True, slots=True)
class LongDistance:
    """장거리 통행 비중을 올린다 (`io/long_distance.py`).

    share 는 코리도 진입 대비 비중이다. 실측 교통량이 담을 수 없는 값은
    `long_distance.split` 이 거부한다 — 여기서는 범위만 본다.
    """

    share: float
    min_trip_km: float = 200.0

    kind = "long_distance"

    def label(self) -> str:
        return f"장거리 {self.share:.0%} ({self.min_trip_km:.0f}km+)"


Layer = EvAdoption | LongDistance

#: kind → (클래스, 필수 키, 선택 키)
_KINDS: dict[str, tuple[type, tuple[str, ...], tuple[str, ...]]] = {
    EvAdoption.kind: (EvAdoption, ("ev_share",), ()),
    LongDistance.kind: (LongDistance, ("share",), ("min_trip_km",)),
}

KINDS: tuple[str, ...] = tuple(_KINDS)


def parse_layers(raw: Any, errors, path: str = "demand.layers") -> tuple[Layer, ...]:
    """config 의 `demand.layers` 를 읽는다. 잘못된 것은 errors 에 쌓고 건너뛴다."""

    if raw is None:
        return ()

    if not isinstance(raw, list):
        errors.add(path, f"목록이어야 한다 (받은 값: {type(raw).__name__})")
        return ()

    layers: list[Layer] = []

    for i, item in enumerate(raw):
        where = f"{path}[{i}]"

        if not isinstance(item, dict):
            errors.add(where, "매핑이어야 한다 (예: {kind: long_distance, share: 0.1})")
            continue

        kind = item.get("kind")

        if kind not in _KINDS:
            errors.add(f"{where}.kind", f"{list(KINDS)} 중 하나여야 한다 (받은 값: {kind!r})")
            continue

        cls, required, optional = _KINDS[kind]
        unknown = set(item) - {"kind", *required, *optional}

        if unknown:
            errors.add(where, f"모르는 키: {sorted(unknown)} (오타인지 확인)")
            continue

        missing = [key for key in required if key not in item]

        if missing:
            errors.add(where, f"빠진 키: {missing}")
            continue

        values = {key: item[key] for key in (*required, *optional) if key in item}
        layer = _build(cls, values, errors, where)

        if layer is not None:
            layers.append(layer)

    kinds = [layer.kind for layer in layers]

    for kind in set(kinds):
        if kinds.count(kind) > 1:
            errors.add(path, f"같은 레이어가 여러 번 있다: {kind}")

    return tuple(layers)


def _build(cls: type, values: dict, errors, where: str) -> Layer | None:
    numbers = {
        "ev_share": (0.0, 1.0),
        "share": (0.0, 1.0),
        "min_trip_km": (0.0, None),
    }

    for key, (lo, hi) in numbers.items():
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.add(f"{where}.{key}", f"숫자여야 한다 (받은 값: {value!r})")
            return None
        if value < lo or (hi is not None and value > hi):
            bound = f"{lo} ~ {hi}" if hi is not None else f"{lo} 이상"
            errors.add(f"{where}.{key}", f"{bound} 여야 한다 (받은 값: {value})")
            return None
        values[key] = float(value)

    return cls(**values)


def label(layers: tuple[Layer, ...]) -> str:
    """run params 와 그림 부제에 들어갈 한 줄."""

    if not layers:
        return MEASURED_ONLY

    return "가정: " + " · ".join(layer.label() for layer in layers)


def tag(layers: tuple[Layer, ...]) -> str:
    """scenario_id 에 붙일 짧은 이름. 레이어가 다르면 run 도 달라야 한다."""

    parts = []

    for layer in layers:
        if isinstance(layer, EvAdoption):
            parts.append(f"ev{layer.ev_share * 100:.0f}")
        elif isinstance(layer, LongDistance):
            parts.append(f"ld{layer.share * 100:.0f}")

    return "-".join(parts)


def find(layers: tuple[Layer, ...], kind: str) -> Layer | None:
    return next((layer for layer in layers if layer.kind == kind), None)


def effective_ev_share(layers: tuple[Layer, ...], base_share: float) -> float:
    """레이어를 반영한 EV 비중. 레이어가 없으면 실측 가정 그대로."""

    layer = find(layers, EvAdoption.kind)

    return layer.ev_share if isinstance(layer, EvAdoption) else base_share


def long_distance_share(layers: tuple[Layer, ...]) -> tuple[float, float]:
    """(장거리 비중, 최소 주행거리). 레이어가 없으면 (0, 0) — 얹지 않는다."""

    layer = find(layers, LongDistance.kind)

    if isinstance(layer, LongDistance):
        return layer.share, layer.min_trip_km

    return 0.0, 0.0
