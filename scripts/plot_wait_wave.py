"""대기 파형 — 휴게소를 **도로 순서로** 늘어놓고 칸마다 선 하나 (발표 §5-1).

    python scripts/plot_wait_wave.py --stages S2,S4 \\
        --mnl-dir runs/mnl_world/chuseok_congested_floor/theta0p05_w1p0
    python scripts/plot_wait_wave.py --stages S0,S1,S2,S3,S4 --no-mnl

## `plot_station_bars.py` 와 무엇이 다른가

  막대 그림   칸마다 패널을 따로 둔다. 휴게소별 값을 **읽는** 그림이다
  이 그림     한 축에 선을 겹친다. **점점 완만해지는 것을 보는** 그림이다

가로축이 다르다. 막대 그림은 첫 칸의 대기 **내림차순**이고, 이 그림은
**코리도 위치(offset_km) 순서**다. 그래야 "파형" 으로 읽힌다 — 봉우리가
어디에 서 있고, 칸이 올라갈수록 그 봉우리가 어떻게 주저앉는지가 보인다.

## 숫자는 같은 출처다

`charge_event.wait_min` — 막대 그림·2D 히트맵과 **같은 열**이다. 그림마다
다른 정의를 쓰면 나란히 못 놓는다.

## 완만함을 숫자로도 적는다

선이 "완만해졌다" 를 눈대중으로 두지 않는다. 범례에 세 가지를 적는다.

    평균      전 휴게소 평균 대기 (분)
    최대      가장 오래 기다린 휴게소 (= 봉우리 높이)
    편차      휴게소 사이의 표준편차 — **이게 완만함의 자다**

편차가 줄어드는 것이 "쏠림이 풀렸다" 의 정의다. 평균만 줄고 편차가 그대로면
전체가 같이 좋아진 것이지 배분이 고쳐진 게 아니다.
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

#: 칸이 올라갈수록 짙어진다 (plot_station_bars 와 같은 톤)
COLORS = ["#c45646", "#e8a33d", "#9ec5f4", "#3987e5", "#1c5cab", "#0d366b"]
MARKERS = ["o", "s", "^", "D", "v", "P"]


def station_order() -> tuple[list[str], dict[str, str], dict[str, float]]:
    """코리도 위치 순서 · 이름 · km. **도로 순서가 이 그림의 가로축이다.**"""

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        rows = con.execute(
            "SELECT station_id, name, offset_km FROM station "
            "WHERE station_id LIKE ? ORDER BY offset_km", ("gyeongbu_%",)).fetchall()
    return ([r[0] for r in rows], {r[0]: r[1] for r in rows},
            {r[0]: float(r[2]) for r in rows})


def ladder_waits(cfg, stages: list[str], seeds: list[int]) -> dict[str, pd.Series]:
    """사다리 칸의 휴게소별 평균 대기. **끝난 run 만** 쓴다."""

    ids = {st: [make_run_id(cfg.scenario_id, st, s, cfg.policy.participation)
                for s in seeds] for st in stages}
    flat = sorted({r for v in ids.values() for r in v})
    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        done = {r[0] for r in con.execute(
            f"SELECT run_id FROM run WHERE status='DONE' AND run_id IN "  # noqa: S608
            f"({','.join('?' * len(flat))})", flat)}
    missing = {st: [r for r in v if r not in done] for st, v in ids.items()}
    for st, m in missing.items():
        if m:
            raise SystemExit(f"[중단] {st} 의 끝나지 않은 run {len(m)}개: {m[:2]} …")

    con = duck_connect(db_path=default_db_path(), run_ids=flat, runs_dir=RUNS_DIR)
    w = con.sql("SELECT run_id, station_id, AVG(wait_min) AS wait_min, "
                "COUNT(*) AS n FROM charge_event GROUP BY 1, 2").df()
    out = {}
    for st, rids in ids.items():
        g = w[w.run_id.isin(rids)]
        # 시드 평균 — 시드마다 휴게소별 평균을 내고 그걸 평균한다
        out[st] = g.groupby("station_id")["wait_min"].sum() / len(rids)
    return out


def mnl_waits(mnl_dir: Path, seeds: list[int]) -> pd.Series:
    """MNL 바닥 — `run_mnl_world.py` 의 parquet. DB 에 run 이 없다."""

    acc, used = [], 0
    for s in seeds:
        p = mnl_dir / f"seed{s}" / "MNL.parquet"
        if not p.is_file():
            continue
        used += 1
        acc.append(pd.read_parquet(p).groupby("station_id")["wait_min"].mean())
    if not used:
        raise SystemExit(f"[중단] {mnl_dir} 에서 MNL parquet 를 못 찾았다")
    print(f"  MNL 바닥 — 시드 {used}개 · {mnl_dir.name}")
    return pd.concat(acc, axis=1).sum(axis=1) / used


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_congested.yaml")
    ap.add_argument("--stages", default="S2,S4")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--mnl-dir",
                    default="runs/mnl_world/chuseok_congested_floor/theta0p05_w1p0")
    ap.add_argument("--no-mnl", action="store_true")
    ap.add_argument("--out", default="docs/figures/wait_wave.png")
    args = ap.parse_args()

    cfg = load_config(args.config, stage="S0")
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    seeds = parse_seeds(args.seeds)

    print(f"\n{cfg.scenario_id}  칸 {' │ '.join(stages)}  시드 {len(seeds)}개")
    series: dict[str, pd.Series] = {}
    if not args.no_mnl:
        series["MNL"] = mnl_waits(ROOT / args.mnl_dir, seeds)
    series.update(ladder_waits(cfg, stages, seeds))

    order, names, km = station_order()
    # 이 무대에 실제로 쓰인 휴게소만 (방향이 섞이지 않게)
    used = [s for s in order if any(s in v.index for v in series.values())]
    if not used:
        raise SystemExit("[중단] 겹치는 휴게소가 없다 — 방향이 다른 config 인지 확인")

    # --- 표 ----------------------------------------------------------------
    print(f"\n{'휴게소':<13}{'km':>7}" + "".join(f"{k:>9}" for k in series))
    for sid in used:
        row = f"{names.get(sid, sid)[:12]:<13}{km[sid]:>7.0f}"
        row += "".join(f"{float(v.get(sid, 0.0)):>9.1f}" for v in series.values())
        print(row)

    print(f"\n{'완만함의 자':<13}{'':>7}" + "".join(f"{k:>9}" for k in series))
    stat = {}
    for k, v in series.items():
        a = np.array([float(v.get(s, 0.0)) for s in used])
        stat[k] = (a.mean(), a.max(), a.std(ddof=1))
    for label, i in (("평균 (분)", 0), ("최대 (분)", 1), ("편차 (분)", 2)):
        print(f"{label:<13}{'':>7}" + "".join(f"{stat[k][i]:>9.1f}" for k in series))

    first = next(iter(series))
    print(f"\n  ★ 편차가 {stat[first][2]:.1f} → "
          f"{stat[list(series)[-1]][2]:.1f}분 으로 "
          f"{(1 - stat[list(series)[-1]][2] / stat[first][2]) * 100:.0f}% 줄었다 "
          f"— **이것이 '완만해졌다' 의 자다**")

    # --- 그림 --------------------------------------------------------------
    _korean_font(plt)
    fig, ax = plt.subplots(figsize=(13.5, 5.6))
    x = np.arange(len(used))
    for i, (k, v) in enumerate(series.items()):
        y = [float(v.get(s, 0.0)) for s in used]
        mu, mx, sd = stat[k]
        ax.plot(x, y, marker=MARKERS[i % len(MARKERS)], markersize=5.5,
                linewidth=2.6 if i == 0 else 2.0,
                color=COLORS[i % len(COLORS)], zorder=5 - i,
                label=f"{k}   평균 {mu:.1f} · 최대 {mx:.1f} · 편차 {sd:.1f}분")
    # 첫 칸(현실)을 옅게 채워 "깎여 나간 몫" 을 보이게 한다
    y0 = [float(series[first].get(s, 0.0)) for s in used]
    ax.fill_between(x, y0, color=COLORS[0], alpha=0.10, zorder=0)

    ax.set_xticks(list(x))
    ax.set_xticklabels([names.get(s, s).replace("휴게소", "")[:4] for s in used],
                       fontsize=9)
    ax.set_ylabel("휴게소별 평균 대기 (분)")
    ax.set_xlabel(f"← 코리도 위치 순서 ({km[used[0]]:.0f} → {km[used[-1]]:.0f} km) →")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", alpha=0.25, linewidth=0.7)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")
    ax.set_title(
        f"대기 파형 — 칸이 올라갈수록 봉우리가 주저앉는다   "
        f"({cfg.scenario_id} · 시드 {len(seeds)}개)", fontsize=11.5, loc="left")
    fig.tight_layout()

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160)
    print(f"\n  저장 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
