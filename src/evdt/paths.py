"""프로젝트 경로 해석.

팀원마다 클론 위치가 다르고, 실행 방식도 다르다 (PyCharm 실행 / 터미널 /
pytest / 노트북). 상대경로를 코드 여기저기에 흩뿌리면 "내 PC 에선 되는데"
문제가 반드시 생긴다. 그래서 루트 탐색을 이 모듈 한 곳에만 둔다.

탐색 순서
    1. 환경변수 EVDT_PROJECT_ROOT
    2. 이 파일에서 위로 올라가며 pyproject.toml 이 있는 첫 디렉터리
    3. 현재 작업 디렉터리 (설치 패키지로만 쓰는 최후의 경우)
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_project_root() -> Path:
    env = os.environ.get("EVDT_PROJECT_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd().resolve()


PROJECT_ROOT: Path = _find_project_root()

CONFIG_DIR: Path = PROJECT_ROOT / "config"
DATA_DIR: Path = PROJECT_ROOT / "data"
DATA_RAW_DIR: Path = DATA_DIR / "raw"
DATA_PROCESSED_DIR: Path = DATA_DIR / "processed"
RUNS_DIR: Path = PROJECT_ROOT / "runs"

#: 패키지에 동봉된 스키마. 설치본에서도 찾을 수 있도록 패키지 안에 둔다.
SCHEMA_PATH: Path = Path(__file__).resolve().parent / "sql" / "schema.sql"


def default_db_path() -> Path:
    """SQLite 마스터 DB 경로. EVDT_DB_PATH 로 덮어쓸 수 있다."""
    env = os.environ.get("EVDT_DB_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT / "evdt.db"


def run_dir(run_id: str) -> Path:
    """runs/<run_id> 경로 (생성은 하지 않는다)."""
    return RUNS_DIR / run_id


def ensure_dirs() -> None:
    """프로젝트가 기대하는 디렉터리를 만들어 둔다. 여러 번 불러도 안전."""
    for d in (CONFIG_DIR, DATA_RAW_DIR, DATA_PROCESSED_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)
