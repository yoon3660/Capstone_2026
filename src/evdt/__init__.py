"""evdt — 명절 고속도로 EV 충전 쏠림 완화 디지털 트윈.

레이어 구조 (설계문서 §10):

    world/   디지털 트윈 그 자체 (CTM + SimPy 충전 큐)
    engine/  배정 엔진 (정책, 예약 원장, MILP)
    io/      SQLite 마스터 + Parquet 이벤트 로그
    viz/     스냅샷 → 렌더러

⚠ 의존 방향 규칙 (설계문서 §4.1 규칙 2):
    world/ 는 engine/ 을 임포트하지 않는다.
    시뮬레이터는 Policy 객체를 주입받을 뿐, 어떤 스테이지가 도는지 모른다.
    이것이 공정 비교의 물리적 보장이다. tests/test_import_boundaries.py 가 강제한다.
"""

from evdt.paths import (
    CONFIG_DIR,
    DATA_PROCESSED_DIR,
    DATA_RAW_DIR,
    PROJECT_ROOT,
    RUNS_DIR,
    default_db_path,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "PROJECT_ROOT",
    "CONFIG_DIR",
    "DATA_RAW_DIR",
    "DATA_PROCESSED_DIR",
    "RUNS_DIR",
    "default_db_path",
]
