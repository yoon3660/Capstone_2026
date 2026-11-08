"""휴게소 편의시설 선호 (#146).

검사할 것은 셋이다.

1. **꺼져 있으면 비트 단위로 같다** — 아니면 "선호를 켰더니 달라졌다" 를 증명할 수 없다
2. **결정비용에만 들어간다** — 물리(원장)와 사적비용(이탈 판정)은 건드리지 않는다.
   큰 휴게소의 대기를 0.7배로 **기록**하면 거기로 몰아넣을수록 "대기가 줄었다" 고
   보고된다. 이름만 바꿔서 이기는 것이다
3. **조용한 0 이 없다** — 지수가 없는 휴게소는 멈춘다. a_j=0 과 "껐다" 는 구분돼야 한다
"""

from __future__ import annotations

import math

import pytest

from evdt.engine.amenity import NO_AMENITY, AmenityPrefs


# ---------------------------------------------------------------------------
# 꺼짐이 진짜 꺼짐인가
# ---------------------------------------------------------------------------


def test_off_by_default() -> None:
    assert not NO_AMENITY.enabled
    assert not NO_AMENITY.needs_wait


def test_off_returns_exactly_zero() -> None:
    """0.0 이어야 한다 — 1e-18 도 안 된다. 부동소수 덧셈이 순서를 바꾼다."""
    out = NO_AMENITY.adjust("anywhere", 999.0)

    assert out == 0.0
    assert math.copysign(1.0, out) > 0      # -0.0 도 아니다


def test_zero_knobs_with_an_index_is_still_off() -> None:
    prefs = AmenityPrefs(index={"a": 1.0}, source="t")

    assert not prefs.enabled
    assert prefs.adjust("a", 50.0) == 0.0


# ---------------------------------------------------------------------------
# 식이 맞는가 — 항이 둘이다
# ---------------------------------------------------------------------------


def test_pull_works_without_any_queue() -> None:
    """**이게 핵심이다.** β 는 대기가 0 이어도 작동해야 한다.

    (θ−1)·W 는 W≈0 이면 0 이라, θ 만으로는 혼잡 없는 세계에서 쏠림을 만들 수 없다.
    실제 운전자가 큰 휴게소로 가는 것은 줄이 짧아서가 아니다 — McFadden (1974) 의
    대안 고유 상수다.
    """
    prefs = AmenityPrefs(pull=10.0, index={"big": 1.0, "small": 0.0}, source="t")

    assert prefs.adjust("big", wait_min=0.0) == -10.0
    assert prefs.adjust("small", wait_min=0.0) == 0.0


def test_patience_is_dead_without_a_queue() -> None:
    """γ 만 켜고 대기가 0 이면 아무 일도 일어나지 않는다 — 설계상 그렇다."""
    prefs = AmenityPrefs(patience=4.0, index={"big": 1.0}, source="t")

    assert prefs.adjust("big", wait_min=0.0) == 0.0
    assert prefs.adjust("big", wait_min=30.0) < 0.0


def test_theta_is_exp_minus_gamma_a() -> None:
    prefs = AmenityPrefs(patience=2.0, index={"big": 1.0, "mid": 0.5}, source="t")

    assert prefs.theta("big") == pytest.approx(math.exp(-2.0))
    assert prefs.theta("mid") == pytest.approx(math.exp(-1.0))
    assert prefs.theta("unknown") == 1.0          # 지수가 없으면 효과 없음


def test_both_terms_add() -> None:
    prefs = AmenityPrefs(pull=5.0, patience=1.0, index={"a": 1.0}, source="t")
    want = -5.0 + (math.exp(-1.0) - 1.0) * 10.0

    assert prefs.adjust("a", 10.0) == pytest.approx(want)


def test_a_better_station_is_never_less_attractive() -> None:
    prefs = AmenityPrefs(pull=3.0, patience=1.0,
                         index={f"s{i}": i / 10 for i in range(11)}, source="t")
    vals = [prefs.adjust(f"s{i}", 20.0) for i in range(11)]

    assert vals == sorted(vals, reverse=True)      # 지수가 클수록 비용이 작다


def test_negative_wait_is_clamped() -> None:
    """대기가 음수로 들어오면 부호가 뒤집혀 '나쁜 휴게소가 더 좋아진다'."""
    prefs = AmenityPrefs(patience=2.0, index={"a": 1.0}, source="t")

    assert prefs.adjust("a", -5.0) == 0.0


# ---------------------------------------------------------------------------
# 조용한 0 이 없는가
# ---------------------------------------------------------------------------


def test_enabled_without_an_index_raises() -> None:
    with pytest.raises(ValueError, match="지수가 비어 있다"):
        AmenityPrefs(pull=5.0)


def test_index_out_of_range_raises() -> None:
    with pytest.raises(ValueError, match="0~1"):
        AmenityPrefs(pull=5.0, index={"a": 1.4}, source="t")


def test_require_catches_a_missing_station() -> None:
    prefs = AmenityPrefs(pull=5.0, index={"a": 1.0}, source="t")

    with pytest.raises(ValueError, match="지수가 없는 휴게소"):
        prefs.require(["a", "b"])


def test_require_is_silent_when_off() -> None:
    NO_AMENITY.require(["a", "b", "c"])        # 꺼져 있으면 검사하지 않는다


@pytest.mark.parametrize("kw", [{"pull": -1.0}, {"patience": -1.0}])
def test_negative_knobs_raise(kw) -> None:
    with pytest.raises(ValueError):
        AmenityPrefs(index={"a": 1.0}, source="t", **kw)


def test_needs_wait_only_when_gamma_is_on() -> None:
    """γ=0 이면 대기 질의를 **아예 하지 않는다** — 후보마다 원장을 한 번 덜 부른다."""
    assert not AmenityPrefs(pull=9.0, index={"a": 1.0}, source="t").needs_wait
    assert AmenityPrefs(patience=0.5, index={"a": 1.0}, source="t").needs_wait


# ---------------------------------------------------------------------------
# 정책에 붙었을 때 — 결정만 바뀌고 물리는 안 바뀐다
# ---------------------------------------------------------------------------


def test_s2_refuses_gamma() -> None:
    """S2 이상은 체류를 예측으로 묻는데 편의시설 대기 질의는 예측을 안 본다.

    섞으면 같은 후보의 대기가 두 값이 되고, 어느 쪽이 결정을 바꿨는지 설명할 수 없다.
    조용히 하나를 고르는 대신 멈춘다.
    """
    from evdt.engine.s2 import S2Policy, S2Settings

    prefs = AmenityPrefs(patience=1.0, index={"a": 1.0}, source="t")
    policy = S2Policy(S2Settings(
        buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8, max_stops=2,
        amenity=prefs))

    with pytest.raises(NotImplementedError, match="체감 대기 계수"):
        policy._amenity(None, "a", None, None)


def test_s2_allows_pull_only() -> None:
    from evdt.engine.s2 import S2Policy, S2Settings

    prefs = AmenityPrefs(pull=7.0, index={"a": 1.0}, source="t")
    policy = S2Policy(S2Settings(
        buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8, max_stops=2,
        amenity=prefs))

    assert policy._amenity(None, "a", None, None) == -7.0


def test_the_runner_lets_every_stage_take_amenity_options() -> None:
    """편의시설은 `S0Settings` 에 있어서 전 스테이지가 쓴다.

    한 칸만 받으면 그 차이가 **정책 차이**로 보고된다. `engine_opts` 의 다른 키는
    S3·S4·MEC 전용이라는 제약이 그대로 남아 있어야 한다.
    """
    from evdt.runner import AMENITY_KEYS

    assert set(AMENITY_KEYS) == {"amenity_pull", "amenity_patience", "amenity_source"}
