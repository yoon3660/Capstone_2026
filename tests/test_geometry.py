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

def test_build_centerline_route(tmp_path, monkeypatch):
    """중심선을 IC 사이에서 자르고 구서IC를 0km로 맞춘다."""
    import importlib
    from pathlib import Path

    import pandas as pd
    import pytest

    # scripts/build_route.py를 불러올 수 있도록 경로 설정
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    route_builder = importlib.import_module("build_route")

    # 테스트용 중심선: 이정 0.0~0.4km
    df = pd.DataFrame({
        "offset_km": [0.0, 0.1, 0.2, 0.3, 0.4],
        "lat": [35.0] * 5,
        "lon": [129.000, 128.999, 128.998, 128.997, 128.996],
    })
    df.to_parquet(tmp_path / "centerline_gyeongbu.parquet", index=False)

    # 실제 프로젝트의 Parquet 대신 테스트용 파일을 읽도록 설정
    monkeypatch.setattr(route_builder, "DATA_PROCESSED_DIR", tmp_path)

    line = Polyline.from_points(
        df[["lat", "lon"]].itertuples(index=False, name=None),
        offsets=df["offset_km"],
    )

    # 구서IC = 0.05km, 양재IC = 0.35km로 가정
    origins = {
        "UP": line.point_at(0.05),
        "DOWN": line.point_at(0.35),
    }

    route = route_builder.build_centerline_route(origins)

    # 두 IC 사이의 길이만 사용해야 한다.
    assert route.length_km == pytest.approx(0.3, abs=1e-6)

    # 구서IC를 0km로 맞추고 이정이 증가해야 한다.
    assert route.mileposts[0] == 0.0
    assert len(route.points) == 5
    assert all(
        a < b
        for a, b in zip(route.mileposts, route.mileposts[1:])
    )

    # 각 방향의 출발점은 0km다.
    assert route.offset_of(*origins["UP"], "UP") == pytest.approx(0.0, abs=1e-6)
    assert route.offset_of(*origins["DOWN"], "DOWN") == pytest.approx(0.0, abs=1e-6)

    # 기존 JSON 저장·불러오기 방식과도 호환되는지 확인
    from evdt.io.route import GyeongbuRoute

    test_path = tmp_path / "test_route.json"
    route.save(test_path, raw_dir="test_fixture")
    loaded = GyeongbuRoute.load(test_path)

    assert loaded.length_km == pytest.approx(0.3, abs=1e-6)