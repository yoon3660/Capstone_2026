"""윈도우를 키우면 결합이 더 메워지나 (#143 → #139 · #140).

    python scripts/sweep_window.py --windows 4,6,8,12
    python scripts/sweep_window.py --windows 4,8 --seeds 1,2 --fresh

## 왜 이걸 재는가

#143 의 전수 열거가 두 가지를 보였다 (`docs/ladder.md` 의 전수 열거 블록).

    ① 휴리스틱은 그룹 안에서 거의 안 진다   22/1,990 그룹 · 하루 1.3h
    ② 그런데 정확히 풀면 전역 209h 나빠진다

②의 이유는 **목적함수가 윈도우 밖을 안 세기 때문**이다. 그룹 4대의 Σd 를
최소화하면 그 선택이 그룹 밖 수천 대에게 미는 비용은 목적함수에 없다.
`|W| = 4` 는 전체의 0.1% 도 안 되므로 **외부효과의 거의 전부가 윈도우 밖**이다.

**윈도우를 키우는 것은 그 원인을 직접 건드린다.** 밖에 있던 것이 안으로 들어온다.

그런데 반대 힘도 있다 — 윈도우가 크면 이웃 탐색도 넓어져서 **국소 이득을 더
공격적으로 긁어낸다.** 그게 ②의 손해를 만든 바로 그 동작이다. 어느 쪽이 큰지는
재야 안다.

## 미리 정한 갈림길 (결과를 보기 전에 적는다)

| 만약 총 사회적 비용이 | 뜻 | 다음 |
|---|---|---|
| 윈도우를 키울수록 **내려간다** | 공간축은 **윈도우에 묶여 있었다** | #139 판정을 다시 열어야 한다. 그리고 **solver 의 목적이 분명해진다** — 큰 윈도우를 감당하는 것 |
| **평평하다** | 윈도우는 손잡이가 아니다 | 중요한 외부효과가 **어떤 감당 가능한 윈도우보다도 밖**에 있다. 키우지 말고 **값을 매겨야** 한다 — MEC(#138) · terminal penalty(#144) |
| 키울수록 **올라간다** | 공동 최적화 자체가 틀을 잘못 잡았다 | ②와 같은 모양이 커지는 것이다. 윈도우를 키우는 것도 답이 아니다 |

**#139 는 window = 4 에서 공간축을 닫았다** (S3 − S2 = +1.20 ± 2.01 %p). 그리고
`S3Settings.window` 주석이 4 를 고른 이유를 적어 뒀다 — *"window=12 는 10분을
넘겼다. 비용이 대략 window² ~ window³ 로 커진다."* 즉 **옳아서 4 가 아니라
휴리스틱이 그 이상을 못 감당해서 4 다.** 이 스윕이 그 제약을 벗겨 본다.

## ⚠ 샌드박스에서 돈다 — run_id 가 윈도우를 구분하지 못한다

S3 는 어느 윈도우로 돌아도 run_id 가 `__S3__` 다. 그래서 이 스윕을 **실제 DB 에서
돌리면 사다리의 S3(윈도우 4)를 윈도우 12 결과로 덮어쓴다.**

#123 에서 흐린 오라클이 날것을 덮어썼고, #142 에서 리드타임 스윕이 사다리의 S3 를
덮어썼다. **세 번째는 안 만든다.**

그래서 이 스크립트는 **실제 DB 를 복사해 만든 샌드박스 DB 와 별도 runs 디렉터리**
에서 돈다. 마스터 테이블(휴게소·충전기·셀·시나리오)이 복사본에 그대로 따라오므로
세계는 같고, 등록된 런과 parquet 은 **한 바이트도 안 건드린다.**

결과는 CSV 가 유일한 기록이다 — 샌드박스 DB 안에서도 run_id 는 여전히 겹치므로
**점마다 저장한다.**
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds, run_once, summarize_kpis  # noqa: E402

#: 관측 예측 신뢰도. **사다리와 같은 값이어야 한다** (`matched_ladder.py`)
OBSERVED_STRENGTH = 0.30

#: 이탈이 이보다 더 벌어지면 비교가 성립하지 않는다 (`docs/compare_rule.md`)
ESCAPE_TOL = 5

METRICS = ("dwell_total_h", "n_escaped", "wait_mean_min", "wait_p95_min",
           "bottleneck_slots", "n_ev_charging", "s3_window",
           "s3_move_tried", "s3_move_done", "s3_swap_tried", "s3_swap_done",
           "s3_swap_ms")


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
        # `sqlite3.Connection.backup` 은 쓰기 중이어도 일관된 복사를 준다
        a, b = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(db)
        try:
            with b:
                a.backup(b)
        finally:
            a.close()
            b.close()
        with sqlite3.connect(db) as conn:
            # 등록된 런은 지운다 — 샌드박스는 **세계만** 물려받고 결과는 새로 만든다.
            # 남겨 두면 재사용 판정이 실제 DB 의 런을 보고 "재사용" 이라고 말한다
            conn.execute("DELETE FROM run")
        print("  마스터 테이블만 남기고 run 을 비웠다")
    return db, runs


def kpis(run_id: str, db: Path) -> dict[str, float]:
    s = summarize_kpis([run_id], db_path=db).set_index("metric")["mean"]
    return {k: float(s.get(k, float("nan"))) for k in METRICS}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--windows", default="4,6,8,12",
                    help="같이 정하는 대수. 비용이 대략 window²~window³ 로 커진다")
    ap.add_argument("--strength", type=float, default=OBSERVED_STRENGTH)
    ap.add_argument("--fresh", action="store_true", help="샌드박스 DB 를 다시 만든다")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    seeds = parse_seeds(args.seeds)
    windows = [int(x) for x in str(args.windows).split(",")]
    spec = ObservedSpec(strength=args.strength)
    cfg = load_config(args.config, stage="S3")
    esc_cost = float(cfg.demand.escape_cost_min)

    db, runs = sandbox(RUNS_DIR / "sweeps" / "window_sandbox", fresh=args.fresh)
    out = Path(args.out) if args.out else (RUNS_DIR / "sweeps" / "window_sweep.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{cfg.scenario_id}")
    print(f"시드 {seeds} · 윈도우 {windows} · strength {args.strength:g}")
    print(f"이탈 비용 {esc_cost:.0f}분 · CSV → {out}\n")

    rows: list[dict] = []
    for seed in seeds:
        for w in windows:
            t0 = time.perf_counter()
            print(f"  시드 {seed} 윈도우 {w:>3} — 실행", end="", flush=True)
            rid = run_once(cfg, seed=seed, overwrite=True, observed=spec,
                           s3_window=w, db_path=db, runs_dir=runs,
                           gap_plot=False, log=lambda *_: None)
            k = kpis(rid, db)
            el = time.perf_counter() - t0
            total = k["dwell_total_h"] + k["n_escaped"] * esc_cost / 60.0
            print(f"  {el:7.1f}s   총 {total:9.1f}h  이탈 {k['n_escaped']:.0f}")
            rows.append({"seed": seed, "window": w, "wall_s": el,
                         "total_h": total, **k})
            pd.DataFrame(rows).to_csv(out, index=False)

    _report(pd.DataFrame(rows), out)
    return 0


def _paired(t: pd.DataFrame) -> None:
    """★ **대응 비교** — 같은 시드끼리 짝지어 차를 내고 t 신뢰구간을 낸다.

    런 단위로 평균을 내고 비교하면 안 된다. 시드 사이 변동이 윈도우 효과보다
    크기 때문이다 — #121 · #139 가 그걸로 두 번 속았다. **같은 시드 안에서는 세계가
    같으므로**, 시드마다 차를 내고 그 차들의 분포를 보면 변동이 상쇄된다.

    기준은 **가장 작은 윈도우**다 (보통 1 = 결합 없음 = S2 와 비트 단위로 같다).
    """

    from scipy import stats

    windows = sorted(t["window"].unique())
    base_w = windows[0]
    wide = t.pivot(index="seed", columns="window",
                   values=["total_h", "dwell_total_h", "n_escaped"])

    print(f"\n=== 대응 비교 (기준 윈도우 {base_w:.0f}) ===")
    print("윈도우   n          총 사회적 비용 차 (95% CI)        이탈 차     판정")

    for w in windows[1:]:
        d = (wide["total_h"][w] - wide["total_h"][base_w]).dropna()
        de = (wide["n_escaped"][w] - wide["n_escaped"][base_w]).dropna()
        n = len(d)
        if n < 2:
            continue
        m, sd = float(d.mean()), float(d.std(ddof=1))
        half = float(stats.t.ppf(0.975, n - 1)) * sd / n ** 0.5
        lo, hi = m - half, m + half

        if hi < 0:
            verdict = "★ 이 윈도우가 낫다"
        elif lo > 0:
            verdict = "이 윈도우가 나쁘다"
        else:
            verdict = "0 과 구분 안 됨"
        esc = f"{de.mean():+.1f}"
        if abs(de.mean()) > ESCAPE_TOL:
            esc += " ⚠"
        print(f"{w:>5.0f} {n:>4}   {m:>+9.1f}h  [{lo:>+8.1f}, {hi:>+8.1f}]"
              f"   {esc:>9}     {verdict}")

    print("\n⚠ 이탈 차가 ±5 를 넘으면(⚠) 비교가 성립하지 않는다 — "
          "`docs/compare_rule.md`.")
    print("   총 사회적 비용은 이탈을 120분으로 환산해 품은 값이라 그대로 쓰지만,")
    print("   **격차 축소율은 보고하지 않는다.**")


def _report(t: pd.DataFrame, out: Path) -> None:
    cols = ["seed", "window", "total_h", "dwell_total_h", "n_escaped",
            "wait_mean_min", "bottleneck_slots", "s3_move_done", "s3_swap_done",
            "wall_s"]
    print("\n=== 윈도우 스윕 ===")
    print(t[cols].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    if t["seed"].nunique() <= 3:
        for seed, g in t.groupby("seed"):
            g = g.sort_values("window")
            base = g.iloc[0]
            print(f"\n--- 시드 {seed}: 윈도우 {base['window']:.0f} 대비 "
                  f"(총 사회적 비용 {base['total_h']:,.1f}h) ---")
            for _, r in g.iloc[1:].iterrows():
                d = r["total_h"] - base["total_h"]
                de = r["n_escaped"] - base["n_escaped"]
                flag = "  ⚠ 이탈 차이 큼" if abs(de) > ESCAPE_TOL else ""
                print(f"  윈도우 {r['window']:>3.0f}  {d:+9.1f}h  "
                      f"(이탈 {de:+.0f}, 이동 {r['s3_move_done']:,.0f}, "
                      f"교환 {r['s3_swap_done']:,.0f}, {r['wall_s']:,.0f}s){flag}")
        print("\n⚠ 시드가 적다 — **방향만** 본다. 판정은 시드를 늘려서 한다.")
    else:
        _paired(t)

    print(f"\nCSV: {out}")
    print("\n미리 정한 갈림길 (스크립트 머리말):")
    print("  내려간다 → 공간축이 윈도우에 묶여 있었다. #139 를 다시 열고 solver 의")
    print("             목적이 '큰 윈도우를 감당하는 것' 으로 분명해진다")
    print("  평평하다 → 윈도우는 손잡이가 아니다. 키우지 말고 **값을 매긴다**")
    print("             (MEC #138 · terminal penalty #144)")
    print("  올라간다 → 공동 최적화의 틀 자체가 문제다")
    print("\n⚠ 샌드박스에서 돌았다 — 등록된 런과 parquet 은 안 건드렸다.")
    print("⚠ 시드가 적으면 **판정이 아니다.** 방향만 본다.")


if __name__ == "__main__":
    raise SystemExit(main())
