"""한 번 돌리기 — 진짜 휴게소·충전기·교통량으로. 스테이지는 config(또는 --stage)가 정한다.

    python scripts/build_demand_profile.py        # 처음 한 번 (수요 프로파일 CSV)
    python scripts/run_ue.py                      # config 의 스테이지·시드
    python scripts/run_ue.py --stage S0           # 같은 세계, 앱 화면만 보는 운전자 (#59)
    python scripts/run_ue.py --seed 3 --overwrite
    python scripts/run_ue.py --soc high                   # 출발 SoC 높음 (집에서 충전하고 출발)
    python scripts/run_ue.py --soc low --demand-multiplier 2
    python scripts/sweep_ue.py                            # SoC × 수요 배율 격자를 한 번에
    python scripts/run_experiment.py --seeds 1-20         # 시드 반복 + 신뢰구간 + 히트맵

    --soc / --demand-multiplier 를 주면 config 를 고치지 않고 그 값만 바꾼 실험이 된다.
    scenario_id 뒤에 __soc-high, __dm2 처럼 붙어서 서로 덮어쓰지 않는다.

    --stage 는 **scenario_id 를 바꾸지 않는다.** 세계가 아니라 정책만 바뀌기 때문이다.
    대신 run_id 에 들어간다: <scenario>__UE__p100__s0007 / <scenario>__S0__p100__s0007.

남는 것
    runs/<run_id>/charge_event.parquet   균형 상태의 충전 정차 (정차마다 한 행)
    runs/<run_id>/snapshot.parquet       휴게소 상태 시계열
    runs/<run_id>/solver_log.parquet     반복별 gap (iteration, rel_gap)
    runs/<run_id>/ue_gap.png             수렴 그래프
    run_kpi                              대기·체류·gap·쏠림 지표

수렴하지 못하면 gap 이력을 남긴 채 run 을 FAILED 로 기록하고 멈춘다.
실행 로직은 src/evdt/runner.py 에 있다 (테스트가 같은 코드를 부른다).
"""

from __future__ import annotations

import argparse

from _bootstrap import ROOT  # noqa: E402,F401

from evdt.runner import (  # noqa: E402
    BOTTLENECK_WAIT_MIN,
    RUNNABLE_STAGES,
    load_config,
    run_once,
    station_table,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seed", type=int, default=None, help="기본: config 의 vehicles.seed")
    ap.add_argument("--soc", default=None, help="출발 SoC 프로파일 (config 의 vehicles.soc_profiles 이름: low | high)")
    ap.add_argument("--demand-multiplier", type=float, default=None, help="수요 배율 (기본: config 값)")
    ap.add_argument("--stage", default=None, choices=RUNNABLE_STAGES,
                    help="정책 단계 (기본: config 의 policy.stage)")
    ap.add_argument("--overwrite", action="store_true", help="같은 run_id 가 있으면 덮어쓴다")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, soc=args.soc, demand_multiplier=args.demand_multiplier,
                      stage=args.stage)
    run_id = run_once(cfg, seed=args.seed, overwrite=args.overwrite)

    print("\n--- 휴게소별: 충전기 몫 대비 수요 몫, 대기 (쏠림) ---")
    print(station_table(run_id, cfg.corridor_id).to_string(index=False))
    print(f"\n병목 기준: 휴게소×시간 평균 대기 ≥ {BOTTLENECK_WAIT_MIN:.0f}분")
    if cfg.policy.stage == "UE":
        print(f"수렴 그래프: runs/{run_id}/ue_gap.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
