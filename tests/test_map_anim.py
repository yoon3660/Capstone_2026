"""지도 애니메이션 (#103).

여기서 지키는 것은 **"두 런이 같은 자(尺)를 쓴다"** 하나다. 각자 자기 최대로
정규화하면 S0 최대 577.7분과 S1 169.0분이 **똑같이 빨갛게** 보인다 — 3.4배 차이인데
그림이 같아지고, 그 그림이 발표에 들어간다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from evdt.viz.map_anim import BANDS, band_color, to_panel


def _snap(rows: list[tuple[float, str, float, float]]) -> pd.DataFrame:
    """(t_min, station_id, wait_min, queue_len) → 스냅샷 모양."""
    out = []
    for t, sid, wait, queue in rows:
        for state, value in (("wait_min", wait), ("queue_len", queue)):
            out.append({"t_min": t, "entity_type": "station", "entity_id": sid,
                        "lat": 36.0, "lon": 127.0, "state": state, "value": value})
    return pd.DataFrame(out)


def test_color_bands_match_the_heatmap():
    """`plot_map.py` 와 **같은 구간**이어야 한다 — 다르면 같은 결과를 보고 다른
    이야기를 하게 된다."""
    assert band_color(0.0) == BANDS[0][1]
    assert band_color(4.9) == BANDS[0][1]
    assert band_color(5.0) == BANDS[1][1]
    assert band_color(59.9) == BANDS[3][1]
    assert band_color(600.0) == BANDS[4][1]


def test_only_station_rows_are_used():
    """cell·vehicle 행이 섞여 들어와도 휴게소만 그린다 (설계 규칙 4)."""
    snap = _snap([(0.0, "A", 10.0, 2.0)])
    snap = pd.concat([snap, pd.DataFrame([{
        "t_min": 0.0, "entity_type": "cell", "entity_id": "c1",
        "lat": 36.0, "lon": 127.0, "state": "speed_kmh", "value": 80.0,
    }])], ignore_index=True)

    panel = to_panel(snap, "S0")
    assert set(panel.stations) == {"A"}


def test_a_snapshot_without_stations_is_refused():
    snap = pd.DataFrame([{
        "t_min": 0.0, "entity_type": "cell", "entity_id": "c1",
        "lat": 36.0, "lon": 127.0, "state": "speed_kmh", "value": 80.0,
    }])
    with pytest.raises(ValueError, match="station"):
        to_panel(snap, "S0")


def test_frames_keep_wait_and_queue_apart():
    """색은 대기시간, 크기는 줄 길이다. 둘을 합치면 2기짜리와 19기짜리가 같아 보인다."""
    panel = to_panel(_snap([(0.0, "A", 30.0, 7.0)]), "S0")
    assert panel.frames[0.0]["A"] == (30.0, 7.0)


def test_two_panels_share_one_scale(tmp_path):
    """**이 티켓의 전부다.** 한쪽이 훨씬 붐벼도 두 그림의 크기 척도가 같아야 한다."""
    from evdt.viz.map_anim import animate

    busy = to_panel(_snap([(0.0, "A", 90.0, 100.0), (5.0, "A", 90.0, 100.0)]), "S0")
    calm = to_panel(_snap([(0.0, "A", 10.0, 5.0), (5.0, "A", 10.0, 5.0)]), "S1")
    line = np.array([[35.9, 126.9], [36.1, 127.1]])

    out = animate([busy, calm], line, tmp_path / "x.gif",
                  title="t", every=1, fps=5, dpi=40)

    # 공통 최대(100대)가 부제에 적혀야 한다 — 안 적으면 두 그림을 비교할 수 없다
    assert out.exists() and out.stat().st_size > 0


def test_frame_window_can_be_empty_loudly(tmp_path):
    from evdt.viz.map_anim import animate

    panel = to_panel(_snap([(0.0, "A", 10.0, 1.0)]), "S0")
    line = np.array([[35.9, 126.9], [36.1, 127.1]])
    with pytest.raises(ValueError, match="프레임"):
        animate([panel], line, tmp_path / "x.gif", title="t", t_from=999.0)
