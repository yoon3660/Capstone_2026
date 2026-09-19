import random
from pathlib import Path

import pandas as pd
import pytest

from evdt.world.geometry import Polyline


def test_point_at():
    """두 지점 사이의 좌표를 보간한다."""
    line = Polyline.from_points(
        [(35.0, 129.0), (35.1, 129.1)],
        offsets=[0.0, 10.0],
    )

    assert line.point_at(5.0) == pytest.approx(
        (35.05, 129.05)
    )


def test_round_trip():
    """좌표로 변환했다가 이정으로 돌아오면 원래 값과 같다."""
    line = Polyline.from_points(
        [(35.0, 129.0), (35.1, 129.1)],
        offsets=[0.0, 10.0],
    )

    original_offset = 5.3

    lat, lon = line.point_at(original_offset)
    recovered_offset, distance = line.offset_of(lat, lon)

    assert recovered_offset == pytest.approx(
        original_offset, abs=0.001
    )
    assert distance < 0.001


def test_out_of_range():
    """범위 밖의 이정을 요청하면 오류가 발생한다."""
    line = Polyline.from_points(
        [(35.0, 129.0), (35.1, 129.1)],
        offsets=[0.0, 10.0],
    )

    with pytest.raises(ValueError):
        line.point_at(-1.0)

    with pytest.raises(ValueError):
        line.point_at(11.0)


def test_offsets_must_increase():
    """이정이 증가하지 않으면 중심선을 생성하지 않는다."""
    with pytest.raises(ValueError):
        Polyline.from_points(
            [(35.0, 129.0), (35.1, 129.1)],
            offsets=[10.0, 5.0],
        )


def test_offsets_can_be_calculated():
    """이정을 전달하지 않으면 좌표 간 거리를 누적한다."""
    line = Polyline.from_points(
        [(35.0, 129.0), (35.001, 129.001)]
    )

    assert line.offsets[0] == 0.0
    assert line.offsets[1] > 0.0


def test_real_centerline_round_trip():
    """실제 경부선 50개 지점의 거리 → 좌표 → 거리 왕복 검증."""

    parquet_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "processed"
        / "centerline_gyeongbu.parquet"
    )

    if not parquet_path.exists():
        pytest.skip("경부선 Parquet 파일이 없어 실제 데이터 검증 생략")

    df = pd.read_parquet(parquet_path)

    line = Polyline.from_points(
        df[["lat", "lon"]].itertuples(index=False, name=None),
        offsets=df["offset_km"],
    )

    rng = random.Random(23)

    for _ in range(50):
        original = rng.uniform(
            line.offsets[0],
            line.offsets[-1],
        )

        lat, lon = line.point_at(original)
        recovered, snap_km = line.offset_of(lat, lon)

        # 거리 왕복 오차 1m 이내
        assert abs(recovered - original) < 0.001

        # 자신이 생성한 좌표이므로 중심선까지 거리도 1m 이내
        assert snap_km < 0.001