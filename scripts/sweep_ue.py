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
import run_ue  # noqa: E402  (같은 scripts/ 폴더)
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.ue import UENotConverged  # noqa: E402
from evdt.io.db import get_conn  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402

COLUMNS = {
    "n_ev_charging": "충전필요",
    "ue_iterations": "반복",
    "ue_final_gap": "gap",
    "wait_mean_min": "평균대기",
    "wait_p95_min": "P95대기",
    "wait_worst_station_min": "최악휴게소",
    "share_ratio_max": "몰림비",
    "bottleneck_slots": "병목슬롯",
}


def _status(db, run_id: str) -> str | None:
    with get_conn(db, readonly=True) as conn:
        row = conn.execute("SELECT status FROM run WHERE run_id = ?", (run_id,)).fetchone()
    return None if row is None else row[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--soc", nargs="+", default=["low", "high"], help="출발 SoC 프로파일 이름들")
    ap.add_argument("--dm", nargs="+", type=float, default=[1.0, 2.0, 3.0], help="수요 배율들")
    ap.add_argument("--seeds", nargs="+", type=int, default=None, help="기본: config 의 시드 하나")
    ap.add_argument("--overwrite", action="store_true", help="이미 있는 칸도 다시 돌린다")
    args = ap.parse_args()

    db = default_db_path()
    cells: list[dict] = []

    for soc in args.soc:
        for dm in args.dm:
            cfg = run_ue.load_config(args.config, soc=soc, demand_multiplier=dm)
            for seed in args.seeds or [cfg.vehicles.seed]:
                run_id = make_run_id(cfg.scenario_id, cfg.policy.stage, seed, cfg.policy.participation)
                cell = {"soc": soc, "dm": dm, "seed": seed, "run_id": run_id}
                cells.append(cell)

                if _status(db, run_id) == "DONE" and not args.overwrite:
                    print(f"\n[건너뜀] {run_id} (이미 DONE)")
                    continue

                try:
                    run_ue.run(cfg, seed=seed, overwrite=True)
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

    for col in ("평균대기", "P95대기", "최악휴게소"):
        table[col] = table[col].round(1)
    table["gap"] = (table["gap"] * 100).round(2).astype(str) + "%"
    table["몰림비"] = table["몰림비"].round(2)

    print("\n=== 출발 SoC × 수요 배율 ===")
    print(table.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
