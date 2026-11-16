"""T1 진단의 불변식 — **실측과 모델의 분모가 같은 자리에서만 견준다**.

2026-11-14 에 이 검사가 없어서 틀린 진단을 두 번 냈다. 둘 다 "같은 자가 아닌
두 숫자를 나란히 놓은" 것이었다:

  ① 운영사 범위   실측(환경부 원장)은 **공공 운영사(환경부·한전) 충전기만**
                 담는데 모델은 전 운영사다. 보정 없이 점유율을 견주면
                 모델 오차가 아니라 **공공 보급률 차이**를 재게 된다
  ② 기간         실측은 **10일**이고 모델은 시드당 **하루**다. 1기당 부하를
                 그냥 견주면 10배 어긋난다
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_choice_fit.py"


@pytest.fixture(scope="module")
def mod():
    name = "diagnose_choice_fit_under_test"
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.pop(0)


def test_the_ledger_covers_public_operators_not_just_the_ministry(mod) -> None:
    """★ 환경부 원장은 **환경부 + 한국전력공사** 를 담는다.

    처음에는 `statId` 가 "ME" 로 시작하는 것만 공공으로 셌다. 그러면 **양산
    UP** 이 설명되지 않는다 — 한국전력공사 4기뿐인데 실측 140건이 있다.

    `core_validation_table_153.md` 이 "환경부 제공 거래 전체를 ME-only 로
    단정하지 않는다" 고 적어 둔 것이 맞았다. 운영사 집합을 좁히면 관측된
    휴게소를 '미관측' 으로 잘못 분류한다.
    """

    assert "한국전력공사" in mod.PUBLIC_OPERATORS, (
        "한전을 빼면 양산 UP(한전 4기 · 실측 140건)이 설명되지 않는다")
    assert "기후에너지환경부" in mod.PUBLIC_OPERATORS


def test_the_observation_period_is_divided_out(mod) -> None:
    """실측 10일 ÷ 모델 하루. 안 나누면 1기당 부하가 10배로 보인다."""

    assert mod.OBS_DAYS == 10, "#167 은 2026-02-13~22 의 10일치다"
    src = SCRIPT.read_text(encoding="utf-8")
    assert "/ OBS_DAYS" in src, (
        "1기당 부하에서 관측 일수를 안 나눈다 — 10배 어긋난 값을 견주게 된다")


def test_the_unobserved_stations_are_not_treated_as_zero(mod) -> None:
    """UP 칠곡·옥천은 **공공 충전기가 0기**다 — 0건이 아니라 N/A 다.

    칠곡 UP = SK일렉링크5 + 브라이트에너지파3 · 옥천 UP = SK시그넷7.
    환경부 원장으로는 **영원히 관측되지 않는다.** 0 으로 넣으면 "모델이 과대
    배정한다" 는 결론이 그 두 칸에서 자동으로 나온다.
    """

    assert "칠곡휴게소" not in mod.OBS_UP
    assert "옥천휴게소" not in mod.OBS_UP
    assert len(mod.OBS_UP) == 14, f"UP 관측은 14곳이다: {len(mod.OBS_UP)}"


def test_tvd_normalizes_inside_the_shared_station_set(mod) -> None:
    """TVD 는 **겹치는 휴게소 안에서** 다시 정규화한다.

    모델에만 있는 휴게소를 분모에 넣으면, 관측되지 않은 곳의 질량이 그대로
    오차로 잡힌다. 그건 모델 잘못이 아니라 자료 범위다.
    """

    import pandas as pd

    model = pd.Series({"a": 50.0, "b": 50.0, "없는곳": 900.0})
    obs = pd.Series({"a": 1.0, "b": 1.0})
    v, n = mod.tvd(model, obs)

    assert n == 2
    assert v == pytest.approx(0.0), (
        "겹치는 두 곳의 비율이 같은데 TVD 가 0 이 아니다 — 모델에만 있는 "
        "휴게소가 분모에 들어갔다")


def test_tvd_is_zero_for_identical_shares_and_one_for_disjoint(mod) -> None:
    import pandas as pd

    obs = pd.Series({"a": 3.0, "b": 1.0})
    same, _ = mod.tvd(pd.Series({"a": 300.0, "b": 100.0}), obs)
    assert same == pytest.approx(0.0)

    flip, _ = mod.tvd(pd.Series({"a": 1.0, "b": 3.0}), obs)
    assert flip == pytest.approx(0.5)      # |0.25-0.75| + |0.75-0.25| = 1 → /2

    gone, _ = mod.tvd(pd.Series({"a": 1.0, "b": 0.0}), obs)
    assert gone == pytest.approx(0.25)


def test_station_matching_is_by_coordinates_not_name(mod) -> None:
    """★ API 의 `statNm` 은 한 휴게소를 여러 이름으로 적는다.

        입장거봉포도(서울) 휴게소   ME178010   (환경부)
        입장휴게소 (서울)          ST441325   (SK일렉링크)   ← 같은 휴게소

    이름으로 맞추면 뒤쪽을 놓쳐서 입장거봉포도를 "공공 100%" 로 잘못 센다
    (실제 5/8 = 62%). 그러면 ③ 판정 부분집합에 잘못 들어간다.
    """

    src = SCRIPT.read_text(encoding="utf-8")
    assert "haversine" in src, "좌표 짝짓기가 없다 — 이름으로 맞추면 변형을 놓친다"
    assert mod.MATCH_KM <= 3.0, (
        f"짝짓기 허용 거리 {mod.MATCH_KM} km 가 너무 넓다 — 옆 휴게소를 삼킨다")


def test_haversine_knows_a_short_distance_from_a_long_one(mod) -> None:
    seoul, busan = (37.5665, 126.9780), (35.1796, 129.0756)
    assert 300.0 < mod.haversine(seoul, busan) < 340.0
    assert mod.haversine(seoul, seoul) == pytest.approx(0.0)
    # 같은 휴게소의 상·하행은 보통 1 km 안쪽이다
    assert mod.haversine(seoul, (37.5665, 126.9880)) < 1.0
