"""이미 DONE 인 run 을 재사용해도 되는가 (#101).

여기서 지키는 것은 **"같은 run_id 가 같은 세계를 뜻하지 않는다"** 하나다.

`run_id` 는 (scenario_id, stage, 참여율, seed) 뿐이다. yaml 을 고치면 **이름은 그대로인데
세계가 바뀐다.** 실제로 그랬다 — #55 가 기본 `departure_soc` 를 low → holiday 로 바꿨는데
9월에 돌린 `soc=low` 결과(충전 필요 10,129대)가 10월에도 "재현 기준선"(1,842대)인 척
계속 제공됐고, #101 의 첫 표가 그걸 그대로 썼다.

`check_same_world` 로는 안 걸린다. 그쪽은 **진입 EV 수**만 보는데 이 경우 18,534 로 같다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evdt.config import ScenarioConfig
from evdt.runner import run_params, stale_reason

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg() -> ScenarioConfig:
    return ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")


def test_params_carry_what_the_run_id_cannot(cfg):
    """run_id 가 구분 못 하는 것이 기록에 남아야 재사용 여부를 판단할 수 있다."""
    p = run_params(cfg)
    for key in ("departure_soc", "departure_soc_mean", "demand_multiplier",
                "demand_layers", "od_profile"):
        assert key in p, f"{key} 가 기록되지 않는다"


def test_same_config_is_reusable(cfg):
    assert stale_reason(run_params(cfg), cfg) is None


def test_the_soc_change_that_actually_happened_is_caught(cfg):
    """**이 테스트가 이 파일의 전부다.**

    9월 run 의 기록(`soc=low`, 평균 0.3429)을 지금 config(holiday, 0.6653)로
    재사용하려 하면 걸려야 한다.
    """
    stored = run_params(cfg) | {"departure_soc": "low", "departure_soc_mean": 0.3429}

    why = stale_reason(stored, cfg)
    assert why is not None
    assert "departure_soc" in why
    assert "low" in why


def test_od_profile_change_is_caught(cfg):
    """목적지 출처가 다르면 다른 세계다 (#99)."""
    stored = run_params(cfg) | {"od_profile": "data/processed/tcs_od_gyeongbu.parquet"}
    assert "od_profile" in (stale_reason(stored, cfg) or "")


def test_assumption_layers_are_caught(cfg):
    """재현과 가정 시나리오가 같은 이름으로 섞이면 안 된다 (#54)."""
    stored = run_params(cfg) | {"demand_layers": "가정: EV 보급률 25%"}
    assert "demand_layers" in (stale_reason(stored, cfg) or "")


def test_keys_missing_from_an_old_record_are_not_treated_as_different(cfg):
    """옛 run 에는 나중에 추가된 키가 없다.

    없다고 전부 다시 돌리면 **쓸 수 있는 결과까지 버린다.** 기록된 값이 다를 때만
    불일치로 본다.
    """
    stored = {"departure_soc": cfg.vehicles.departure_soc}
    assert stale_reason(stored, cfg) is None


def test_every_difference_is_named(cfg):
    """이유를 하나만 말하면 나머지를 고치고 또 걸린다."""
    stored = run_params(cfg) | {"departure_soc": "low", "demand_multiplier": 3.0}

    why = stale_reason(stored, cfg) or ""
    assert "departure_soc" in why and "demand_multiplier" in why
