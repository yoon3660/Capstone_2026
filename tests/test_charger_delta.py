"""충전기 증설 손잡이 (#8-2 증설 랭킹) — 조용히 무효가 되지 않는가."""

from __future__ import annotations

import pytest

from evdt.config import ConfigError, ScenarioConfig
from evdt.runner import _apply_charger_delta, load_config, with_charger_delta

CFG = "config/scenario_chuseok_up_congested.yaml"
CHILGOK = "gyeongbu_up_a00069"
GEONCHEON = "gyeongbu_up_a00077"


def test_existing_configs_add_no_chargers() -> None:
    """손잡이를 안 쓰면 **DB 그대로** 다. 기존 config 는 한 줄도 안 바뀐다."""

    cfg = load_config(CFG, stage="S0")
    assert cfg.supply.charger_delta == {}
    rows = [{"station_id": CHILGOK, "charger_id": "x", "power_kw": 100.0,
             "n_units": 8, "is_active": 1}]
    assert _apply_charger_delta(rows, cfg, lambda *_: None) is rows


def test_the_scenario_id_carries_the_addition() -> None:
    """★ run_id 는 (시나리오, 스테이지, 시드, 참여율) 뿐이다.

    충전기를 바꿔도 이름이 같으면 **기존 run 을 조용히 덮어쓴다** (#123). 그래서
    꼬리표를 `with_charger_delta` 가 붙인다 — 부르는 쪽이 잊을 수 있는 일로
    두지 않는다. 그리고 꼬리표만 보고 증설 내용을 되읽을 수 있어야 한다.
    """

    cfg = load_config(CFG, stage="S0")
    out = with_charger_delta(cfg, {CHILGOK: 3})

    assert out.scenario_id == f"{cfg.scenario_id}__addch-a00069x3-100kw"
    assert out.scenario_id != cfg.scenario_id
    assert out.supply.charger_delta == {CHILGOK: 3}
    assert out.supply.charger_delta_kw == 100.0
    # 증설 내용이 config 전문에 남아야 한다 — runs/<run_id>/config.yaml 의 정본
    assert "charger_delta" in out.raw_yaml


def test_two_stations_sort_into_a_stable_tag() -> None:
    """같은 증설은 **같은 이름**이어야 한다 — 사전 순서에 흔들리면 안 된다."""

    cfg = load_config(CFG, stage="S0")
    a = with_charger_delta(cfg, {CHILGOK: 1, GEONCHEON: 2})
    b = with_charger_delta(cfg, {GEONCHEON: 2, CHILGOK: 1})
    assert a.scenario_id == b.scenario_id
    assert a.scenario_id.endswith("addch-a00069x1-a00077x2-100kw")


def test_a_different_power_is_a_different_run() -> None:
    """출력이 다르면 다른 세계다. 이름이 같으면 서로 덮어쓴다."""

    cfg = load_config(CFG, stage="S0")
    a = with_charger_delta(cfg, {CHILGOK: 2}, power_kw=100.0)
    b = with_charger_delta(cfg, {CHILGOK: 2}, power_kw=350.0)
    assert a.scenario_id != b.scenario_id


def test_a_typo_in_the_station_id_stops_the_run() -> None:
    """★ 오타면 증설이 **조용히 무효**가 되고, 그 run 은 대조군과 구별되지 않는다.

    "증설했는데 효과가 없다" 와 "증설이 안 됐다" 를 결과만 보고 가를 수 없다.
    그래서 멈춘다.
    """

    cfg = load_config(CFG, stage="S0")
    bad = with_charger_delta(cfg, {"gyeongbu_up_a99999": 1})
    rows = [{"station_id": CHILGOK, "charger_id": "x", "power_kw": 100.0,
             "n_units": 8, "is_active": 1}]
    with pytest.raises(ValueError, match="없다"):
        _apply_charger_delta(rows, bad, lambda *_: None)


def test_the_added_rows_are_one_spec_per_station() -> None:
    """증설분은 휴게소마다 **같은 사양**이어야 한다.

    사양이 다르면 "안성에 1대 vs 칠곡에 3대" 가 배치의 비교가 아니라 출력의
    비교가 된다.
    """

    cfg = with_charger_delta(load_config(CFG, stage="S0"),
                             {CHILGOK: 3, GEONCHEON: 1}, power_kw=100.0)
    rows = [{"station_id": s, "charger_id": f"{s}_x", "power_kw": 200.0,
             "n_units": 2, "is_active": 1} for s in (CHILGOK, GEONCHEON)]
    out = _apply_charger_delta(rows, cfg, lambda *_: None)

    added = [r for r in out if r not in rows]
    assert len(added) == 2
    assert {r["power_kw"] for r in added} == {100.0}
    assert {r["station_id"]: r["n_units"] for r in added} == {CHILGOK: 3, GEONCHEON: 1}
    assert all(r["is_active"] == 1 for r in added)


def test_removing_chargers_is_refused() -> None:
    """대수를 줄이면 **없는 충전기로 대기를 계산**하게 된다 (`expand_chargers` 와 같은 이유)."""

    cfg = load_config(CFG, stage="S0")
    data = __import__("yaml").safe_load(cfg.raw_yaml)
    data["supply"] = {"charger_delta": {CHILGOK: -2}}
    with pytest.raises(ConfigError, match="1 이상"):
        ScenarioConfig.from_dict(data, source="t")


def test_an_empty_addition_is_refused() -> None:
    """빈 증설로 꼬리표를 붙이면 **같은 세계가 다른 이름**으로 두 번 저장된다."""

    with pytest.raises(ValueError, match="비었다"):
        with_charger_delta(load_config(CFG, stage="S0"), {})


def test_the_mnl_path_applies_the_addition_too() -> None:
    """★ `run_mnl_world.prepare` 는 `run_once` 의 준비 부분을 **베껴 온** 함수다.

    한쪽만 고치면 MNL 바닥은 증설을 무시하고 사다리만 반영한다 — 그러면
    "증설이 바닥에서는 효과가 없다" 는 틀린 결론이 나온다.
    """

    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_mnl_world.py").read_text(encoding="utf-8")
    prepare = next(n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef) and n.name == "prepare")
    calls = {ast.unparse(c.func) for c in ast.walk(prepare) if isinstance(c, ast.Call)}
    assert "R._apply_charger_delta" in calls, (
        "`prepare` 가 증설을 적용하지 않는다 — MNL 바닥만 조용히 증설 없이 돈다")


def test_the_delta_actually_changes_the_simulated_supply(tmp_path) -> None:
    """끝에서 끝까지 — 증설한 run 의 충전기 수가 실제로 늘어나는가."""

    # ★ 이 검사는 **진짜 DB** (`evdt.db`) 의 노선·휴게소·충전기를 쓴다. CI 러너에는
    #   없으므로 생략한다 (`test_issue134_audit.py` 와 같은 관용구). 로컬에서는 돈다
    from pathlib import Path as _P

    import pytest as _pytest

    from evdt.paths import default_db_path as _dbp

    if not _P(_dbp()).exists():
        _pytest.skip("기본 DB 가 없어 생략 (python -m evdt.io.db init 으로 만든다)")

    import pandas as pd

    from evdt.runner import run_once

    cfg = with_charger_delta(load_config(CFG, stage="S0"), {GEONCHEON: 4})
    rid = run_once(cfg, seed=1, overwrite=True, runs_dir=tmp_path,
                   log=lambda *_: None, gap_plot=False)
    ce = pd.read_parquet(tmp_path / rid / "charge_event.parquet")
    used = ce[ce["station_id"] == GEONCHEON]["charger_id"].nunique()

    # 건천은 DB 에 2기다. 4기를 더하면 6기 — 2기보다 많이 쓰여야 한다
    assert used > 2, f"증설했는데 쓰인 충전기가 {used}기다 (증설 전 2기)"
    assert used <= 6, f"있는 것보다 많이 쓰였다: {used}기"
