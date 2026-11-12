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

    ⚠ **물리를 바꾸는 손잡이는 예외다** — 거기서는 "없음" 이 "꺼져 있었다" 를
      뜻하므로 불일치가 맞다. 지금 셋: `group_scorer` · `DISPATCH_RULE` ·
      `renege`. 여기서는 그 셋을 피해서 **일반 규칙**만 본다.
    """
    import dataclasses

    off = dataclasses.replace(
        cfg, demand=dataclasses.replace(cfg.demand, renege=False))
    stored = {"departure_soc": off.vehicles.departure_soc}

    assert stale_reason(stored, off) is None


def test_the_physics_knobs_are_the_exception(cfg):
    """`renege` 가 기록에 없으면 **꺼져 있던 run** 이다 — 켠 지금과 비교 불가."""
    assert "renege" in (stale_reason(
        {"departure_soc": cfg.vehicles.departure_soc}, cfg) or "")


def test_every_difference_is_named(cfg):
    """이유를 하나만 말하면 나머지를 고치고 또 걸린다."""
    stored = run_params(cfg) | {"departure_soc": "low", "demand_multiplier": 3.0}

    why = stale_reason(stored, cfg) or ""
    assert "departure_soc" in why and "demand_multiplier" in why


def test_no_sweep_script_overwrites_a_real_run() -> None:
    """★ 물리를 바꿔 돌리는 스크립트는 **`variant` 로 run_id 를 갈라야 한다.**

    `run_id` 는 (scenario_id, stage, 참여율, seed) 뿐이다. 그래서

        cfg = dataclasses.replace(cfg, demand=dataclasses.replace(cfg.demand, ...))
        run_once(cfg, seed=seed, overwrite=True)

    는 **진짜 run 을 덮어쓴다** — 물리가 다른데 이름이 같기 때문이다.

    2026-11-12 에 `sweep_patience.py` 가 그렇게 했다. 추석 adoption 의 S0·S2
    시드 2·3 이 스윕의 마지막 경우(m=30 h=0.5)로 덮였고, 사다리 CSV 와 run 폴더가
    갈라져서 알았다 (설 S0 시드1 은 테스트가 같은 방식으로 덮었다 — 총비용
    6,999.8 ↔ 6,576.7, 포기 전 대기 534.0 → 0.0).

    `stale_reason` 은 run 을 **다시 쓸 때** 막는 장치라 이미 덮인 파일은 못
    되돌린다. 그래서 애초에 못 쓰게 막는다.

    `ScenarioConfig.variant(tag, overrides)` 가 `scenario_id` 뒤에 `__<tag>` 를
    붙이고 검증도 다시 한다. `tests/test_s3.py` 가 스윕에 `db_path` 를 요구하는
    것과 같은 규칙이다.
    """
    import re

    from evdt.paths import PROJECT_ROOT

    offenders = []
    for f in sorted((PROJECT_ROOT / "scripts").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        if "run_once(" not in src:
            continue
        # cfg 의 물리를 갈아끼우는가
        if not re.search(r"dataclasses\.replace\([^)]*demand", src, re.S):
            continue
        # 갈아끼웠다면 run_id 를 가르거나(variant/scenario_id) 샌드박스에 써야 한다
        if any(k in src for k in (".variant(", "scenario_id=", "runs_dir=")):
            continue
        offenders.append(f.name)

    assert not offenders, (
        f"물리를 바꿔 돌리면서 run_id 를 안 가르는 스크립트: {offenders} — "
        "`cfg.variant(tag, {...})` 를 쓸 것. 안 그러면 진짜 run 을 덮어쓴다")
