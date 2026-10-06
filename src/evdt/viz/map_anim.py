"""코리도 지도 애니메이션 — **외부 의존이 하나도 없는** GIF.

## 왜 이게 따로 필요한가

`scripts/plot_map.py` 가 만드는 Leaflet HTML 은 **지도 타일을 온라인에서 받는다.**
그래서 저장소 밖에서 링크로 열면 타일이 안 와서 **지도가 안 보인다** (받는 쪽의
네트워크·차단 정책에 달려 있다).

여기서는 타일을 아예 안 쓴다. **노선을 우리가 가진 중심선 좌표로 직접 그린다**
(`data/processed/centerline_gyeongbu.parquet`, 4,226점). 그러면

- 네트워크·API 키·CDN 이 필요 없다
- 파일 하나로 어디서나 재생된다 (발표 자료에 그대로 붙는다)
- 타일 저작권 표기 문제도 없다

대가는 **배경 지형이 없다**는 것이다. 그런데 이 그림이 답하는 질문은
*"어느 휴게소가 빨개지고 어떻게 흩어지나"* 이고, 거기에 지형은 필요 없다.

## 읽는 것

스냅샷 계약만 읽는다 (설계 규칙 4) — `(t_min, entity_type, entity_id, lat, lon,
state, value)`. 시뮬레이터가 UE·S0·S1 중 무엇을 돌렸는지 몰라도 된다.

## ⚠ 두 런을 나란히 놓을 때

**색과 크기 척도를 두 런이 공유해야 한다.** 각자 자기 최대값으로 정규화하면
S0 최대 577.7분과 S1 최대 169.0분이 **똑같이 빨갛게** 보인다 — 3.4배 차이인데.
`shared_scale` 이 그걸 막고, 기준값을 화면에 적는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

#: 대기시간 → 색. `scripts/plot_map.py` 의 BANDS 와 **같은 구간**이다 — 두 그림이
#: 다른 색을 쓰면 같은 결과를 보고 다른 이야기를 하게 된다 (docs/experiment.md).
BANDS: tuple[tuple[float, str], ...] = (
    (0.0, "#2c7fb8"),
    (5.0, "#7fcdbb"),
    (15.0, "#fecc5c"),
    (30.0, "#fd8d3c"),
    (60.0, "#e31a1c"),
)

#: 점 크기 하한·상한 (points²). 큐가 0 이어도 휴게소가 어디 있는지는 보여야 한다.
SIZE_MIN = 28.0
SIZE_MAX = 900.0


def band_color(wait_min: float) -> str:
    out = BANDS[0][1]
    for edge, color in BANDS:
        if wait_min >= edge:
            out = color
    return out


@dataclass(frozen=True)
class Panel:
    """한 런의 애니메이션 재료."""

    label: str
    #: (t_min, station_id) → (wait_min, queue_len)
    frames: dict[float, dict[str, tuple[float, float]]]
    #: station_id → (lat, lon, offset_km, name)
    stations: dict[str, tuple[float, float, float, str]]


def to_panel(snap: pd.DataFrame, label: str,
             names: dict[str, dict] | None = None) -> Panel:
    """스냅샷 → Panel. `station` 외의 entity_type 은 버린다."""

    s = snap[snap["entity_type"] == "station"]
    if s.empty:
        raise ValueError(
            "\n[중단] 스냅샷에 station 행이 없다.\n"
            "  output.write_snapshots 가 true 인지 확인하라."
        )

    wide = s.pivot_table(index=["t_min", "entity_id", "lat", "lon"],
                         columns="state", values="value").reset_index()
    for col in ("wait_min", "queue_len"):
        if col not in wide:
            wide[col] = 0.0

    stations: dict[str, tuple[float, float, float, str]] = {}
    for sid, g in wide.groupby("entity_id"):
        meta = (names or {}).get(sid, {})
        stations[str(sid)] = (
            float(g["lat"].iloc[0]), float(g["lon"].iloc[0]),
            float(meta.get("km", 0.0)), str(meta.get("name", sid)),
        )

    frames: dict[float, dict[str, tuple[float, float]]] = {}
    for r in wide.itertuples(index=False):
        frames.setdefault(float(r.t_min), {})[str(r.entity_id)] = (
            float(r.wait_min), float(r.queue_len))

    return Panel(label=label, frames=frames, stations=stations)


def load_centerline(path: str | Path, step: int = 8) -> np.ndarray:
    """노선 좌표 (lat, lon). 4천 점은 그릴 때 과하니 솎는다."""

    line = pd.read_parquet(path)
    lat = next((c for c in line.columns if "lat" in c.lower()), None)
    lon = next((c for c in line.columns if "lon" in c.lower() or "lng" in c.lower()), None)
    if lat is None or lon is None:
        raise ValueError(f"중심선에 lat/lon 열이 없다: {list(line.columns)}")
    return line.iloc[::step][[lat, lon]].to_numpy(dtype=float)


def _hhmm(t_min: float) -> str:
    t = int(round(t_min))
    return f"{(t // 60) % 24:02d}:{t % 60:02d}"


def animate(
    panels: list[Panel],
    centerline: np.ndarray,
    out_path: str | Path,
    *,
    title: str,
    subtitle: str = "",
    every: int = 2,
    t_from: float | None = None,
    t_to: float | None = None,
    fps: int = 10,
    dpi: int = 100,
) -> Path:
    """GIF 로 쓴다. 돌려주는 것은 쓴 경로.

    `every` 로 프레임을 솎는다 — 5분 간격 289프레임을 그대로 쓰면 GIF 가 커진다.
    솎은 간격은 **화면에 적는다** (안 적으면 두 그림의 재생 속도를 비교할 수 없다).
    """

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import animation

    from evdt.viz.plots import _korean_font
    _korean_font(plt)

    times = sorted(set().union(*(set(p.frames) for p in panels)))
    if t_from is not None:
        times = [t for t in times if t >= t_from]
    if t_to is not None:
        times = [t for t in times if t <= t_to]
    times = times[::max(1, int(every))]
    if not times:
        raise ValueError("\n[중단] 그릴 프레임이 없다 (--from/--to 를 확인하라).")

    # ⚠ 척도는 **모든 패널이 공유**한다. 각자 자기 최대로 정규화하면 S0 577.7분과
    # S1 169.0분이 똑같이 빨개진다 — 3.4배 차이인데 그림이 같아진다
    qmax = max(
        (q for p in panels for f in p.frames.values() for _, q in f.values()),
        default=1.0,
    )
    qmax = max(qmax, 1.0)

    fig, axes = plt.subplots(1, len(panels), figsize=(4.6 * len(panels), 6.4), dpi=dpi)
    axes = np.atleast_1d(axes)

    # 위경도를 1:1 로 두면 가로가 눌린다. 위도 36°에서 경도 1° ≈ 88 km, 위도 1° ≈ 111 km
    # 라서, 그대로 그리면 노선이 실제보다 가팔라 보인다
    mean_lat = float(centerline[:, 0].mean())
    aspect = 1.0 / np.cos(np.radians(mean_lat))

    lat_pad, lon_pad = 0.06, 0.06
    scatters, stamps = [], []

    for ax, panel in zip(axes, panels, strict=True):
        ax.plot(centerline[:, 1], centerline[:, 0], color="#c9c7c2", lw=2.6,
                solid_capstyle="round", zorder=1)
        lats = [v[0] for v in panel.stations.values()]
        lons = [v[1] for v in panel.stations.values()]
        sc = ax.scatter(lons, lats, s=SIZE_MIN, c="#2c7fb8",
                        edgecolors="white", linewidths=0.8, zorder=3)
        scatters.append((sc, list(panel.stations)))

        ax.set_xlim(min(centerline[:, 1].min(), min(lons)) - lon_pad,
                    max(centerline[:, 1].max(), max(lons)) + lon_pad)
        ax.set_ylim(min(centerline[:, 0].min(), min(lats)) - lat_pad,
                    max(centerline[:, 0].max(), max(lats)) + lat_pad)
        ax.set_aspect(aspect, adjustable="box")
        ax.set_xticks([])
        ax.set_yticks([])
        for side in ax.spines.values():
            side.set_visible(False)
        ax.set_title(panel.label, fontsize=12, loc="left", pad=6)
        # 시각은 **왼쪽 아래**에 둔다 — 노선이 좌상→우하라서 거기가 비어 있다
        stamps.append(ax.text(0.03, 0.03, "", transform=ax.transAxes, ha="left",
                              va="bottom", fontsize=18, color="#1f1f1d"))

    # 범례 — **색은 대기시간, 크기는 줄 길이**다. 2차 발표에서 히트맵 색을
    # 이용률로 오해한 일이 있었으므로 (칠곡·평사) 단위를 같이 적는다
    handles = [
        plt.Line2D([], [], marker="o", linestyle="", markersize=8,
                   markerfacecolor=c, markeredgecolor="white",
                   label=f"{int(e)}분 이상" if e else "대기 없음")
        for e, c in BANDS
    ]
    fig.legend(handles=handles, loc="lower center", ncol=len(BANDS), frameon=False,
               fontsize=9, title="색 = 지금 도착하면 기다릴 시간",
               title_fontsize=9, bbox_to_anchor=(0.5, 0.0))

    fig.suptitle(title, x=0.03, y=0.982, ha="left", fontsize=13)
    note = f"크기 = 줄 길이 (공통 최대 {qmax:.0f}대) · {int(every) * 5}분 간격"
    fig.text(0.03, 0.936, f"{subtitle}  |  {note}" if subtitle else note,
             fontsize=8.5, color="#6b6a66")
    fig.subplots_adjust(left=0.02, right=0.98, top=0.87, bottom=0.09, wspace=0.04)

    def draw(i: int):
        t = times[i]
        for (sc, order), panel, stamp in zip(scatters, panels, stamps, strict=True):
            f = panel.frames.get(t, {})
            waits = np.array([f.get(s, (0.0, 0.0))[0] for s in order])
            queues = np.array([f.get(s, (0.0, 0.0))[1] for s in order])
            sc.set_color([band_color(w) for w in waits])
            sc.set_sizes(SIZE_MIN + (SIZE_MAX - SIZE_MIN) * np.clip(queues / qmax, 0, 1))
            stamp.set_text(_hhmm(t))
        return []

    anim = animation.FuncAnimation(fig, draw, frames=len(times), interval=1000 / fps)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    anim.save(out, writer=animation.PillowWriter(fps=fps))
    plt.close(fig)
    return out
