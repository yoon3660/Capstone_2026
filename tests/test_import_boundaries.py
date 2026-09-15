"""설계 규칙 2를 코드로 강제한다 (설계문서 §4.1).

    world/ 는 engine/ 을 임포트하지 않는다.

이걸 문서로만 두면 3주 뒤에 누군가 편의상 한 줄 임포트하고, 그 순간
시뮬레이터가 어떤 스테이지가 도는지 알게 된다. 그러면 "같은 세계에서
decide 만 다르다" 는 공정 비교의 전제가 조용히 깨진다.

AST 로 검사하므로 모듈을 실제로 임포트하지 않아도 잡힌다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "evdt"

# (검사 대상 패키지, 임포트하면 안 되는 패키지)
FORBIDDEN = [
    ("world", "evdt.engine"),
    ("viz", "evdt.engine"),
    ("viz", "evdt.world"),   # 렌더러의 입력은 스냅샷 스트림뿐이다 (설계 규칙 4)
    ("io", "evdt.world"),
    ("io", "evdt.engine"),
]


def _imported_modules(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))
    return out


@pytest.mark.parametrize(("package", "forbidden"), FORBIDDEN)
def test_layer_does_not_import(package: str, forbidden: str) -> None:
    pkg_dir = SRC / package
    offenders = []
    for py in sorted(pkg_dir.rglob("*.py")):
        for mod, lineno in _imported_modules(py):
            if mod == forbidden or mod.startswith(forbidden + "."):
                offenders.append(f"{py.relative_to(SRC)}:{lineno} → {mod}")
    assert not offenders, (
        f"evdt.{package} 가 {forbidden} 를 임포트한다:\n  " + "\n  ".join(offenders)
    )


def test_interfaces_module_has_no_internal_dependencies() -> None:
    """interfaces.py 는 계약만 담는다. 여기서 world/engine 을 임포트하면 순환이 생긴다."""
    for mod, _ in _imported_modules(SRC / "interfaces.py"):
        assert not mod.startswith("evdt."), f"interfaces.py 가 {mod} 를 임포트한다"


def test_every_subpackage_has_an_init() -> None:
    for name in ("world", "engine", "io", "viz"):
        assert (SRC / name / "__init__.py").is_file(), f"evdt/{name}/__init__.py 가 없다"
