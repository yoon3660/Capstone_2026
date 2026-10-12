"""S2 — 관측 가능한 것만으로 도착을 예측하고 쓴다 (#119).

    python scripts/run_s2.py --seeds 1-6
    python scripts/run_s2.py --seeds 1 --strength 0,0.5,1,1.5,2   # 과대·과소 민감도

## #118 과 무엇이 다른가 — 이것만 다르다

    PERFECT_ROLLING   시뮬레이터의 **실제 미래 도착**을 넣는다 → 벤치마크, 구현 불가
    S2 (여기)         **상류 진입 + 통행시간**만으로 추정한다 → 엔진 칸, 구현 가능

엔진 코드(`S2Policy`)는 **한 줄도 다르지 않다.** 예측 객체만 바꿔 끼운다. 그래서
두 결과의 차이가 **오로지 예측 오차**다 — `DESIGN §8.2` 의 격차 분해 한 칸이다.

## 왜 이 예측이 현실에서 구현 가능한가

내가 `t` 에 offset `y` 에서 offset `x` 의 휴게소를 보면 도착은 `t + (x−y)/v` 다.
그 창에 `x` 를 지나갈 차의 진입 시각을 풀면 **전부 `t` 이전**이다 (나보다 상류에서
진입한 차는). 즉 **미래 진입량을 추정할 필요가 없고, VDS 가 지금까지 센 값만으로
계산된다.**

모르는 것 — 그 차가 **어디에 설지** — 는 **충전 용량 몫**으로 본다. 순환하지 않고
관측 가능하다 (충전기 수는 공개 정보).

## 두 가지 오차가 반대 방향이다

    과소  나보다 **하류에서** 진입하는 차를 못 센다 (아직 진입 전이니 아무도 못 센다)
    과대  내 앞 휴게소에서 이미 충전을 마친 차도 "선다" 고 센다

어느 쪽이 이기는지는 **예측 오차 KPI** 와 `--strength` 민감도로 가른다.

## 읽을 때

    예측 오차   휴게소 × 시(時) 예상 도착 vs 실제 도착 (WAPE)
    예측 손실   PERFECT_ROLLING 과 S2 의 차이 = **예측이 완벽하지 않아서 잃은 몫**
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.forecast import ObservedSpec, observed_from_entries  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import (  # noqa: E402
    departure_soc_mean,
    load_config,
    parse_seeds,
    reusable_run,
    run_once,
    summarize_kpis,
)

STAGES = ("UE", "S0", "S1", "S2", "PERFECT_ROLLING")
SHORT = {"PERFECT_ROLLING": "완전예측"}


def social_cost(run_id: str, escape_cost_min: float) -> dict[str, float]:
    s = summarize_kpis([run_id]).set_index("metric")["mean"]
    esc = float(s.get("n_escaped", 0.0))
    return {
        "dwell_total_h": float(s.get("dwell_total_h", float("nan"))),
        "n_escaped": esc,
        "total_h": float(s.get("dwell_total_h", float("nan"))) + esc * escape_cost_min / 60.0,
        "wait_mean_min": float(s.get("wait_mean_min", float("nan"))),
        "wait_p95_min": float(s.get("wait_p95_min", float("nan"))),
        "bottleneck_slots": float(s.get("bottleneck_slots", float("nan"))),
        "n_ev_charging": float(s.get("n_ev_charging", float("nan"))),
    }


def forecast_error(run_id: str, cfg, built_trips, offsets, chargers,
                   spec: ObservedSpec) -> dict[str, float]:
    """예측한 도착 vs 실제 도착 (#119 완료조건).

    **WAPE 를 쓴다.** MAPE 는 분모가 작은 칸(새벽)에서 터진다 — #62 에서 같은 함정을
    밟았다. 과대·과소를 가르기 위해 **부호 있는 편향**도 같이 낸다.
    """

    fc = observed_from_entries(
        built_trips, offsets, chargers,
        speed_kmh=cfg.demand.cruise_speed_kmh,
        soc_from=cfg.demand.low_soc_threshold,
        soc_to=cfg.vehicles.target_soc_cap,
        consumption_kwh_km=sum(t.consumption_kwh_km for t in built_trips)
        / max(len(built_trips), 1),
        entry_soc_mean=departure_soc_mean(cfg), spec=spec)
    pred = fc.predicted_hourly()

    ce = pd.read_parquet(RUNS_DIR / run_id / "charge_event.parquet")
    ce["hour"] = (ce["t_arrive_min"] // 60).astype(int)
    actual = ce.groupby(["station_id", "hour"]).size().to_dict()

    cells = set(pred) | set(actual)
    err = sum(abs(pred.get(c, 0.0) - actual.get(c, 0)) for c in cells)
    bias = sum(pred.get(c, 0.0) - actual.get(c, 0) for c in cells)
    tot = sum(actual.get(c, 0) for c in cells)

    return {
        "n_cells": float(len(cells)),
        "pred_total": float(sum(pred.values())),
        "actual_total": float(tot),
        "wape_pct": err / tot * 100.0 if tot else float("nan"),
        "bias_pct": bias / tot * 100.0 if tot else float("nan"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-6")
    ap.add_argument("--strength", default="1.0",
                    help="예측을 얼마나 믿나. 쉼표로 여러 개 주면 민감도 (0 이면 S1 과 같다)")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config, stage="S2")
    seeds = parse_seeds(args.seeds)
    strengths = [float(x) for x in str(args.strength).split(",")]
    db = default_db_path()
    esc = float(cfg.demand.escape_cost_min)

    print(f"\n{cfg.scenario_id}  ·  시드 {seeds}  ·  strength {strengths}")
    print(f"수요: {cfg.demand_label}  ·  이탈 비용 {esc:.0f}분")

    rows, errs = [], []
    for seed in seeds:
        for st in strengths:
            spec = ObservedSpec(strength=st)
            rid = make_run_id(cfg.scenario_id, "S2", seed, cfg.policy.participation)
            ok, why = reusable_run(db, rid, cfg, observed=spec)
            if ok and not args.overwrite and len(strengths) == 1:
                print(f"  시드 {seed} strength {st:g} — 재사용")
            else:
                print(f"  시드 {seed} strength {st:g} — 실행 ({why})")
                rid = run_once(cfg, seed=seed, overwrite=True, observed=spec,
                               log=lambda *_: None)
            rows.append({"seed": seed, "strength": st, "stage": "S2",
                         **social_cost(rid, esc)})

            # 예측 오차는 **이 런의 차량 집합**으로 다시 만들어 대조한다
            errs.append({"seed": seed, "strength": st, **_error_for(cfg, seed, rid, spec)})

    t = pd.DataFrame(rows)
    e = pd.DataFrame(errs)

    print("\n=== 관측 예측 S2 ===")
    print(t[["seed", "strength", "wait_mean_min", "bottleneck_slots", "n_escaped",
             "dwell_total_h", "total_h"]]
          .to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    print("\n=== 예측 오차 (휴게소 × 시) ===")
    print(e.to_string(index=False, float_format=lambda v: f"{v:,.1f}"))
    print("  bias > 0 이면 **과대예측** (엉뚱한 곳으로 보낸다) · < 0 이면 과소예측 "
          "(S1 쪽으로 돌아간다)")

    if len(strengths) == 1:
        _ladder(cfg, seeds, esc, float(strengths[0]))
    return 0


def _error_for(cfg, seed: int, run_id: str, spec: ObservedSpec) -> dict[str, float]:
    """예측 오차를 내려면 그 런의 차량 집합이 필요하다. config 로 다시 만든다."""

    from evdt.io.db import get_conn
    from evdt.io.stations import read_station_chargers
    from evdt.io.vehicles import load_from_db
    from evdt.runner import build_demand, with_seed
    from evdt.world.sim import station_specs

    c = with_seed(cfg, seed)
    with get_conn(default_db_path(), readonly=True) as conn:
        srows, crows = read_station_chargers(conn, corridor_id=c.corridor_id)
        vclasses, curves, temps = load_from_db(conn)
        end_km = float(conn.execute(
            "SELECT length_km FROM corridor WHERE corridor_id = ?", (c.corridor_id,)
        ).fetchone()[0])
    specs = station_specs(srows, crows)
    keep = {s.station_id for s in specs}
    stations = [r for r in srows if r["station_id"] in keep]
    offsets = {r["station_id"]: float(r["offset_km"]) for r in stations}
    built, _, _ = build_demand(c, stations, vclasses, curves, temps, end_km,
                               log=lambda *_: None)
    return forecast_error(run_id, c, built.trips, offsets,
                          {s.station_id: len(s.chargers) for s in specs}, spec)


def _ladder(cfg, seeds, esc: float, strength: float) -> None:
    """UE · S0 · S1 · S2 · 완전예측 — 공통 시드만 (#119 완료조건)."""

    cols, have = {}, {}
    for st in STAGES:
        ids = {s: make_run_id(cfg.scenario_id, st, s, cfg.policy.participation)
               for s in seeds}
        done = []
        for s, rid in ids.items():
            try:
                k = summarize_kpis([rid])
            except Exception:       # noqa: BLE001 — 없는 run 은 그냥 뺀다
                continue
            if not k.empty:
                done.append(s)
        have[st] = done
        cols[st] = ids

    shared = sorted(set.intersection(*(set(v) for v in have.values())) if all(have.values())
                    else set())
    if not shared:
        print("\n[건너뜀] 5단이 모두 끝낸 시드가 없다: "
              + " · ".join(f"{st} {len(v)}개" for st, v in have.items()))
        return

    print(f"\n=== 5단 비교 — 공통 시드 {len(shared)}개 {shared} ===")
    out = {}
    for st in STAGES:
        vals = [social_cost(cols[st][s], esc) for s in shared]
        out[SHORT.get(st, st)] = {k: sum(v[k] for v in vals) / len(vals) for k in vals[0]}
    d = pd.DataFrame(out).T

    if d["n_ev_charging"].nunique() > 1:
        raise SystemExit(f"\n[중단] 단계마다 충전 필요 EV 가 다르다: "
                         f"{d['n_ev_charging'].to_dict()}")

    floor, ceil = d.loc["S0", "total_h"], d.loc["UE", "total_h"]
    d["메운%"] = (floor - d["total_h"]) / (floor - ceil) * 100.0
    print(d[["wait_mean_min", "wait_p95_min", "bottleneck_slots", "n_escaped",
             "total_h", "메운%"]].to_string(float_format=lambda v: f"{v:,.1f}"))

    # ★ 격차 분해 — 이 티켓의 기댓값
    s2, pr = d.loc["S2", "total_h"], d.loc["완전예측", "total_h"]
    print(f"\n=== 격차 분해 (DESIGN §8.2) · strength {strength:g} ===")
    print(f"  S0 (바닥)            {floor:>9,.0f}h")
    print(f"  S2 (관측 예측)        {s2:>9,.0f}h   S0→UE 격차의 {(floor - s2) / (floor - ceil):.1%} 를 메웠다")
    print(f"  완전예측 (벤치마크)    {pr:>9,.0f}h   {(floor - pr) / (floor - ceil):.1%}")
    print(f"  UE (천장)            {ceil:>9,.0f}h   100%")
    # ⚠ S2 가 완전예측보다 **싸게 나올 수 있다** — 더 많이 내보내서다. 그때 "예측 오차
    #   손실" 은 음수가 되어 말이 안 된다 (처음에 그대로 찍어서 −1,388h · 198% 가 나왔다)
    e2, epr, e1 = (d.loc["S2", "n_escaped"], d.loc["완전예측", "n_escaped"],
                   d.loc["S1", "n_escaped"])
    print(f"\n  이탈  S2 {e2:,.0f}대  ·  완전예측 {epr:,.0f}대  ·  S1 {e1:,.0f}대")

    if s2 <= pr:
        print(f"\n  ⚠ **관측 예측이 완전 예측보다 싸게 나왔다** ({s2:,.0f}h < {pr:,.0f}h).")
        print(f"     예측이 더 좋아서가 아니라 **더 많이 내보내서**다 — 이탈 "
              f"{e2:,.0f} vs {epr:,.0f}대.")
        print("     **이 값에서 '예측 오차 손실' 을 읽으면 안 된다.** 아래 손익분기를 볼 것.")
    elif abs(floor - pr) > 1e-9:
        share = (s2 - pr) / (floor - pr) * 100.0
        print(f"\n  **예측 오차 손실 = {s2 - pr:,.0f}h** — 완전 예측이 얻은 것 중 "
              f"{100 - share:.0f}% 를 관측 예측이 가져왔다")

    # 손익분기 — 순위가 가정 하나에 달려 있는지 본다 (#121 에서 쓴 것과 같은 자)
    print("\n  === 손익분기 이탈 비용 (이보다 비싸면 이탈 적은 쪽이 이긴다) ===")
    for left, right in (("S2", "완전예측"), ("S2", "S1"), ("S0", "S1")):
        wa, ea = d.loc[left, "dwell_total_h"], d.loc[left, "n_escaped"]
        wb, eb = d.loc[right, "dwell_total_h"], d.loc[right, "n_escaped"]
        if abs(ea - eb) < 1e-9:
            continue
        c = (wb - wa) / (ea - eb) * 60.0
        flag = "   ← 가정값 바로 옆이다" if abs(c - esc) < 60 else ""
        print(f"    {left:6} vs {right:6}  {c:7,.0f}분 ({c / 60:.1f}시간){flag}")
    print(f"    우리가 쓴 값 {esc:,.0f}분 — `docs/platform.md` 가 **근거 없음**으로 "
          "표시해 둔 값이다")


if __name__ == "__main__":
    raise SystemExit(main())
