"""기준선을 못 박고, 바뀌었는지 1분 안에 확인한다 (#54).

    python scripts/baseline.py --check      # 돌려서 못 박아 둔 값과 비교
    python scripts/baseline.py --pin        # 지금 결과를 새 기준으로 못 박는다
    python scripts/baseline.py --check --seeds 1-5   # 빠르게 (기본 20개)

## 왜 필요한가

기준선이 근거 없는 잠정값 여러 개(EV 비중 · 이탈 비용 · 출발 SoC · 기온) 위에 서
있다. 하나를 건드리면 전부 다시 돌려야 하고, 무엇이 얼마나 움직였는지 눈으로
비교하다 보면 20분이 든다. 실제로 하루에 세 번 그랬다.

못 박아 둔 값과의 **차이만** 보여주면 그게 1분이 된다.

    평균 대기 (분)      20.5 →  20.5   (  +0.0%)
    n_escaped        1,142 → 1,203   (  +5.3%)  ← 여기가 움직였다

## 무엇을 못 박나

`reference/baseline_<방향>.csv` — 시드 20개 KPI 평균. 저장소에 커밋한다.
**결과 파일이 아니라 계약이다** — 여기가 움직이면 무언가 바뀐 것이고, 그게
의도한 것인지 답할 수 있어야 한다.

## 주의

**기준선이 움직였다고 반드시 나쁜 것은 아니다.** 버그를 고치면 움직인다.
이 도구는 "움직였다/안 움직였다" 만 말한다. 옳고 그름은 사람이 판단한다.
"""

from __future__ import annotations

import argparse

import pandas as pd
import yaml
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.paths import CONFIG_DIR  # noqa: E402
from evdt.runner import parse_seeds, run_experiment  # noqa: E402

REFERENCE = ROOT / "reference"

#: 기준선에 쓰는 설정. **가정 레이어는 얹지 않는다** — 기준선은 재현 그 자체다.
BASELINE = {"departure_soc": "low"}

#: 차이가 이보다 크면 표시한다. 시드 20개의 신뢰구간 폭 정도로 잡았다
MOVED_PCT = 3.0


def run_one(direction: str, seeds) -> pd.DataFrame:
    raw = yaml.safe_load(
        (CONFIG_DIR / f"scenario_seollal_{direction}.yaml").read_text(encoding="utf-8"))
    raw["demand"] = {**raw["demand"], "layers": []}
    raw["vehicles"] = {**raw["vehicles"], **BASELINE}
    cfg = ScenarioConfig.from_dict(raw, source="baseline").variant("replay", {})

    print(f"\n[{direction.upper()}] {cfg.demand_label}")
    result = run_experiment(cfg, seeds, reuse_done=False, min_seeds=1,
                            log=lambda *a: None, heatmaps=())

    return pd.read_csv(result.out_dir / "kpi_ci.csv")[["metric", "label", "mean"]]


def compare(now: pd.DataFrame, pinned: pd.DataFrame) -> bool:
    both = pinned.merge(now, on="metric", suffixes=("_pin", "_now"), how="outer")
    moved = False

    for r in both.itertuples():
        label = r.label_now if isinstance(r.label_now, str) else r.label_pin
        if pd.isna(r.mean_pin):
            print(f"  {label:<28} {'—':>12} → {r.mean_now:>12,.1f}   (새 지표)")
            continue
        if pd.isna(r.mean_now):
            print(f"  {label:<28} {r.mean_pin:>12,.1f} → {'—':>12}   (사라짐)")
            moved = True
            continue

        pct = (r.mean_now - r.mean_pin) / r.mean_pin * 100 if r.mean_pin else 0.0
        mark = "  ← 움직였다" if abs(pct) >= MOVED_PCT else ""
        moved = moved or bool(mark)
        print(f"  {label:<28} {r.mean_pin:>12,.1f} → {r.mean_now:>12,.1f}   ({pct:>+6.1f}%){mark}")

    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description="기준선 고정과 비교")
    ap.add_argument("--check", action="store_true", help="돌려서 못 박아 둔 값과 비교")
    ap.add_argument("--pin", action="store_true", help="지금 결과를 새 기준으로 못 박는다")
    ap.add_argument("--seeds", default="1-20")
    ap.add_argument("--direction", default=None, choices=("down", "up"))
    args = ap.parse_args()

    if not (args.check or args.pin):
        ap.error("--check 또는 --pin 중 하나가 필요합니다")

    seeds = parse_seeds(args.seeds)
    directions = [args.direction] if args.direction else ["down", "up"]
    REFERENCE.mkdir(exist_ok=True)
    any_moved = False

    for direction in directions:
        now = run_one(direction, seeds)
        path = REFERENCE / f"baseline_{direction}.csv"

        if args.pin:
            now.to_csv(path, index=False)
            print(f"  못 박음: {path.relative_to(ROOT)} ({len(now)}개 지표, 시드 {len(seeds)}개)")
            continue

        if not path.is_file():
            print(f"  못 박아 둔 값이 없습니다: {path.relative_to(ROOT)}\n"
                  "  먼저:  python scripts/baseline.py --pin")
            return 1

        any_moved |= compare(now, pd.read_csv(path))

    if args.check:
        print("\n기준선이 움직였습니다. 의도한 변화인지 확인하세요." if any_moved
              else "\n기준선 그대로입니다.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
