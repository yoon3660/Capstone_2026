"""시나리오를 여러 시드로 반복 실행하고, KPI 신뢰구간과 시공간 히트맵을 뽑는다 (이슈 #30).

    python scripts/run_experiment.py --seeds 1-20
    python scripts/run_experiment.py --seeds 1-20 --stage S0      # 같은 세계, 다른 정책 (#59)
    python scripts/run_experiment.py --seeds 1-20 --soc high --demand-multiplier 3
    python scripts/run_experiment.py --seeds 1-30 --fresh          # DONE 인 run 도 다시 돌린다

단일 시드 결과는 우연일 수 있다. 시드마다 EV 집합(도착 시각·차종·SoC·목적지)이 달라지고,
그 차이가 KPI 를 얼마나 흔드는지를 신뢰구간으로 본다. "A 가 B 보다 낫다" 는 두 구간이
겹치지 않을 때 말한다.

동작
    1. 시드마다 run 을 등록하고 돌린다. run_id 는 make_run_id() 그대로
       (<scenario>__UE__p100__s0007). 이미 DONE 인 run 은 재사용한다 (--fresh 로 끔).
    2. 하나라도 실패하면 **그 자리에서 멈추고 결과를 내지 않는다.** 실패한 run 은 FAILED 로 남는다.
    3. 전부 성공하면 runs/experiments/<experiment_id>/ 에
         kpi_ci.csv · kpi_ci.md   KPI 별 평균 · 표준편차 · 95% 신뢰구간 (t 분포)
         heatmap_wait.png          가로 = 시각, 세로 = 기점거리 offset_km, 색 = 실제 대기 (시드 합산)
         heatmap_queue.png         같은 축, 색 = 큐 길이 (스냅샷)
         manifest.json             시드 · run_id 목록 · config 해시

시드는 20개 이상이어야 한다 (--min-seeds 로 낮출 수 있지만 발표 수치에는 쓰지 말 것).
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import ROOT  # noqa: E402,F401

from evdt.runner import (  # noqa: E402
    MIN_SEEDS,
    RUNNABLE_STAGES,
    ExperimentFailed,
    load_config,
    parse_seeds,
    run_experiment,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seeds", required=True, help="시드 목록: 1-20 / 1,2,5 / 1-10,20")
    ap.add_argument("--soc", default=None, help="출발 SoC 프로파일 (low | high)")
    ap.add_argument("--demand-multiplier", type=float, default=None)
    ap.add_argument("--stage", default=None, choices=RUNNABLE_STAGES,
                    help="정책 단계 (기본: config 의 policy.stage). scenario_id 는 안 바뀌고 "
                         "run_id 에만 들어가서, UE 와 나란히 놓고 볼 수 있다")
    ap.add_argument("--fresh", action="store_true", help="이미 DONE 인 run 도 다시 돌린다 (코드를 고친 뒤)")
    ap.add_argument("--min-seeds", type=int, default=MIN_SEEDS, help=f"최소 시드 수 (기본 {MIN_SEEDS})")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, soc=args.soc, demand_multiplier=args.demand_multiplier,
                      stage=args.stage)
    seeds = parse_seeds(args.seeds)

    print(f"{cfg.scenario_id} · {cfg.policy.stage} · 시드 {len(seeds)}개")

    try:
        result = run_experiment(cfg, seeds, reuse_done=not args.fresh, min_seeds=args.min_seeds)
    except ExperimentFailed as exc:
        print(f"\n[멈춤] {exc}", file=sys.stderr)
        return 1

    print(f"\n=== {result.experiment_id} ===")
    print((result.out_dir / "kpi_ci.md").read_text(encoding="utf-8"))
    print(f"결과 폴더: {result.out_dir}")
    for p in result.heatmaps:
        print(f"  히트맵: {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
