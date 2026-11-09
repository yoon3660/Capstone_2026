"""편의시설 선호 스윕 — **쏠림이 혼잡을 만들 수 있는가** (#146 · D3).

    python scripts/sweep_amenity.py
    python scripts/sweep_amenity.py --betas 0 2 5 10 20 40 --stage S0

## 무엇을 묻는가

지금 모형은 쏠릴 수가 없다. 줄이 없으면 체류가 휴게소와 무관해서 (동점 깨기),
추석 ev10% 에서 유효 휴게소수가 9.4/16 이고 가장 붐비는 곳도 가동률 37% 다.
그 상태의 "병목 0" 이 **세계의 성질인지 모형의 누락인지** 가른다.

β(끌림)를 올려 가며 본다 —

    HHI · 유효 휴게소수 · 상위3 점유율   쏠림이 실제로 생기나
    평균 대기 · p95 · 병목 셀           그 쏠림이 혼잡을 만드나
    최대 휴게소 가동률                   어디까지 찼나

## 판정 (미리 적는다)

    β* 가 존재하고 그럴듯한 범위  →  무대는 유효하다. 보급률을 올릴 필요가 없다
    β 를 끝까지 올려도 병목 0     →  무대가 쉬운 것이 맞다. **더 강한 결론**이다
    β* 가 비현실적으로 커야 함     →  중간. 보급률 축과 같이 본다

## ⚠ 지수는 대리변수다

`amenity_index_proxy.csv` 는 충전기 수에서 만든다. 순환 논리 위험이 있어
**존재 질문**에만 쓴다 — 효과의 크기를 재는 데는 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, run_once, summarize_kpis  # noqa: E402

INDEX = "data/processed/amenity_index_proxy.csv"
METRICS = ("dwell_total_h", "n_escaped", "wait_mean_min", "wait_p95_min",
           "bottleneck_slots", "n_ev_charging")


def capacity() -> pd.Series:
    con = sqlite3.connect(default_db_path())
    cap = pd.read_sql_query(
        "SELECT station_id, SUM(n_units) AS n FROM charger WHERE is_active = 1 "
        "GROUP BY station_id", con).set_index("station_id")["n"].astype(float)
    con.close()
    return cap


def concentration(run_id: str, cap: pd.Series, horizon_min: float) -> dict:
    """쏠림과 가동률. 체류가 아니라 **세션 분포**를 본다."""

    path = RUNS_DIR / run_id / "charge_event.parquet"
    if not path.exists():
        return {}
    d = pd.read_parquet(path)
    n = d.groupby("station_id").size().sort_values(ascending=False)
    share = n / n.sum()          # ★ 내림차순이어야 head(3) 이 **상위** 3 이다
    hhi = float((share ** 2).sum())

    # 가동률: 휴게소별 총 충전시간 / (충전기 수 × 지평)
    col = next((c for c in ("charge_min", "dwell_min", "service_min")
                if c in d.columns), None)
    busiest = float("nan")
    if col:
        used = d.groupby("station_id")[col].sum()
        util = used / (cap.reindex(used.index).fillna(1.0) * horizon_min)
        busiest = float(util.max())

    return {"쓴휴게소": int(len(n)), "HHI": hhi, "유효휴게소수": 1.0 / hhi,
            "상위3점유": float(share.head(3).sum()) if len(share) >= 3
            else float(share.sum()),
            "최대가동률": busiest}


#: 추석 config 는 #146 (PR #159) 가 들여온다. 그 전에는 이 저장소에 없다 —
#: 없는 파일을 `load_config` 가 그냥 FileNotFoundError 로 터뜨리면 "내가 뭘
#: 잘못했나" 를 한참 찾게 된다.
def _need_config(path: str) -> None:
    if not (ROOT / path).is_file():
        raise SystemExit(
            f"config 가 없다: {path}\n"
            "  추석 시나리오는 #146 (PR #159) 가 들여온다. 그 브랜치를 머지하거나,\n"
            "  --config 로 지금 있는 시나리오를 지정할 것\n"
            "    예: --config config/scenario_seollal_up_adoption.yaml")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_adoption.yaml")
    ap.add_argument("--stage", default="S0")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--betas", type=float, nargs="+",
                    default=[0.0, 2.0, 5.0, 10.0, 20.0, 40.0, 80.0])
    ap.add_argument("--gammas", type=float, nargs="+", default=[0.0],
                    help="체감 대기 계수. S2 이상에서는 0 만 된다")
    ap.add_argument("--index", default=INDEX)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    _need_config(args.config)
    cap = capacity()
    base = load_config(args.config, stage=args.stage)
    horizon = float(base.time.end_min)
    esc = float(base.demand.escape_cost_min)
    out = Path(args.out or (ROOT / "runs" / "sweeps" /
                            f"amenity_{base.scenario_id}_{args.stage}.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n편의시설 선호 스윕 · {args.stage} · 시드 {args.seed}")
    print(f"  {base.label}")
    print(f"  지수 {args.index}  (⚠ 충전기 수 대리변수)")
    print(f"  β {args.betas} · γ {args.gammas}")
    print(f"  CSV → {out}\n")

    rows = []
    for gamma in args.gammas:
        for beta in args.betas:
            opts = {"amenity_pull": beta, "amenity_patience": gamma,
                    "amenity_source": args.index}
            cfg = load_config(args.config, stage=args.stage)
            t0 = time.perf_counter()
            rid = run_once(
                cfg, seed=args.seed, overwrite=True, log=lambda *_: None,
                engine_opts=opts,
                **({"observed": ObservedSpec(strength=0.30)}
                   if args.stage in ("S2", "S3", "S4") else {}))
            s = summarize_kpis([rid]).set_index("metric")["mean"]
            k = {m: float(s.get(m, float("nan"))) for m in METRICS}
            total = k["dwell_total_h"] + k["n_escaped"] * esc / 60.0
            row = {"beta": beta, "gamma": gamma, "total_h": total, **k,
                   **concentration(rid, cap, horizon),
                   "wall_s": time.perf_counter() - t0, "run_id": rid}
            rows.append(row)
            print(f"  β {beta:>5.0f} γ {gamma:>4.1f}  "
                  f"유효휴게소 {row.get('유효휴게소수', float('nan')):>5.2f}  "
                  f"상위3 {row.get('상위3점유', float('nan')):>5.1%}  "
                  f"최대가동 {row.get('최대가동률', float('nan')):>5.1%}  "
                  f"대기 {k['wait_mean_min']:>6.1f}분  p95 {k['wait_p95_min']:>6.1f}  "
                  f"병목 {k['bottleneck_slots']:>4.0f}  이탈 {k['n_escaped']:>4.0f}  "
                  f"총 {total:>8.1f}h")
            pd.DataFrame(rows).to_csv(out, index=False)

    t = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    cols = ["beta", "gamma", "유효휴게소수", "상위3점유", "최대가동률",
            "wait_mean_min", "wait_p95_min", "bottleneck_slots", "n_escaped", "total_h"]
    print(t[[c for c in cols if c in t.columns]]
          .to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

    print("\n=== 판정 ===")
    hit = t[(t["bottleneck_slots"] >= 50) & (t["wait_mean_min"] >= 10)]
    seen = t[(t["bottleneck_slots"] > 0) | (t["wait_mean_min"] >= 3)]
    if not hit.empty:
        b = hit.iloc[0]
        print(f"  ★ β={b['beta']:g} 에서 혼잡이 설 수준에 닿는다 "
              f"(병목 {b['bottleneck_slots']:.0f} · 대기 {b['wait_mean_min']:.1f}분)")
        print("    → 무대는 유효하다. 보급률을 올릴 필요가 없다")
    elif not seen.empty:
        b = seen.iloc[0]
        print(f"  β={b['beta']:g} 부터 혼잡이 **보이기** 시작한다 "
              f"(병목 {b['bottleneck_slots']:.0f} · 대기 {b['wait_mean_min']:.1f}분)")
        print("    → 중간 결론. 보급률 축과 같이 봐야 한다")
    else:
        print(f"  β 를 {max(args.betas):g} 까지 올려도 병목이 생기지 않는다")
        print("    → 무대가 쉬운 것이 맞다. 선호를 극단까지 밀어도 추석엔 안 막힌다")

    print(f"\nCSV: {out}")
    print("⚠ 시드 하나 · 지수는 대리변수다. **존재 질문의 답**이지 크기의 답이 아니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
