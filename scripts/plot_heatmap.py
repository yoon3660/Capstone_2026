"""휴게소 × 시간대 평균 대기 히트맵. run 을 여러 개 주면 같은 색 구간으로 나란히 그린다.

    python scripts/plot_heatmap.py                                   # 가장 최근 UE run 하나
    python scripts/plot_heatmap.py --run seollal_2026_down_base__soc-low__dm1__UE__p100__s0007 \
                                   --run seollal_2026_down_base__soc-high__dm3__UE__p100__s0007
    python scripts/plot_heatmap.py --run <run_id> --out figures/heatmap.png

기본 저장 위치: runs/<첫 run_id>/wait_heatmap.png
값은 src/evdt/sql/station_hourly_wait.sql 한 줄에서 나온다 (도착 시각 기준, 실제로 기다린 시간).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import ROOT  # noqa: E402

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.event_log import load_sql  # noqa: E402
from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.viz.plots import plot_wait_heatmap  # noqa: E402


def _label(conn, run_id: str) -> str:
    row = conn.execute("SELECT params_json FROM run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"run 이 DB 에 없다: {run_id}")
    import json

    p = json.loads(row[0] or "{}")
    if "departure_soc" in p:
        return f"출발 SoC {p['departure_soc']} (평균 {p['departure_soc_mean']:.0%}) · 수요 ×{p['demand_multiplier']:g}"
    return run_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", default=[], help="run_id (여러 번 주면 나란히)")
    ap.add_argument("--out", default=None, help="저장 경로 (기본: runs/<첫 run_id>/wait_heatmap.png)")
    args = ap.parse_args()

    db = default_db_path()

    with get_conn(db, readonly=True) as conn:
        run_ids = args.run or [r[0] for r in conn.execute(
            "SELECT run_id FROM run WHERE stage = 'UE' AND status = 'DONE' ORDER BY finished_at DESC LIMIT 1"
        )]
        if not run_ids:
            raise SystemExit("DONE 인 UE run 이 없다. 먼저 실행할 것:  python scripts/run_ue.py")
        panels = [(r, _label(conn, r)) for r in run_ids]
        corridor = conn.execute(
            "SELECT s.corridor_id FROM run r JOIN scenario s USING (scenario_id) WHERE r.run_id = ?",
            (run_ids[0],),
        ).fetchone()[0]

    con = duck_connect(db_path=db, run_ids=run_ids)
    hourly = con.sql(load_sql("station_hourly_wait")).df()
    stations = con.sql(
        f"""SELECT station_id, name, offset_km FROM station
            WHERE corridor_id = '{corridor}'
              AND station_id IN (SELECT station_id FROM charger WHERE is_active = 1)"""  # noqa: S608
    ).df()

    out = Path(args.out) if args.out else ROOT / "runs" / run_ids[0] / "wait_heatmap.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    plot_wait_heatmap(hourly, stations, panels, out, title="휴게소 × 시간대 평균 대기 (UE 균형)")
    print(f"저장: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
