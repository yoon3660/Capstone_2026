"""MILP ablation — 필터를 끄면 · 근사를 끄면 (#143 완료조건).

    python scripts/ablation_milp.py
    python scripts/ablation_milp.py --seeds 1,2 --window 4 --fresh

#143 이 요구한 네 줄을 한 번에 낸다.

    ① solver 연결                 무엇을 썼고 왜 썼나 → `docs/objective.md` §6
    ② 후보 필터링 전후 변수 수     `milp_binaries` (scope group ↔ all)
    ③ 근사 오차                   `milp_approx_err_min` = (정확한 Σd) − (MILP 목적값)
    ④ ablation 두 줄              필터 끄면 / 근사 끄면 — 느려짐과 달라짐

## 네 가지 설정을 돌린다

| 이름 | solver | 후보 | 무엇을 재나 |
|---|---|---|---|
| `local` | 이동·교환 | 그룹이 쓰는 곳 | 기준선. 지금 사다리의 S3 |
| `milp` | MILP (HiGHS) | 그룹이 쓰는 곳 | **같은 범위를 전역으로** 푼다 |
| `milp-feas` | MILP | 코리도 전체 (**필터 통과만**) | 후보 범위를 그룹 밖까지 넓히면 |
| `milp-all` | MILP | 코리도 전체 (**필터 끔**) | ablation ① — 필터를 끄면 |
| `exact` | 전수 열거 | 그룹이 쓰는 곳 | ablation ② — **근사를 끄면** |

⚠ **필터 ablation 은 `milp-feas` ↔ `milp-all` 이다.** `milp` ↔ `milp-all` 로
재면 두 가지가 섞인다 — 필터를 끈 것과 **후보 범위를 넓힌 것**. 후자는 축이
달라지는 변경이다 (`docs/objective.md` §3.6 의 *"C_i 를 넓히면 축이 섞인다"*).
실제로 섞어서 재 보니 답이 달라졌다 (6,425.3h → 6,412.6h) — **필터 탓이 아니라
새 휴게소가 열린 탓**이다.

`exact` 가 *"근사를 끈 것"* 인 이유: MILP 의 근사는 쌍별 겹침 과대계산이고
(`engine/milp.py`), 전수 열거는 그 근사 없이 **정확한 채점기로 모든 조합을**
재서 참 최적해를 낸다. 즉 둘의 차가 **근사 오차가 결과에 미친 영향**이다.

## ⚠ 샌드박스에서 돈다

run_id 는 solver 를 구분하지 못한다 — S3 는 어느 solver 로 돌아도 `__S3__` 다.
실제 DB 에서 돌리면 사다리의 S3 를 MILP 결과로 덮어쓴다 (#123 · #142 에서 두 번
밟았다). 그래서 `sweep_window.py` 와 같은 샌드박스를 쓴다.

`params_json` 의 `engine_opts` 에도 적히므로, 뒤에 누가 재사용하려 하면
`stale_reason` 이 *"기록 solver=milp ≠ 지금 (없음)"* 으로 잡는다.

## ⚠ 시드가 적으면 **판정이 아니다**

이 스크립트는 *"MILP 가 사다리를 더 메우나"* 를 **판정하지 않는다.** 그건 대응
비교가 할 일이고(`sweep_window.py::_paired`), 이 프로젝트에서 시드 하나로 세 번
속았다 (#121 · #139 · #143). 여기서 읽을 것은 **비용과 크기와 근사 오차**다 —
그 셋은 시드 하나에서도 뜻이 있다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.io.db import default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    PROJECT_ROOT,
    load_config,
    parse_seeds,
    run_once,
    summarize_kpis,
)

RUNS_DIR = PROJECT_ROOT / "runs"

#: 이탈 차가 이보다 크면 비교가 성립하지 않는다 (`docs/compare_rule.md`).
ESCAPE_TOL = 5

#: 사다리가 쓰는 관측 예측 세기. **같아야 한다** (`scripts/matched_ladder.py`).
OBSERVED_STRENGTH = 0.30

#: 돌릴 설정. (이름, engine_opts)
SETUPS: tuple[tuple[str, dict], ...] = (
    ("local", {}),
    ("milp", {"solver": "milp"}),
    ("milp-feas", {"solver": "milp", "candidate_scope": "feasible"}),
    ("milp-all", {"solver": "milp", "candidate_scope": "all"}),
    ("exact", {"solver": "exact"}),
)

METRICS = ("dwell_total_h", "n_escaped", "wait_mean_min", "bottleneck_slots",
           "s3_window", "s3_move_done", "s3_swap_done", "s3_swap_ms",
           "milp_groups", "milp_better_groups", "milp_tied_groups",
           "milp_lost_groups", "milp_rejected_groups", "milp_timeouts",
           "milp_gain_min", "milp_approx_err_min", "milp_approx_abs_min",
           "milp_binaries", "milp_pairs", "milp_solve_s",
           "exact_groups", "exact_better_groups", "exact_tried", "exact_gain_min")


def sandbox(root: Path, *, fresh: bool) -> tuple[Path, Path]:
    """실제 DB 를 복사한 샌드박스. 등록된 런을 건드리지 않으려고 쓴다."""

    root.mkdir(parents=True, exist_ok=True)
    db, runs = root / "evdt.db", root / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    if fresh and db.exists():
        db.unlink()
    if not db.exists():
        src = default_db_path()
        print(f"샌드박스 DB 를 만든다: {src.name} → {db}")
        a, b = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(db)
        try:
            with b:
                a.backup(b)
        finally:
            a.close()
            b.close()
        with sqlite3.connect(db) as conn:
            # 등록된 런은 지운다 — 샌드박스는 **세계만** 물려받는다
            conn.execute("DELETE FROM run")
        print("  마스터 테이블만 남기고 run 을 비웠다")
    return db, runs


def kpis(run_id: str, db: Path) -> dict[str, float]:
    s = summarize_kpis([run_id], db_path=db).set_index("metric")["mean"]
    return {k: float(s.get(k, float("nan"))) for k in METRICS}


def _report(t: pd.DataFrame, esc_cost: float, out: Path) -> None:
    print("\n=== 설정별 결과 ===")
    cols = ["seed", "setup", "total_h", "dwell_total_h", "n_escaped",
            "bottleneck_slots", "wall_s"]
    print(t[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    print("\n=== ② 후보 필터링 전후 — 변수 수 ===")
    size = ["setup", "milp_binaries", "milp_pairs", "milp_groups", "milp_solve_s"]
    m = t[t["setup"].str.startswith("milp")]
    if not m.empty:
        print(m[size].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))
        for seed, g in m.groupby("seed"):
            narrow = g[g["setup"] == "milp-feas"]
            wide = g[g["setup"] == "milp-all"]
            if narrow.empty or wide.empty:
                continue
            n, w = narrow.iloc[0], wide.iloc[0]
            ratio = w["milp_binaries"] / max(n["milp_binaries"], 1.0)
            print(f"  시드 {seed}: 필터 켜면 {n['milp_binaries']:,.0f} 개 · "
                  f"끄면 {w['milp_binaries']:,.0f} 개 (**{ratio:.1f}배**)")
            print("  (둘 다 후보 범위는 코리도 전체다 — **필터만** 다르다)")

    print("\n=== ③ 근사 오차 — (정확한 Σd) − (MILP 목적값) ===")
    if not m.empty:
        for _, r in m.iterrows():
            per = r["milp_approx_err_min"] / max(r["milp_groups"], 1.0)
            print(f"  {r['setup']:>9} 시드 {r['seed']}: 합 {r['milp_approx_err_min']:,.1f}분 "
                  f"· 그룹당 {per:,.3f}분")
        print("  ⚠ **부호는 보장되지 않는다.** 크기만 읽는다 — 큐는 열 효과가")
        print("     더해지지도 겹치지도 않는다. 선행자 둘의 지연이 겹치면 과대,")
        print("     둘이 같이 있어야 비로소 기다리게 되면 과소다 (engine/milp.py).")

    print("\n=== ④ ablation 두 줄 ===")
    for seed, g in t.groupby("seed"):
        by = g.set_index("setup")
        if "local" not in by.index:
            continue
        base = by.loc["local"]

        # ⚠ `docs/compare_rule.md` — 엔진을 비교할 때는 **이탈을 맞춘다.** 못
        #   맞추면 격차 축소율을 보고하지 않는다. 총 사회적 비용은 이탈을
        #   120분으로 환산해 품은 값이라 그대로 쓴다
        for name in by.index:
            de = by.loc[name, "n_escaped"] - base["n_escaped"]
            if abs(de) > ESCAPE_TOL:
                print(f"  ⚠ 시드 {seed} · {name}: 이탈 차 {de:+.0f} 가 허용"
                      f"({ESCAPE_TOL})을 넘는다 — **격차 축소율은 보고하지 않는다**")

        def line(name: str, what: str, by=by, base=base, seed=seed) -> None:
            if name not in by.index:
                return
            r = by.loc[name]
            dt = r["total_h"] - base["total_h"]
            slow = r["wall_s"] / max(base["wall_s"], 1e-9)
            print(f"  시드 {seed} · {what}: 총 사회적 비용 {dt:+.1f}h "
                  f"({r['total_h']:,.1f} vs {base['total_h']:,.1f}) · "
                  f"계산시간 {slow:.2f}배 ({r['wall_s']:.0f}s vs {base['wall_s']:.0f}s)")

        # ⚠ **둘을 가른다.** `candidate_scope="all"` 은 (a) 못 가는 휴게소를
        #   변수로 만들고 (b) **갈 수 있는** 휴게소를 그룹 밖까지 넓힌다.
        #   섞으면 "필터를 끄면 답이 같다" 를 확인할 수 없다
        if {"milp-feas", "milp-all"} <= set(by.index):
            a, b = by.loc["milp-feas"], by.loc["milp-all"]
            ratio = b["milp_binaries"] / max(a["milp_binaries"], 1.0)
            print(f"  시드 {seed} · **필터를 끄면** (feasible → all): 변수 "
                  f"{a['milp_binaries']:,.0f} → {b['milp_binaries']:,.0f} "
                  f"(**{ratio:.1f}배**) · 계산시간 {b['wall_s'] / max(a['wall_s'], 1e-9):.2f}배 "
                  f"· 총 사회적 비용 {b['total_h'] - a['total_h']:+.1f}h "
                  "(**0 이어야 한다** — 못 가는 칸은 0 으로 묶여 있다)")

        line("milp-feas", "후보 범위를 그룹 밖까지 넓히면")
        line("exact", "근사를 끄면 (전수 열거)")

        if {"milp", "exact"} <= set(by.index):
            d = by.loc["milp", "total_h"] - by.loc["exact", "total_h"]
            print(f"  시드 {seed} · **근사가 결과에 미친 영향**: {d:+.1f}h "
                  "(MILP − 전수 열거)")

    print(f"\nCSV: {out}")
    print("\n⚠ 샌드박스에서 돌았다 — 등록된 런과 parquet 은 안 건드렸다.")
    print("⚠ 시드가 적으면 **판정이 아니다.** 비용·크기·근사 오차만 읽는다.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--window", type=int, default=4)
    ap.add_argument("--strength", type=float, default=OBSERVED_STRENGTH)
    ap.add_argument("--setups", default=",".join(n for n, _ in SETUPS),
                    help="돌릴 설정 이름 (쉼표)")
    ap.add_argument("--fresh", action="store_true", help="샌드박스 DB 를 다시 만든다")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    want = [s.strip() for s in str(args.setups).split(",")]
    setups = [(n, o) for n, o in SETUPS if n in want]
    if not setups:
        print(f"모르는 설정이다: {want} (쓸 수 있는 것: {[n for n, _ in SETUPS]})")
        return 2

    spec = ObservedSpec(strength=args.strength)
    cfg = load_config(args.config, stage="S3")
    esc_cost = float(cfg.demand.escape_cost_min)

    db, runs = sandbox(RUNS_DIR / "sweeps" / "milp_sandbox", fresh=args.fresh)
    out = Path(args.out) if args.out else (RUNS_DIR / "sweeps" / "milp_ablation.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{cfg.scenario_id}")
    print(f"시드 {seeds} · 윈도우 {args.window} · strength {args.strength:g}")
    print(f"설정 {[n for n, _ in setups]} · CSV → {out}\n")

    rows: list[dict] = []
    for seed in seeds:
        for name, opts in setups:
            t0 = time.perf_counter()
            print(f"  시드 {seed} {name:>9} — 실행", end="", flush=True)
            rid = run_once(cfg, seed=seed, overwrite=True, observed=spec,
                           s3_window=args.window, engine_opts=opts or None,
                           db_path=db, runs_dir=runs, gap_plot=False,
                           log=lambda *_: None)
            k = kpis(rid, db)
            el = time.perf_counter() - t0
            total = k["dwell_total_h"] + k["n_escaped"] * esc_cost / 60.0
            print(f"  {el:7.1f}s   총 {total:9.1f}h  이탈 {k['n_escaped']:.0f}")
            rows.append({"seed": seed, "setup": name, "wall_s": el,
                         "total_h": total, **k})
            pd.DataFrame(rows).to_csv(out, index=False)

    _report(pd.DataFrame(rows), esc_cost, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
