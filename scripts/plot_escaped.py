"""코리도를 벗어난 차를 그린다 (#54).

    python scripts/plot_escaped.py <run_id>
    python scripts/plot_escaped.py <run_id> --out figures/escaped.png

**평균 대기만 보면 이 차들이 빠져서 좋아 보인다.** 문제가 사라진 것이 아니라 고속도로
밖으로 옮겨간 것이므로, 대기 히트맵과 **반드시 같이** 본다.

세 칸으로 그린다.

    왼쪽    시간대별 — 언제 밀려났나 (충전한 차와 나란히)
    가운데  진입 지점별 — 어디서 탄 차가 밀려나나 (이유별로 나눠서)
    오른쪽  balked 이 남았다면 갔을 휴게소 — **어느 휴게소가 차를 밀어냈나**

⚠ **이유를 반드시 가른다.** 실측에서 이탈의 91% 가 `no_plan` 이다 — 줄이 길어서가
아니라 **닿는 휴게소가 아예 없어서**다.

    no_plan   엔진이 못 고친다. 충전기 증설이나 출발 SoC 모델의 문제다 (#55)
    balked    엔진이 고칠 수 있다. 덜 붐비는 곳으로 돌려보내면 남는다

오른쪽 칸이 엔진이 손댈 자리다. 둘을 합쳐 놓고 "엔진이 이탈을 줄였다" 고 하면
고칠 수 없는 몫까지 성과로 세게 된다.

기본 저장 위치: runs/<run_id>/escaped.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import numpy as np
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.loaders import load_run_table  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BALKED = "#d62728"      # 엔진이 고칠 수 있다
NO_PLAN = "#f0a0a0"     # 엔진이 못 고친다
CHARGED = "#7f9fbf"


def main() -> int:
    parser = argparse.ArgumentParser(description="이탈 차량 그림")
    parser.add_argument("run_id")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    out = args.out or RUNS_DIR / args.run_id / "escaped.png"
    escaped = load_run_table(args.run_id, "escape_event", RUNS_DIR)

    if escaped.empty:
        print(f"이탈한 차가 없습니다: {args.run_id}\n"
              "  demand.escape_cost_min 이 0 이면 이탈 선택지가 없습니다")
        return 1

    charged = load_run_table(args.run_id, "charge_event", RUNS_DIR)
    corridor = "gyeongbu_up" if "_up_" in args.run_id else "gyeongbu_down"

    with get_conn(default_db_path(), readonly=True) as conn:
        offsets = dict(conn.execute(
            "SELECT station_id, offset_km FROM station WHERE corridor_id = ?",
            (corridor,)).fetchall())

    balked = escaped[escaped["reason"] == "balked"]
    no_plan = escaped[escaped["reason"] == "no_plan"]
    total = len(escaped) + len(charged["ev_id"].unique())
    share = len(escaped) / max(total, 1)

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))

    # 1. 시간대별
    ax = axes[0]
    hours = np.arange(24)
    def by_hour(frame, col):
        return (frame[col] // 60).astype(int).clip(0, 23).value_counts().reindex(
            hours, fill_value=0)

    chg_h = by_hour(charged, "t_arrive_min")
    nop_h = by_hour(no_plan, "entry_time_min")
    blk_h = by_hour(balked, "entry_time_min")
    ax.bar(hours, chg_h, color=CHARGED, label="charged on the corridor")
    ax.bar(hours, nop_h, bottom=chg_h, color=NO_PLAN, label="left: nothing in range")
    ax.bar(hours, blk_h, bottom=chg_h + nop_h, color=BALKED, label="left: queue too long")
    ax.set_xlabel("hour of entry")
    ax.set_ylabel("vehicles")
    ax.set_title("when", fontsize=10)
    ax.legend(fontsize=8)

    # 2. 진입 지점별 이탈률
    ax = axes[1]
    bins = np.arange(0, 440, 20.0)
    nop_x = np.histogram(no_plan["entry_offset_km"], bins=bins)[0]
    blk_x = np.histogram(balked["entry_offset_km"], bins=bins)[0]
    ax.bar(bins[:-1], nop_x, width=18, align="edge", color=NO_PLAN,
           label=f"nothing in range ({len(no_plan):,})")
    ax.bar(bins[:-1], blk_x, bottom=nop_x, width=18, align="edge", color=BALKED,
           label=f"queue too long ({len(balked):,})")
    ax.set_xlabel("entry offset (km)")
    ax.set_ylabel("vehicles")
    ax.set_title("where they joined", fontsize=10)
    ax.legend(fontsize=8)

    # 3. 남았다면 갔을 휴게소 — 엔진이 손댈 자리
    ax = axes[2]
    by_station = balked["best_station_id"].value_counts().head(10)
    # 한글 휴게소 이름은 기본 폰트에 없어 네모로 나온다. 기점거리로 쓴다
    labels = [f"{offsets.get(s, float('nan')):.0f} km" for s in by_station.index][::-1]
    ax.barh(range(len(by_station)), by_station.to_numpy()[::-1], color=BALKED)
    ax.set_yticks(range(len(by_station)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("vehicles pushed out")
    ax.set_title(f"which stop pushed them out (balked only, {len(balked):,})", fontsize=10)

    for ax in axes:
        ax.grid(alpha=0.3, axis="y")

    fig.suptitle(
        f"{args.run_id}\n"
        f"left the corridor: {len(escaped):,} of {total:,} charging vehicles ({share:.1%}) "
        f"— nothing in range {len(no_plan):,} (chargers / SoC, #55), "
        f"queue too long {len(balked):,} (what the engine can fix)",
        fontsize=9)
    fig.tight_layout()

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140)
    print(f"이탈 {len(escaped):,}대 / 충전 필요 {total:,}대 ({share:.1%})")
    print(f"  닿는 휴게소 없음 {len(no_plan):,} ({len(no_plan)/len(escaped):.1%}) — 엔진이 못 고친다")
    print(f"  줄이 길어서     {len(balked):,} ({len(balked)/len(escaped):.1%}) — 엔진이 고칠 수 있다")
    print(f"  저장 {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
