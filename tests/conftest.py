from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from evdt.config import ScenarioConfig
from evdt.io.db import get_conn, init_db, upsert_df

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """빈 스키마가 적용된 임시 DB."""
    return init_db(tmp_path / "test.db")


@pytest.fixture
def seeded_db(db_path: Path) -> Path:
    """corridor / station / charger 가 한 줄씩 들어 있는 임시 DB."""
    with get_conn(db_path) as conn:
        upsert_df(conn, "corridor", pd.DataFrame([{
            "corridor_id": "gyeongbu_down", "name": "경부고속도로", "direction": "DOWN",
            "origin_name": "Seoul", "dest_name": "Busan", "length_km": 416.0,
        }]))
        upsert_df(conn, "station", pd.DataFrame([{
            "station_id": "st_anseong", "corridor_id": "gyeongbu_down",
            "name": "안성휴게소", "direction": "DOWN", "offset_km": 62.0,
            "lat": 37.0075, "lon": 127.27, "source": "test",
        }]))
        upsert_df(conn, "charger", pd.DataFrame([{
            "charger_id": "ch_anseong_200", "station_id": "st_anseong",
            "power_kw": 200.0, "n_units": 4, "source": "test",
        }]))
    return db_path


@pytest.fixture
def cfg() -> ScenarioConfig:
    return ScenarioConfig.from_yaml(ROOT / "config" / "scenario_seollal_down.yaml")
