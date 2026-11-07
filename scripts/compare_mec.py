"""기준 축 — 사적 비용 vs 사회적 비용 (#138).

    python scripts/compare_mec.py
    python scripts/compare_mec.py --seeds 1,2,3,4,5 --weights 0.5,1.0

S1 과 MEC 를 **같은 세계에서** 돌려 둘을 비교한다. 정보도 결합도 같고 **고르는
값**만 다르다 (설계문서 §5.1 의 "세 번째 종류의 변경").

## 재는 것이 둘이다

1. **효과** — 사회적 비용이 줄었나. 여러 시드의 **대응 비교**로만 판정한다
2. **비용** — 원장 질의가 얼마나 비싸졌나. #138 이 *"재고 적는다"* 고 적은 값이다

②가 ①보다 먼저다. `evaluate` 는 FIFO 라서 **내 앞**만 보면 끝나는데, MEC 는
**내 뒤까지** 두 번 투영해야 한다. Δt(5분)를 넘으면 **그 자체가 결과다** (#77).

## ⚠ 샌드박스에서 돈다

run_id 는 `mec_weight` 를 구분하지 못한다 — MEC 는 어느 가중치로 돌아도
`__MEC__` 다. 실제 DB 에서 가중치를 쓸면 서로 덮어쓴다 (#123 · #142 에서 두 번
밟았다). `params_json` 의 `engine_opts` 에도 적히므로 뒤에 누가 재사용하려 하면
`stale_reason` 이 잡는다.

## ⚠ 시드가 적으면 **판정이 아니다**

미리 정한 판정 기준은 *"S1 대비 S0→UE 격차를 3%p 이상"* 이고, 그것은
`scripts/matched_ladder.py` 의 사다리에서 **이탈을 맞춘 뒤** 재야 한다. 이
스크립트는 **비용과 방향**만 본다. 이 프로젝트에서 시드 하나로 세 번 속았다
(#121 · #139 · #143).
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

METRICS = ("dwell_total_h", "n_escaped", "n_escaped_balked", "n_escaped_stranded",
           "wait_mean_min", "wait_p95_min",
           "bottleneck_slots", "n_ev_charging", "policy_decide_calls",
           "mec_weight", "mec_chosen_min", "mec_chosen_nonzero",
           "mec_probed_min", "mec_calls", "mec_nonzero", "mec_ms")


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
            conn.execute("DELETE FROM run")
        print("  마스터 테이블만 남기고 run 을 비웠다")
    return db, runs


def kpis(run_id: str, db: Path) -> dict[str, float]:
    s = summarize_kpis([run_id], db_path=db).set_index("metric")["mean"]
    return {k: float(s.get(k, float("nan"))) for k in METRICS}


def _paired(diffs: list[float]) -> str:
    """같은 시드끼리 짝지은 차의 t 신뢰구간. 런 평균끼리 비교하면 속는다."""

    n = len(diffs)
    if n < 2:
        return "시드가 하나다 — 판정이 아니다"
    mean = statistics.fmean(diffs)
    if n < 4:
        return f"{mean:+.1f}h (시드 {n}개 — 판정이 아니다)"
    half = 2.0 * statistics.stdev(diffs) / (n ** 0.5)      # t≈2
    lo, hi = mean - half, mean + half
    call = ("낫다" if hi < 0 else "나쁘다" if lo > 0 else "0 과 구분 안 됨")
    return f"{mean:+.1f}h  [{lo:+.1f}, {hi:+.1f}]  {call}"


def _report(t: pd.DataFrame, esc_cost: float, out: Path) -> None:
    print("\n=== 설정별 결과 ===")
    cols = ["seed", "setup", "total_h", "dwell_total_h", "n_escaped",
            "wait_mean_min", "bottleneck_slots", "wall_s"]
    print(t[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    base = t[t["setup"] == "S1"]
    if base.empty:
        print("\nS1 이 없다 — 비교할 기준이 없다")
        return
    by_seed = base.set_index("seed")["total_h"].to_dict()
    esc_by_seed = base.set_index("seed")["n_escaped"].to_dict()

    print("\n=== ② 비용 — 원장 질의가 얼마나 비싸졌나 (#77 · #138) ===")
    s1_time = base.set_index("seed")["wall_s"].to_dict()
    for setup, g in t[t["setup"] != "S1"].groupby("setup"):
        for _, r in g.iterrows():
            ref = s1_time.get(r["seed"], float("nan"))
            calls = r["mec_calls"]
            per_call = (r["mec_ms"] / calls) if calls else float("nan")
            print(f"  {setup:>10} 시드 {r['seed']}: "
                  f"계산시간 {r['wall_s']:.0f}s vs S1 {ref:.0f}s "
                  f"(**{r['wall_s'] / max(ref, 1e-9):.2f}배**) · "
                  f"원장 질의 {calls:,.0f}회 · 질의당 {per_call:.3f}ms · "
                  f"MEC 에 쓴 시간 {r['mec_ms'] / 1000.0:.1f}s")
    print(f"  ⚠ Δt 를 넘으면 **그 자체가 결과다** — decide() 호출당 여유는 "
          f"Δt 이고, 하루 {base.iloc[0]['policy_decide_calls']:.0f}번 돈다")

    print("\n=== ③ 외부비용이 실제로 얼마나 내재화됐나 ===")
    for setup, g in t[t["setup"] != "S1"].groupby("setup"):
        for _, r in g.iterrows():
            share = (r["mec_chosen_nonzero"] / max(r["mec_calls"], 1.0))
            print(f"  {setup:>10} 시드 {r['seed']}: "
                  f"고른 자리의 외부비용 합 {r['mec_chosen_min']:,.0f}분 "
                  f"({r['mec_chosen_min'] / 60.0:,.1f}h) · "
                  f"0 이 아닌 결정 {r['mec_chosen_nonzero']:,.0f}건 · "
                  f"후보 합 {r['mec_probed_min']:,.0f}분 (내재화가 아니다)")
            del share

    print("\n=== ① 효과 — S1 대비 (대응 비교) ===")
    for setup, g in t[t["setup"] != "S1"].groupby("setup"):
        diffs, bad = [], []
        for _, r in g.iterrows():
            if r["seed"] not in by_seed:
                continue
            diffs.append(r["total_h"] - by_seed[r["seed"]])
            de = r["n_escaped"] - esc_by_seed[r["seed"]]
            if abs(de) > ESCAPE_TOL:
                bad.append((r["seed"], de))
        print(f"  {setup:>10}: {_paired(diffs)}")
        for seed, de in bad:
            print(f"    ⚠ 시드 {seed}: 이탈 차 {de:+.0f} 가 허용({ESCAPE_TOL})을 "
                  "넘는다 — **격차 축소율은 보고하지 않는다**")

    print("\n=== 이탈이 왜 늘었나 — balked 와 stranded 를 가른다 ===")
    cols2 = ["seed", "setup", "n_escaped", "n_escaped_balked",
             "n_escaped_stranded", "dwell_total_h"]
    print(t[cols2].to_string(index=False, float_format=lambda v: f"{v:,.0f}"))
    print("  balked   = 고른 자리가 **나한테** 이탈 비용보다 비싸다")
    print("  stranded = 한 번 서고 나서 닿는 곳이 없다")
    print("  \u26a0 MEC 가 사회적으로 나은 자리를 권하면 그 자리가 **그 운전자에게는**")
    print("     더 비쌀 수 있다. 그러면 balked 가 는다 — 기준 변경의 **분배 효과**다")

    print("\n=== 이탈을 못 맞출 때 — **손익분기 이탈비용** ===")
    print("  총 사회적 비용은 이탈을 분으로 환산해 품는다. 그래서 '어느 이탈비용까지")
    print("  이 결론이 버티나' 를 물을 수 있다 (`docs/ladder.md` \u00a74).")
    for setup, g in t[t["setup"] != "S1"].groupby("setup"):
        for _, r in g.iterrows():
            mine = base[base["seed"] == r["seed"]]
            if mine.empty:
                continue
            b0 = mine.iloc[0]
            d_dwell = b0["dwell_total_h"] - r["dwell_total_h"]   # MEC 가 줄인 체류
            d_esc = r["n_escaped"] - b0["n_escaped"]              # MEC 가 늘린 이탈
            if d_esc <= 0:
                print(f"  {setup:>10} 시드 {r['seed']}: 이탈이 늘지 않았다 "
                      f"(체류 {d_dwell:+.1f}h)")
                continue
            breakeven = d_dwell * 60.0 / d_esc
            verdict = "버틴다" if breakeven > esc_cost else "안 버틴다"
            print(f"  {setup:>10} 시드 {r['seed']}: 체류 {d_dwell:+,.1f}h 절약 \u00b7 "
                  f"이탈 {d_esc:+.0f}대 증가 \u2192 **손익분기 {breakeven:,.0f}분** "
                  f"({breakeven / 60.0:,.1f}h). 등록값 {esc_cost:.0f}분에서 "
                  f"**{verdict}**")

    print(f"\nCSV: {out}")
    print("\n⚠ 샌드박스에서 돌았다 — 등록된 런과 parquet 은 안 건드렸다.")
    print("⚠ 미리 정한 판정은 *'S1 대비 S0→UE 격차 3%p 이상'* 이고, 그것은")
    print("   `scripts/matched_ladder.py` 의 사다리에서 이탈을 맞춘 뒤 재야 한다.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--weights", default="1.0",
                    help="MEC 가중치 (쉼표). 0.0 은 S1 과 비트 단위로 같다")
    ap.add_argument("--fresh", action="store_true", help="샌드박스 DB 를 다시 만든다")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    weights = [float(x) for x in str(args.weights).split(",")]

    s1_cfg = load_config(args.config, stage="S1")
    mec_cfg = load_config(args.config, stage="MEC")
    esc_cost = float(s1_cfg.demand.escape_cost_min)

    db, runs = sandbox(RUNS_DIR / "sweeps" / "mec_sandbox", fresh=args.fresh)
    out = Path(args.out) if args.out else (RUNS_DIR / "sweeps" / "mec_compare.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{s1_cfg.scenario_id}")
    print(f"시드 {seeds} · 가중치 {weights}")
    print(f"이탈 비용 {esc_cost:.0f}분 · CSV → {out}\n")

    rows: list[dict] = []
    for seed in seeds:
        plan: list[tuple[str, object, dict | None]] = [("S1", s1_cfg, None)]
        plan += [(f"MEC w={w:g}", mec_cfg, {"mec_weight": w}) for w in weights]

        for name, cfg, opts in plan:
            t0 = time.perf_counter()
            print(f"  시드 {seed} {name:>10} — 실행", end="", flush=True)
            rid = run_once(cfg, seed=seed, overwrite=True, engine_opts=opts,
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
