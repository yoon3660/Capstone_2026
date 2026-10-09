"""속도 시공간 그림 — 실측 vs CTM 나란히 (#58).

발표 "모형 검증" 슬라이드 한 장이 이 함수의 산출물이다. 가로 시간, 세로 기점거리로
**대기 히트맵과 같은 축**을 쓴다 — 두 그림을 나란히 놓고 *"정체가 여기서 생기고,
그래서 저 휴게소가 빨개진다"* 를 말할 수 있어야 한다.

## 색을 속도에 거는 법

대기 히트맵은 **클수록 나쁘다**. 속도는 **작을수록 나쁘다.** 그래서 같은 색판을
뒤집어 쓴다 — 보는 사람이 "빨간 쪽이 문제" 라는 하나의 규칙만 기억하면 된다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

#: 속도 구간과 색. 느릴수록 빨갛다 (대기 히트맵과 반대 방향, 같은 의미).
BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "#7f0000"),
    (30.0, "#e31a1c"),
    (60.0, "#fd8d3c"),
    (80.0, "#fecc5c"),
    (90.0, "#c7e9b4"),
)


def _grid(df: pd.DataFrame, value: str, order: list[str]) -> np.ndarray:
    wide = df.pivot_table(index="conzone_id", columns="hour", values=value)
    return wide.reindex(index=order, columns=range(24)).to_numpy(dtype=float)


def plot_speed_fields(cmp: pd.DataFrame, conzone_path: str | Path, direction: str,
                      out_path: str | Path, *, title: str, subtitle: str = "") -> Path:
    """`cmp` 는 (conzone_id, hour, ctm_kmh, vds_kmh) 를 가진 표."""

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    from evdt.viz.plots import _korean_font
    _korean_font(plt)

    z = pd.read_parquet(conzone_path)
    z = z[z["direction"].astype(str) == direction]
    pos = (z.drop_duplicates("conzone_id").set_index("conzone_id")["offset_km"]
           .reindex(cmp["conzone_id"].unique()).dropna().sort_values())
    order = list(pos.index)

    edges = [b[0] for b in BANDS] + [999.0]
    cmap = ListedColormap([b[1] for b in BANDS])
    norm = BoundaryNorm(edges, cmap.N)

    fig, axes = plt.subplots(1, 2, figsize=(12.2, 6.6), dpi=110, sharey=True)
    for ax, col, name in zip(axes, ("vds_kmh", "ctm_kmh"), ("실측 (VDS)", "CTM"), strict=True):
        g = _grid(cmp, col, order)
        ax.imshow(g, aspect="auto", cmap=cmap, norm=norm, interpolation="nearest",
                  extent=(0, 24, pos.max(), pos.min()))
        ax.set_title(name, fontsize=11, loc="left")
        ax.set_xticks(range(0, 25, 3))
        ax.set_xticklabels([f"{h}시" for h in range(0, 25, 3)], fontsize=8, color="#6b6a66")
        ax.set_xlabel("시각", fontsize=9, color="#6b6a66")
        for side in ax.spines.values():
            side.set_visible(False)

    axes[0].set_ylabel("기점거리 offset_km", fontsize=9, color="#6b6a66")

    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, c in BANDS]
    labels = []
    for i, (lo, _) in enumerate(BANDS):
        hi = BANDS[i + 1][0] if i + 1 < len(BANDS) else None
        labels.append(f"{lo:.0f}–{hi:.0f}" if hi else f"{lo:.0f}+")
    fig.legend(handles, labels, loc="lower center", ncol=len(BANDS), frameon=False,
               fontsize=8.5, title="속도 (km/h) — 왼쪽일수록 느리다", title_fontsize=8.5,
               bbox_to_anchor=(0.5, 0.0))

    fig.suptitle(title, x=0.02, ha="left", fontsize=12)
    if subtitle:
        fig.text(0.02, 0.935, subtitle, fontsize=8.5, color="#6b6a66")
    fig.subplots_adjust(left=0.07, right=0.98, top=0.87, bottom=0.14, wspace=0.06)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out
