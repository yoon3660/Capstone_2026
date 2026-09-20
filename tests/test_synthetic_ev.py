from __future__ import annotations

import hashlib
from dataclasses import replace

import pandas as pd
from pandas.testing import assert_frame_equal

from evdt.config import ScenarioConfig
from evdt.io.synthetic_ev import generate_evs
from evdt.paths import CONFIG_DIR

DEST_OFFSET_KM = 415.0


def _config() -> ScenarioConfig:
    """테스트용 설 하행 시나리오 config."""
    return ScenarioConfig.from_yaml(
        CONFIG_DIR / "scenario_seollal_down.yaml"
    )


def _traffic() -> pd.DataFrame:
    """테스트용 3시간 교통량."""
    return pd.DataFrame(
        {
            "hour": [0, 1, 2],
            "volume_veh": [1000.0, 2000.0, 3000.0],
        }
    )


def _generate(
    cfg: ScenarioConfig | None = None,
    traffic: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """반복되는 generate_evs 호출을 한 곳에 모은다."""
    return generate_evs(
        traffic if traffic is not None else _traffic(),
        cfg if cfg is not None else _config(),
        dest_offset_km=DEST_OFFSET_KM,
    )


def _df_hash(df: pd.DataFrame) -> str:
    """DataFrame 내용을 SHA-256 해시로 만든다."""
    values = pd.util.hash_pandas_object(
        df,
        index=True,
    ).values.tobytes()

    return hashlib.sha256(values).hexdigest()


def test_same_seed_produces_identical_evs() -> None:
    """같은 시나리오와 seed면 완전히 같은 차량 집합이 생성된다."""
    first = _generate()
    second = _generate()

    assert_frame_equal(first, second)
    assert _df_hash(first) == _df_hash(second)


def test_hourly_ev_count_matches_volume_times_ev_share() -> None:
    """시간대별 EV 수가 교통량 × EV 비율과 1% 이내로 일치한다."""
    cfg = _config()
    traffic = _traffic()

    evs = _generate(cfg=cfg, traffic=traffic)

    actual = (
        evs.assign(
            hour=(evs["entry_time_min"] // 60).astype(int)
        )
        .groupby("hour")
        .size()
    )

    for row in traffic.itertuples(index=False):
        expected = (
            row.volume_veh
            * cfg.demand.demand_multiplier
            * cfg.demand.ev_share
        )

        generated = int(actual.get(row.hour, 0))

        if expected == 0:
            assert generated == 0
            continue

        relative_error = abs(generated - expected) / expected

        assert relative_error <= 0.01


def test_initial_soc_is_inside_config_range() -> None:
    """생성된 모든 초기 SoC가 config의 [lo, hi] 범위 안에 있다."""
    cfg = _config()
    evs = _generate(cfg=cfg)

    assert not evs.empty

    assert evs["initial_soc"].between(
        cfg.vehicles.soc_beta.lo,
        cfg.vehicles.soc_beta.hi,
        inclusive="both",
    ).all()


def test_zero_ev_share_generates_zero_evs() -> None:
    """EV 비율이 0이면 차량을 한 대도 생성하지 않는다."""
    cfg = _config()

    zero_cfg = replace(
        cfg,
        demand=replace(
            cfg.demand,
            ev_share=0.0,
        ),
    )

    evs = _generate(cfg=zero_cfg)

    assert evs.empty
    assert evs.columns.tolist() == [
        "ev_id",
        "vclass_id",
        "entry_time_min",
        "initial_soc",
        "dest_offset_km",
    ]


def test_destination_offset_is_assigned() -> None:
    """생성된 모든 차량에 전달한 목적지 offset이 기록된다."""
    evs = _generate()

    assert not evs.empty
    assert (evs["dest_offset_km"] == DEST_OFFSET_KM).all()