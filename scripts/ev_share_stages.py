"""EV 보급률별 MNL 바닥 · S2 · S4 (10시드 짝지은 비교).

    python scripts/ev_share_stages.py

같은 추석 무대(2026-09-25 상행 혼잡)에서 **EV 보급률만** 바꿔 돌린 결과를 한 표로
모은다. 보급률 손잡이는 `--ev-share` 이고 `cfg.variant` 로 꼬리표가 붙는다 (#123) —
안 붙이면 진짜 run 을 조용히 덮는다.

바닥은 **각 보급률의 MNL** 이다 (θ=0.05 · w=1.0 · ASC 없음 = 엔진 없는 평상시).
보급률이 다르면 바닥도 다르므로 **같은 줄 안에서만** 비율을 읽는다.

⚠ EV 15% 줄은 config 기본값이라 꼬리표가 없다 (`chuseok_2026_up_congested__S2__…`).
  `--ev-share 0.15` 로 다시 돌리면 물리는 같고 run_id 만 달라져 같은 일을 두 번
  한다. 그래서 기존 run 을 그대로 읽는다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: F401

#: (보급률 라벨, 사다리 CSV, MNL 바닥 디렉터리)
SHARES = (
    ("3.7% (현재 수준)", "ladder_chuseok_2026_up_congested__ev04__S0-S1-S2-S3-S4.csv",
     "chuseok_ev037_floor"),
    ("5%",  "ladder_chuseok_2026_up_congested__ev05__S0-S2-S4.csv", "chuseok_ev05_floor"),
    ("10%", "ladder_chuseok_2026_up_congested__ev10.csv",           "chuseok_ev10_floor"),
    ("15%", "ladder_chuseok_2026_up_congested.csv",
     "chuseok_congested_floor/theta0p05_w1p0"),
)
STAGES = ("S0", "S2", "S4")


def paired_ci(x) -> tuple[float, float, float]:
    """짝지은 비교의 평균과 95% CI. 시드가 하나면 CI 가 없다 (nan)."""

    x = np.asarray(x, float)
    if len(x) < 2:
        return float(x.mean()), float("nan"), float("nan")
    se = x.std(ddof=1) / np.sqrt(len(x))
    return float(x.mean()), float(x.mean() - 1.96 * se), float(x.mean() + 1.96 * se)


def floor_cells(name: str) -> pd.DataFrame:
    f = pd.read_csv(ROOT / f"runs/mnl_world/{name}/cells.csv")
    f = f[(f.stage == "MNL") & f.final]
    if f.seed.duplicated().any():
        raise ValueError(f"{name}: 시드가 겹친다 — 어느 칸을 바닥으로 쓸지 알 수 없다")
    return f.set_index("seed")


def main() -> int:
    print("추석 2026-09-25 상행 혼잡 무대 · 바닥 = MNL θ=0.05 · w=1.0 · ASC 없음")
    print("=" * 92)
    print(f"{'EV':<16} {'시드':>4} {'MNL 바닥':>10} {'S2':>10} {'S4':>10} "
          f"{'S2 개선':>16} {'S4 개선':>16}")
    print("-" * 92)
    detail = []
    for label, ladder, floor in SHARES:
        p = ROOT / "runs/sweeps" / ladder
        if not p.is_file():
            print(f"{label:<16}  (아직 안 돌렸다 — {ladder})")
            continue
        L, F = pd.read_csv(p), floor_cells(floor)
        seeds = sorted(set(L.seed) & set(F.index))
        base = F.loc[seeds, "total_h"]
        row, imp = {}, {}
        for st in STAGES:
            g = L[L.stage == st].set_index("seed")
            if g.empty:
                continue
            s = [x for x in seeds if x in g.index]
            row[st] = g.loc[s, "total_h"].mean()
            imp[st] = paired_ci(100 * (1 - g.loc[s, "total_h"].to_numpy()
                                       / base.loc[s].to_numpy()))
            detail.append((label, st, row[st], imp[st], g.loc[s]))
        print(f"{label:<16} {len(seeds):>4} {base.mean():9.1f}h "
              f"{row.get('S2', float('nan')):9.1f}h {row.get('S4', float('nan')):9.1f}h "
              + "  ".join(f"{imp[st][0]:6.2f}% [{imp[st][1]:5.1f},{imp[st][2]:5.1f}]"
                          for st in ("S2", "S4") if st in imp))

        if {"S0", "S4"} <= row.keys():
            b = base.mean()
            print(f"{'':<16} {'':>4}  ├ 안내  (MNL→S0) {100 * (b - row['S0']) / b:6.2f}%p"
                  f"   └ 최적화 (S0→S4) {100 * (row['S0'] - row['S4']) / b:6.2f}%p")

    print()
    print("=" * 92)
    print("이탈 · 대기 · 병목칸 (평균)")
    print(f"{'EV':<16} {'단계':<5} {'총비용':>9} {'이탈':>7} {'대기':>7} {'병목칸':>7}")
    for label, st, tot, _, g in detail:
        print(f"{label:<16} {st:<5} {tot:8.1f}h {g.n_escaped.mean():6.1f} "
              f"{g.wait_mean_min.mean():6.2f} {g.bottleneck_slots.mean():6.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
