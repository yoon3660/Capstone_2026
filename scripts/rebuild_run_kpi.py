"""저장된 run 폴더에서 `run_kpi` 를 **다시 계산해 채운다**.

    python scripts/rebuild_run_kpi.py --check          # 계산만 하고 대조
    python scripts/rebuild_run_kpi.py --write          # DB 에 쓴다
    python scripts/rebuild_run_kpi.py --write --only chuseok_2026_up_congested

## 왜 필요한가

2026-11-13, `migrate_stage_check` 를 `init_db` 를 거치지 않고 직접 불렀다.
`run` 을 다시 만드는 함수인데 **외래키가 켜진 채**였고, `run_kpi` 가
`ON DELETE CASCADE` 로 매달려 있어서 **884개 run 의 KPI 가 통째로 지워졌다.**

함수의 docstring 에 그 위험이 적혀 있었지만 **막지는 않았다.** 지금은 막는다
(`db.migrate_stage_check` 가 외래키가 켜져 있으면 거절한다).

다행히 잃은 것은 **집계뿐**이다. 원자료(`charge_event.parquet`,
`escape_event.parquet`, `snapshot.parquet`)는 그대로이고, KPI 는 전부 거기서
나온다. 그래서 다시 계산할 수 있다.

## ⚠ 다시 계산한 값은 **검산해야 한다**

`run_once` 안에서 메모리의 객체로 세던 것을 여기서는 디스크의 parquet 로
센다. 같아야 하지만 "같아야 한다" 는 검사가 아니다. 그래서 이미 내보내 둔
CSV(`docs/handoff/ladder_*/ladder_seeds.csv`, 커밋돼 있다)와 **전 항목을
대조한다.** 하나라도 어긋나면 멈춘다.

## 다시 계산하지 않는 것

  · `n_ev`            수요 생성 전체 대수. 충전 안 하는 차까지라 parquet 에 없다
  · UE 전용 KPI       gap·반복 수 등. solver_log 에 있지만 이 스크립트 밖이다
  · 집중도 KPI        `bottleneck_slots` · `share_ratio_max` ·
                      `wait_worst_station_min` · **`n_escaped_stranded`** —
                      `runner._record_concentration_kpis` 를 그대로 부른다
                      (같은 코드를 두 벌 두지 않는다)

    ★ `n_escaped_stranded` 를 여기서 세려다 검산에 걸렸다. `reason == "stranded"`
      가 아니다 — 이탈 사유는 `balked`/`reneged`/`no_plan` 셋이고, stranded 는
      **`no_plan` 중 이미 한 번 충전한 차**다 (정책이 몰아넣은 몫). 그 구분은
      DuckDB 질의로 하고, 그 함수에만 있다.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import _record_concentration_kpis  # noqa: E402

#: parquet 에서 나오는 KPI. (이름, 단위)
UNITS = {
    "n_ev_charging": "count", "n_charge_visits": "count",
    "n_escaped": "count", "n_escaped_balked": "count",
    "n_escaped_reneged": "count",
    "wait_mean_min": "min", "wait_p95_min": "min", "wait_max_min": "min",
    "dwell_total_h": "h", "renege_wait_total_h": "h", "escape_cost_total_h": "h",
}


def _col_sum(frame: pd.DataFrame, column: str) -> float:
    """없는 칸은 0. 옛 run 과 새 run 을 같은 코드로 읽는다."""

    if column not in frame.columns or not len(frame):
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0).sum())


def kpis_from_run(run_dir) -> dict[str, float] | None:
    ce = run_dir / "charge_event.parquet"
    if not ce.exists():
        return None
    c = pd.read_parquet(ce)
    ee = run_dir / "escape_event.parquet"
    e = pd.read_parquet(ee) if ee.exists() else pd.DataFrame(
        columns=["ev_id", "reason", "waited_min", "escape_cost_min"])

    waits = c["wait_min"].to_numpy(dtype=float) if len(c) else np.zeros(1)
    reason = e["reason"] if "reason" in e else pd.Series(dtype=str)
    # 충전이 필요했던 차 = 충전한 차 ∪ 이탈한 차 (한 대가 여러 번 설 수 있다)
    need = set(c["ev_id"].astype(str)) | set(e["ev_id"].astype(str)) if len(e) else \
        set(c["ev_id"].astype(str))

    return {
        "n_ev_charging": float(len(need)),
        "n_charge_visits": float(len(c)),
        "n_escaped": float(len(e)),
        "n_escaped_balked": float((reason == "balked").sum()),
        "n_escaped_reneged": float((reason == "reneged").sum()),
        "wait_mean_min": float(waits.mean()),
        "wait_p95_min": float(np.percentile(waits, 95)),
        "wait_max_min": float(waits.max()),
        "dwell_total_h": float(c["dwell_min"].sum()) / 60.0 if len(c) else 0.0,
        # ⚠ 옛 run 에는 이 칸이 없다 (#147 이전). 없으면 0 이 맞다 — 그때는
        #   포기 기제 자체가 없었고 이탈 비용도 KPI 로 안 적었다
        "renege_wait_total_h": _col_sum(e, "waited_min") / 60.0,
        "escape_cost_total_h": _col_sum(e, "escape_cost_min") / 60.0,
    }


def truth_tables() -> pd.DataFrame:
    """검산의 기준 = `runs/sweeps/ladder_*.csv`.

    ★ **내보낸 `docs/handoff/*` 를 기준으로 쓰면 안 된다.** 그건 저장된 run 에서
      다시 뽑은 **2차 산물**이고, 뽑은 시점에 run 이 오염돼 있었으면 그 오류를
      그대로 담는다. 실제로 그랬다 — `seollal_2026_up_adoption__S0__p100__s0001`
      이 테스트에 덮인 상태(포기 꺼짐: 체류 6,011.7 · 포기 전 대기 0)로
      내보내졌고, 그 뒤 run 은 고쳤지만 내보내기는 다시 하지 않았다.

      `runs/sweeps/ladder_*.csv` 는 **사다리 실행이 그 자리에서 쓴 1차 기록**이다.
    """

    frames = []
    for p in sorted((ROOT / "runs" / "sweeps").glob("ladder_*.csv")):
        f = pd.read_csv(p)
        if "run_id" in f.columns:
            frames.append(f)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(
        "run_id", keep="last")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="DB 에 쓴다")
    ap.add_argument("--only", default=None, help="run_id 에 이 문자열이 든 것만")
    ap.add_argument("--tol", type=float, default=1e-6)
    args = ap.parse_args()

    dirs = sorted(d for d in RUNS_DIR.iterdir()
                  if d.is_dir() and "__" in d.name
                  and (args.only is None or args.only in d.name))
    print(f"\nrun 폴더 {len(dirs)}개")

    built: dict[str, dict[str, float]] = {}
    for d in dirs:
        got = kpis_from_run(d)
        if got is not None:
            built[d.name] = got
    print(f"  KPI 를 다시 센 run {len(built)}개 "
          f"(충전 기록이 없는 {len(dirs) - len(built)}개는 건너뛴다)")

    # -- 검산 ----------------------------------------------------------------
    truth = truth_tables()
    if truth.empty:
        print("\n⚠ 검산할 CSV 가 없다 — runs/sweeps/ladder_*.csv")
        return 1

    cols = [c for c in UNITS if c in truth.columns]
    bad, checked = [], 0
    for r in truth.itertuples(index=False):
        got = built.get(r.run_id)
        if got is None:
            continue
        checked += 1
        for c in cols:
            want = float(getattr(r, c))
            if abs(got[c] - want) > max(args.tol, abs(want) * 1e-9):
                bad.append((r.run_id, c, want, got[c]))

    print(f"\n=== 검산 — 내보낸 CSV 와 대조 ({checked}개 run × {len(cols)}개 항목) ===")
    if bad:
        print(f"  ★ 어긋난 항목 {len(bad)}개:")
        for rid, c, want, got in bad[:12]:
            print(f"    {rid}  {c}: CSV {want:,.4f} ≠ 재계산 {got:,.4f}")
        print("\n  멈춘다 — 재계산이 원래 값과 다르면 DB 에 쓰면 안 된다")
        return 1
    print("  전 항목 일치")

    if not args.write:
        print("\n--write 가 없다 — 쓰지 않았다")
        return 0

    # -- 쓴다 ----------------------------------------------------------------
    db = default_db_path()
    with get_conn(db) as conn:
        known = {r[0] for r in conn.execute("SELECT run_id FROM run")}
        rows = [(rid, m, v, UNITS[m])
                for rid, kv in built.items() if rid in known
                for m, v in kv.items()]
        conn.executemany(
            "INSERT OR REPLACE INTO run_kpi (run_id, metric, value, unit) "
            "VALUES (?, ?, ?, ?)", rows)
    print(f"\n  parquet 기반 KPI {len(rows):,}줄 썼다 "
          f"(run 테이블에 없는 폴더는 건너뛴다)")

    # 집중도 KPI 는 runner 의 함수를 그대로 부른다
    done = 0
    for rid in built:
        if rid not in known:
            continue
        try:
            _record_concentration_kpis(rid, db, RUNS_DIR, lambda *_: None)
            done += 1
        except Exception as exc:                       # noqa: BLE001
            print(f"  [건너뜀] {rid} 집중도 KPI — {type(exc).__name__}: {exc}")
    print(f"  집중도 KPI {done:,}개 run")

    with get_conn(db, readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM run_kpi").fetchone()[0]
    print(f"\nrun_kpi 총 {n:,}줄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
