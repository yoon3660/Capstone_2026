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
    OpportunityCharge,
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

    assert cfg.demand_label.endswith("가정: EV 보급률 25% · 장거리 10% (200km+)")
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


# ---------------------------------------------------------------------------
# 재현 기간 스위치 (#54)
# ---------------------------------------------------------------------------


def test_period_appears_in_the_label() -> None:
    """어느 연휴를 재현했는지가 그림 부제와 run params 에 남아야 한다."""
    cfg = _config([])

    assert "2026 설 연휴" in cfg.demand_label
    assert "seollal2026" in cfg.demand_label


def test_unknown_period_is_allowed_but_flagged() -> None:
    """등록 안 된 기간도 쓸 수 있다 — 다만 그렇다고 말한다."""
    from evdt.replay import describe

    assert "등록되지 않은" in describe("seollal2030")
    assert "등록되지 않은" not in describe("chuseok2026")


def test_profile_tag_drops_the_year() -> None:
    from evdt.replay import PERIODS

    assert PERIODS["seollal2026"].profile_tag() == "seollal"
    assert PERIODS["chuseok2026"].profile_tag() == "chuseok"


# ---------------------------------------------------------------------------
# 레이어 하나만 갈아끼우기 (#146)
#
# 이 테스트가 없어서 `load_config(ev_share=...)` 가 기회충전 레이어를 조용히
# 날리고 있었다. "보급률 하나만 바꿨다" 고 믿은 실험이 실은 두 축을 움직였고,
# 추석 config 에서 같은 10% 인데 충전 필요 대수가 4,958 ↔ 2,138 로 갈렸다.
# ---------------------------------------------------------------------------


def test_as_dict_round_trips_through_the_parser() -> None:
    """as_dict 는 parse_layers 의 역함수여야 한다 — 아니면 되돌릴 때 값이 샌다."""
    from evdt.config import _Errors  # type: ignore[attr-defined]
    from evdt.demand_layers import as_dict, parse_layers

    before = (EvAdoption(0.25), OpportunityCharge(prob=0.2, soc_margin=0.4),
              LongDistance(share=0.1, min_trip_km=250.0))
    errors = _Errors("test")
    after = parse_layers([as_dict(layer) for layer in before], errors)

    assert not errors.items, errors.items
    assert after == before


def test_replace_kind_keeps_the_other_layers() -> None:
    from evdt.demand_layers import replace_kind

    layers = (EvAdoption(0.1), OpportunityCharge(prob=0.2, soc_margin=0.5))
    out = replace_kind(layers, {"kind": "ev_adoption", "ev_share": 0.4})

    assert out[0] == {"kind": "ev_adoption", "ev_share": 0.4}
    assert out[1] == {"kind": "opportunity_charge", "prob": 0.2, "soc_margin": 0.5}


def test_replace_kind_appends_when_the_kind_is_absent() -> None:
    from evdt.demand_layers import replace_kind

    out = replace_kind((OpportunityCharge(prob=0.2),),
                       {"kind": "ev_adoption", "ev_share": 0.4})

    assert [d["kind"] for d in out] == ["opportunity_charge", "ev_adoption"]


def test_replace_kind_keeps_the_original_position() -> None:
    """자리가 흔들리면 tag() 가 달라지고, 같은 세계가 다른 run_id 를 갖는다."""
    from evdt.demand_layers import replace_kind

    layers = (OpportunityCharge(prob=0.2), EvAdoption(0.1))
    out = replace_kind(layers, {"kind": "ev_adoption", "ev_share": 0.4})

    assert [d["kind"] for d in out] == ["opportunity_charge", "ev_adoption"]


def test_ev_share_override_does_not_drop_opportunity_charge(tmp_path) -> None:
    """러너 손잡이 쪽 회귀 테스트 — 여기가 실제로 깨져 있던 자리다."""
    from evdt.runner import load_config

    raw = yaml.safe_load(
        (CONFIG_DIR / "scenario_seollal_down.yaml").read_text(encoding="utf-8"))
    raw["demand"]["layers"] = [{"kind": "ev_adoption", "ev_share": 0.1},
                               {"kind": "opportunity_charge", "prob": 0.2}]
    (tmp_path / "scenario.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    cfg = load_config("scenario.yaml", ev_share=0.4, root=tmp_path)

    assert cfg.demand.layers == (EvAdoption(0.4), OpportunityCharge(prob=0.2))
