"""스크립트 공통 준비 작업. 다른 임포트보다 **먼저** 불러야 한다.

    import _bootstrap  # noqa: F401

하는 일 두 가지:

1. `src/` 를 임포트 경로에 넣는다.
   `pip install -e .` 를 아직 안 한 상태에서도 스크립트가 돌아야
   "설치가 안 돼서 설치 스크립트를 못 돌리는" 상황을 피할 수 있다.

2. 표준 출력의 인코딩 오류를 치명적이지 않게 만든다.
   한국어 Windows 콘솔은 기본이 cp949 다. 출력 문자열에 cp949 로 표현할 수 없는
   글자(≥, ⭐, 이모지 등)가 섞이면 UnicodeEncodeError 로 스크립트가 **중간에 죽는다**.
   실패 원인과 아무 상관없는 곳에서 죽기 때문에 원인 찾기가 어렵다.
   errors="replace" 로 바꿔서, 그런 글자는 '?' 로 나오고 스크립트는 끝까지 돈다.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 프로젝트 루트 (scripts/ 의 부모)
ROOT: Path = Path(__file__).resolve().parents[1]

_SRC = str(ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        pass  # 리다이렉트된 스트림 등 — 그냥 넘어간다
