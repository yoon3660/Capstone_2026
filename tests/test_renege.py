"""기다리다 포기한다 (#147).

검사할 것은 넷이다.

1. **껐으면 비트 단위로 같다** — 아니면 "켰더니 달라졌다" 를 증명할 수 없다
2. **하나씩 뺀다** — 한 번에 다 빼면 너무 많이 뺀다 (앞차가 빠지면 뒤차는 참는다)
3. **떠난 순서가 맞다** — 인내가 가장 먼저 끝나는 차가 먼저 떠난다
4. **쓴 시간이 안 사라진다** — 포기한 차도 인내만큼은 기다렸다
"""

from __future__ import annotations

import math

import pytest

from evdt.world.queue_rule import Charger, assign, assign_with_renege
from evdt.world.sim import Arrival, StationQueue, expand_chargers

FLAT = ((0.0, 1.0, 10_000.0),)


def _arr(ev_id: str, at: float, *, soc_from: float = 0.2, soc_to: float = 0.8) -> Arrival:
    return Arrival(ev_id=ev_id, arrival_min=at, soc_from=soc_from, soc_to=soc_to,
                   battery_kwh=60.0, vmax_kw=200.0, curve=FLAT)


def _one_charger(power_kw: float = 50.0) -> tuple[Charger, ...]:
    return tuple(expand_chargers(
        [{"charger_id": "c", "power_kw": power_kw, "n_units": 1}]))


# ---------------------------------------------------------------------------
# 껐으면 꺼진 것인가
# ---------------------------------------------------------------------------


def test_no_patience_is_identical_to_plain_assign() -> None:
    arrivals = [_arr(f"e{i}", float(i)) for i in range(6)]
    chargers = _one_charger()

    got, reneged = assign_with_renege(arrivals, chargers, {})

    assert reneged == ()
    assert got == assign(arrivals, chargers)


def test_infinite_patience_never_reneges() -> None:
    """인내에 **없는** 차는 포기하지 않는다. 조용한 0 과 '값 없음' 은 다르다."""
    arrivals = [_arr(f"e{i}", 0.0) for i in range(5)]
    chargers = _one_charger()

    got, reneged = assign_with_renege(arrivals, chargers, {"e0": math.inf})

    assert reneged == ()
    assert len(got) == 5


def test_admit_and_admit_patient_agree_when_patience_is_empty() -> None:
    arrivals = [_arr(f"e{i}", float(i)) for i in range(5)]

    a = StationQueue("S", _one_charger()).admit(arrivals)
    b, reneged = StationQueue("S", _one_charger()).admit_patient(arrivals, {})

    assert a == b
    assert reneged == ()


# ---------------------------------------------------------------------------
# 식이 맞는가
# ---------------------------------------------------------------------------


def test_a_car_that_waits_past_its_patience_leaves() -> None:
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0)]
    # a 가 먼저 붙고 b 는 a 가 끝날 때까지 기다린다 — 그 대기가 5분보다 길다
    plain = assign(arrivals, chargers)
    assert plain[1].wait_min > 5.0

    got, reneged = assign_with_renege(arrivals, chargers, {"a": 999.0, "b": 5.0})

    assert reneged == ("b",)
    assert [a.ev_id for a in got] == ["a"]


def test_exactly_at_the_patience_is_tolerated() -> None:
    """120분 인내에 120분 대기는 **참는다.** 부동소수로 경계가 흔들리면 안 된다."""
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0)]
    wait = assign(arrivals, chargers)[1].wait_min

    _, reneged = assign_with_renege(arrivals, chargers, {"b": wait})

    assert reneged == ()


def test_only_one_leaves_when_that_is_enough() -> None:
    """**이 테스트가 설계의 핵심이다.**

    한 번에 다 빼면 너무 많이 뺀다. 앞차가 빠지면 뒤차의 대기가 줄어 **참을 수
    있게** 되는 경우가 있다. 하나씩 빼는 것이 근사가 아니라 정확하다.
    """
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0), _arr("c", 0.0)]
    plain = assign(arrivals, chargers)
    one_slot = plain[1].wait_min          # 한 대만 앞에 있을 때의 대기

    # b·c 둘 다 "두 대 앞" 은 못 참지만 "한 대 앞" 은 참는다
    patience = {"b": one_slot, "c": one_slot}
    got, reneged = assign_with_renege(arrivals, chargers, patience)

    assert len(reneged) == 1, f"하나만 빠져야 하는데 {reneged} 가 빠졌다"
    assert len(got) == 2


def test_the_first_to_run_out_of_patience_leaves_first() -> None:
    """떠나는 순서는 `arrival_min + 인내` 다 — 실제로 그 차가 먼저 떠난다.

    순서가 틀리면 뒤차의 줄이 잘못 짧아진다.
    """
    chargers = _one_charger()
    # 늦게 왔지만 인내가 짧아 **먼저** 바닥나는 차를 만든다
    arrivals = [_arr("a", 0.0), _arr("late", 1.0), _arr("early", 2.0)]
    patience = {"late": 1.0, "early": 100.0}

    _, reneged = assign_with_renege(arrivals, chargers, patience)

    assert reneged[0] == "late"


def test_leaving_frees_the_charger_for_the_cars_behind() -> None:
    """포기한 차는 충전기를 **안 잡는다** — 그게 포기의 효과다."""
    chargers = _one_charger()
    arrivals = [_arr("a", 0.0), _arr("b", 0.0), _arr("c", 0.0)]

    with_all = assign(arrivals, chargers)
    got, reneged = assign_with_renege(arrivals, chargers, {"b": 0.0})

    assert reneged == ("b",)
    c_before = next(x for x in with_all if x.ev_id == "c").wait_min
    c_after = next(x for x in got if x.ev_id == "c").wait_min
    assert c_after < c_before


def test_everyone_can_leave() -> None:
    """전원이 못 참으면 전원이 떠난다 — 무한 루프가 아니라 빈 배정으로 끝난다."""
    chargers = _one_charger()
    arrivals = [_arr(f"e{i}", 0.0) for i in range(4)]
    # e0 은 대기 0 이라 남는다. 나머지는 인내 0 이면 전부 떠난다
    got, reneged = assign_with_renege(
        arrivals, chargers, {f"e{i}": 0.0 for i in range(4)})

    assert len(got) == 1 and got[0].ev_id == "e0"
    assert len(reneged) == 3


def test_the_fifo_guard_still_fires() -> None:
    """`admit_patient` 도 `admit` 과 **같은 검사**를 거친다."""
    q = StationQueue("S", _one_charger())
    q.admit_patient([_arr("a", 20.0)], {})

    with pytest.raises(ValueError, match="거꾸로"):
        q.admit_patient([_arr("b", 10.0)], {})


# ---------------------------------------------------------------------------
# 배선 — 손잡이와 기록
# ---------------------------------------------------------------------------


def test_the_dataclass_default_stays_off() -> None:
    """**코드** 기본값은 꺼짐이고, **config** 가 켠다 (2026-11-10 결정).

    둘을 가르는 이유: 손으로 만든 `DemandConfig`(테스트·실험 코드)가 조용히
    물리를 바꾸면 안 된다. 켠 것은 눈에 보이는 config 한 줄이어야 한다.
    실제 시나리오가 빼먹는 것은 `test_every_scenario_with_an_escape_has_renege_on`
    이 잡는다.
    """
    import dataclasses

    from evdt.config import DemandConfig

    assert dataclasses.fields(DemandConfig)
    default = next(f for f in dataclasses.fields(DemandConfig)
                   if f.name == "renege").default

    assert default is False


def test_the_run_records_whether_renege_was_on() -> None:
    """물리가 바뀌는 손잡이는 **반드시 기록에 남아야** 한다 (#101 과 같은 고장)."""
    from evdt.runner import load_config, run_params

    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")

    assert "renege" in run_params(cfg)


def test_an_old_run_cannot_be_reused_once_renege_is_on() -> None:
    import dataclasses

    from evdt.runner import load_config, stale_reason

    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")
    on = dataclasses.replace(
        cfg, demand=dataclasses.replace(cfg.demand, renege=True))

    # 옛 run 에는 renege 칸이 아예 없다
    why = stale_reason({"escape_cost_min": 120.0}, on)

    assert why and "renege" in why


def test_renege_needs_an_escape_cost() -> None:
    """이탈 비용 0 이면 인내도 0 이라 전원이 즉시 포기한다 — 로딩에서 막는다."""
    import yaml

    from evdt.config import ConfigError, ScenarioConfig
    from evdt.paths import CONFIG_DIR

    raw = yaml.safe_load(
        (CONFIG_DIR / "scenario_seollal_down.yaml").read_text(encoding="utf-8"))
    raw["demand"] = {**raw["demand"], "renege": True, "escape_cost_min": 0.0}

    with pytest.raises(ConfigError, match="즉시 포기"):
        ScenarioConfig.from_dict(raw, source="test")


def test_reneged_is_a_separate_escape_reason() -> None:
    """발길질과 섞으면 안 된다 — 엔진이 손댈 자리가 다르다."""
    from evdt.interfaces import ESCAPE_REASON, ESCAPE_RENEGED

    assert ESCAPE_REASON[ESCAPE_RENEGED] == "reneged"
    assert "reneged" not in (ESCAPE_REASON[k] for k in ESCAPE_REASON
                             if k != ESCAPE_RENEGED)


def test_the_escape_schema_carries_the_wasted_wait() -> None:
    """포기한 차가 쓴 시간이 사라지면 사다리가 실제보다 좋아 보인다."""
    from evdt.io.writers import SCHEMAS

    names = SCHEMAS["escape_event"].names

    assert "waited_min" in names


# ---------------------------------------------------------------------------
# 이탈 비용은 **차마다** 다르다 (#78 회귀 — #147 에서 드러났다)
# ---------------------------------------------------------------------------


def test_the_logged_escape_cost_is_the_cars_own(tmp_path) -> None:
    """결정과 기록이 **같은 값**을 써야 한다.

    `corridor_sim` 은 발길질 판단을 차마다의 `escape_cost_min` 으로 하는데
    (`:411`), 기록만 시나리오 중앙값을 적고 있었다. `escape_cost_sigma=0` 이던
    동안은 둘이 같아서 드러나지 않았다. 인내를 분포로 바꾸자 총비용이 틀렸다 —
    설 S1 에서 순진한 `n x 120분` 은 888h, 실제는 359h 다.

    ★ **`runs_dir=tmp_path` 가 필수다.** `make_run_id` 는 (시나리오, 스테이지,
      시드, 참여율) 뿐이라 여기서 인내·포기를 바꿔도 run_id 가 같다. 빼먹으면
      이 테스트가 **진짜 `__S0__p100__s0001` 을 덮어쓴다** — 포기가 꺼지고
      인내가 지수분포인 물리로. 2026-11-11 에 실제로 그랬고, 같은 run 을 읽던
      분석이 조용히 다른 숫자를 냈다 (체류 5,641.8h → 3,773.8h).
      `test_no_test_writes_into_the_real_runs_dir` 가 이제 그걸 막는다.
    """
    import dataclasses

    import pandas as pd

    from evdt.runner import load_config, run_once

    #: ★ **산포를 `escape_cost_sigma` 로 준다** (#147 후속). 전에는
    #:   `patience_kind="exponential"` 로 흩었는데, 인내와 우회 비용을 가른
    #:   뒤로는 인내 가족이 **이탈 비용을 건드리지 않는다** — 그래서 저 손잡이로는
    #:   전원이 120.0 이 되고 이 검사가 무의미해진다. 이탈 비용의 산포는
    #:   이제 이 칸이 맡는다 (지리의 산포 ↔ 선호의 모양).
    cfg = load_config("config/scenario_seollal_up_adoption.yaml", stage="S0")
    cfg = dataclasses.replace(cfg, demand=dataclasses.replace(
        cfg.demand, escape_cost_sigma=0.5, renege=False))

    rid = run_once(cfg, seed=1, overwrite=True, log=lambda *_: None,
                   runs_dir=tmp_path)
    rows = pd.read_parquet(tmp_path / rid / "escape_event.parquet")
    balked = rows[rows["reason"] == "balked"]["escape_cost_min"]

    assert len(balked) > 5, "발길질이 거의 없어 검사가 무의미하다"
    assert balked.nunique() > 1, (
        f"발길질 {len(balked)}건의 이탈 비용이 전부 {balked.iloc[0]}분이다 — "
        "기록이 차마다의 값을 안 쓴다")


def test_the_escape_cost_total_is_a_kpi() -> None:
    """`n_escaped x 중앙값` 으로 대신 셀 수 없다 — 이탈은 짧은 인내에 쏠린다."""
    from evdt.runner import KPI_LABELS

    assert "escape_cost_total_h" in KPI_LABELS


# ---------------------------------------------------------------------------
# 기본값이 **켜짐** 인가 (#147 — 2026-11-10 결정)
# ---------------------------------------------------------------------------


def test_every_scenario_with_an_escape_has_renege_on() -> None:
    """이탈 기제가 있는 config 는 **전부** 포기를 켠다.

    끄면 "이탈 기준 120분" 이라고 선언한 모형에서 372.5분을 기다리는 차가
    생기고, 그 혼란 요인이 S0 에 가장 많이 얹혀 사다리를 부풀린다
    (S0-S2 격차 884.3h 중 540.7h).

    새 config 가 이 줄을 빼먹으면 **조용히 옛 물리로 돌아간다.** 그걸 막는다.
    """
    import yaml

    from evdt.paths import CONFIG_DIR

    missing = []
    for f in sorted(CONFIG_DIR.glob("scenario_*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")).get("demand", {})
        if not float(d.get("escape_cost_min", 0.0)):
            continue                      # 이탈 기제가 없는 config — 켤 수 없다
        if not d.get("renege", False):
            missing.append(f.name)

    assert not missing, f"포기가 꺼진 config: {missing}"


def test_the_default_patience_family_is_point() -> None:
    """분포는 **아직** 기본이 아니다.

    인내 추출이 그대로 이탈 비용이 되므로(`ue_demand.py:385`) 분포로 바꾸면
    조급한 차에게 공짜 탈출구가 열린다 — weibull:0.7 에서 S0 가 수요의
    18.9% 를 잃는다. 둘을 가르기 전까지는 point 다.
    """
    import yaml

    from evdt.paths import CONFIG_DIR

    bad = {}
    for f in sorted(CONFIG_DIR.glob("scenario_*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")).get("demand", {})
        kind = d.get("patience_kind", "point")
        if kind != "point" or float(d.get("escape_cost_sigma", 0.0)):
            bad[f.name] = (kind, d.get("escape_cost_sigma", 0.0))

    assert not bad, (
        f"인내가 분포인 config: {bad} — 이탈 비용과 갈라지기 전까지는 point 다")


# ---------------------------------------------------------------------------
# 총 사회적 비용의 정의가 **한 곳**인가
# ---------------------------------------------------------------------------


def test_social_cost_adds_all_three_terms() -> None:
    from evdt.runner import social_cost_h

    got = social_cost_h({"dwell_total_h": 100.0, "escape_cost_total_h": 20.0,
                         "renege_wait_total_h": 5.0})

    assert got == pytest.approx(125.0)


def test_social_cost_refuses_a_filtered_kpi_dict() -> None:
    """**이 테스트가 핵심이다.**

    처음에는 옛 run 을 위해 `escape_cost_min=` 되돌림을 뒀다. 그 되돌림이 바로
    다음 측정을 조용히 망쳤다: 스크립트들이 KPI 를 `{m: s.get(m) for m in
    METRICS}` 로 추려 넘기는데 METRICS 에 새 칸이 없어서, 멀쩡한 run 인데도
    되돌림이 타고 **설 사다리가 534h 모자랐다.**

    되돌림을 없앴다. 추려 넘기면 **멈춘다.**
    """
    from evdt.runner import social_cost_h

    filtered = {"dwell_total_h": 5_641.8, "n_escaped": 412.0}

    with pytest.raises(KeyError, match="escape_cost_total_h"):
        social_cost_h(filtered)


def test_social_cost_names_every_missing_term() -> None:
    """하나만 말하면 고치고 또 걸린다."""
    from evdt.runner import social_cost_h

    with pytest.raises(KeyError) as e:
        social_cost_h({"dwell_total_h": 1.0})

    assert "escape_cost_total_h" in str(e.value)
    assert "renege_wait_total_h" in str(e.value)


def test_every_filtered_metrics_list_carries_the_three_terms() -> None:
    """추려 넘기는 스크립트는 METRICS 에 세 항을 넣어야 한다."""
    import importlib
    import sys

    from evdt.paths import PROJECT_ROOT
    from evdt.runner import SOCIAL_COST_TERMS

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    bad = {}
    for name in ("ablation_milp", "compare_mec", "sweep_window",
                 "run_chuseok_ladder", "sweep_amenity", "compare_renege"):
        m = importlib.import_module(name)
        missing = [k for k in SOCIAL_COST_TERMS if k not in m.METRICS]
        if missing:
            bad[name] = missing

    assert not bad, f"METRICS 에 빠진 항: {bad}"


def test_no_script_counts_the_total_by_hand() -> None:
    """식이 여덟 벌이었던 게 이 버그의 원인이다. 다시 흩어지면 안 된다."""
    from evdt.paths import PROJECT_ROOT

    offenders = []
    for f in sorted((PROJECT_ROOT / "scripts").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for n, line in enumerate(src.splitlines(), 1):
            if "n_escaped" in line and "escape_cost" in line and "*" in line:
                offenders.append(f"{f.name}:{n}")

    assert not offenders, (
        f"총비용을 손으로 세는 곳: {offenders} — runner.social_cost_h 를 쓸 것")


def test_no_test_writes_into_the_real_runs_dir() -> None:
    """★ 테스트는 **실제 `runs/` 에 쓰지 않는다.**

    `run_once` 는 `runs_dir` 을 안 주면 진짜 `RUNS_DIR` 에 쓴다. 그리고
    `make_run_id` 는 (시나리오, 스테이지, 시드, 참여율) 뿐이라, 물리 손잡이를
    바꿔 돌려도 **run_id 가 같다** — 즉 조용히 덮어쓴다.

    2026-11-11 에 `test_the_logged_escape_cost_is_the_cars_own` 이
    `seollal_2026_up_adoption__S0__p100__s0001` 을 포기 꺼진 지수분포 물리로
    덮어썼다. 사다리 CSV 는 체류 5,641.8h 인데 같은 run 폴더는 3,773.8h 가 돼서,
    그 폴더를 읽던 분석이 **다른 세계의 숫자**를 냈다. `stale_reason` 은 run 을
    *다시 쓸 때* 막는 장치라 이미 덮인 파일은 못 되돌린다.

    `tests/test_s3.py` 가 스윕에 `db_path` 를 요구하는 것과 같은 규칙이다.
    """
    import re

    from evdt.paths import PROJECT_ROOT

    offenders = []
    for f in sorted((PROJECT_ROOT / "tests").glob("test_*.py")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"\brun_once\s*\(", src):
            # 호출 끝까지 괄호를 세어 인자 전체를 본다
            i, depth = m.end() - 1, 0
            while i < len(src):
                depth += (src[i] == "(") - (src[i] == ")")
                if depth == 0:
                    break
                i += 1
            call = src[m.end():i]
            if "runs_dir" in call:
                continue
            line = src[:m.start()].count("\n") + 1
            if "monkeypatch" in call or "setattr" in call:
                continue
            offenders.append(f"{f.name}:{line}")

    assert not offenders, (
        f"`runs_dir` 없이 run_once 를 부르는 테스트: {offenders} — "
        "`runs_dir=tmp_path` 를 줄 것. 안 주면 진짜 run 을 덮어쓴다")
