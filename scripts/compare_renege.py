"""포기를 켜면 사다리가 어떻게 바뀌나 (#147).

    python scripts/compare_renege.py
    python scripts/compare_renege.py --kinds point exponential weibull:0.7 weibull:2.0

## 왜 재나

지금까지 이 모형에는 **나갈 길이 없었다.** 고를 때 한 번 재고(balk), 일단 도착하면
줄이 아무리 길어도 끝까지 기다렸다. 설 S0 에서 **372.5분**(6.2시간)을 기다린 차가
있다 — 이탈 기준이 120분이라고 선언한 모형에서.

그게 사다리를 오염시킨다. 인내를 넘겨 기다린 시간이

    S0 561.8h   S1 99.5h   S2 21.1h   S3 39.5h   S4 34.9h

이고, S0−S2 격차 884.3h 중 540.7h 가 **현실에 없는 기제**에서 온다. 바닥이 더
당하는 이유는 발길질 판단이 **예상** 대기로 이뤄지는데 S0 의 예상("지금 화면")이
가장 많이 틀리기 때문이다.

## 무엇을 비교하나

    껐을 때  = 지금까지의 사다리 (저장된 결과와 같아야 한다)
    켰을 때  = 인내가 끝나면 떠난다

인내 가족도 같이 흔든다. 전부 **중앙값이 같으므로**(= escape_cost_min) 모양만
비교된다 — 수준이 아니다.

    point         전원이 같은 값 (지금 config)
    exponential   일정 위험률. Erlang-A 교과서 가정 (Garnett 외 2002)
    weibull:0.7   감소 위험률 — 기다릴수록 못 나간다 (매몰비용)
    weibull:2.0   증가 위험률 — 기다릴수록 잘 터진다
    lognormal:0.8 지금 코드가 쓸 수 있던 것 (#78)

## 총 사회적 비용에 **포기 전 대기**를 더한다

포기한 차는 인내만큼 기다리다 떠났다. 그 시간은 사라지지 않는다. 더하지 않으면
포기를 켤수록 비용이 줄어드는 것처럼 보인다.

    총 = 총 체류 + 이탈 비용 합 + 포기 전 대기
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec  # noqa: E402
from evdt.runner import load_config, run_once, social_cost_h, summarize_kpis  # noqa: E402

STRENGTH = 0.30
WINDOW = 4
METRICS = ("dwell_total_h", "n_escaped", "n_escaped_balked", "n_escaped_reneged",
           "renege_wait_total_h", "escape_cost_total_h", "wait_mean_min", "wait_p95_min", "wait_max_min",
           "bottleneck_slots", "n_ev_charging")


def kw_for(stage: str) -> dict:
    if stage == "S2":
        return {"observed": ObservedSpec(strength=STRENGTH)}
    if stage in ("S3", "S4"):
        out = {"observed": ObservedSpec(strength=STRENGTH), "s3_window": WINDOW}
        if stage == "S4":
            out["s4_lead_min"] = 10.0
        return out
    return {}


def spec(text: str) -> tuple[str, float]:
    """"weibull:0.7" → ("weibull", 0.7)."""

    kind, _, shape = text.partition(":")
    return kind, float(shape or 0.0)


def tweak(cfg, *, renege: bool, kind: str, shape: float):
    """물리를 바꾼 변이. **`scenario_id` 를 반드시 가른다.**

    ★ 전에는 `dataclasses.replace` 로 `demand` 만 바꾸고 "scenario_id 는 그대로
      둔다 — 같은 세계다" 라고 적어 뒀다. **틀렸다.** 포기와 인내 가족은 물리다.
      물리가 다른데 `run_id` 가 같으면 이 스크립트가 **진짜 run 을 덮어쓴다.**

      2026-11-12 에 같은 모양의 버그가 `sweep_patience.py` 에서 실제로 터졌다 —
      추석 adoption 의 S0·S2 시드 2·3 이 스윕 결과로 덮였고, 사다리 CSV 와
      run 폴더가 갈라져서 알았다. `stale_reason` 은 run 을 *다시 쓸 때* 막는
      장치라 이미 덮인 파일은 못 되돌린다.
    """

    tag = f"ren-{'on' if renege else 'off'}-{kind}-{shape:g}".replace(".", "p")

    return cfg.variant(tag, {
        "demand.renege": renege,
        "demand.patience_kind": kind,
        "demand.patience_shape": shape,
    })


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--stages", nargs="+", default=["S0", "S1", "S2", "S3", "S4"])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--kinds", nargs="+",
                    default=["point", "exponential", "weibull:0.7", "weibull:2.0"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    base = load_config(args.config, stage=args.stages[0])
    esc = float(base.demand.escape_cost_min)
    out = Path(args.out or (ROOT / "runs" / "sweeps" /
                            f"renege_{base.scenario_id}.csv"))
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n포기(renege) 비교 · 시드 {args.seed}")
    print(f"  {base.label}")
    print(f"  인내 중앙값 {esc:g}분 · 가족 {args.kinds}")
    print(f"  CSV → {out}\n")

    #: (라벨, renege, kind, shape)
    cases = [("꺼짐", False, "point", 0.0)]
    cases += [(f"켬 · {k}", True, *spec(k)) for k in args.kinds]

    rows = []
    for label, renege, kind, shape in cases:
        for stage in args.stages:
            cfg = tweak(load_config(args.config, stage=stage),
                        renege=renege, kind=kind, shape=shape)
            t0 = time.perf_counter()
            rid = run_once(cfg, seed=args.seed, overwrite=True,
                           log=lambda *_: None, **kw_for(stage))
            s = summarize_kpis([rid]).set_index("metric")["mean"]
            k = {m: float(s.get(m, 0.0)) for m in METRICS}
            # 이탈 비용은 **차마다** 다르다 — `n_escaped x 중앙값` 으로 세면
            # 안 된다 (이탈은 인내가 짧은 쪽에 쏠린다)
            total = social_cost_h(s)
            rows.append({"칸": stage, "조건": label, "total_h": total,
                         "wall_s": time.perf_counter() - t0, **k})
            print(f"  {label:<18} {stage:<3} {time.perf_counter() - t0:7.1f}s  "
                  f"총 {total:>9,.1f}h  체류 {k['dwell_total_h']:>8,.1f}h  "
                  f"발길질 {k['n_escaped_balked']:>5.0f}  포기 {k['n_escaped_reneged']:>5.0f}  "
                  f"포기전대기 {k['renege_wait_total_h']:>7,.1f}h  "
                  f"최대대기 {k['wait_max_min']:>6.1f}분")
            pd.DataFrame(rows).to_csv(out, index=False)

    t = pd.DataFrame(rows)
    print("\n" + "=" * 108)
    print("=== 조건 × 칸 — 총 사회적 비용 (체류 + 이탈 + 포기 전 대기) ===")
    piv = t.pivot(index="조건", columns="칸", values="total_h")
    piv = piv.reindex([c[0] for c in cases])[args.stages]
    print(piv.to_string(float_format=lambda v: f"{v:,.1f}"))

    print("\n=== S0 대비 개선율 (%) — **포기를 켜면 바닥이 내려간다** ===")
    imp = piv.apply(lambda r: (r[args.stages[0]] - r) / r[args.stages[0]] * 100, axis=1)
    print(imp.to_string(float_format=lambda v: f"{v:+5.1f}"))

    print("\n=== 최대 대기 (분) — 갇힌 차가 풀렸나 ===")
    print(t.pivot(index="조건", columns="칸", values="wait_max_min")
          .reindex([c[0] for c in cases])[args.stages]
          .to_string(float_format=lambda v: f"{v:,.1f}"))

    print("\n=== 이탈 내역 (발길질 / 포기) ===")
    for label, *_ in cases:
        g = t[t["조건"] == label].set_index("칸")
        parts = " · ".join(
            f"{st} {g.loc[st, 'n_escaped_balked']:.0f}/{g.loc[st, 'n_escaped_reneged']:.0f}"
            for st in args.stages if st in g.index)
        print(f"  {label:<18} {parts}")

    print(f"\nCSV: {out}")
    print("⚠ 시드 하나다. **판정이 아니다.**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
