"""`run_mnl_world` 의 총비용이 사다리와 **같은 정의**인가 (#147 · 2026-11-14).

MNL 바닥과 사다리 칸을 나란히 놓고 "엔진이 몇 % 줄였나" 를 말하려면 두 숫자가
**같은 자**여야 한다. 검증 리스트 내적 2번(동일 비용 정의)이 그것이다.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_mnl_world.py"


@pytest.fixture(scope="module")
def mod():
    """스크립트를 모듈로 읽는다 (`scripts/` 는 패키지가 아니다)."""

    name = "run_mnl_world_under_test"
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        # ★ `exec_module` **전에** 등록해야 한다. 이 스크립트의 `@dataclass` 가
        #   문자열 어노테이션을 풀 때 `sys.modules[cls.__module__]` 를 보는데,
        #   없으면 dataclasses 안에서 AttributeError 로 죽는다
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.pop(0)


def test_cost_terms_adds_all_three_terms(mod) -> None:
    """★ 총비용 = 체류 + **이탈 비용** + **포기 전 대기**.

    2026-11-14 까지 이 스크립트는 `체류 + 이탈 대수 x escape_cost_min` 으로
    **두 항만** 셌다. `runner.social_cost_h` 의 docstring 이 바로 그 식의 두
    결함을 적어 둔 식이다:

      ① 이탈 비용은 차마다 다르다 — 중앙값 x 대수로 세면 과대계상된다
      ② 포기 전 대기를 안 더했다 — 포기한 차는 인내만큼 기다리다 떠났다

    그래서 사다리(세 항)와 MNL 바닥(두 항)을 견주면 **비용 정의가 다른 두
    숫자**를 견주게 된다. 추석 무대 S0 에서 포기 전 대기가 43.9h 였다.
    """

    events = [{"dwell_min": 30.0}, {"dwell_min": 90.0}]            # 2.0 h
    escapes = [
        # 차마다 이탈 비용이 다르다 — 평균이 아니라 **합**이어야 한다
        {"reason": "reneged", "escape_cost_min": 120.0, "waited_min": 60.0},
        {"reason": "reneged", "escape_cost_min": 60.0, "waited_min": 30.0},
        {"reason": "no_plan", "escape_cost_min": 120.0, "waited_min": 0.0},
    ]
    dwell_h, esc_h, renege_h = mod.cost_terms(events, escapes)

    assert dwell_h == pytest.approx(2.0)
    assert esc_h == pytest.approx(300.0 / 60.0), "이탈 비용을 차마다 더하지 않는다"
    assert renege_h == pytest.approx(90.0 / 60.0), "포기 전 대기가 빠졌다"
    assert dwell_h + esc_h + renege_h == pytest.approx(2.0 + 5.0 + 1.5)


def test_the_mean_times_count_shortcut_is_wrong_when_costs_differ(mod) -> None:
    """중앙값 x 대수가 **왜** 안 되는지를 숫자로 고정한다.

    이탈은 인내가 짧은 쪽에 쏠린다. 그 차들은 이탈 비용도 작은 경우가 많아,
    전역값 x 대수로 세면 실제와 어긋난다. `escape_cost_sigma: 0.0` 인 동안만
    우연히 맞는다 — 그 우연에 기대면 안 된다.
    """

    escapes = [{"reason": "balked", "escape_cost_min": c, "waited_min": 0.0}
               for c in (30.0, 45.0, 120.0)]
    _, esc_h, _ = mod.cost_terms([], escapes)

    assert esc_h == pytest.approx(195.0 / 60.0)
    shortcut = len(escapes) * 120.0 / 60.0            # 옛 식: 대수 x 전역 이탈 비용
    assert shortcut == pytest.approx(6.0)
    assert esc_h < shortcut, "옛 식이 과대계상한다는 사실이 사라졌다"


def test_the_cost_formula_lives_in_exactly_one_place() -> None:
    """식은 **한 곳**에만 둔다.

    #147 의 사고는 같은 식이 여덟 벌 있다가 두 가지로 틀어진 것이었다. 이
    파일 안에서도 `run_cell` 과 `run_ue_cell` 에 두 벌이 있었고 **둘 다**
    포기 전 대기를 빼먹고 있었다. 한 벌을 고치고 다른 한 벌을 놓치면 UE 칸만
    조용히 다른 자로 남는다.
    """

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    defs = [n.name for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "cost_terms"]
    assert len(defs) == 1, f"`cost_terms` 가 {len(defs)}벌이다"

    body = SCRIPT.read_text(encoding="utf-8")
    # 체류에 이탈 **대수**를 곱해 더하는 옛 식이 남아 있으면 안 된다
    assert "n_esc * esc_h" not in body, (
        "옛 두 항 식(`n_esc * esc_h`)이 남아 있다 — 포기 전 대기가 빠진 자다")
    assert body.count("cost_terms(events, escapes)") >= 2, (
        "비용을 세는 자리가 모두 `cost_terms` 를 쓰지 않는다")
