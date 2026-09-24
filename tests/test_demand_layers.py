"""수요 레이어 (#54).

레이어의 존재 이유는 **실측과 가정이 섞이지 않게 하는 것**이다. 그래서 검사할 것은
(1) 잘못된 레이어가 로딩 시점에 걸리는가 (2) 얹은 것이 이름으로 드러나는가 두 가지다.
"""

from __future__ import annotations

import pytest
import yaml

from evdt.config import ConfigError, ScenarioConfig
from evdt.demand_layers import (
    MEASURED_ONLY,
    EvAdoption,
    LongDistance,
    effective_ev_share,
    label,
    long_distance_share,
    tag,
)
from evdt.paths import CONFIG_DIR


def _config(layers) -> ScenarioConfig:
    raw = yaml.safe_load((CONFIG_DIR / "scenario_seollal_down.yaml").read_text(encoding="utf-8"))
    raw["demand"] = {**raw["demand"], "layers": layers}
    return ScenarioConfig.from_dict(raw, source="test")


# ---------------------------------------------------------------------------
# 얹은 것이 이름으로 드러난다
# ---------------------------------------------------------------------------


def test_no_layers_says_it_is_the_replay() -> None:
    """레이어가 없으면 '재현' 이라고 말한다. 침묵하지 않는다."""
    assert label(()) == MEASURED_ONLY
    assert "재현" in _config([]).demand_label


def test_label_names_every_layer() -> None:
    cfg = _config([
        {"kind": "ev_adoption", "ev_share": 0.25},
        {"kind": "long_distance", "share": 0.1, "min_trip_km": 200},
    ])

    assert cfg.demand_label == "가정: EV 보급률 25% · 장거리 10% (200km+)"
    assert "가정" in cfg.demand_label


def test_tag_separates_runs() -> None:
    """레이어가 다르면 run_id 도 달라야 한다. 안 그러면 서로 덮어쓴다."""
    a = tag((EvAdoption(0.25), LongDistance(0.10)))
    b = tag((EvAdoption(0.25), LongDistance(0.20)))

    assert a != b
    assert a == "ev25-ld10"


# ---------------------------------------------------------------------------
# 레이어가 실제로 값을 바꾼다
# ---------------------------------------------------------------------------


def test_ev_adoption_overrides_the_measured_share() -> None:
    """곱하지 않고 덮어쓴다 — '2030년에 25% 라면' 을 그대로 쓰기 위해서다."""
    assert effective_ev_share((EvAdoption(0.25),), base_share=0.08) == 0.25


def test_without_the_layer_the_measured_share_stands() -> None:
    assert effective_ev_share((), base_share=0.08) == 0.08


def test_long_distance_defaults_to_zero() -> None:
    """레이어가 없으면 얹지 않는다. 기본값이 몰래 수요를 키우면 안 된다."""
    assert long_distance_share(()) == (0.0, 0.0)


def test_long_distance_carries_its_threshold() -> None:
    assert long_distance_share((LongDistance(0.1, 250.0),)) == (0.1, 250.0)


# ---------------------------------------------------------------------------
# 잘못된 레이어는 로딩 시점에 걸린다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layers", "needle"),
    [
        ([{"kind": "oops"}], "중 하나여야 한다"),
        ([{"kind": "long_distance"}], "빠진 키"),
        ([{"kind": "long_distance", "share": 2.0}], "0.0 ~ 1.0"),
        ([{"kind": "long_distance", "share": 0.1, "shrae": 1}], "모르는 키"),
        ([{"kind": "ev_adoption", "ev_share": "많이"}], "숫자여야 한다"),
        ([{"kind": "long_distance", "share": 0.1},
          {"kind": "long_distance", "share": 0.2}], "여러 번"),
        ("장거리", "목록이어야 한다"),
        ([{"share": 0.1}], "중 하나여야 한다"),
    ],
)
def test_bad_layers_are_refused_at_load(layers, needle) -> None:
    """3시간 뒤가 아니라 로딩 시점에 터져야 한다."""
    with pytest.raises(ConfigError, match=needle):
        _config(layers)


def test_the_run_records_what_was_assumed() -> None:
    """결과를 다시 볼 때 가장 먼저 봐야 하는 값이라 run params 에 들어간다."""
    cfg = _config([{"kind": "long_distance", "share": 0.1}])

    assert "장거리 10%" in cfg.demand_label
