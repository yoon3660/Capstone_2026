"""cell_state parquet 으로 시공간 속도 지도를 그린다 (#56).

    python scripts/plot_cell_state.py <run_id>
    python scripts/plot_cell_state.py <run_id> --out figures/ctm.png

가로 시간 · 세로 기점거리 · 색 속도. 대기 히트맵(plot_heatmap.py)과 세로축이 같아서
나란히 놓고 "몇 시에 어디가 막혀서 어느 휴게소 도착이 몰렸나" 를 볼 수 있다.
읽는 법은 docs/ctm.md §5.

기본 저장 위치: runs/<run_id>/cell_state_map.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.cells import read_cells  # noqa: E402
from evdt.io.loaders import load_run_table  # noqa: E402
from evdt.paths import RUNS_DIR  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="cell_state → 시공간 속도 지도")
    parser.add_argument("run_id")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out = args.out or RUNS_DIR / args.run_id / "cell_state_map.png"

    state = load_run_table(args.run_id, "cell_state", RUNS_DIR)

    if state.empty:
        print(f"cell_state 가 비어 있습니다: {args.run_id}\n"
              "  demand.travel_time: ctm 으로 돌린 run 이어야 합니다")
        return 1

    corridor = "gyeongbu_up" if "_up_" in args.run_id else "gyeongbu_down"
    rows = read_cells(corridor)
    order = [r["cell_id"] for r in rows]

    grid = state.pivot(index="cell_id", columns="t_min", values="speed_kmh").reindex(order)
    y = np.array([r["offset_km_start"] for r in rows])
    x = np.array(sorted(state["t_min"].unique())) / 60.0

    fig, ax = plt.subplots(figsize=(11, 6))
    mesh = ax.pcolormesh(x, y, grid.to_numpy(), cmap="RdYlGn", vmin=0, vmax=100, shading="nearest")
    ax.set_xlabel("time (h)")
    ax.set_ylabel("offset from corridor start (km)")
    ax.set_title(args.run_id, fontsize=9)
    ax.invert_yaxis()   # 지도처럼 출발지가 위
    fig.colorbar(mesh, ax=ax, label="cell speed (km/h)")
    fig.tight_layout()

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140)
    print(f"{len(state):,}행 · 셀 {len(rows)}개 · 저장 {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
