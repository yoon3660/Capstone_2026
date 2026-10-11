"""OD 를 VDS 주변분포에 맞춘다 — IPF (#111).

여기서 지키는 것은 **"맞췄다고 말하면 정말 맞았어야 한다"** 하나다. IPF 는 수렴하지
않아도 숫자를 내놓으므로, 행·열 합이 목표와 다른 채로 통과하면 아무도 모른다.
"""

from __future__ import annotations

import numpy as np
import pytest

from evdt.io.od_balance import FLOOR_SHARE, balance


def test_the_floor_perturbs_a_fitting_seed_only_by_its_own_share():
    """이미 주변분포를 만족하는 씨앗이라도 **바닥이 조금 흔든다.**

    바닥 없이는 IPF 가 0 칸에 질량을 못 넣으므로 바닥은 필수고, 그 대가가 이것이다.
    흔들리는 양이 **바닥 세기 정도에 머문다**는 것이 보장이다 — 그보다 크면 바닥이
    구조를 덮고 있다는 뜻이다.
    """
    origins = np.array([0.0, 10.0])
    dests = np.array([20.0, 30.0])
    seed = np.array([[30.0, 10.0], [10.0, 50.0]])

    b = balance(seed, origins, dests, seed.sum(axis=1), seed.sum(axis=0))

    moved = 0.5 * np.abs(b.matrix / b.matrix.sum() - seed / seed.sum()).sum()
    assert moved <= 2 * FLOOR_SHARE, f"바닥이 구조를 덮는다 (옮겨진 몫 {moved:.3f})"
    np.testing.assert_allclose(b.matrix.sum(axis=1), seed.sum(axis=1), atol=1e-6)


def test_marginals_are_actually_met():
    origins = np.array([0.0, 10.0, 20.0])
    dests = np.array([30.0, 40.0])
    seed = np.array([[1.0, 2.0], [3.0, 1.0], [1.0, 1.0]])
    entry = np.array([100.0, 200.0, 300.0])
    exit_ = np.array([250.0, 350.0])

    b = balance(seed, origins, dests, entry, exit_)

    np.testing.assert_allclose(b.matrix.sum(axis=1), entry, atol=1e-6)
    np.testing.assert_allclose(b.matrix.sum(axis=0), exit_, atol=1e-6)
    assert b.residual < 1e-8


def test_upstream_cells_stay_empty():
    """종점은 기점보다 하류여야 한다. 바닥을 깔고 나서도 그렇다."""
    origins = np.array([0.0, 50.0])
    dests = np.array([25.0, 75.0])
    seed = np.array([[10.0, 10.0], [0.0, 10.0]])

    b = balance(seed, origins, dests, np.array([40.0, 60.0]), np.array([40.0, 60.0]))

    assert b.matrix[1, 0] == 0.0, "상류로 가는 칸에 질량이 들어갔다"


def test_the_floor_opens_a_cell_the_seed_left_empty():
    """**IPF 는 0 에 무엇을 곱해도 0 이다.**

    TCS 가 못 본 외부 통행이 바로 그 0 칸에 있으므로, 바닥 없이는 주변분포를
    만족시킬 수 없다.
    """
    origins = np.array([0.0, 10.0])
    dests = np.array([20.0, 30.0])
    seed = np.array([[10.0, 0.0], [10.0, 0.0]])   # 둘째 종점이 비어 있다

    b = balance(seed, origins, dests, np.array([50.0, 50.0]), np.array([60.0, 40.0]))

    assert b.matrix[:, 1].sum() == pytest.approx(40.0, abs=1e-6)


def test_unbalanced_marginals_are_refused():
    """행 합과 열 합이 다르면 못 푼다. 조용히 한쪽에 맞추면 총량이 말없이 바뀐다."""
    origins = np.array([0.0])
    dests = np.array([10.0])
    with pytest.raises(ValueError, match="다르다"):
        balance(np.array([[1.0]]), origins, dests, np.array([100.0]), np.array([90.0]))


def test_an_origin_with_nowhere_to_go_is_refused():
    """하류 칸이 없는 기점에 대수가 배정되면 IPF 가 발산한다."""
    origins = np.array([0.0, 100.0])
    dests = np.array([50.0])
    with pytest.raises(ValueError, match="갈 수 있는 칸이 없다"):
        balance(np.zeros((2, 1)), origins, dests, np.array([50.0, 50.0]), np.array([100.0]))


def test_negative_inputs_are_refused():
    with pytest.raises(ValueError, match="음수"):
        balance(np.array([[1.0]]), np.array([0.0]), np.array([10.0]),
                np.array([-1.0]), np.array([-1.0]))
