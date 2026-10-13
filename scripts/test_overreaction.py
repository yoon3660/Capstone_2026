"""과잉반응 가설 — 오라클은 **정확해서** 해로운가 (#123).

    python scripts/test_overreaction.py --seeds 1-7

## 가르려는 것

#119 에서 **관측 예측이 오라클을 이겼다** (S0→UE 격차의 39.5% vs 28.8%). 설명이 둘
있는데 서로 다르다.

    (A) 과잉반응   오라클이 **정확하고 똑같아서** 전원이 같이 피한다
    (B) 낡은 정보   오라클이 보는 미래는 S1 의 미래라 그냥 **틀렸다**

관측 예측 쪽 증거만으로는 안 갈린다 — 관측 예측은 오라클과 **두 가지가** 다르다.
덜 정확하고(accuracy), 더 흐릿하다(precision).

## 그래서 하나를 고정한다

`BlurredForecast` 가 오라클의 **개수를 그대로** 쓰면서 **누가 언제 오는지만 지운다.**
정확도는 같고 정밀도만 없앤 대조군이다.

    오라클(날것)   맞는 개수 · 맞는 시각 · 맞는 차종
    오라클(흐림)   맞는 개수 · 지운 시각 · 대표 차종   ← 대조군
    관측 예측      추정 개수 · 지운 시각 · 대표 차종

## 미리 정한 판정 (결과를 보기 전에 정한다)

    흐림이 날것보다 **총 대기 5% 이상** 낮으면   →  (A) 과잉반응. 가설 확인
    차이가 ±5% 안이면                          →  (B) 낡은 정보. **가설 기각**

5% 인 이유: #119 에서 오라클과 관측 예측의 총 대기 차이가 **3,796 vs 3,232h = 15%**
였다. 정밀도가 그 차이의 3분의 1도 설명하지 못하면 주된 원인이 아니다.

**기각되면 `docs/s2_observed.md` §7 을 고친다.** 다른 설명 후보는 거기 적는다.

## 왜 대기만 보나

이탈 수가 엔진마다 달라서 총 사회적 비용은 섞인다 (#119 §5). **총 대기시간**은
이탈에 덜 흔들리고, 이 가설이 예측하는 바로 그 양이다. 이탈도 같이 찍어서 눈으로
확인한다.
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec, OracleSource  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    load_config,
    parse_seeds,
    reusable_run,
    run_once,
    summarize_kpis,
)

#: #123 에 미리 적은 기준. **결과를 보고 고치지 않는다**
PASS_DROP_PCT = 5.0

ARRIVAL_COLS = ["ev_id", "station_id", "t_arrive_min", "soc_in", "soc_out", "cold_factor"]
#: 관측 예측의 기준 설정 (#119 와 같다 — 이탈을 맞춘 지점)
OBSERVED_STRENGTH = 0.5


def arrivals_of(run_id: str) -> list[dict]:
    return pd.read_parquet(RUNS_DIR / run_id / "charge_event.parquet")[
        ARRIVAL_COLS].to_dict("records")


def wait_of(run_id: str) -> dict[str, float]:
    s = summarize_kpis([run_id]).set_index("metric")["mean"]
    d = pd.read_parquet(RUNS_DIR / run_id / "charge_event.parquet")
    return {
        "총대기h": float(d["wait_min"].sum()) / 60.0,
        "평균대기": float(s["wait_mean_min"]),
        "p95대기": float(s["wait_p95_min"]),
        "병목칸": float(s["bottleneck_slots"]),
        "이탈": float(s["n_escaped"]),
        "정차": float(len(d)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-7")
    ap.add_argument("--blur-scale", type=float, default=0.6,
                    help="흐린 예측의 개수 배율. **이탈을 날것과 맞추는 값**을 준다 "
                         "— 안 맞추면 '내보내서 이긴 것' 을 '흐려서 이긴 것' 으로 읽는다")
    args = ap.parse_args()

    cfg_s1 = load_config(args.config, stage="S1")
    cfg_pr = load_config(args.config, stage="PERFECT_ROLLING")
    cfg_s2 = load_config(args.config, stage="S2")
    db = default_db_path()
    seeds = parse_seeds(args.seeds)

    print(f"\n{cfg_s1.scenario_id}  ·  시드 {seeds}")
    print(f"\n미리 정한 기준: **흐림이 날것보다 총 대기 {PASS_DROP_PCT:.0f}% 이상 낮으면** "
          "과잉반응 확인\n")

    rows = []
    for seed in seeds:
        s1_id = make_run_id(cfg_s1.scenario_id, "S1", seed, cfg_s1.policy.participation)
        ok, _ = reusable_run(db, s1_id, cfg_s1)
        if not ok:
            print(f"  시드 {seed} — S1 이 없다. 건너뛴다")
            continue
        src = arrivals_of(s1_id)
        rows.append({"seed": seed, "엔진": "S1", **wait_of(s1_id)})

        # 날것 — #119 와 같은 2회차를 재현한다
        rid = None
        for rnd in (1, 2):
            rid = run_once(cfg_pr, seed=seed, overwrite=True, log=lambda *_: None,
                           oracle=OracleSource(rows=src, source_run_id=s1_id, round_no=rnd))
            src = arrivals_of(rid)
        rows.append({"seed": seed, "엔진": "오라클(날것)", **wait_of(rid)})

        # 흐림 — **같은 오라클 재료**로 개수만 쓴다. 1회차부터 같은 사슬을 다시 탄다
        src2 = arrivals_of(s1_id)
        for rnd in (1, 2):
            rid = run_once(cfg_pr, seed=seed, overwrite=True, log=lambda *_: None,
                           oracle=OracleSource(rows=src2, source_run_id=s1_id,
                                               round_no=rnd, blur=True,
                                               blur_scale=args.blur_scale))
            src2 = arrivals_of(rid)
        rows.append({"seed": seed, "엔진": f"오라클(흐림×{args.blur_scale:g})", **wait_of(rid)})

        # 관측 예측 — 참고
        rid = run_once(cfg_s2, seed=seed, overwrite=True, log=lambda *_: None,
                       observed=ObservedSpec(strength=OBSERVED_STRENGTH))
        rows.append({"seed": seed, "엔진": "관측예측", **wait_of(rid)})
        print(f"  시드 {seed} 완료")

    t = pd.DataFrame(rows)
    order = ["S1", "오라클(날것)", f"오라클(흐림×{args.blur_scale:g})", "관측예측"]
    m = t.groupby("엔진")[["총대기h", "평균대기", "p95대기", "병목칸", "이탈", "정차"]].mean()
    m = m.reindex(order)

    print("\n=== 평균 (공통 시드) ===")
    print(m.to_string(float_format=lambda v: f"{v:,.1f}"))

    bl = f"오라클(흐림×{args.blur_scale:g})"
    raw, blur, obs = (m.loc["오라클(날것)", "총대기h"], m.loc[bl, "총대기h"],
                      m.loc["관측예측", "총대기h"])
    e_raw, e_blur = m.loc["오라클(날것)", "이탈"], m.loc[bl, "이탈"]
    ok = abs(e_blur - e_raw) / max(e_raw, 1) < 0.4
    print(f"\n  이탈 맞춤 확인: 날것 {e_raw:,.0f}대 · 흐림 {e_blur:,.0f}대 "
          + ("맞음" if ok else "★안 맞음 — --blur-scale 을 조정할 것"))
    if not ok:
        print("     ⚠ 이탈이 안 맞으면 '내보내서 이긴 것' 을 '흐려서 이긴 것' 으로 읽는다")
    drop = (raw - blur) / raw * 100.0

    print("\n  판정 (기준은 결과 보기 전에 정했다, #123)")
    print(f"    흐림이 날것보다 총 대기 {drop:+.1f}% 낮다   기준 ≥ {PASS_DROP_PCT:.0f}%")
    print(f"    (날것 {raw:,.0f}h · 흐림 {blur:,.0f}h · 관측 {obs:,.0f}h)")

    # ⚠ **이탈이 안 맞으면 판정하지 않는다.** 처음에 가드가 걸렸는데도 "확인" 을
    #   찍었다 — #112 에서 고쳤던 것과 같은 보고 버그다 (검사를 건너뛰고 통과 선언).
    #   이탈이 48% 더 많으면 대기가 준 것이 정밀도 때문인지 내보냈기 때문인지 모른다
    if not ok:
        print(f"\n  ⇒ **판정 보류.** 이탈이 안 맞는다 (날것 {e_raw:,.0f} · 흐림 "
              f"{e_blur:,.0f}대, {abs(e_blur - e_raw) / max(e_raw, 1):.0%} 차이).")
        print(f"     대기가 {drop:+.1f}% 준 것이 **정밀도 때문인지 더 내보냈기 때문인지 "
              "가를 수 없다.**")
        print("     --blur-scale 을 낮춰 이탈을 맞추고 다시 돌릴 것.")
        print("\n=== 시드별 총 대기h ===")
        print(t.pivot(index="seed", columns="엔진", values="총대기h")
              .reindex(columns=order).to_string(float_format=lambda v: f"{v:,.0f}"))
        return 0

    if drop >= PASS_DROP_PCT:
        covered = (raw - blur) / (raw - obs) * 100.0 if abs(raw - obs) > 1e-9 else float("nan")
        print(f"\n  ⇒ **과잉반응 확인.** 정확도를 그대로 둔 채 **정밀도만 지웠는데** "
              f"대기가 {drop:.1f}% 줄었다.\n"
              f"     날것↔관측 격차({raw - obs:,.0f}h) 중 **{covered:.0f}%** 를 정밀도가 "
              "설명한다.")
        print("     ⇒ 발표에서 쓸 수 있다: *혼잡 게임에서는 정확한 예측이 해로울 수 있다.*")
    else:
        print(f"\n  ⇒ **가설 기각.** 정밀도를 지워도 대기가 {drop:+.1f}% 밖에 안 움직인다.\n"
              f"     날것↔관측 격차({raw - obs:,.0f}h) 는 **정밀도가 아니라 다른 것**이다.\n"
              "     `docs/s2_observed.md` §7 을 고치고 다른 설명을 찾는다 — 후보: "
              "오라클이 보는\n     미래가 S1 의 미래라 그냥 틀렸다(낡은 정보).")

    print("\n=== 시드별 총 대기h ===")
    print(t.pivot(index="seed", columns="엔진", values="총대기h")
          .reindex(columns=order).to_string(float_format=lambda v: f"{v:,.0f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
