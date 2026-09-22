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


#: 대기 히트맵 구간 (분). **여러 run 을 나란히 놓을 때 같은 구간을 써야** 색 차이가 곧 대기 차이다.
WAIT_BINS_MIN: tuple[float, ...] = (0, 1, 10, 30, 60, 120, 240, float("inf"))
WAIT_BIN_LABELS: tuple[str, ...] = ("0", "1–10", "10–30", "30–60", "1–2시간", "2–4시간", "4시간+")
#: 한 가지 색(파랑)의 진하기. 옅음 = 대기 없음, 짙음 = 오래
WAIT_RAMP: tuple[str, ...] = ("#f4f3f0", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#1c5cab", "#0d366b")


def plot_wait_heatmap(
    hourly: pd.DataFrame,
    stations: pd.DataFrame,
    panels: list[tuple[str, str]],
    path: str | Path,
    *,
    title: str = "휴게소 × 시간대 평균 대기",
) -> Path:
    """휴게소(세로, 위 = 코리도 시작) × 도착 시각(가로) 평균 대기 히트맵. run 마다 한 칸.

    hourly   : src/evdt/sql/station_hourly_wait.sql 결과 (run_id, station_id, hour, n_ev, mean_wait_min)
    stations : station_id, name, offset_km — 그릴 휴게소와 순서
    panels   : [(run_id, 제목), ...]  나란히 그릴 run 들. 색 구간은 모두 같다

    차가 한 대도 오지 않은 칸은 대기 0 칸과 같은 회색이다 (둘 다 "기다린 사람 없음").
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import BoundaryNorm, ListedColormap

    _korean_font(plt)

    if not panels:
        raise ValueError("그릴 run 이 없다")

    order = stations.sort_values("offset_km").reset_index(drop=True)
    row_of = {sid: i for i, sid in enumerate(order["station_id"])}
    cmap = ListedColormap(list(WAIT_RAMP))
    norm = BoundaryNorm(list(WAIT_BINS_MIN[:-1]) + [1e9], len(WAIT_RAMP))

    fig, axes = plt.subplots(1, len(panels), figsize=(6.5 * len(panels), 0.33 * len(order) + 2.2),
                             dpi=150, sharey=True, squeeze=False)

    for ax, (run_id, label) in zip(axes[0], panels, strict=True):
        grid = np.zeros((len(order), 24))
        for r in hourly[hourly["run_id"] == run_id].itertuples():
            if r.station_id in row_of and 0 <= r.hour < 24:
                grid[row_of[r.station_id], int(r.hour)] = r.mean_wait_min

        ax.imshow(grid, aspect="auto", cmap=cmap, norm=norm, interpolation="nearest")
        ax.set_xticks(np.arange(-0.5, 24, 1), minor=True)
        ax.set_yticks(np.arange(-0.5, len(order), 1), minor=True)
        ax.grid(which="minor", color="white", linewidth=1.2)
        ax.tick_params(which="minor", length=0)
        ax.set_xticks(range(0, 24, 3))
        ax.set_xticklabels([f"{h}시" for h in range(0, 24, 3)], fontsize=8, color="#6b6a66")
        ax.set_xlabel("도착 시각", fontsize=9, color="#6b6a66")
        ax.set_title(label, fontsize=10, loc="left")
        for spine in ax.spines.values():
            spine.set_visible(False)

    axes[0][0].set_yticks(range(len(order)))
    axes[0][0].set_yticklabels(
        [f"{n.replace('휴게소', '')}  {km:.0f}km" for n, km in zip(order["name"], order["offset_km"], strict=True)],
        fontsize=8,
    )

    fig.legend([plt.Rectangle((0, 0), 1, 1, color=c) for c in WAIT_RAMP], list(WAIT_BIN_LABELS),
               title="평균 대기 (분)", loc="lower center", ncol=len(WAIT_RAMP), frameon=False,
               fontsize=8, title_fontsize=8)
    fig.suptitle(title, x=0.02, ha="left", fontsize=12)
    fig.tight_layout(rect=(0, 0.08, 1, 0.95))

    path = Path(path)
    fig.savefig(path)
    plt.close(fig)
    return path
