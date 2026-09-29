"""T-03 완료 조건: 정상 로딩 성공, 고의로 망친 config 3종이 명확한 메시지로 실패."""

from __future__ import annotations

from pathlib import Path

import pytest

from evdt.config import ConfigError, ScenarioConfig
from tests.conftest import FIXTURES, ROOT


def test_all_shipped_scenarios_load() -> None:
    files = sorted((ROOT / "config").glob("scenario_*.yaml"))
    assert len(files) >= 3
    for p in files:
        cfg = ScenarioConfig.from_yaml(p)
        assert cfg.scenario_id
        assert 0.0 <= cfg.demand.ev_share <= 1.0
        assert cfg.time.end_min > cfg.time.start_min


def test_hash_is_stable_and_content_addressed(cfg: ScenarioConfig, tmp_path: Path) -> None:
    same = ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
    assert cfg.config_hash == same.config_hash

    edited = tmp_path / "edited.yaml"
    # 바꿀 자리가 없어지면 "안 바뀌었는데 해시가 같다" 로 조용히 통과한다. 먼저 확인한다
    changed = cfg.raw_yaml.replace("demand_multiplier: 1.0", "demand_multiplier: 2.0")
    assert changed != cfg.raw_yaml, "config 에서 바꿀 자리를 못 찾았다 — 테스트를 갱신할 것"

    edited.write_text(changed, encoding="utf-8")
    assert ScenarioConfig.from_yaml(edited).config_hash != cfg.config_hash


def test_missing_keys_are_all_reported_at_once() -> None:
    with pytest.raises(ConfigError) as exc:
        ScenarioConfig.from_yaml(FIXTURES / "bad_missing_key.yaml")
    msg = str(exc.value)
    assert "time" in msg
    assert "demand.ev_share" in msg


def test_out_of_range_values_name_the_offending_key() -> None:
    with pytest.raises(ConfigError) as exc:
        ScenarioConfig.from_yaml(FIXTURES / "bad_out_of_range.yaml")
    msg = str(exc.value)
    for needle in ("demand.ev_share", "policy.participation", "time.end_min", "vehicles.soc_beta.hi"):
        assert needle in msg, f"{needle} 가 메시지에 없다:\n{msg}"


def test_typo_is_not_silently_ignored() -> None:
    with pytest.raises(ConfigError) as exc:
        ScenarioConfig.from_yaml(FIXTURES / "bad_typo.yaml")
    msg = str(exc.value)
    assert "vehicle" in msg          # 오타 키가 지적된다
    assert "vehicles" in msg         # 진짜 필요한 섹션이 없다고도 말한다
    assert "policy.stage" in msg
    assert "day_type" in msg


def test_ue_requires_full_participation() -> None:
    base = ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
    import yaml

    data = yaml.safe_load(base.raw_yaml)
    data["policy"]["participation"] = 0.6
    with pytest.raises(ConfigError, match="참여율"):
        ScenarioConfig.from_dict(data)


def test_scenario_row_matches_db_columns(cfg: ScenarioConfig, db_path) -> None:
    from evdt.io.db import column_names, get_conn

    with get_conn(db_path, readonly=True) as conn:
        cols = set(column_names(conn, "scenario"))
    assert set(cfg.scenario_row()).issubset(cols)
