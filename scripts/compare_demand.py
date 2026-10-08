"""옛 수요 vs 실측 OD 수요를 **같은 시드로만** 비교한다 (#101).

    python scripts/compare_demand.py                      # 하행 재현, 시드 1-20
    python scripts/compare_demand.py --config config/scenario_seollal_up.yaml
    python scripts/compare_demand.py --seeds 1-20 --stage UE

## 이 비교는 다른 비교들과 성격이 다르다

`compare_stages3.py` 는 **같은 세계**에서 정책만 바꾼다. 여기는 반대다 — **세계가 바뀐다.**
목적지 분포가 달라지므로 **차량 집합 자체가 다르다.** 그래서 `check_same_world` 같은
방어가 여기서는 맞지 않는다. 대신 **무엇이 얼마나 달라졌는지를 맨 앞에 찍는다.**

## 맨 앞에 충전 필요 대수를 찍는 이유

**그 수가 나머지를 전부 움직인다.** 대기·병목·이탈은 모두 "충전기를 쓰러 온 차" 가
몇 대냐에 달려 있다. 그걸 안 보고 평균 대기만 비교하면 *"엔진이 좋아졌다"* 와
*"손님이 줄었다"* 를 구분할 수 없다.

#99 에서 한 시드로 본 변화는 하행 1,854 → 870대였다. 여기서 20시드로 확정한다.

## ⚠ 수렴한 시드만 쓴다

한쪽만 수렴한 시드를 섞으면 **다른 차 집합**을 비교하게 된다 (#59). 제외한 시드 수를
항상 같이 찍는다 — 조용히 빼면 표가 더 깨끗해 보이는 만큼 덜 정직해진다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.ue import UENotConverged  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    load_config,
    parse_seeds,
    run_once,
    stale_reason,
    summarize_kpis,
)

OD_PATH = "data/processed/tcs_od_gyeongbu.parquet"

#: 맨 위 둘이 **원인**이고 나머지는 결과다. 순서를 바꾸지 말 것.
SHOW = ["n_ev", "n_ev_charging",
        "wait_mean_min", "wait_p95_min", "wait_max_min", "wait_worst_station_min",
        "dwell_total_h", "bottleneck_slots", "share_ratio_max",
        "n_escaped", "n_escaped_balked", "ue_iterations"]


def reusable(conn, run_id: str, cfg) -> tuple[bool, str]:
    """이미 있는 run 을 그대로 써도 되나.

    ⚠ **DONE 인 것만으로는 부족하다.** `run_id` 는 (scenario, stage, 참여율, seed) 뿐이라
    같은 이름으로 다른 세계를 돌릴 수 있다. 실제로 이 스크립트를 처음 돌렸을 때
    `seollal_2026_down_base__UE__p100__s0001` 이 **9월에 `soc=low` 로 돌린 결과**
    (충전 필요 10,129대)를 내놨다 — #55 가 yaml 기본값을 holiday 로 바꾼 뒤였는데도.
    그대로 썼으면 **OD 효과에 진입 SoC 수정이 섞인 표**를 냈을 것이다.
    """

    row = conn.execute(
        "SELECT status, params_json FROM run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return False, "없음"
    if row[0] != "DONE":
        return False, str(row[0])

    why = stale_reason(json.loads(row[1] or "{}"), cfg)
    return (False, f"설정이 다르다 — {why}") if why else (True, "DONE")


def run_arm(path: str, od: str | None, seeds: list[int], stage: str,
            overwrite: bool) -> tuple[object, dict[int, str]]:
    """한쪽 수요를 시드마다 돌린다. 돌려주는 것은 (config, 시드→run_id)."""

    cfg = load_config(path, od=od, stage=stage)
    ids: dict[int, str] = {}
    db = default_db_path()
    warned = False

    for seed in seeds:
        run_id = make_run_id(cfg.scenario_id, cfg.policy.stage, seed, cfg.policy.participation)
        ids[seed] = run_id

        with sqlite3.connect(db) as conn:
            ok, why = reusable(conn, run_id, cfg)

        if ok and not overwrite:
            continue
        if why.startswith("설정이 다르다") and not warned:
            print(f"  ⚠ 기존 run 을 못 쓴다 ({why}).\n"
                  f"    같은 run_id 로 다른 세계가 돌아 있었다 — 다시 돌린다.")
            warned = True

        try:
            run_once(cfg, seed=seed, overwrite=True)
        except UENotConverged as exc:
            print(f"  [수렴 실패 → FAILED] seed {seed}\n    {exc}")

    return cfg, ids


def done_seeds(ids: dict[int, str]) -> set[int]:
    with sqlite3.connect(default_db_path()) as conn:
        rows = conn.execute(
            f"SELECT run_id FROM run WHERE status='DONE' AND run_id IN "  # noqa: S608
            f"({','.join('?' * len(ids))})", list(ids.values())).fetchall()
    done = {r[0] for r in rows}
    return {s for s, r in ids.items() if r in done}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seeds", default="1-20")
    ap.add_argument("--stage", default="UE")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    arms = {}

    for name, od in (("옛 수요", None), ("OD 수요", OD_PATH)):
        print(f"\n########## {name} ##########")
        arms[name] = run_arm(args.config, od, seeds, args.stage, args.overwrite)

    ok = {name: done_seeds(ids) for name, (_, ids) in arms.items()}
    shared = set.intersection(*ok.values())

    if not shared:
        raise SystemExit(
            "\n[중단] 양쪽이 모두 끝낸 시드가 없다.\n"
            + "\n".join(f"  {n}: {len(s)}개" for n, s in ok.items())
        )

    for name, s in ok.items():
        extra = sorted(s - shared)
        if extra:
            print(f"\n  [제외] {name} 에만 있는 시드 {len(extra)}개 — 공통 시드만 비교한다")

    cols, labels = {}, {}
    for name, (_cfg, ids) in arms.items():
        run_ids = [ids[s] for s in sorted(shared)]
        k = summarize_kpis(run_ids).set_index("metric")
        labels.update(k["label"].to_dict())
        cols[name] = k["mean"]

    table = pd.DataFrame(cols).reindex(SHOW)
    table["변화율"] = (table["OD 수요"] / table["옛 수요"].replace(0, float("nan")) - 1) * 100
    table.index = [labels.get(m, m) for m in SHOW]

    print(f"\n{arms['옛 수요'][0].scenario_id}  **공통 시드 {len(shared)}개**  stage={args.stage}")
    print(f"수요: {arms['옛 수요'][0].demand_label}\n")

    # ⚠ 원인을 먼저 찍는다 — 이 수가 나머지를 전부 움직인다
    need = pd.DataFrame(cols).loc["n_ev_charging"]
    print("=== 먼저: 충전 필요 대수 (나머지를 전부 움직이는 수) ===")
    print(f"  옛 수요 {need['옛 수요']:>9,.0f}대")
    print(f"  OD 수요 {need['OD 수요']:>9,.0f}대   "
          f"({need['OD 수요'] / need['옛 수요']:.2f}배)")
    print("\n  진입 EV 는 같아야 한다 — 바뀌면 수요 자체가 달라진 것이다:")
    ent = pd.DataFrame(cols).loc["n_ev"]
    same = abs(ent["OD 수요"] - ent["옛 수요"]) < 1.0
    print(f"    {ent['옛 수요']:,.0f} vs {ent['OD 수요']:,.0f}  "
          f"{'✅ 같다' if same else '⚠ 다르다 — 목적지만 바뀌어야 하는데 진입이 바뀌었다'}")

    print("\n=== 전체 KPI ===")
    print(table.to_string(float_format=lambda v: f"{v:,.1f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
