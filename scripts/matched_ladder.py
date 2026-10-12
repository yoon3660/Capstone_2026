"""이탈을 맞춘 사다리 비교 (#123).

    python scripts/matched_ladder.py --seeds 1-7

## 왜 "이탈을 맞춰야" 하나

사다리의 모든 칸이 같은 통로를 하나 공유한다 — **예측을 비관적으로 만들면 한계
차량이 코리도를 나간다.** 나간 차는 충전기를 안 쓰므로 남은 사람의 줄이 짧아진다.

이 프로젝트에서 **네 번** 이 통로에 속을 뻔했다.

    S0            294대를 내보내고 S1 보다 좋아 보였다 (docs/ladder.md §3)
    #119 str 2    847대. 총비용이 제일 낮았지만 EV 의 12% 를 내보낸 값이다
    #123 흐림     404대. 대표 수요가 2.4배라 줄을 과대추정했다
    #119 str 0.5   78대. **이것 때문에 "관측 예측이 완전 예측을 이긴다" 가 나왔다**

마지막 것이 발표 문장까지 갔다가 여기서 잡혔다. 그래서 **비교는 이탈을 맞추고
한다** 를 규칙으로 만들고, 그 규칙을 코드로 강제한다.

## ⚠ 저장된 런을 그냥 믿지 않는다

`run_id` 는 (시나리오·stage·참여율·시드) 뿐이라 **같은 이름으로 다른 실험을 돌릴 수
있다.** 실제로 그랬다 — 흐린 오라클이 날것 오라클을 `__PERFECT_ROLLING__` 에
덮어썼고, 파케이만 읽은 분석이 그걸 날것인 줄 알고 표를 만들었다 (#123).

그래서 이 스크립트는 **`run` 표의 `params_json` 을 먼저 확인하고**, 기대와 다르면
멈춘다. #101 에서 만든 `stale_reason` 과 같은 방어다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec, OracleSource  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds, run_once, summarize_kpis  # noqa: E402

ARRIVAL_COLS = ["ev_id", "station_id", "t_arrive_min", "soc_in", "soc_out", "cold_factor"]

#: 이탈을 S1 근처로 맞추는 값 (#123 에서 쓸어서 찾았다)
OBSERVED_STRENGTH = 0.30

#: 이탈이 이보다 더 벌어지면 비교를 믿지 않는다
ESCAPE_TOL = 0.35


def params_of(run_id: str) -> dict:
    with sqlite3.connect(default_db_path()) as conn:
        row = conn.execute("SELECT params_json FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row[0] or "{}") if row else {}


def check(run_id: str, **want) -> None:
    """저장된 런이 **우리가 생각하는 그 실험**인지 확인한다.

    파케이만 읽으면 이 검사가 건너뛰어진다 — 그래서 흐린 오라클을 날것인 줄 알고
    표를 만든 적이 있다 (#123).
    """

    got = params_of(run_id)
    bad = {k: (got.get(k), v) for k, v in want.items() if got.get(k) != v}
    if bad:
        raise SystemExit(
            f"\n[중단] {run_id} 가 기대한 실험이 아니다:\n  "
            + "\n  ".join(f"{k}: 기록 {a!r} ≠ 기대 {b!r}" for k, (a, b) in bad.items())
            + "\n\n  같은 run_id 에 다른 실험이 덮어써 있다. 다시 돌릴 것.")


def stats(run_id: str) -> dict[str, float]:
    k = summarize_kpis([run_id]).set_index("metric")["mean"]
    d = pd.read_parquet(RUNS_DIR / run_id / "charge_event.parquet")
    return {"총대기h": float(d["wait_min"].sum()) / 60.0,
            "평균대기": float(k["wait_mean_min"]), "p95": float(k["wait_p95_min"]),
            "병목": float(k["bottleneck_slots"]), "이탈": float(k["n_escaped"]),
            "체류h": float(k["dwell_total_h"]), "충전EV": float(k["n_ev_charging"])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-7")
    ap.add_argument("--strength", type=float, default=OBSERVED_STRENGTH)
    args = ap.parse_args()

    cfg = {st: load_config(args.config, stage=st)
           for st in ("UE", "S0", "S1", "S2", "PERFECT_ROLLING")}
    sc = cfg["S1"].scenario_id
    esc_cost = float(cfg["S1"].demand.escape_cost_min)
    seeds = parse_seeds(args.seeds)

    print(f"\n{sc}  ·  시드 {seeds}  ·  관측 strength {args.strength:g}")
    print("  (이탈을 S1 근처로 맞춘 지점에서 비교한다 — 맞추지 않으면 "
          "'내보내서 이긴 것' 을 읽게 된다)\n")

    rows, used = [], []
    for seed in seeds:
        ids = {st: make_run_id(sc, st, seed, cfg[st].policy.participation) for st in cfg}
        if not all((RUNS_DIR / ids[st] / "charge_event.parquet").exists()
                   for st in ("UE", "S0", "S1")):
            print(f"  시드 {seed} — UE·S0·S1 이 다 없다. 건너뛴다")
            continue

        # 완전예측(날것) — **흐림이 아님을 보장하려고 매번 다시 돌린다**
        src = pd.read_parquet(RUNS_DIR / ids["S1"] / "charge_event.parquet")[
            ARRIVAL_COLS].to_dict("records")
        for rnd in (1, 2):
            run_once(cfg["PERFECT_ROLLING"], seed=seed, overwrite=True, log=lambda *_: None,
                     oracle=OracleSource(rows=src, source_run_id=ids["S1"], round_no=rnd))
            src = pd.read_parquet(RUNS_DIR / ids["PERFECT_ROLLING"] /
                                  "charge_event.parquet")[ARRIVAL_COLS].to_dict("records")
        check(ids["PERFECT_ROLLING"], forecast_kind="oracle", oracle_blur=False, oracle_round=2)

        run_once(cfg["S2"], seed=seed, overwrite=True, log=lambda *_: None,
                 observed=ObservedSpec(strength=args.strength))
        check(ids["S2"], forecast_kind="observed", forecast_strength=args.strength)

        for st in cfg:
            rows.append({"seed": seed, "stage": st, **stats(ids[st])})
        used.append(seed)
        print(f"  시드 {seed} 완료")

    if not used:
        raise SystemExit("\n[중단] 쓸 수 있는 시드가 없다.")

    t = pd.DataFrame(rows)
    bad = {s: g["충전EV"].unique().tolist()
           for s, g in t.groupby("seed") if g["충전EV"].nunique() > 1}
    if bad:
        raise SystemExit(f"\n[중단] 같은 시드인데 단계마다 충전 필요 EV 가 다르다: {bad}")

    m = t.groupby("stage")[["총대기h", "평균대기", "p95", "병목", "이탈", "체류h"]].mean()
    m = m.reindex(["UE", "S0", "S1", "S2", "PERFECT_ROLLING"])
    m["총비용h"] = m["체류h"] + m["이탈"] * esc_cost / 60.0
    floor, ceil = m.loc["S0", "총비용h"], m.loc["UE", "총비용h"]
    m["메운%"] = (floor - m["총비용h"]) / (floor - ceil) * 100.0

    print(f"\n=== 사다리 (공통 시드 {len(used)}개 {used}) ===")
    print(m[["총대기h", "평균대기", "p95", "병목", "이탈", "총비용h", "메운%"]]
          .to_string(float_format=lambda v: f"{v:,.1f}"))

    # 이탈이 정말 맞았나 — 아니면 위 표를 믿으면 안 된다
    base = m.loc["S1", "이탈"]
    off = {st: m.loc[st, "이탈"] / base - 1.0 for st in ("S2", "PERFECT_ROLLING")}
    worst = max(abs(v) for v in off.values())
    print("\n  이탈 맞춤 확인 (S1 기준)")
    for st, v in off.items():
        print(f"    {st:16} {m.loc[st, '이탈']:5.1f}대  ({v:+.0%})")
    if worst > ESCAPE_TOL:
        print(f"\n  ⚠ **{worst:.0%} 벌어졌다 — 위 표의 '메운%' 를 믿지 말 것.**")
        print("     --strength 를 조정해 이탈을 맞추고 다시 돌린다.")
    else:
        print(f"\n  ⇒ 최대 {worst:.0%} 차이. 이 비교는 **이탈 통로가 아니라 "
              "정보의 차이**를 잰다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
