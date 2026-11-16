"""그림마다 **무슨 자료를 썼는지** 같은 꼴로 찍는다.

발표 그림은 질의응답에서 "그 숫자 어디서 나온 겁니까" 를 반드시 받는다. 그때
스크립트를 다시 열어 읽는 대신, 그림을 뽑을 때 **출처가 같이 찍히게** 한다.

    from evdt.viz.sources import Sources

    src = Sources("chuseok_2026_up_congested", seeds=[1, 2, 3])
    src.column("charge_event.wait_min", "실제로 기다린 시간 (앱 화면 값 아님)")
    src.note("구간", "도착 시각 10분")
    src.files("MNL", paths, "θ 0.05 · w 1.0 · ASC 없음")
    src.print()

## 왜 `print` 로 찍나 — 그림 안에 넣지 않고

그림 안에 경로를 적으면 슬라이드에서 읽히지 않을 글씨가 자리만 먹는다. 출처는
**그림을 만든 사람**이 보는 것이고, 필요하면 발표 노트에 옮겨 적는다.

## 경로를 줄여서 찍는다

`runs/chuseok_2026_up_congested__S0__p100__s0001/charge_event.parquet` 가 10개면
열 줄이 된다. 같은 꼴이면 **첫 줄 + 범위**로 접는다.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path


def _fold(paths: Sequence[Path]) -> str:
    """같은 꼴의 경로 여러 개를 '첫 경로 … (N개)' 로 접는다."""

    if not paths:
        return "(없음)"
    if len(paths) == 1:
        return _rel(paths[0])
    return f"{_rel(paths[0])}  … ({len(paths)}개)"


def _rel(p: Path) -> str:
    try:
        return os.path.relpath(p, Path.cwd()).replace("\\", "/")
    except ValueError:                      # 다른 드라이브
        return str(p).replace("\\", "/")


class Sources:
    """한 그림의 자료 출처를 모아 뒀다가 한 번에 찍는다."""

    def __init__(self, scenario: str, *, seeds: Iterable[int] | None = None,
                 config: str | None = None) -> None:
        self.scenario = scenario
        self.config = config
        self.seeds = list(seeds) if seeds is not None else []
        self._rows: list[tuple[str, str, str]] = []

    def note(self, label: str, value: str, extra: str = "") -> None:
        self._rows.append((label, value, extra))

    def column(self, name: str, why: str) -> None:
        """어느 열을 썼나 — 그림마다 다른 열을 쓰면 나란히 못 놓는다."""

        self._rows.append(("열", name, why))

    def files(self, label: str, paths: Sequence[Path], extra: str = "") -> None:
        existing = [p for p in paths if Path(p).is_file()]
        missing = len(paths) - len(existing)
        text = _fold(existing)
        if missing:
            text += f"  ⚠ 없는 파일 {missing}개"
        self._rows.append((label, text, extra))

    def print(self) -> None:
        print("\n" + "─" * 78)
        print("자료 출처")
        print("─" * 78)
        head = [("무대", self.scenario, self.config or "")]
        if self.seeds:
            rng = (f"{min(self.seeds)}-{max(self.seeds)}"
                   if len(self.seeds) == max(self.seeds) - min(self.seeds) + 1
                   else ",".join(map(str, self.seeds)))
            head.append(("시드", f"{len(self.seeds)}개", rng))
        for label, value, extra in head + self._rows:
            line = f"  {label:<8}{value}"
            print(f"{line}\n{'':10}{extra}" if extra else line)
        print("─" * 78)
