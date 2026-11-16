"""대기 파형 — **시간축으로 샘플링**해서 칸마다 패널 하나 (발표 §5-1).

    python scripts/plot_wait_signal.py
    python scripts/plot_wait_signal.py --station 건천휴게소
    python scripts/plot_wait_signal.py --bin 5 --dark

## 다른 두 그림과 무엇이 다른가

    plot_station_bars.py    가로축 = 휴게소, 칸마다 패널.  **어디가** 막히나
    plot_wait_dumbbell.py   가로축 = 대기,  한 줄 = 휴게소. **얼마나** 깎이나
    이 그림                  가로축 = **시각**, 칸마다 패널. **언제** 막히나

신호 분해 그림처럼 **겹치지 않고 쌓는다.** 겹쳐 그리면 선이 서로를 가리고,
"진폭이 줄었다" 가 선 세 개의 엉킴으로 보인다. 쌓아 놓고 **세로 눈금을 공유**
하면 위에서 아래로 내려가며 봉우리가 주저앉는 것이 한눈에 보인다.

★ **세로 눈금을 공유하지 않으면 이 그림은 거짓말이 된다.** 패널마다 제 눈금을
  쓰면 세 파형이 똑같아 보인다 (#93 에서 겪은 그 고장).

## 대기를 어떻게 샘플링하나

`charge_event.wait_min` 을 **도착 시각**으로 구간에 담아 평균한다.

  · 사다리 run 에는 5분 간격 `snapshot.parquet` 에 `wait_min` 이 있지만 **쓰지
    않는다.** 그건 "지금 가면 기다릴 시간"(앱 화면 값)이고, 차가 한 대도 없는
    프레임까지 평균에 들어가 값이 희석된다
  · `charge_event.wait_min` 은 **실제로 기다린 시간**이고, 막대 그림·덤벨·
    2D 히트맵과 **같은 열**이다. 그림마다 다른 정의를 쓰면 나란히 못 놓는다
  · 그리고 MNL 은 `run_mnl_world.py` 로 돌아서 **스냅샷이 아예 없다.**
    충전 이벤트로 뽑아야 세 칸을 같은 자로 견줄 수 있다
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds  # noqa: E402
from evdt.viz.plots import _korean_font  # noqa: E402
from evdt.viz.sources import Sources  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

#: ★ **색은 칸(entity)을 따라간다. 순서를 따라가면 안 된다.** S0 을 넣었다고
#: S2 의 색이 바뀌면 덤벨 그림과 색이 어긋나 나란히 못 놓는다.
#: 검증된 범주형 고정 순서 1·2·3·4 번 칸을 칸마다 묶어 둔다
#: (`validate_palette.js`: 명도대·채도·색각분리·정상시야 전부 PASS,
#:  light 에서 청록 2.74 · 노랑 2.11 이 3:1 아래라 **직접 라벨 의무** —
#:  이 그림은 패널마다 한 선이고 **제목이 이름을 지므로** 색만으로 읽지 않는다)
STAGE_COLOR = {"MNL": "#2a78d6", "S2": "#eb6834", "S4": "#1baf7a", "S0": "#eda100"}
STAGE_COLOR_DARK = {"MNL": "#3987e5", "S2": "#d95926", "S4": "#199e70",
                    "S0": "#c98500"}
#: 패널을 놓는 순서 — 나쁜 쪽에서 좋은 쪽으로 내려간다
PANEL_ORDER = ["MNL", "S0", "S1", "S2", "S3", "S4"]
LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e8e7e2"}
DARK = {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "grid": "#2c2c29"}


def binned(paths: list[Path], bin_min: float, station: str | None,
           sid_of: dict[str, str]) -> pd.Series:
    """도착 시각 구간별 평균 실제 대기. 시드마다 내고 평균한다."""

    acc = []
    for p in paths:
        if not p.is_file():
            continue
        f = pd.read_parquet(p)
        if station:
            f = f[f["station_id"] == sid_of[station]]
        if not len(f):
            continue
        f = f.assign(b=(f["t_arrive_min"] // bin_min) * bin_min)
        acc.append(f.groupby("b")["wait_min"].mean())
    if not acc:
        raise SystemExit("[중단] 충전 이벤트를 못 찾았다")
    return pd.concat(acc, axis=1).mean(axis=1).sort_index()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_congested.yaml")
    ap.add_argument("--stages", default="S2,S4",
                    help="S0 도 넣을 수 있다 (`--stages S0,S2,S4`) — 색은 칸마다 "
                         "묶여 있어 넣고 빼도 다른 칸의 색이 안 바뀐다")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--mnl-dir",
                    default="runs/mnl_world/chuseok_congested_floor/theta0p05_w1p0")
    ap.add_argument("--bin", type=float, default=10.0, help="샘플 구간 (분)")
    ap.add_argument("--station", default=None,
                    help="한 휴게소만 (예: 건천휴게소). 비우면 코리도 전체")
    ap.add_argument("--from-hour", type=float, default=5.0)
    ap.add_argument("--to-hour", type=float, default=23.0)
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--out", default="docs/figures/wait_signal.png")
    args = ap.parse_args()

    cfg = load_config(args.config, stage="S0")
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    seeds = parse_seeds(args.seeds)
    unknown = [s for s in stages if s not in STAGE_COLOR]
    if unknown:
        raise SystemExit(f"[중단] 색을 정해 둔 칸이 아니다: {unknown} "
                         f"(있는 것: {sorted(STAGE_COLOR)})")

    # ★ 휴게소 **이름은 상·하행에 둘 다 있다** (건천휴게소가 UP·DOWN 두 곳).
    #   코리도로 좁히지 않으면 이름 → id 사전이 반대 방향을 가리켜 빈 결과가 난다
    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        sid_of = {n: s for s, n in con.execute(
            "SELECT station_id, name FROM station WHERE corridor_id = ?",
            (cfg.corridor_id,))}
    if args.station and args.station not in sid_of:
        raise SystemExit(f"[중단] 그런 휴게소가 없다: {args.station}")

    series: dict[str, pd.Series] = {
        "MNL": binned([ROOT / args.mnl_dir / f"seed{s}" / "MNL.parquet" for s in seeds],
                      args.bin, args.station, sid_of)}
    for st in stages:
        series[st] = binned(
            [RUNS_DIR / make_run_id(cfg.scenario_id, st, s, cfg.policy.participation)
             / "charge_event.parquet" for s in seeds],
            args.bin, args.station, sid_of)

    # 패널 순서 — 나쁜 쪽에서 좋은 쪽으로
    order = [k for k in PANEL_ORDER if k in series]
    series = {k: series[k] for k in order}

    lo, hi = args.from_hour * 60, args.to_hour * 60
    grid = np.arange(lo, hi + args.bin, args.bin)
    frame = pd.DataFrame({k: v.reindex(grid).fillna(0.0) for k, v in series.items()},
                         index=grid)
    keys = list(frame.columns)

    where = args.station or "코리도 전체"

    # --- 자료 출처 — 그림마다 **무엇을 썼는지** 같이 찍는다 -------------------
    mf = ROOT / args.mnl_dir / "manifest.json"
    mnl_note = "(manifest 없음)"
    if mf.is_file():
        m = json.loads(mf.read_text(encoding="utf-8"))
        mnl_note = (f"θ {m.get('theta')} · w {m.get('wait_weight')} · "
                    f"ASC {'있음' if m.get('asc_file') else '없음'}")
    src = Sources(cfg.scenario_id, seeds=seeds, config=args.config)
    src.column("charge_event.wait_min",
               "실제로 기다린 시간. 스냅샷의 wait_min(앱 화면 값)이 아니다 — "
               "그건 차 없는 프레임까지 평균에 넣어 희석되고 MNL 은 스냅샷이 없다")
    src.note("구간", f"도착 시각 {args.bin:g}분", "시드마다 내고 시드 평균")
    src.note("범위", f"{args.from_hour:g}시 ~ {args.to_hour:g}시", where)
    src.files("MNL", [ROOT / args.mnl_dir / f"seed{s}" / "MNL.parquet"
                      for s in seeds], f"run_mnl_world.py · {mnl_note}")
    for st in stages:
        src.files(st, [RUNS_DIR / make_run_id(cfg.scenario_id, st, s,
                                              cfg.policy.participation)
                       / "charge_event.parquet" for s in seeds])
    src.print()

    print(f"\n{cfg.scenario_id} · {where} · 시드 {len(seeds)}개 · {args.bin:g}분 구간")
    print(f"\n{'칸':<6}{'평균':>8}{'최대':>8}{'봉우리 시각':>12}{'편차':>8}")
    for k in keys:
        pk = frame[k].idxmax()
        print(f"{k:<6}{frame[k].mean():>8.1f}{frame[k].max():>8.1f}"
              f"{int(pk) // 60:>9}시{int(pk) % 60:02d}분{frame[k].std(ddof=1):>8.1f}")
    print(f"\n  ★ 봉우리 {frame[keys[0]].max():.1f} → {frame[keys[-1]].max():.1f}분 "
          f"({(1 - frame[keys[-1]].max() / frame[keys[0]].max()) * 100:.0f}% 감소)")

    # --- 그림 ---------------------------------------------------------------
    C = DARK if args.dark else LIGHT
    cmap = STAGE_COLOR_DARK if args.dark else STAGE_COLOR
    _korean_font(plt)
    n = len(keys)
    fig, axes = plt.subplots(n, 1, figsize=(11.0, 2.35 * n + 1.6), sharex=True,
                             sharey=True)                 # ★ 세로 눈금 공유
    fig.patch.set_facecolor(C["surface"])
    top = float(frame.to_numpy().max()) * 1.22

    for k, ax in zip(keys, axes, strict=True):
        col = cmap[k]
        ax.set_facecolor(C["surface"])
        x = frame.index / 60.0
        ax.fill_between(x, frame[k], color=col, alpha=0.16, zorder=2)
        ax.plot(x, frame[k], color=col, linewidth=2.0, zorder=3)

        pk = frame[k].idxmax()
        ax.plot([pk / 60.0], [frame[k].max()], "o", markersize=9, color=col,
                markeredgecolor=C["surface"], markeredgewidth=2.0, zorder=4)
        ax.annotate(f"최대 {frame[k].max():.1f}분",
                    (pk / 60.0, frame[k].max()), textcoords="offset points",
                    xytext=(9, 4), fontsize=10, color=C["ink2"], zorder=5)

        ax.set_ylim(0, top)
        ax.set_ylabel("대기 (분)", fontsize=10, color=C["ink2"])
        ax.grid(axis="y", color=C["grid"], linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(C["grid"])
        ax.tick_params(colors=C["ink2"], length=0, labelsize=10)
        # 한 패널에 한 선이라 범례가 없다 — **제목이 이름을 진다**
        ax.set_title(f"{k}   평균 {frame[k].mean():.1f}분", fontsize=12.5,
                     color=C["ink"], loc="left", pad=7)

    axes[-1].set_xlabel("시각", fontsize=10.5, color=C["ink2"])
    axes[-1].set_xticks(np.arange(args.from_hour, args.to_hour + 1, 2))
    axes[-1].set_xticklabels([f"{int(h)}시" for h in
                              np.arange(args.from_hour, args.to_hour + 1, 2)])

    cut = (1 - frame[keys[-1]].max() / frame[keys[0]].max()) * 100
    # ★ 제목은 `suptitle` 과 `fig.text` 를 섞지 않는다 — 둘의 y 기준이 달라
    #   겹친다. 같은 방식으로 두 줄을 쓰고 간격을 직접 준다.
    #   그리고 matplotlib 은 `**굵게**` 를 못 읽는다 (그대로 찍힌다)
    fig.text(0.012, 0.985, f"언제 막히나 — {where} · 봉우리 "
             f"{frame[keys[0]].max():.1f} → {frame[keys[-1]].max():.1f}분 "
             f"({cut:.0f}% 감소)",
             fontsize=15, color=C["ink"], ha="left", va="top")
    fig.text(0.012, 0.947, f"{n}개 칸이 세로 눈금을 공유한다 · 시드 {len(seeds)}개 · "
             f"{args.bin:g}분 구간 평균 실제 대기",
             fontsize=10, color=C["ink2"], ha="left", va="top")

    fig.tight_layout(rect=(0, 0, 1, 0.925))
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=C["surface"])
    print(f"\n  저장 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
