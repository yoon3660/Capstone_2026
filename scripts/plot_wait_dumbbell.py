"""휴게소별 대기 — 덤벨 그림 (발표 §5-1). MNL → S2 → S4 를 한 장에.

    python scripts/plot_wait_dumbbell.py
    python scripts/plot_wait_dumbbell.py --stages S2,S4 --top 0
    python scripts/plot_wait_dumbbell.py --dark

## 왜 덤벨인가 — 선 그래프가 아니라

`plot_wait_wave.py` 는 휴게소를 가로축에 늘어놓고 선으로 이었다. 두 가지가 걸린다.

  ① **선은 "사이" 를 뜻한다.** 건천(75km)과 경산(110km) 사이에는 휴게소가 없다.
     이은 선분은 존재하지 않는 값을 그린 것이다
  ② **16곳 중 7곳이 거의 0 분**이다. 선이 가로축 절반을 바닥에 붙어 가면서
     죽은 공간이 생기고, 봉우리 다섯 개만 삐죽 솟아 지저분해진다

덤벨은 **항목마다 전·후를 잇는다** — 이을 선이 한 휴게소 **안에서만** 그어지므로
①이 사라진다. 그리고 가로로 눕히면

  · 긴 한국어 휴게소 이름이 그대로 들어간다 (세로축 눕힘 라벨 불필요)
  · MNL 내림차순으로 정렬하면 **위에서 아래로 읽히는 순서**가 생긴다
  · 0 분짜리 휴게소들이 아래쪽에 짧은 줄로 모인다 — 죽은 공간이 아니라
    **"16곳 중 5곳만 막힌다" 는 쏠림 그 자체**가 된다

## 색

세 칸이 **주제**이므로 범주형(categorical) 이다. 고정 순서로 1·2·3 번 칸을 쓴다.

    MNL  파랑  #2a78d6      S2  주황  #eb6834      S4  청록  #1baf7a

검증기를 돌렸다 (`dataviz/scripts/validate_palette.js`): 명도대·채도·색각
분리(최악 인접쌍 ΔE 9.2 deutan)·정상시야(27.6) 전부 통과. 청록의 배경 대비가
2.74 로 3:1 아래라 **직접 라벨이 의무**다 — 그래서 세 칸 모두 점 옆에 값을
적고 표도 같이 찍는다.

## 완만함은 숫자로도 적는다

제목 아래에 **편차**(휴게소 사이 표준편차)를 적는다. 봉우리가 낮아지는 것은
눈에 보이지만, "고르게 됐다" 는 편차가 줄어든 것이다. 평균만 줄고 편차가
그대로면 전체가 같이 좋아진 것이지 배분이 고쳐진 게 아니다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds  # noqa: E402
from evdt.viz.plots import _korean_font  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

#: 검증된 범주형 1·2·3 번 칸 (dataviz/references/palette.md). **고정 순서**다
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70"]

LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
         "rule": "#d8d7d2", "grid": "#e8e7e2"}
DARK = {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7",
        "rule": "#3a3a37", "grid": "#2c2c29"}


def mnl_waits(mnl_dir: Path, seeds: list[int]) -> pd.Series:
    acc, used = [], 0
    for s in seeds:
        p = mnl_dir / f"seed{s}" / "MNL.parquet"
        if not p.is_file():
            continue
        used += 1
        acc.append(pd.read_parquet(p).groupby("station_id")["wait_min"].mean())
    if not used:
        raise SystemExit(f"[중단] {mnl_dir} 에서 MNL parquet 를 못 찾았다")
    return pd.concat(acc, axis=1).sum(axis=1) / used


def ladder_waits(cfg, stages: list[str], seeds: list[int]) -> dict[str, pd.Series]:
    ids = {st: [make_run_id(cfg.scenario_id, st, s, cfg.policy.participation)
                for s in seeds] for st in stages}
    flat = sorted({r for v in ids.values() for r in v})
    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        done = {r[0] for r in con.execute(
            f"SELECT run_id FROM run WHERE status='DONE' AND run_id IN "  # noqa: S608
            f"({','.join('?' * len(flat))})", flat)}
    for st, v in ids.items():
        missing = [r for r in v if r not in done]
        if missing:
            raise SystemExit(f"[중단] {st} 의 끝나지 않은 run {len(missing)}개: "
                             f"{missing[:2]} …")
    con = duck_connect(db_path=default_db_path(), run_ids=flat, runs_dir=RUNS_DIR)
    w = con.sql("SELECT run_id, station_id, AVG(wait_min) AS wait_min "
                "FROM charge_event GROUP BY 1, 2").df()
    return {st: w[w.run_id.isin(r)].groupby("station_id")["wait_min"].sum() / len(r)
            for st, r in ids.items()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_congested.yaml")
    ap.add_argument("--stages", default="S2,S4")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--mnl-dir",
                    default="runs/mnl_world/chuseok_congested_floor/theta0p05_w1p0")
    ap.add_argument("--top", type=int, default=0,
                    help="첫 칸 기준 상위 N 곳만 크게. 0 이면 `--min-wait` 로 거른다")
    ap.add_argument("--min-wait", type=float, default=1.0,
                    help="첫 칸 대기가 이 아래인 휴게소는 줄에서 빼고 각주로 적는다. "
                         "0 분짜리 줄이 그림 절반을 먹으면 봉우리가 안 보인다")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--out", default="docs/figures/wait_dumbbell.png")
    args = ap.parse_args()

    cfg = load_config(args.config, stage="S0")
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    seeds = parse_seeds(args.seeds)
    if len(stages) + 1 > len(SERIES):
        raise SystemExit(f"[중단] 칸이 {len(stages) + 1}개다 — 범주형 색은 3개까지 "
                         "직접 라벨로 안전하다. 더 필요하면 작은 다중 그림으로 가를 것")

    series = {"MNL": mnl_waits(ROOT / args.mnl_dir, seeds)}
    series.update(ladder_waits(cfg, stages, seeds))

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        nm = dict(con.execute("SELECT station_id, name FROM station"))

    used = sorted({s for v in series.values() for s in v.index})
    frame = pd.DataFrame({k: v.reindex(used).fillna(0.0) for k, v in series.items()},
                         index=used)
    frame.index = [nm.get(i, i).replace("휴게소", "") for i in frame.index]
    frame = frame.sort_values(frame.columns[0], ascending=True)   # 아래→위 = 작은→큰
    # ★ 거의 0 분인 휴게소를 줄에서 뺀다. 16곳 중 7곳이 1분 미만이라 그대로 두면
    #   그림 절반이 빈 줄이 되고 봉우리가 눌린다. **버리는 게 아니라 각주로 옮긴다** —
    #   "막히는 곳은 몇 곳뿐" 이라는 쏠림 자체가 그 숫자다
    # ★ 통계는 **거르기 전 전체**로 낸다. 조용한 휴게소를 빼고 편차를 내면
    #   "고르게 됐다" 의 분모가 바뀌어 다른 주장이 된다 (전체 10.5 → 3.2 가
    #   거른 뒤에는 9.8 → 3.5 로 보인다)
    full = frame
    if args.top:
        quiet = frame.iloc[:-args.top]
        frame = frame.tail(args.top)
    else:
        quiet = frame[frame[frame.columns[0]] < args.min_wait]
        frame = frame[frame[frame.columns[0]] >= args.min_wait]

    # --- 표 (대비 WARN 의 구제책 겸 검산) -----------------------------------
    keys = list(full.columns)
    print(f"\n{cfg.scenario_id} · 시드 {len(seeds)}개")
    print(f"\n{'휴게소':<12}" + "".join(f"{k:>9}" for k in keys) + f"{'MNL→끝':>10}")
    for nmx, r in full.iloc[::-1].iterrows():
        cut = r[keys[0]] - r[keys[-1]]
        print(f"{nmx[:11]:<12}" + "".join(f"{r[k]:>9.1f}" for k in keys)
              + f"{cut:>+10.1f}")
    stat = {k: (full[k].mean(), full[k].max(), full[k].std(ddof=1)) for k in keys}
    print(f"\n{'평균':<12}" + "".join(f"{stat[k][0]:>9.1f}" for k in keys))
    print(f"{'최대':<12}" + "".join(f"{stat[k][1]:>9.1f}" for k in keys))
    print(f"{'편차':<12}" + "".join(f"{stat[k][2]:>9.1f}" for k in keys))
    drop = (1 - stat[keys[-1]][2] / stat[keys[0]][2]) * 100
    print(f"\n  ★ 편차 {stat[keys[0]][2]:.1f} → {stat[keys[-1]][2]:.1f}분 "
          f"({drop:.0f}% 감소) — 이것이 '완만해졌다' 의 자다")

    # --- 그림 ---------------------------------------------------------------
    C = DARK if args.dark else LIGHT
    cols = SERIES_DARK if args.dark else SERIES
    _korean_font(plt)
    n = len(frame)
    big = n <= 6                      # 줄이 적으면 슬라이드용으로 키운다
    fs = 15.0 if big else 10.5
    fig, ax = plt.subplots(figsize=(11.2, (0.92 if big else 0.46) * n + 2.6))
    fig.patch.set_facecolor(C["surface"])
    ax.set_facecolor(C["surface"])

    y = np.arange(n)
    lo = frame[keys].min(axis=1).to_numpy()
    hi = frame[keys].max(axis=1).to_numpy()
    # 항목 안에서만 잇는다 — 휴게소 **사이** 를 잇지 않는다
    ax.hlines(y, lo, hi, color=C["rule"], linewidth=3.0 if big else 2.0, zorder=1)

    for i, k in enumerate(keys):
        # 겹친 점에 바탕색 테두리를 둘러 앞뒤를 가른다. 2px 는 이 크기에서
        # 초승달처럼 깨져 보여서 1.4px 로 낮췄다
        ax.scatter(frame[k], y, s=250 if big else 104, color=cols[i], zorder=3 + i,
                   edgecolors=C["surface"], linewidths=2.0 if big else 1.4,
                   label=k)

    # 직접 라벨 — 청록의 배경 대비가 3:1 아래라 **의무**다.
    # ★ **그 줄의 최소·최대 점**에 바깥쪽으로 붙인다. 처음에는 "첫 칸은 오른쪽,
    #   마지막 칸은 왼쪽" 으로 뒀는데 두 군데서 깨졌다:
    #     · 엔진이 더 나빠진 줄(양산 S2 4.0 < S4 9.5)에서 왼쪽 라벨이 가운데
    #       점 위에 얹혔다
    #     · 값이 0.0 인 줄에서 왼쪽 라벨이 휴게소 이름을 먹었다
    #   최소·최대에 붙이면 **라벨이 항상 바로 옆 점의 값**이라 모호하지 않다
    for row in range(n):
        vals = frame[keys].iloc[row]
        vmin, vmax = float(vals.min()), float(vals.max())
        ax.annotate(f"{vmax:.1f}", (vmax, row), textcoords="offset points",
                    xytext=(13, 0), ha="left", va="center", fontsize=9.5,
                    color=C["ink2"], zorder=9)
        if vmax - vmin > 0.15:          # 겹쳐 있으면 한 번만 적는다
            ax.annotate(f"{vmin:.1f}", (vmin, row), textcoords="offset points",
                        xytext=(-13, 0), ha="right", va="center", fontsize=9.5,
                        color=C["ink2"], zorder=9)

    ax.set_yticks(y)
    ax.set_yticklabels(frame.index, fontsize=fs, color=C["ink"])
    ax.set_xlabel("휴게소별 평균 대기 (분)", fontsize=10.5, color=C["ink2"])
    top = float(frame[keys].to_numpy().max())
    ax.set_xlim(-top * 0.085, top * 1.17)
    ax.set_ylim(-0.8, n - 0.2)
    ax.grid(axis="x", color=C["grid"], linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(C["grid"])
    ax.tick_params(colors=C["ink2"], length=0)

    lead = frame.index[-1]
    ax.set_title(
        f"엔진이 봉우리를 깎는다 — {lead} {frame[keys[0]].iloc[-1]:.1f} → "
        f"{frame[keys[-1]].iloc[-1]:.1f}분",
        fontsize=14.5, color=C["ink"], loc="left", pad=26, fontweight="medium")
    ax.annotate(
        f"휴게소 사이 편차 {stat[keys[0]][2]:.1f} → {stat[keys[-1]][2]:.1f}분 "
        f"({drop:.0f}% 감소) · 평균 {stat[keys[0]][0]:.1f} → "
        f"{stat[keys[-1]][0]:.1f}분 · 시드 {len(seeds)}개",
        xy=(0, 1), xycoords="axes fraction", xytext=(0, 14),
        textcoords="offset points", fontsize=10, color=C["ink2"])

    if len(quiet):
        # ★ 각주는 **거른 방식에 맞춰** 쓴다. `--top` 으로 자른 뒤에도
        #   "1분 미만" 이라고 적으면 거짓말이 된다 (상위 5곳만 남기면 나머지에
        #   경산 12.9분이 들어 있다)
        worst_nm = str(quiet[keys[0]].idxmax())
        worst = float(quiet[keys[0]].max())
        note = (f"나머지 {len(quiet)}곳은 그림에서 뺐다 — 가장 큰 곳이 "
                f"{worst_nm} {worst:.1f}분"
                if args.top else
                f"나머지 {len(quiet)}곳은 {keys[0]} 기준 {args.min_wait:g}분 미만 "
                f"(가장 큰 곳 {worst_nm} {worst:.1f}분) — 막히는 곳은 몇 곳뿐이다")
        ax.annotate(note, xy=(0, 0), xycoords="axes fraction", xytext=(0, -46),
                    textcoords="offset points", fontsize=9.5, color=C["ink2"])

    leg = ax.legend(loc="lower right", frameon=False, fontsize=11,
                    handletextpad=0.4, borderaxespad=1.2)
    for t in leg.get_texts():
        t.set_color(C["ink"])

    fig.tight_layout()
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=C["surface"])
    print(f"\n  저장 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
