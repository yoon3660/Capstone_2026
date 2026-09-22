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
#: 큐 길이 히트맵 구간 (대). 줄 선 차 수 (충전 중인 차는 뺀다)
QUEUE_BINS: tuple[float, ...] = (0, 0.5, 2, 5, 10, 20, 50, float("inf"))
QUEUE_BIN_LABELS: tuple[str, ...] = ("0", "1–2", "2–5", "5–10", "10–20", "20–50", "50+")
#: 한 가지 색(파랑)의 진하기. 옅음 = 없음, 짙음 = 많음
RAMP: tuple[str, ...] = ("#f4f3f0", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#1c5cab", "#0d366b")
WAIT_RAMP = RAMP

#: 휴게소 한 곳이 세로축에서 차지하는 두께 (km). 이웃 휴게소가 더 가까우면 그 중간까지만
STATION_BAND_KM = 6.0


def _station_bands(offsets: list[float]) -> list[tuple[float, float]]:
    """휴게소마다 세로 띠 [아래, 위] (km). 기점거리에 그대로 놓고, 이웃과 겹치지 않게 자른다."""

    half = STATION_BAND_KM / 2
    bands = []
    for i, km in enumerate(offsets):
        lo = km - half if i == 0 else max(km - half, (offsets[i - 1] + km) / 2)
        hi = km + half if i == len(offsets) - 1 else min(km + half, (km + offsets[i + 1]) / 2)
        bands.append((lo, hi))
    return bands


def _label_groups(names: list[str], offsets: list[float], min_gap_km: float = STATION_BAND_KM) -> list[tuple[float, str]]:
    """가까운 휴게소 이름은 한 줄로 묶는다 (옥천만남·옥천 처럼 2.5 km 떨어진 곳이 겹치지 않게)."""

    groups: list[list[int]] = []
    for i, km in enumerate(offsets):
        if groups and km - offsets[groups[-1][0]] < min_gap_km:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [
        (sum(offsets[i] for i in g) / len(g), "·".join(names[i].replace("휴게소", "") for i in g))
        for g in groups
    ]


def plot_corridor_heatmap(
    panels: list[tuple[str, pd.DataFrame]],
    stations: pd.DataFrame,
    path: str | Path,
    *,
    bins: tuple[float, ...] = WAIT_BINS_MIN,
    bin_labels: tuple[str, ...] = WAIT_BIN_LABELS,
    value_label: str = "평균 대기 (분)",
    title: str = "휴게소 × 시간대",
    subtitle: str | None = None,
) -> Path:
    """가로 = 도착 시각(시), 세로 = 기점거리 offset_km (위 = 코리도 시작), 색 = 값.

    panels   : [(제목, DataFrame(station_id, hour, value)), ...] 나란히 그린다. 색 구간은 모두 같다
    stations : station_id, name, offset_km

    세로축은 **실제 기점거리**다. 휴게소를 같은 간격 줄로 늘어놓으면 40 km 떨어진 곳과
    2.5 km 떨어진 곳이 똑같아 보이고, 가까이 붙은 휴게소들이 "한 구간이 통째로 막힌"
    것처럼 과장된다 (이슈 #30: offset_km 왜곡을 안 고쳤다면 쏠림이 가짜로 보였을 것).
    휴게소는 제 위치에 얇은 띠로 그리고, 사이의 도로는 비워 둔다.
    값이 없는 칸(그 시각에 온 차가 없음)은 0 과 같은 색이다.
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Rectangle

    _korean_font(plt)

    if not panels:
        raise ValueError("그릴 패널이 없다")

    order = stations.sort_values("offset_km").reset_index(drop=True)
    offsets = order["offset_km"].astype(float).tolist()
    bands = _station_bands(offsets)
    row_of = {sid: i for i, sid in enumerate(order["station_id"])}
    cmap = ListedColormap(list(RAMP))
    norm = BoundaryNorm(list(bins[:-1]) + [1e12], len(RAMP))
    y_top = min(0.0, bands[0][0])
    y_bottom = bands[-1][1]

    fig, axes = plt.subplots(1, len(panels), figsize=(6.2 * len(panels) + 1.4, 8.2), dpi=150,
                             sharey=True, squeeze=False)

    for ax, (label, grid) in zip(axes[0], panels, strict=True):
        values = {(r.station_id, int(r.hour)): float(r.value) for r in grid.itertuples() if r.station_id in row_of}
        for sid, i in row_of.items():
            lo, hi = bands[i]
            for h in range(24):
                v = values.get((sid, h), 0.0)
                ax.add_patch(Rectangle((h, lo), 1, hi - lo, facecolor=cmap(norm(v)), edgecolor="white", linewidth=0.6))

        ax.set_xlim(0, 24)
        ax.set_ylim(y_bottom, y_top)                    # 위 = 기점
        ax.set_xticks(range(0, 25, 3))
        ax.set_xticklabels([f"{h}시" for h in range(0, 25, 3)], fontsize=8, color="#6b6a66")
        ax.set_xlabel("도착 시각", fontsize=9, color="#6b6a66")
        ax.set_title(label, fontsize=10, loc="left")
        ax.grid(axis="y", color="#e6e5e1", linewidth=0.6)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_visible(False)

    first = axes[0][0]
    first.set_ylabel("기점거리 offset_km", fontsize=9, color="#6b6a66")
    first.set_yticks(range(0, int(y_bottom) + 1, 50))
    first.tick_params(axis="y", labelsize=8, colors="#6b6a66")

    last = axes[0][-1]
    names = order["name"].astype(str).tolist()
    for km, text in _label_groups(names, offsets):
        last.text(24.3, km, text, va="center", ha="left", fontsize=7.5, color="#1f1f1d", clip_on=False)

    fig.legend([plt.Rectangle((0, 0), 1, 1, color=c) for c in RAMP], list(bin_labels), title=value_label,
               loc="lower center", ncol=len(RAMP), frameon=False, fontsize=8, title_fontsize=8)
    fig.suptitle(title, x=0.02, ha="left", fontsize=12)
    if subtitle:
        fig.text(0.02, 0.945, subtitle, fontsize=8, color="#6b6a66")
    fig.tight_layout(rect=(0, 0.07, 0.93, 0.93))

    path = Path(path)
    fig.savefig(path)
    plt.close(fig)
    return path


def plot_wait_heatmap(
    hourly: pd.DataFrame,
    stations: pd.DataFrame,
    panels: list[tuple[str, str]],
    path: str | Path,
    *,
    title: str = "휴게소 × 시간대 평균 대기",
) -> Path:
    """station_hourly_wait.sql 결과로 run 마다 한 칸씩 (plot_corridor_heatmap 의 대기 버전)."""

    grids = [
        (label, hourly[hourly["run_id"] == run_id].rename(columns={"mean_wait_min": "value"}))
        for run_id, label in panels
    ]
    return plot_corridor_heatmap(grids, stations, path, title=title)
