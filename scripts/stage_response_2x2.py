"""평시·추석 × 추움·따뜻함 2×2 에서 사다리가 어떻게 반응하나 (#178).

    python scripts/stage_response_2x2.py

**왜 2×2 인가.** "평시에는 엔진이 어떻게 반응하나" 를 그냥 재면 답이 둘로 섞인다 —
평시(3월)와 추석(9월)은 **교통량도 기온도** 다르다. 기온은 `cold_factor` 로 충전
속도에 직접 걸린다 (5.33°C → 0.752). 그래서 네 칸을 다 돌려야 어느 쪽이 엔진의
반응을 바꾸는지 말할 수 있다.

**무엇을 찍나.** 칸마다

    안내 (MNL → S0)      "지금 어디가 빈지 알려 주기만" 하면 생기는 몫
    최적화 (S0 → S4)     그 위에 엔진이 조율해서 더 얻는 몫

이 둘을 가르는 이유는 `ev_share_engine_value.txt` 와 같다 — 섞어 말하면 "줄도 없는데
엔진이 왜 필요하냐" 에 답할 수 없다.

⚠ 바닥(MNL)은 칸마다 다르다. 같은 칸 안에서만 비율을 읽어야 한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: F401

#: 2×2 의 네 칸. (이름, 사다리 CSV, MNL 바닥 디렉터리)
CELLS = (
    ("추석 교통 · 22.18°C (추석 실측)", "ladder_chuseok_2026_up_congested.csv",
     "chuseok_congested_floor/theta0p05_w1p0"),
    ("추석 교통 ·  5.33°C",             "ladder_chuseok_2026_up_congested__t5p33__S0-S1-S2-S3-S4.csv",
     "chuseok_cold_floor"),
    ("평시 교통 · 22.18°C",             "ladder_quiet_2026_up_congested__t22p18__S0-S1-S2-S3-S4.csv",
     "quiet_warm_floor"),
    ("평시 교통 ·  5.33°C (평시 실측)", "ladder_quiet_2026_up_congested__S0-S1-S2-S3-S4.csv",
     "quiet_congested_floor"),
)
STAGES = ("S0", "S1", "S2", "S3", "S4")


def paired_ci(x: np.ndarray) -> tuple[float, float, float]:
    """짝지은 비교의 평균과 95% CI (시드마다 같은 바닥을 쓴다).

    시드가 하나면 CI 가 없다 — nan 을 돌려주고 호출자가 비워 찍는다. 아직 돌고
    있는 칸을 표에서 빼지 않고 그대로 보기 위한 것이다.
    """

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
    share: dict[str, tuple[float, float]] = {}
    for label, ladder, floor in CELLS:
        lad_p = ROOT / "runs/sweeps" / ladder
        if not lad_p.is_file():
            print(f"\n{label}\n  (아직 안 돌렸다 — {ladder})")
            continue
        L = pd.read_csv(lad_p)
        F = floor_cells(floor)
        seeds = sorted(set(L.seed) & set(F.index))
        base = F.loc[seeds, "total_h"]

        print(f"\n{'=' * 80}\n{label}   시드 {len(seeds)}개")
        print(f"  MNL 바닥  {base.mean():8.1f}h   이탈 {F.loc[seeds, 'n_escaped'].mean():6.1f}대  "
              f"대기 {F.loc[seeds, 'wait_mean_min'].mean():5.2f}분  "
              f"정차 {F.loc[seeds, 'n_charge_visits'].mean():6.0f}건")
        print(f"  {'단계':<4} {'총비용':>9} {'바닥대비':>8} {'95% CI':>17} "
              f"{'이탈':>6} {'대기':>6} {'병목칸':>6}")

        got = {}
        for st in STAGES:
            g = L[L.stage == st].set_index("seed")
            if g.empty:
                continue
            s = [x for x in seeds if x in g.index]
            imp = 100 * (1 - g.loc[s, "total_h"].to_numpy() / base.loc[s].to_numpy())
            m, lo, hi = paired_ci(imp)
            got[st] = g.loc[s, "total_h"].mean()
            ci = "       —  ,   —  " if np.isnan(lo) else f"{lo:6.2f},{hi:6.2f}"
            print(f"  {st:<4} {got[st]:8.1f}h {m:7.2f}% [{ci}] "
                  f"{g.loc[s, 'n_escaped'].mean():6.1f} {g.loc[s, 'wait_mean_min'].mean():5.2f} "
                  f"{g.loc[s, 'bottleneck_slots'].mean():6.1f}")

        if {"S0", "S4"} <= got.keys():
            b = base.mean()
            print(f"  ├ 안내  (MNL→S0)  {100 * (b - got['S0']) / b:6.2f}%p")
            print(f"  └ 최적화 (S0→S4)  {100 * (got['S0'] - got['S4']) / b:6.2f}%p")
            share[label] = (100 * (b - got["S0"]) / b, 100 * (got["S0"] - got["S4"]) / b)

        # S4 가 S3 보다 나쁜 칸이 있다 — 짝지은 부호 검정으로 확인한다
        if {"S3", "S4"} <= got.keys():
            a = L[L.stage == "S3"].set_index("seed").loc[seeds, "total_h"].to_numpy()
            c = L[L.stage == "S4"].set_index("seed").loc[seeds, "total_h"].to_numpy()
            d = 100 * (a - c) / a
            m, lo, hi = paired_ci(d)
            mark = "" if lo > 0 else ("  ★ S4 가 S3 를 못 넘는다" if hi < 0 else "  ★ 동점")
            print(f"  S3 → S4  {m:+6.2f}%  [{lo:+6.2f},{hi:+6.2f}]  "
                  f"S4 가 더 싼 시드 {int((c < a).sum())}/{len(seeds)}{mark}")

    _factorial(share)
    return 0


#: 2×2 를 가르는 축. CELLS 의 이름과 글자가 같아야 한다.
WARM, COLD = "22.18°C", "5.33°C"
CHUSEOK, QUIET = "추석 교통", "평시 교통"


def _factorial(share: dict[str, tuple[float, float]]) -> None:
    """네 칸이 다 있으면 교통량 효과와 기온 효과를 갈라 찍는다.

    1절(바닥)의 2×2 와 **결론이 다르다** — 바닥의 총비용은 기온이 교통량보다 3배
    크게 움직이지만, **조율의 몫**은 교통량이 기온보다 2배 크게 움직인다. 그래서
    두 표를 섞어 말하면 안 된다.
    """

    def pick(traffic: str, temp: str) -> tuple[float, float] | None:
        for k, v in share.items():
            if traffic in k and temp in k:
                return v
        return None

    grid = {(t, c): pick(t, c) for t in (CHUSEOK, QUIET) for c in (WARM, COLD)}
    if any(v is None for v in grid.values()):
        print()
        print("(네 칸이 다 돌아야 효과를 가른다)")
        return

    for i, name in ((0, "안내 (MNL→S0)"), (1, "최적화 (S0→S4)")):
        print()
        print("=" * 80)
        print(f"{name} — %p")
        print(f"  {'':<12} {WARM:>10} {COLD:>10}   기온 효과")
        for t in (CHUSEOK, QUIET):
            w, c = grid[(t, WARM)][i], grid[(t, COLD)][i]
            print(f"  {t:<12} {w:10.2f} {c:10.2f}   {c - w:+8.2f}")
        print(f"  {'교통량 효과':<10}", end="")
        for c in (WARM, COLD):
            print(f" {grid[(QUIET, c)][i] - grid[(CHUSEOK, c)][i]:+9.2f}", end="")
        print()



if __name__ == "__main__":
    raise SystemExit(main())
