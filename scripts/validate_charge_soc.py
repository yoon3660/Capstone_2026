"""충전 시작 SoC 가 실측과 맞는지 본다 (#82).

    python scripts/validate_charge_soc.py --seeds 1-20
    python scripts/validate_charge_soc.py --seeds 1-20 --config config/scenario_seollal_up.yaml

## 왜 이게 검증인가

진입 SoC(고속도로에 올라올 때 배터리)는 **세계 어디서도 관측되지 않는다.** 충전기는
꽂는 순간부터 기록하고, 통행이 시작되는 순간은 OBD 수집 차량이 아니면 아무도 안 본다.
그래서 #55 는 값을 찾는 대신 **구조를 뒤집었다.**

    [이전]  충전 시작 SoC(34%) ──직접 투입──> 진입 SoC        ❌ 범주 오류
    [현재]  진입 SoC ──시뮬레이션──> 충전 시작 SoC → 30~34% 가 나오는가?

근거 없던 입력이 **검증 가능한 출력**이 된다. 이 스크립트가 그 마지막 화살표다.

## 목표값

| | 충전 시작 SoC | 출처 |
|---|---|---|
| 국내 | **30%** (→85% 까지 충전) | 김범일·안근원·신희철 (2022), 대한교통학회지 40(5) |
| EU | **34%** (Beta(2,5) 평균) | Rupnik et al. (2025) |

둘이 독립적으로 거의 같다는 점이 이 목표값을 믿을 만하게 만든다.

## ⚠ 통과해도 `mu` 가 맞다는 뜻은 아니다

충전 시작 SoC 는 진입 SoC 말고도 **통행거리·충전 판단 규칙**에 같이 달려 있다.
셋이 서로 상쇄해서 우연히 맞을 수도 있다. **필요조건이지 충분조건이 아니다** —
통행거리는 `validate_demand.py` 와 실측 OD(#61)가 따로 본다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.io.loaders import load_run_table  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402
from evdt.runner import parse_seeds  # noqa: E402

#: 목표값. 국내와 EU 가 독립적으로 거의 같다 (위 docstring).
TARGET_LO, TARGET_HI = 0.30, 0.34

#: 목표 구간에서 이만큼 벗어나면 "안 맞는다" 로 본다. 교통량 MAPE(12.1%) 와 비슷한 눈금.
TOLERANCE = 0.05


def collect(run_ids: list[str], runs_dir: Path) -> pd.DataFrame:
    rows = []
    for rid in run_ids:
        try:
            ce = load_run_table(rid, "charge_event", runs_dir)
        except FileNotFoundError:
            continue
        if ce.empty:
            continue
        rows.append(pd.DataFrame({
            "run_id": rid,
            "soc_in": ce["soc_in"].astype(float),
            "soc_out": ce["soc_out"].astype(float),
            "stop_seq": ce["stop_seq"].astype(int),
        }))
    if not rows:
        raise SystemExit(
            "\n[중단] 충전 기록을 찾지 못했다.\n"
            "  먼저:  python scripts/run_experiment.py --seeds <시드>"
        )
    return pd.concat(rows, ignore_index=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seeds", default="1-20")
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    args = ap.parse_args()

    cfg = ScenarioConfig.from_yaml(PROJECT_ROOT / args.config).variant("replay", {})
    seeds = parse_seeds(args.seeds)
    run_ids = [make_run_id(cfg.scenario_id, cfg.policy.stage, s, cfg.policy.participation)
               for s in seeds]

    ce = collect(run_ids, args.runs_dir)
    soc_in = ce["soc_in"]

    print(f"\n{cfg.scenario_id}  시드 {len(ce.run_id.unique())}개 · 충전 기록 {len(ce):,}건")
    print(f"수요: {cfg.demand_label}\n")

    print("=== 충전 시작 SoC ===")
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<3d} {np.percentile(soc_in, q):6.1%}")
    print(f"  평균  {soc_in.mean():6.1%}   표준편차 {soc_in.std():.1%}")

    # 첫 정차와 두 번째 정차를 갈라 본다 — 두 번째는 **직전 충전이 정한 값**이라
    # 실측(대부분 첫 충전)과 성격이 다르다. 섞어서 비교하면 목표값과 어긋난다.
    print("\n=== 정차 순서별 (실측과 비교할 것은 1번째다) ===")
    for seq, g in ce.groupby("stop_seq"):
        print(f"  {seq}번째 정차  n={len(g):6,}  평균 {g.soc_in.mean():6.1%}"
              f"  중앙값 {g.soc_in.median():6.1%}")

    first = ce[ce.stop_seq == 1]["soc_in"]
    mean = float(first.mean())

    print("\n=== 실측 대조 ===")
    print(f"  목표   {TARGET_LO:.0%} (국내, 김범일 외 2022) ~ {TARGET_HI:.0%} (EU, Rupnik 2025)")
    print(f"  우리   {mean:.1%}  (첫 정차 n={len(first):,})")

    if TARGET_LO - TOLERANCE <= mean <= TARGET_HI + TOLERANCE:
        gap = 0.0 if TARGET_LO <= mean <= TARGET_HI else min(
            abs(mean - TARGET_LO), abs(mean - TARGET_HI))
        print(f"\n  ✅ 맞는다 (목표 구간에서 {gap:.1%}p 차이)")
        print("     진입 SoC 가정이 **관측 가능한 양을 재현한다.**")
        print("     ⚠ 다만 통행거리·충전 판단 규칙에도 같이 달려 있다 — 필요조건이지")
        print("        충분조건이 아니다 (docs/departure_soc.md §6).")
        rc = 0
    else:
        side = "낮다" if mean < TARGET_LO else "높다"
        print(f"\n  ❌ 안 맞는다 — 목표보다 {side}")
        print("     진입 SoC(mu)를 보정해야 한다. 보정하면 **기준선 재고정**이 따라온다.")
        print("     ⚠ 보정 전에 통행거리부터 본다 — 통행이 틀렸는데 SoC 를 맞추면")
        print("        두 오류가 서로를 가린다 (#82 B).")
        rc = 1

    print(f"\n  참고: 충전 종료 SoC 평균 {ce['soc_out'].mean():.1%} "
          f"(목표 85%, 우리 상한 {cfg.vehicles.target_soc_cap:.0%})")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
