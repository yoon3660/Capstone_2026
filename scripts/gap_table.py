"""바닥–천장 격차 축소율 표 — 각 칸이 S0→UE 거리의 몇 %를 좁혔나 (#121, 설계문서 §8.2).

    python scripts/gap_table.py --seeds 1-20

## 왜 이 형태인가

단계별 절대값을 나열하면 *"그래서 얼마나 한 거냐"* 에 답이 안 된다. **천장과 바닥을
먼저 고정**하면 각 칸이 한 숫자가 된다.

    바닥  S0   가장 적은 정보 (앱 화면만)
    천장  UE   전원이 서로의 최종 선택을 안다 (반복 균형)

    격차 축소율 = (S0 − 이 칸) / (S0 − UE)

**상한도 하한도 없다.** 바닥보다 나쁘면 음수, 천장을 이기면 100% 를 넘는다
(UE 는 이기적 균형이라 조정이 이기는 것이 이론적으로 가능하다). 그래서
"달성률·도달률" 이 아니라 **축소율** 이다.

**남은 몫이 곧 로드맵이다.** S3·S4 행의 빈칸이 남은 작업 목록이다.

## UE 를 무엇으로 쓰는가 — (A) 참고 천장 (2026-10-11 확정)

근거는 설계문서 자신의 조항이다 — §1.2 가 UE 를 "재현 기준선" 으로 두면서
*"재현 검증(교통·충전) 없이 '현실' 이라 부르지 않는다"* 를 조건으로 달았는데,
**그 조건이 아직 충족되지 않았다** (정체 재현율 0.00 #58 · 충전 판정 보류 #112).

**바닥은 S0 다** — 앱 화면만 보는 운전자가 지금 가진 가장 현실에 가까운 행동
모델이다. #64 의 실측 충전 로그가 들어와 재현 검증을 통과하면 되돌릴 수 있다.

## ⚠ 두 가지를 강제한다 (#123 에서 비싸게 배웠다)

### 1. 이탈을 맞춘다

사다리의 모든 칸이 통로를 하나 공유한다 — **예측을 비관적으로 만들면 한계 차량이
코리도를 나가고, 남은 사람의 줄이 짧아진다.** 이 프로젝트에서 **네 번** 여기에
속을 뻔했다 (`docs/compare_rule.md` §4).

가장 비싼 것: #119 가 관측 예측을 **strength 0.5 (이탈 +59%)** 로 돌려
*"39.5% — 완전 예측보다 낫다"* 를 발표 문장까지 가져갔다. 이탈을 맞추면
**24.3%** 이고 완전 예측(28.8%)이 이긴다.

그래서 **손잡이가 있는 칸**(S2 의 strength)은 이탈이 S1 근처가 되게 맞춰야 하고,
벗어나면 **그 칸의 격차 축소율을 찍지 않는다.**

> S0·S1 은 손잡이가 없다. 이탈 수가 **설정이 아니라 결과**라서 맞출 대상이 아니다.
> 다만 S0 가 233대를 내보낸다는 사실은 **바닥 자체에 들어가 있으므로** 표에 같이 찍는다.

### 2. 저장된 런을 그냥 믿지 않는다

`run_id` 는 (시나리오·stage·참여율·시드) 뿐이라 **같은 이름으로 다른 실험을 돌릴 수
있다.** 실제로 그랬다 — 흐린 오라클이 날것 오라클을 `__PERFECT_ROLLING__` 에
덮어썼다 (#123 §5). 그래서 `params_json` 을 확인하고 안 맞는 런은 **뺀다.**
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds  # noqa: E402

#: 사다리 칸. S3·S4 는 아직 없다 — **빈칸이 남은 작업 목록이다**
LADDER = ("S0", "S1", "S2", "S3", "S4")
#: 사다리가 아니라 **벤치마크**다 — 구현할 수 없다 (DESIGN §8.2)
BENCH = "PERFECT_ROLLING"
FLOOR, CEIL = "S0", "UE"

#: 이탈을 맞출 기준. 손잡이 없는 칸 중 가장 가까운 것
ESCAPE_REF = "S1"
#: 이보다 벗어나면 그 칸의 격차 축소율을 찍지 않는다
ESCAPE_TOL = 0.35

#: 스테이지가 **반드시 만족해야 하는** params. 안 맞으면 그 런은 뺀다 (#123 §5)
EXPECTED: dict[str, dict] = {
    "S2": {"forecast_kind": "observed"},
    BENCH: {"forecast_kind": "oracle", "oracle_blur": False},
}

#: 손잡이가 있어서 이탈을 맞춰야 하는 칸
TUNABLE = frozenset({"S2", BENCH})


def mean_ci(v: pd.Series) -> tuple[float, float]:
    """평균과 95% 신뢰구간 반폭 (t 분포). 한 시드로 발표하지 않기 위한 것."""

    n = len(v)
    if n < 2:
        return float(v.mean()), float("nan")
    from scipy import stats
    return float(v.mean()), float(stats.t.ppf(0.975, n - 1) * v.std(ddof=1) / math.sqrt(n))


def closed(value: float, floor: float, ceil: float) -> float:
    """바닥 대비 천장까지의 몇 %를 메웠나. 바닥=0%, 천장=100%."""

    span = floor - ceil
    return float("nan") if abs(span) < 1e-12 else (floor - value) / span * 100.0


def collect(cfg, seeds, done, wide, esc_min) -> tuple[pd.DataFrame, list[str]]:
    """런을 모은다. **params 가 안 맞는 런은 뺀다** (#123 §5)."""

    want = [CEIL, *LADDER, BENCH]
    rows, dropped = [], []
    for seed in seeds:
        ids = {st: make_run_id(cfg.scenario_id, st, seed, cfg.policy.participation)
               for st in want}
        # 사다리 비교는 **세 칸이 다 있는 시드만** 쓴다 (#59)
        if any(ids[st] not in done for st in (CEIL, "S0", "S1")):
            continue
        for st in want:
            rid = ids[st]
            if rid not in done:
                continue
            bad = {k: (done[rid].get(k), v) for k, v in EXPECTED.get(st, {}).items()
                   if done[rid].get(k) != v}
            if bad:
                dropped.append(f"{rid} — " + " · ".join(
                    f"{k}: 기록 {a!r} ≠ 기대 {b!r}" for k, (a, b) in bad.items()))
                continue
            k = wide.loc[rid]
            rows.append({
                "seed": seed, "stage": st,
                "wait_mean_min": k["wait_mean_min"],
                "bottleneck_slots": k["bottleneck_slots"],
                "n_escaped": k["n_escaped"],
                "dwell_total_h": k["dwell_total_h"],
                "n_ev_charging": k["n_ev_charging"],
                "total_h": k["dwell_total_h"] + k["n_escaped"] * esc_min / 60.0,
                "strength": done[rid].get("forecast_strength"),
            })
    return pd.DataFrame(rows), dropped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-20")
    args = ap.parse_args()

    cfg = load_config(args.config)
    esc_min = float(cfg.demand.escape_cost_min)
    want = [CEIL, *LADDER, BENCH]

    with sqlite3.connect(default_db_path()) as conn:
        done = {r[0]: json.loads(r[1] or "{}") for r in conn.execute(
            "SELECT run_id, params_json FROM run WHERE status='DONE'")}
        kpi = pd.read_sql("SELECT run_id, metric, value FROM run_kpi", conn)
    wide = kpi.pivot(index="run_id", columns="metric", values="value")

    t, dropped = collect(cfg, parse_seeds(args.seeds), done, wide, esc_min)
    if t.empty:
        raise SystemExit("\n[중단] UE·S0·S1 을 모두 끝낸 시드가 없다.")

    # 같은 시드 안에서 단계마다 세계가 같은지 (#59)
    bad = {s: g["n_ev_charging"].unique().tolist()
           for s, g in t.groupby("seed") if g["n_ev_charging"].nunique() > 1}
    if bad:
        raise SystemExit(f"\n[중단] 같은 시드인데 단계마다 충전 필요 EV 가 다르다: {bad}")

    # ★ **모든 칸이 끝낸 시드만** 쓴다 (#59 · compare_stages3.py 와 같은 규칙).
    #
    #   안 그러면 바닥·천장을 14개 시드로 잡고 S2 를 6개로 재게 되어, **시드 변동이
    #   격차 축소율에 섞인다.** 실제로 그랬다 — 같은 런인데 ladder.md 는 30.2%,
    #   run_s2.py 는 28.8% 였다 (바닥·천장의 시드 집합이 달랐다).
    present = [st for st in want if (t.stage == st).any()]
    shared = sorted(set.intersection(*(set(t[t.stage == st].seed) for st in present)))
    extra = {st: sorted(set(t[t.stage == st].seed) - set(shared)) for st in present}
    t = t[t.seed.isin(shared)]
    have = {st: sorted(t[t.stage == st].seed) for st in want}

    print(f"\n{cfg.scenario_id}   ·   이탈 비용 가정 {esc_min:.0f}분")
    print(f"**공통 시드 {len(shared)}개** {shared}")
    for st, ex in extra.items():
        if ex:
            print(f"  [제외] {st} 에만 있는 시드 {len(ex)}개 {ex} — 같은 세계만 비교한다")
    print(f"수요: {cfg.demand_label}")
    if dropped:
        print(f"\n  [제외] params 가 안 맞는 런 {len(dropped)}개 — 같은 run_id 에 다른 "
              "실험이 덮어써 있다 (#123):")
        for d in dropped:
            print(f"    {d}")
    for st in want:
        n = len(have[st])
        extra = ""
        if st in TUNABLE and n:
            s = t[t.stage == st]["strength"].dropna().unique()
            extra = f"   strength {s[0]:g}" if len(s) == 1 else ""
        print(f"  {st:16} 시드 {n:2d}개{extra}" + ("" if n else "   ← 아직 없음"))

    print(f"\n기준점: 바닥 {FLOOR} = 0%  ·  천장 {CEIL} = 100%")
    print("  UE 는 **참고 천장**이다 — 실시간 엔진이 가질 수 없는 정보를 쓴다 "
          "(DESIGN §1 갱신 2026-10-11)")

    # === 이탈 맞춤 검사 — 격차 축소율을 찍어도 되는지 먼저 정한다 ============
    ref = t[t.stage == ESCAPE_REF]["n_escaped"].mean()
    off, trust = {}, {}
    for st in want:
        if not have[st]:
            continue
        e = t[t.stage == st]["n_escaped"].mean()
        off[st] = e / max(ref, 1e-9) - 1.0
        trust[st] = st not in TUNABLE or abs(off[st]) <= ESCAPE_TOL

    print(f"\n이탈 맞춤 ({ESCAPE_REF} = {ref:,.1f}대 기준)")
    for st, v in off.items():
        mark = ""
        if st in TUNABLE:
            mark = "  ✅" if trust[st] else "  ★ 벗어남 — 격차 축소율을 찍지 않는다"
        elif st == FLOOR:
            mark = "  (손잡이 없음 — 바닥 자체에 들어간 값이다)"
        print(f"  {st:16} {t[t.stage == st]['n_escaped'].mean():6.1f}대  ({v:+.0%}){mark}")

    # === 표 ==============================================================
    out = []
    for metric, label in (("wait_mean_min", "평균 대기(분)"),
                          ("total_h", "총 사회적 비용(h)")):
        base = {st: t[t.stage == st][metric].mean() for st in want if have[st]}
        for st in want:
            if not have[st]:
                out.append({"지표": label, "칸": st, "값": None, "±95%": None,
                            "격차축소율%": None, "이탈": None, "시드": 0})
                continue
            m, ci = mean_ci(t[t.stage == st][metric])
            pct = (0.0 if st == FLOOR else 100.0 if st == CEIL
                   else closed(m, base[FLOOR], base[CEIL]))
            out.append({
                "지표": label, "칸": st, "값": m, "±95%": ci,
                "격차축소율%": pct if trust[st] else None,
                "이탈": t[t.stage == st]["n_escaped"].mean(),
                "시드": len(have[st]),
            })
    o = pd.DataFrame(out)

    print("\n=== 바닥–천장 격차 축소율 ===")
    for label in o["지표"].unique():
        g = o[o["지표"] == label].drop(columns="지표")
        print(f"\n  {label}")
        print(g.to_string(index=False, na_rep="—", float_format=lambda v: f"{v:,.1f}"))
    print(f"\n  ※ {BENCH} 는 사다리 칸이 아니라 **벤치마크**다 — 시뮬레이터의 실제 "
          "미래를 넣으므로 현실에서 구현할 수 없다 (DESIGN §8.2)")
    print("  ※ S3·S4 행의 빈칸이 **남은 작업 목록**이다")

    # === 격차 분해 =======================================================
    tot = o[o["지표"] == "총 사회적 비용(h)"].set_index("칸")["격차축소율%"]
    if pd.notna(tot.get("S2")) and pd.notna(tot.get(BENCH)):
        print("\n=== 격차 분해 (DESIGN §8.2) ===")
        print(f"  관측 예측의 격차 축소율   {tot['S2']:5.1f}%")
        print(f"  완전 예측의 격차 축소율   {tot[BENCH]:5.1f}%")
        print(f"  **예측 오차 손실**      {tot[BENCH] - tot['S2']:5.1f}%p"
              "   ← 예측이 완벽하지 않아서 잃은 몫")
        print(f"  **남은 몫**            {100 - tot[BENCH]:5.1f}%p"
              "   ← 정보로는 못 좁힌다. S3(결합)·S4(롤링)의 과녁")

        # ⚠ 위의 ±95% 는 **수준**의 신뢰구간이라 서로 겹친다 (S2 ±401h · 벤치 ±235h).
        #   그걸로 "차이가 없다" 고 읽으면 틀린다 — 두 칸은 **같은 시드**를 돌았으므로
        #   시드별 차이로 대응 비교해야 한다. 시드 간 변동이 양쪽에서 상쇄된다
        pair = (t[t.stage == "S2"].set_index("seed")["total_h"]
                - t[t.stage == BENCH].set_index("seed")["total_h"]).dropna()
        if len(pair) >= 2:
            m, ci = mean_ci(pair)
            span = t[t.stage == FLOOR]["total_h"].mean() - t[t.stage == CEIL]["total_h"].mean()
            sign = "확실하다" if abs(m) > ci else "**잡음과 구분되지 않는다**"
            print(f"\n  대응 비교 (같은 시드끼리, n={len(pair)})")
            print(f"    S2 − 완전예측 = {m:+,.0f} ± {ci:,.0f} h"
                  f"  =  {m / span * 100:+.1f} ± {ci / span * 100:.1f} %p   → {sign}")
            if abs(m) <= ci:
                print("    ⇒ 예측 오차 손실을 **하나의 숫자로 말하지 않는다.** "
                      "시드를 늘릴 것")

    # === 이탈 비용 민감도 =================================================
    print("\n=== 이탈 비용 가정을 바꿔 보면 (총 사회적 비용, h) ===")
    grid = []
    for c in (60, 120, 180, 240):
        r = {"이탈(분)": c}
        for st in want:
            if have[st]:
                s = t[t.stage == st]
                r[st] = (s["dwell_total_h"] + s["n_escaped"] * c / 60.0).mean()
        grid.append(r)
    print(pd.DataFrame(grid).to_string(index=False, na_rep="—",
                                       float_format=lambda v: f"{v:,.0f}"))
    print(f"  우리가 쓴 값 {esc_min:.0f}분 — `docs/platform.md` 가 **근거 없음**으로 "
          "표시해 둔 값이다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
