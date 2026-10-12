"""완전 예측 롤링 — 완전히 예측하면 얼마나 좋아지나 (벤치마크, #118 · DESIGN §8.2).

    python scripts/run_perfect_rolling.py --seeds 1-5
    python scripts/run_perfect_rolling.py --seeds 1 --rounds 4   # 고정점까지 반복

## ⚠ 이건 엔진 칸이 아니라 **벤치마크**다

시뮬레이터의 실제 미래를 넣으므로 **현실에서 구현할 수 없다.** 그래서 스테이지
이름도 `S2` 가 아니라 `PERFECT_ROLLING` 이다 — 사다리의 S2 는 관측 가능한 것만
쓰는 쪽이고(#119), 둘은 run_id 가 갈라져야 나란히 놓을 수 있다.

## 무엇을 재는가

**예측기를 만드는 것이 아니다.** *"미래를 완전히 알면 얼마나 좋아지나"* 를 먼저 잰다.
천장이 낮으면 예측기를 만들 이유가 없고, 그걸 **먼저 아는 것이 이 티켓의 성공**이다.

예측 대신 **직전 런에서 실제로 일어난 도착**을 넣는다. 설계문서 §8.2 의 "완전 예측
롤링" 이다.

## ⚠ 이건 상한이 아니라 상한 추정치다

고속도로 코리도에서 미래 도착은 **외생적이지 않다.** 어느 휴게소에 설지는 지금
고르려는 바로 그 값이다.

    "미래 도착" 을 알려면 미래의 결정을 알아야 하고
    미래의 결정을 하려면 "미래 도착" 을 알아야 한다

그래서 1회차 오라클이 보는 미래는 **S1 의 미래**다. S2 가 차를 다른 데로 보내면
실제 미래는 그와 달라진다.

`--rounds N` 이 이걸 좁힌다 — 직전 회차의 도착을 다음 회차의 오라클로 넣는다 (UE 의
최적반응과 같은 모양). `moved_share` 가 0 에 가까워지면 **자기 예측과 일치하는 해**다.
진동하면 그 사실 자체가 결과다: *"완전 예측이 이 문제에서 잘 정의되지 않는다."*

## 미리 정한 판정 기준 (결과를 보기 전에 정했다, #118)

    S1 대비 총 사회적 비용이 **5% 이상** 줄지 않으면 → S2 를 접고 S3 로 간다

5% 인 이유: #59 가 S0→S1 에서 평균 대기 **40.8%** 를 움직였다. 정보를 한 칸 더 줘서
그 1/8 도 못 움직인다면 남은 격차는 **정보가 아니라 결합**에 있다 — 그게 S3(윈도우 내
동시 결정) 의 전제다.

## 총 사회적 비용

    총비용 = 총 체류(h) + 이탈 차 수 × 이탈 비용

`compare_stages3.py` 와 **같은 정의**다. 이탈을 안 세면 "힘든 사람을 내보내면 이긴다"
가 된다.
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import (  # noqa: E402
    ArrivalSpec,
    OracleForecast,
    OracleSource,
    describe_shift,
)
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    load_config,
    parse_seeds,
    reusable_run,
    run_once,
    summarize_kpis,
)

#: #118 에 미리 적은 기준. **결과를 보고 고치지 않는다**
PASS_DROP_PCT = 5.0

#: moved_share 가 이 밑이면 고정점으로 본다
FIXED_POINT_SHARE = 0.01

ARRIVAL_COLS = ["ev_id", "station_id", "t_arrive_min", "soc_in", "soc_out", "cold_factor"]


def arrivals_of(run_id: str) -> list[dict]:
    """그 런에서 **실제로 일어난** 충전 도착. 이게 다음 회차의 미래가 된다."""

    path = RUNS_DIR / run_id / "charge_event.parquet"
    if not path.exists():
        raise SystemExit(f"\n[중단] 충전 기록이 없다: {path}")
    return pd.read_parquet(path)[ARRIVAL_COLS].to_dict("records")


def social_cost(run_id: str, escape_cost_min: float) -> dict[str, float]:
    s = summarize_kpis([run_id]).set_index("metric")["mean"]
    dwell = float(s.get("dwell_total_h", float("nan")))
    escaped = float(s.get("n_escaped", 0.0))
    return {
        "dwell_total_h": dwell,
        "n_escaped": escaped,
        "escape_h": escaped * escape_cost_min / 60.0,
        "total_h": dwell + escaped * escape_cost_min / 60.0,
        "wait_mean_min": float(s.get("wait_mean_min", float("nan"))),
        "n_ev_charging": float(s.get("n_ev_charging", float("nan"))),
    }


def ensure_s1(cfg_s1, seed: int, db, *, overwrite: bool) -> str:
    """1회차 오라클은 **같은 세계의 S1** 이다."""

    run_id = make_run_id(cfg_s1.scenario_id, "S1", seed, cfg_s1.policy.participation)
    ok, why = reusable_run(db, run_id, cfg_s1)
    if ok and not overwrite:
        print(f"  S1 재사용 — {run_id}")
        return run_id
    print(f"  S1 실행 ({why}) — {run_id}")
    return run_once(cfg_s1, seed=seed, overwrite=True, log=lambda *_: None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--rounds", type=int, default=1,
                    help="고정점 반복 회차. 1 이면 S1 의 미래만 쓴다")
    ap.add_argument("--rerun-s1", action="store_true")
    args = ap.parse_args()

    cfg_s1 = load_config(args.config, stage="S1")
    cfg_s2 = load_config(args.config, stage="PERFECT_ROLLING")
    seeds = parse_seeds(args.seeds)
    db = default_db_path()
    esc = float(cfg_s1.demand.escape_cost_min)

    print(f"\n{cfg_s1.scenario_id}  ·  시드 {seeds}  ·  회차 {args.rounds}")
    print(f"수요: {cfg_s1.demand_label}  ·  이탈 비용 {esc:.0f}분")
    print(f"\n미리 정한 기준: S1 대비 총 사회적 비용 **{PASS_DROP_PCT:.0f}% 이상** 감소\n")

    rows: list[dict] = []

    for seed in seeds:
        print(f"--- 시드 {seed} ---")
        s1_id = ensure_s1(cfg_s1, seed, db, overwrite=args.rerun_s1)
        base = social_cost(s1_id, esc)
        rows.append({"seed": seed, "stage": "S1", "round": 0, **base})

        source_id, source_rows, prev = s1_id, arrivals_of(s1_id), None

        for rnd in range(1, args.rounds + 1):
            oracle = OracleSource(rows=source_rows, source_run_id=source_id, round_no=rnd)
            # ⚠ run_id 는 어느 회차든 `__PERFECT_ROLLING__` 다 — 회차마다 덮어쓴다. 회차별 결과는
            #   이 표에 들고, 재사용 방지는 run 기록의 oracle_from_run 이 한다
            s2_id = run_once(cfg_s2, seed=seed, overwrite=True, oracle=oracle,
                             log=lambda *_: None)
            got = social_cost(s2_id, esc)

            drop = (1.0 - got["total_h"] / base["total_h"]) * 100.0
            line = {"seed": seed, "stage": "PR", "round": rnd,
                    "oracle_from": source_id, "drop_pct": drop, **got}

            now = arrivals_of(s2_id)
            if prev is not None:
                sh = describe_shift(
                    OracleForecast.from_rows(prev, _specs(prev), source_run_id="prev"),
                    OracleForecast.from_rows(now, _specs(now), source_run_id="now"))
                line["moved_share"] = sh["moved_share"]
                line["shift_min"] = sh["mean_abs_shift_min"]

            rows.append(line)
            print(f"  {rnd}회차  총비용 {got['total_h']:,.1f}h  "
                  f"(S1 {base['total_h']:,.1f}h, {drop:+.1f}%)"
                  + (f"  배정변화 {line['moved_share']:.1%}" if "moved_share" in line else ""))

            # 다음 회차는 이번 회차의 미래를 본다
            source_id, source_rows, prev = s2_id, now, now

    table = pd.DataFrame(rows)
    print("\n=== 총 사회적 비용 ===")
    show = ["seed", "stage", "round", "dwell_total_h", "n_escaped", "escape_h",
            "total_h", "drop_pct", "wait_mean_min"]
    print(table.reindex(columns=[c for c in show if c in table])
          .to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    # 같은 세계인지 — 다르면 비교가 성립하지 않는다 (#59).
    #
    # ⚠ **시드 안에서** 본다. 시드가 다르면 EV 집합이 다른 것이 정상이다 — 전체를
    #   한 번에 보면 시드를 늘릴 때마다 오경보가 난다 (처음에 그렇게 썼다).
    #   여기서 잡아야 하는 것은 "같은 시드인데 stage 마다 차가 다르다" 뿐이다
    bad = {s: g["n_ev_charging"].unique().tolist()
           for s, g in table.groupby("seed") if g["n_ev_charging"].nunique() > 1}
    if bad:
        raise SystemExit(
            f"\n[중단] 같은 시드인데 단계마다 충전 필요 EV 가 다르다: {bad}\n"
            "  stage 는 세계를 바꾸지 않는다. 다르면 다른 수요를 비교하는 것이다.")

    _verdict(table, args.rounds)
    return 0


def _specs(rows: list[dict]) -> dict:
    """`describe_shift` 는 휴게소와 도착 시각만 본다 — 물리는 더미여도 된다."""

    dummy = ArrivalSpec(60.0, 100.0, ())
    return {str(r["ev_id"]): dummy for r in rows}


def _verdict(table: pd.DataFrame, rounds: int) -> None:
    last = table[(table["stage"] == "PR") & (table["round"] == rounds)]
    drop = float(last["drop_pct"].mean())

    print("\n  판정 (기준은 결과 보기 전에 정했다, #118)")
    print(f"    총 사회적 비용 감소 {drop:+.1f}%   기준 ≥ {PASS_DROP_PCT:.0f}%   "
          f"{'통과' if drop >= PASS_DROP_PCT else '미달'}")

    if "moved_share" in last and last["moved_share"].notna().any():
        mv = float(last["moved_share"].mean())
        fixed = mv < FIXED_POINT_SHARE
        print(f"    마지막 회차 배정 변화 {mv:.1%}   "
              f"{'고정점 — 자기 예측과 일치하는 해다' if fixed else '아직 안 가라앉았다'}")
        if not fixed:
            print("      ⚠ 수렴하지 않았으면 이 수치는 **완전 예측의 값이 아니다.** "
                  "--rounds 를 늘려 보고, 두 값 사이를 오가면 진동이다 (#55·#78·#112)")
    elif rounds == 1:
        print("    ⚠ 1회차만 돌렸다 — 이 미래는 **S1 의 미래**다. 상한이 아니라 "
              "상한 추정치다. --rounds 2 이상으로 고정점을 확인할 것")

    if drop < PASS_DROP_PCT:
        print(f"\n  ⇒ **S2 를 접고 S3 로 간다.** 완전히 알아도 {drop:+.1f}% 라면, 남은 "
              "격차는 정보가 아니라 **결합**에 있다 — 그게 S3(윈도우 내 동시 결정)의 "
              "전제다. 천장이 낮다는 것을 먼저 안 것이 이 티켓의 성과다")
    else:
        print(f"\n  ⇒ **예측에 값이 있다 ({drop:.1f}%).** 관측 가능한 것만으로 "
              "예측하는 일로 넘어간다 (#119). 그쪽 결과는 이 수치를 천장으로 읽는다")


if __name__ == "__main__":
    raise SystemExit(main())
