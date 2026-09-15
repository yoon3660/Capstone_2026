"""설치 파일들이 어느 OS·어느 로케일에서도 읽히는지.

이 테스트가 있는 이유 (실제로 겪은 사고)
    requirements.txt 에 한글 주석을 넣었더니 한국어 Windows 에서 pip 이 터졌다:

        UnicodeDecodeError: 'cp949' codec can't decode byte 0xeb

    pip 은 requirements 파일에 BOM 이나 인코딩 선언이 없으면 **시스템 로케일
    인코딩**으로 디코딩한다. 한국어 Windows 는 cp949 라서 UTF-8 한글 바이트에서
    실패한다. 개발자가 macOS/Linux(UTF-8 로케일)만 쓰면 절대 발견하지 못한다.

    → requirements 파일은 ASCII 로만 쓴다. 이 테스트가 그걸 강제한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: pip / 빌드 도구가 로케일 인코딩으로 읽을 수 있는 파일들
ASCII_ONLY_FILES = [
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
]


@pytest.mark.parametrize("name", ASCII_ONLY_FILES)
def test_install_files_are_ascii(name: str) -> None:
    path = ROOT / name
    raw = path.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        snippet = raw[max(0, exc.start - 30) : exc.start + 30].decode("utf-8", "replace")
        pytest.fail(
            f"{name}:{line} 에 ASCII 가 아닌 문자가 있다 — "
            f"한국어 Windows(cp949)에서 pip 이 UnicodeDecodeError 로 터진다.\n"
            f"  ...{snippet}...\n"
            f"  주석을 영어로 바꿀 것."
        )


@pytest.mark.parametrize("name", ASCII_ONLY_FILES)
def test_install_files_have_no_bom(name: str) -> None:
    """BOM 을 붙이는 것도 해결책이지만, 편집기마다 쉽게 사라진다. ASCII 로 간다."""
    assert not (ROOT / name).read_bytes().startswith(b"\xef\xbb\xbf"), (
        f"{name} 에 UTF-8 BOM 이 붙었다. BOM 대신 ASCII 로 유지할 것."
    )


def test_requirements_dev_includes_runtime() -> None:
    text = (ROOT / "requirements-dev.txt").read_text(encoding="ascii")
    assert "-r requirements.txt" in text, "개발 환경이 런타임 의존성을 놓친다"
