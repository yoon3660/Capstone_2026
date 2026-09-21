"""matplotlib 정적 그림.

입력은 Parquet 에서 읽은 DataFrame 뿐이다. 시뮬레이터·엔진을 임포트하지 않는다
(tests/test_import_boundaries.py).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def _korean_font(plt) -> None:
    """한글 라벨이 네모로 깨지지 않게 (scripts/check_traffic.py 와 같은 순서)."""

    import matplotlib.font_manager as fm

    names = {f.name for f in fm.fontManager.ttflist}

    for family in ("Malgun Gothic", "AppleGothic", "NanumGothic"):
        if family in names:
            plt.rcParams["font.family"] = family
            break

    plt.rcParams["axes.unicode_minus"] = False


def plot_ue_gap(solver_log: pd.DataFrame, path: str | Path, *, gap_tol: float | None = None) -> Path:
    """UE 반복별 상대 gap (로그 축). 발표용 수렴 그래프.

    solver_log 에서 iteration 이 있는 행만 쓴다 (UE 는 iteration·rel_gap 두 칸만 채운다).
    gap 이 0 이 되면 로그 축에 못 찍으므로 바닥값으로 올려 찍고 "0" 으로 표시한다.
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _korean_font(plt)
    df = solver_log.dropna(subset=["iteration"]).sort_values("iteration")

    if df.empty:
        raise ValueError("solver_log 에 iteration 행이 없다 (UE 결과가 아니다)")

    floor = 1e-5
    y = df["rel_gap"].clip(lower=floor)

    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150)
    ax.plot(df["iteration"], y, marker="o", color="#2b6cb0")
    ax.set_yscale("log")
    # 기본 로그 눈금(10^-2)은 수식 글꼴이라 한글 글꼴에서 마이너스가 깨진다. 발표용으로 % 가 더 읽기 쉽다
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v * 100:g}%"))
    ax.yaxis.set_minor_formatter(plt.NullFormatter())
    ax.set_xlabel("반복 (0 = 대기를 모르는 선택)")
    ax.set_ylabel("상대 gap")
    ax.set_title("UE 수렴: 혼자 바꿔서 줄일 수 있는 체류시간의 비율")
    ax.set_xticks(df["iteration"].astype(int).tolist())

    if gap_tol is not None:
        ax.axhline(gap_tol, color="#c53030", linestyle="--", linewidth=1)
        ax.text(df["iteration"].min(), gap_tol, f" 수렴 기준 {gap_tol * 100:g}%", va="bottom", ha="left",
                color="#c53030", fontsize=8)

    for it, g in zip(df["iteration"], df["rel_gap"], strict=True):
        ax.annotate("0" if g <= 0 else f"{g:.2%}", (it, max(g, floor)), textcoords="offset points",
                    xytext=(0, 6), ha="center", fontsize=7)

    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()

    path = Path(path)
    fig.savefig(path)
    plt.close(fig)
    return path
