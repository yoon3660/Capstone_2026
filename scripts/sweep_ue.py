"""출발 SoC × 수요 배율 격자로 UE 를 돌리고 한 표로 비교한다 (RQ1: 쏠림이 어느 수요 구간에서 나타나나).

    python scripts/sweep_ue.py                                  # low·high × 1, 2, 3 (시드 7)
    python scripts/sweep_ue.py --soc high --dm 1 1.5 2 2.5 3
    python scripts/sweep_ue.py --seeds 7 8 9 --overwrite

한 칸 = run 하나. scenario_id 가 <원래 id>__soc-<이름>__dm<배율> 이라서 칸끼리 덮어쓰지 않는다.
이미 DONE 인 칸은 건너뛴다 (--overwrite 로 다시 돌린다). 수렴하지 못한 칸은 FAILED 로
남기고 다음 칸으로 넘어간다.

표의 열 (docs/UE_equilibrium.md §7)
    충전필요    UE 에 들어온 차 (목적지까지 충전 없이 못 가는 차)
    반복·gap    수렴까지 반복 수와 마지막 상대 gap
    평균·P95    충전 정차의 대기 (분)
    최악휴게소   휴게소 평균 대기의 최댓값 (분)
    몰림비      수요몫 ÷ 충전기몫 의 최댓값. 1 이면 충전기만큼만 몰림
    병목슬롯    휴게소×시간 평균 대기 ≥ 30분 인 칸 수
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.demand_layers import effective_ev_share  # noqa: E402
from evdt.engine.ue import UENotConverged  # noqa: E402
from evdt.io.db import get_conn  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import load_config, reusable_run, run_once  # noqa: E402

OD_PATH = "data/processed/tcs_od_gyeongbu.parquet"

COLUMNS = {
    "n_ev": "진입EV",
    "n_ev_charging": "충전필요",
    "ue_iterations": "반복",
    "ue_final_gap": "gap",
    "wait_mean_min": "평균대기",
    "wait_p95_min": "P95대기",
    "wait_worst_station_min": "최악휴게소",
    "share_ratio_max": "몰림비",
    "bottleneck_slots": "병목슬롯",
}


def check_same_world(table: pd.DataFrame) -> None:
    """같은 수요 배율 칸끼리 **같은 차 집합**을 상대했는지 본다.

    출발 SoC 프로파일은 **세계가 아니라 차의 상태**를 바꾼다. 그래서 수요 배율이 같으면
    진입 EV 수가 프로파일·시드와 무관하게 같아야 한다 (`ev_count_method: fixed`).
    다르면 칸끼리 **다른 세계**를 비교하고 있는 것이고, 그 표는 아무 말도 할 수 없다.
    수요 배율과 EV 비중은 세계를 바꾸므로, **그 조합이 같은 칸끼리만** 본다.

    실제로 걸린 적이 있다 (#55). DB 에 남아 있던 **#54 이전 run**(진입 EV 8,361)이
    `--overwrite` 없이 "이미 DONE" 으로 건너뛰어져 두 칸만 옛 세계였다. 평균 대기
    신뢰구간이 [1, 83] 로 터져서야 알아챘다.

    같은 방어가 `compare_stages.py` 에는 있었는데 여기에는 없었다.
    """

    done = table[table["상태"] == "DONE"]
    if done.empty or "진입EV" not in done:
        return

    axes = [c for c in ("dm", "ev%") if c in done]
    bad = {key: sorted(g["진입EV"].dropna().unique())
           for key, g in done.groupby(axes) if g["진입EV"].nunique(dropna=True) > 1}
    if not bad:
        return

    def _name(key) -> str:
        vals = key if isinstance(key, tuple) else (key,)
        return " · ".join(f"{a}={v:g}" for a, v in zip(axes, vals, strict=True))

    lines = [f"  {_name(key)}: 진입 EV 가 {', '.join(f'{v:,.0f}' for v in vals)} 로 갈린다"
             for key, vals in bad.items()]
    raise SystemExit(
        "\n[중단] 같은 수요 배율인데 칸마다 진입 EV 가 다르다 — 다른 세계를 비교하려 했다.\n"
        + "\n".join(lines)
        + "\n\n  출발 SoC 는 차의 상태만 바꾼다. 진입 EV 가 달라지는 것은 **옛 코드로 돈 run** 이\n"
          "  DB 에 남아 건너뛰어졌다는 뜻이다. --overwrite 로 다시 돌려라."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--soc", nargs="+", default=["low", "high"], help="출발 SoC 프로파일 이름들")
    ap.add_argument("--dm", nargs="+", type=float, default=[1.0, 2.0, 3.0], help="수요 배율들")
    ap.add_argument("--ev-share", nargs="+", type=float, default=[None],
                    help="EV 비중들 (예: 0.05 0.10 0.15). 빼면 config 값 그대로")
    ap.add_argument("--od", action="store_true",
                    help="목적지를 실측 OD 에서 뽑는다 (#99). scenario_id 에 __od 가 붙는다")
    ap.add_argument("--seeds", nargs="+", type=int, default=None, help="기본: config 의 시드 하나")
    ap.add_argument("--overwrite", action="store_true", help="이미 있는 칸도 다시 돌린다")
    args = ap.parse_args()

    db = default_db_path()
    cells: list[dict] = []

    for soc in args.soc:
        for dm in args.dm:
            for ev in args.ev_share:
                cfg = load_config(args.config, soc=soc, demand_multiplier=dm, ev_share=ev,
                                  od=OD_PATH if args.od else None)
                for seed in args.seeds or [cfg.vehicles.seed]:
                    run_id = make_run_id(cfg.scenario_id, cfg.policy.stage, seed,
                                         cfg.policy.participation)
                    # ⚠ `cfg.demand.ev_share` 가 아니라 **레이어까지 반영한 값**이어야
                    # 한다. #54 이후 EV 비중은 `demand.ev_share` 를 덮어쓰지 않고
                    # ev_adoption 레이어로 얹히므로, 그 자리는 **항상 실측 5%** 다.
                    # 그걸 라벨로 쓰면 모든 칸이 ev%=5 로 묶이고, check_same_world 가
                    # 서로 다른 수요를 한 그룹으로 보고 오탐을 낸다 (#101)
                    cells.append({"soc": soc, "dm": dm,
                                  "ev%": effective_ev_share(cfg.demand.layers,
                                                            cfg.demand.ev_share) * 100,
                                  "seed": seed, "run_id": run_id})

                    ok, why = reusable_run(db, run_id, cfg)
                    if ok and not args.overwrite:
                        print(f"\n[건너뜀] {run_id} (이미 DONE)")
                        continue
                    if why.startswith("설정이 다르다"):
                        # 같은 run_id 로 **다른 세계**가 돌아 있었다. 진입 EV 가 같으면
                        # check_same_world 로는 안 걸린다 (#101)
                        print(f"\n[다시 돌림] {run_id}\n  {why}")

                    try:
                        run_once(cfg, seed=seed, overwrite=True)
                    except UENotConverged as exc:
                        print(f"\n[수렴 실패 → FAILED] {run_id}\n  {exc}")

    with get_conn(db, readonly=True) as conn:
        kpi = pd.read_sql_query(
            f"SELECT run_id, metric, value FROM run_kpi WHERE run_id IN ({','.join('?' * len(cells))})",  # noqa: S608
            conn, params=[c["run_id"] for c in cells],
        )
        status = dict(conn.execute(
            f"SELECT run_id, status FROM run WHERE run_id IN ({','.join('?' * len(cells))})",  # noqa: S608
            [c["run_id"] for c in cells],
        ).fetchall())

    wide = kpi.pivot(index="run_id", columns="metric", values="value") if not kpi.empty else pd.DataFrame()
    table = pd.DataFrame(cells).set_index("run_id").join(wide.reindex(columns=list(COLUMNS)))
    table["상태"] = [status.get(r, "-") for r in table.index]
    table = table.rename(columns=COLUMNS).reset_index(drop=True)

    # 표를 보여주기 **전에** 같은 세계인지 확인한다. 틀린 표는 안 내는 게 낫다
    check_same_world(table)

    for col in ("평균대기", "P95대기", "최악휴게소"):
        table[col] = table[col].round(1)
    table["gap"] = (table["gap"] * 100).round(2).astype(str) + "%"
    table["몰림비"] = table["몰림비"].round(2)

    print("\n=== 출발 SoC × 수요 배율 ===")
    print(table.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
