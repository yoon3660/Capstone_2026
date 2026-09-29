"""한 휴게소의 대기 시계열 — S0 의 큐 진동 (#59).

    python scripts/plot_station_wait.py <S0_run_id> --vs <UE_run_id>
    python scripts/plot_station_wait.py <S0_run_id> --vs <UE_run_id> --station 안성휴게소
    python scripts/plot_station_wait.py <S0_run_id> --vs <UE_run_id> --top 3

**설계문서가 말한 "S0 의 큐 진동 시계열" 이 이 그림이다.** 발표에서 "정보를 주는
것만으로는 쏠림이 안 풀린다" 를 한 장으로 말하는 자리다.

무엇을 보나

    위   대기 시계열 — S0 는 톱니처럼 오르내리고, UE 는 평평하다
    아래 5분마다 도착한 대수 — 진동의 **원인**. 같은 화면을 본 무리가 함께 온다

왜 진동하나
    S0 운전자는 출발할 때 화면에 뜬 대기만 본다. 비어 보이면 그 Δt 에 결정한 차가
    전부 그리로 간다 → 한 시간 뒤 한꺼번에 도착해 줄이 선다 → 화면이 빨개진다 →
    다음 무리는 전원 딴 데로 간다 → 여기는 비고 저기가 막힌다. 반복.

    UE 운전자는 **도착했을 때의** 대기를 알고 고르므로 이 진동이 원리상 없다.
    그래서 두 선을 겹쳐 놓는 것이 핵심이다 — S0 만 그리면 "원래 그런 것" 처럼 보인다.

기본 저장 위치: runs/<S0_run_id>/station_wait.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.loaders import load_run_table  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.viz.plots import _korean_font  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

S0_COLOR = "#d62728"
UE_COLOR = "#2b6cb0"


def _wait(run_id: str, runs: Path) -> pd.DataFrame:
    snap = load_run_table(run_id, "snapshot", runs)

    if snap.empty:
        raise SystemExit(
            f"스냅샷이 없습니다: {run_id}\n"
            "  config 의 output.write_snapshots 가 true 여야 합니다")

    wait = snap[(snap["entity_type"] == "station") & (snap["state"] == "wait_min")]
    return wait.pivot_table(index="t_min", columns="entity_id", values="value")


def _arrival_bins(run_id: str, runs: Path, bin_min: float) -> pd.DataFrame:
    """휴게소 × Δt 격자의 도착 대수."""

    ch = load_run_table(run_id, "charge_event", runs)
    if ch.empty:
        return pd.DataFrame()
    ch = ch.assign(bin=(ch["t_arrive_min"] // bin_min * bin_min).astype(float))
    grid = np.arange(0.0, float(ch["bin"].max()) + bin_min, bin_min)
    return (ch.groupby(["bin", "station_id"]).size().unstack(fill_value=0)
            .reindex(grid, fill_value=0))


def clumping(counts: pd.Series, lo: float = 300.0, hi: float = 1300.0) -> float:
    """도착 뭉침 지수 = 분산 ÷ 평균 (분산-평균 비).

    **1.0 이면 무작위 도착**(포아송)이다. 2 면 같은 대수가 두 배로 뭉쳐서 온다.
    "쏠림" 을 대기시간이 아니라 **도착 패턴**으로 재는 지표라서, 큐가 비어 있는
    휴게소에서도 정책의 차이를 잡아낸다.

    새벽·심야는 도착 자체가 드물어 비가 불안정하다. 낮 시간(기본 5~21시)만 본다.
    """

    live = counts[(counts.index >= lo) & (counts.index <= hi)]
    mean = float(live.mean())
    return float(live.var() / mean) if mean > 0 else float("nan")


def _names(corridor_id: str) -> dict[str, tuple[str, float]]:
    with get_conn(default_db_path(), readonly=True) as conn:
        return {
            r[0]: (r[1], float(r[2]))
            for r in conn.execute(
                "SELECT station_id, name, offset_km FROM station WHERE corridor_id = ?",
                (corridor_id,)).fetchall()
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="한 휴게소의 대기 시계열 (S0 의 진동)")
    ap.add_argument("run_id", help="S0 run_id")
    ap.add_argument("--vs", required=True, help="나란히 놓을 UE run_id")
    ap.add_argument("--station", default=None, help="휴게소 이름 또는 ID (기본: 가장 심하게 흔들린 곳)")
    ap.add_argument("--top", type=int, default=1, help="가장 심하게 흔들린 휴게소 n 곳")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    runs = RUNS_DIR
    s0, ue = _wait(args.run_id, runs), _wait(args.vs, runs)
    corridor = "gyeongbu_up" if "_up_" in args.run_id else "gyeongbu_down"
    meta = _names(corridor)

    shared = [c for c in s0.columns if c in ue.columns]
    if not shared:
        raise SystemExit("두 run 에 공통 휴게소가 없습니다. 같은 코리도인지 확인하세요")

    bin_min = float(np.diff(s0.index.to_numpy()[:2])[0]) if len(s0.index) > 1 else 5.0
    a_s0, a_ue = _arrival_bins(args.run_id, runs, bin_min), _arrival_bins(args.vs, runs, bin_min)
    clump = pd.DataFrame({
        "s0": {c: clumping(a_s0[c]) for c in shared if c in a_s0},
        "ue": {c: clumping(a_ue[c]) for c in shared if c in a_ue},
        "n": {c: int(a_s0[c].sum()) for c in shared if c in a_s0},
    }).dropna()

    if args.station:
        picked = [c for c in shared
                  if c == args.station or meta.get(c, ("", 0))[0] == args.station]
        if not picked:
            raise SystemExit(f"그런 휴게소가 없습니다: {args.station}")
    else:
        # **도착이 UE 보다 얼마나 더 뭉쳤나**로 고른다. 평균 대기가 높은 곳이 아니다 —
        # 이 그림의 주제는 "얼마나 오래 기다리나" 가 아니라 "한꺼번에 몰리나" 다.
        # 대수가 적으면 비가 불안정하므로 하루 100대 이상만 본다.
        busy = clump[clump["n"] >= 100]
        order = (busy["s0"] - busy["ue"]).sort_values(ascending=False)
        if order.empty:
            raise SystemExit("하루 100대 이상 받은 휴게소가 없습니다. --station 으로 지정하세요")
        picked = list(order.index[:max(args.top, 1)])
    _korean_font(plt)          # ⚠ subplots 보다 **먼저**. 나중에 부르면 안 먹는다
    fig, axes = plt.subplots(2, len(picked), figsize=(7.2 * len(picked), 7.0),
                             squeeze=False, sharex=True)

    for col, sid in enumerate(picked):
        name, off = meta.get(sid, (sid, float("nan")))

        ax = axes[0][col]
        ax.plot(ue.index / 60.0, ue[sid], color=UE_COLOR, lw=1.4,
                label="UE — 도착했을 때의 대기를 안다")
        ax.plot(s0.index / 60.0, s0[sid], color=S0_COLOR, lw=1.4,
                label="S0 — 출발할 때의 화면만 본다")
        ax.set_ylabel("대기 (분)")
        ax.set_title(f"{name}  ({off:.0f} km)\n"
                     f"평균 대기  UE {ue[sid].mean():.1f}분  →  S0 {s0[sid].mean():.1f}분",
                     fontsize=10)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

        ax = axes[1][col]
        ax.bar(a_s0.index / 60.0, a_s0[sid].to_numpy(), width=bin_min / 60.0,
               color=S0_COLOR, alpha=0.8, label=f"S0 — 뭉침 {clump.loc[sid, 's0']:.1f}")
        ax.step(a_ue.index / 60.0, a_ue[sid].to_numpy(), where="mid",
                color=UE_COLOR, lw=1.1, label=f"UE — 뭉침 {clump.loc[sid, 'ue']:.1f}")
        ax.set_xlabel("시각 (시)")
        ax.set_ylabel(f"{bin_min:.0f}분당 도착 대수")
        ax.set_title("도착이 뭉쳐서 온다 — 대기가 오르는 원인 "
                     "(뭉침 = 분산÷평균, 1.0 이면 무작위 도착)", fontsize=9)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3, axis="y")
        ax.set_xlim(0, 24)

    fig.suptitle(
        "같은 세계, 같은 차. 운전자가 보는 정보만 다르다 (#59)\n"
        f"{args.vs}   vs   {args.run_id}", fontsize=10)
    fig.tight_layout()

    out = args.out or runs / args.run_id / "station_wait.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140)

    print(f"{'휴게소':<12} {'km':>6} {'대수':>6} {'평균대기 UE':>11} {'S0':>7}"
          f"  {'도착뭉침 UE':>11} {'S0':>7}")
    for sid in picked:
        name, off = meta.get(sid, (sid, float("nan")))
        print(f"{name:<12} {off:>6.0f} {clump.loc[sid, 'n']:>6,} "
              f"{ue[sid].mean():>11.1f} {s0[sid].mean():>7.1f}  "
              f"{clump.loc[sid, 'ue']:>11.2f} {clump.loc[sid, 's0']:>7.2f}")
    print(f"저장 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
