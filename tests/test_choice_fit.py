"""선택 분배 적합도 지표 (#168, `evdt.eval.choice_fit`)."""

from __future__ import annotations

import math

import pandas as pd

from evdt.eval.choice_fit import counts, fit_metrics, restrict_to_observed, tvd, wape


def _events(rows):
    return pd.DataFrame(rows, columns=["direction", "station_name", "date", "hour"])


def test_tvd_ignores_scale_and_is_bounded():
    a = pd.Series({"x": 10.0, "y": 30.0})
    assert tvd(a, a * 7) == 0.0                         # 총량이 달라도 분배가 같으면 0
    assert tvd(pd.Series({"x": 1.0}), pd.Series({"y": 5.0})) == 1.0
    assert math.isclose(tvd(pd.Series({"x": 1.0, "y": 1.0}), pd.Series({"x": 3.0, "y": 1.0})),
                        0.25)
    assert math.isnan(tvd(pd.Series(dtype=float), a))


def test_wape_counts_total_difference():
    o = pd.Series({"x": 10.0, "y": 10.0})
    assert wape(o, o * 2) == 100.0
    assert wape(o, pd.Series({"x": 10.0})) == 50.0      # 모델에 없는 칸은 0


def test_unobserved_stations_are_dropped_not_zeroed():
    obs = _events([("DOWN", "A", "2026-09-24", 1)])
    model = _events([("DOWN", "A", "2026-09-24", 1), ("DOWN", "B", "2026-09-24", 2),
                     ("UP", "A", "2026-09-24", 3)])
    kept = restrict_to_observed(model, obs)
    assert list(zip(kept.direction, kept.station_name, strict=True)) == [("DOWN", "A")]


def test_fit_metrics_by_direction_and_date():
    obs = _events([("DOWN", "A", "d1", 1), ("DOWN", "B", "d1", 1),
                   ("UP", "C", "d2", 5), ("UP", "C", "d2", 6)])
    model = _events([("DOWN", "A", "d1", 1), ("DOWN", "A", "d1", 1),
                     ("UP", "C", "d2", 5), ("UP", "C", "d2", 6)])
    rows = {(r["direction"], r["date"]): r for r in fit_metrics(obs, model, dates=["d1", "d2"])}

    assert rows[("UP", "ALL")]["station_tvd"] == 0.0
    assert rows[("DOWN", "ALL")]["station_tvd"] == 0.5
    assert rows[("BOTH", "ALL")]["station_tvd"] == 0.25
    assert rows[("DOWN", "d1")]["n_model"] == 2
    assert math.isnan(rows[("DOWN", "d2")]["station_tvd"])     # 그날 하행 실측 없음
    assert counts(obs, ("direction",)).to_dict() == {"DOWN": 2.0, "UP": 2.0}
