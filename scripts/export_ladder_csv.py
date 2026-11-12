"""돌려 둔 사다리 결과를 **CSV 묶음**으로 내보낸다 — 랩탑에서도 분석할 수 있게.

    python scripts/export_ladder_csv.py
    python scripts/export_ladder_csv.py --config config/scenario_seollal_up_adoption.yaml
    python scripts/export_ladder_csv.py --seeds 1 2 3 --out docs/handoff/ladder_seollal

## 왜 필요한가

시드 하나를 끝까지 돌리는 데 S4 가 수백~천 초다. 랩탑에서는 10시드가 현실적이지
않다. 그런데 **분석에 필요한 것은 run 이 아니라 KPI 표**다 — 신뢰구간, 계단별
효과, 손익분기는 전부 이 표에서 나온다.

그래서 저장된 run 에서 KPI 만 뽑아 **CSV 세 장 + README** 로 묶는다. 받는 쪽은
pandas 만 있으면 된다.

    ladder_seeds.csv     시드 × 칸 한 줄씩 — **원자료**
    ladder_summary.csv   칸별 평균 · S0 대비 개선율 · 95% 신뢰구간 · 이긴 시드
    ladder_steps.csv     계단별(S0→S1, S1→S2 …) 짝지은 차이와 신뢰구간
    README.txt           무대 설정 · 열 설명 · **하면 안 되는 것**

## ⚠ 이 표로 하면 안 되는 것 (README 에도 박는다)

  · **민폐(한계 외부비용) 합계를 총비용과 섞지 말 것** — 한계값은 더해지지 않는다
  · **다른 인내 설정끼리 총비용을 비교하지 말 것** — 충전 마친 차 수가 다르다
  · **평균 대기로 칸을 비교하지 말 것** — S0 가 가장 긴 줄로 갈 차를 흘리므로
    그 평균은 생존자 평균이다. 총 사회적 비용과 충전 마친 차 수로만 본다
  · **'바닥–천장 격차 축소율' 을 쓰지 말 것** — 바닥과 천장의 이탈 대수가 다르면
    분모가 같은 모집단이 아니다 (지금 무대는 전부 어긋난다)
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections.abc import Sequence

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import RUNS_DIR  # noqa: E402
from evdt.runner import (  # noqa: E402
    SOCIAL_COST_TERMS,
    load_config,
    make_run_id,
    social_cost_h,
    summarize_kpis,
)

LADDER = ["S0", "S1", "S2", "S3", "S4", "PERFECT_ROLLING"]
NAME = {"PERFECT_ROLLING": "완전예측"}

#: 내보낼 KPI. 총비용의 세 항은 **반드시** 들어간다 (#147)
METRICS = ("n_ev", "n_ev_charging", "n_charge_visits",
           "n_escaped", "n_escaped_balked", "n_escaped_reneged",
           "n_escaped_stranded", "n_ev_no_charge",
           "wait_mean_min", "wait_p95_min", "wait_max_min",
           "wait_worst_station_min", "share_ratio_max", "bottleneck_slots",
           *SOCIAL_COST_TERMS)


def _cfg(path: str, stage: str, ev_share: float | None):
    """config 를 읽되, `--ev-share` 가 있으면 **사다리와 같은 변형**을 만든다.

    `run_chuseok_ladder.py --ev-share` 가 `cfg.variant(f"ev{nn}", ...)` 로
    scenario_id 를 가른다. 내보내기도 같은 이름을 만들어야 그 run 을 찾는다.
    """

    import yaml

    cfg = load_config(path, stage=stage)
    if ev_share is None:
        return cfg
    raw = yaml.safe_load(cfg.raw_yaml)
    layers, found = [], False
    for layer in raw["demand"].get("layers") or []:
        layer = dict(layer)
        if layer.get("kind") == "ev_adoption":
            layer["ev_share"] = ev_share
            found = True
        layers.append(layer)
    if not found:
        raise SystemExit(f"{path} 에 ev_adoption 레이어가 없다")
    return cfg.variant(f"ev{round(ev_share * 100):02d}", {"demand.layers": layers})


def ci95(xs: Sequence[float]) -> tuple[float, float, float]:
    n = len(xs)
    mu = sum(xs) / n
    if n < 2:
        return mu, float("nan"), float("nan")
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    return mu, mu - half, mu + half


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "(알 수 없음)"
    except Exception:
        return "(알 수 없음)"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_chuseok_up_congested.yaml")
    ap.add_argument("--stages", nargs="+", default=LADDER)
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(1, 11)))
    ap.add_argument("--floor", default="S0")
    ap.add_argument("--out", default=None,
                    help="내보낼 폴더 (기본 docs/handoff/ladder_<scenario_id>)")
    # ★ `run_chuseok_ladder.py --ev-share` 와 **같은 변형**을 만들어야 한다.
    #   안 그러면 15% run 을 ev10 폴더에 담는다 (2026-11-13 에 실제로 그랬다)
    ap.add_argument("--ev-share", type=float, default=None,
                    help="EV 보급 레이어 비율. 사다리를 --ev-share 로 돌렸으면 "
                         "여기에도 같은 값을 줘야 한다")
    args = ap.parse_args()

    base = _cfg(args.config, args.stages[0], args.ev_share)
    out = ROOT / (args.out or f"docs/handoff/ladder_{base.scenario_id}")
    out.mkdir(parents=True, exist_ok=True)

    print(f"\n사다리 CSV 내보내기 · {base.label}")
    print(f"  시드 {args.seeds} · 칸 {args.stages}\n")

    # -- 1) 시드 × 칸 원자료 ------------------------------------------------
    rows, missing = [], []
    for seed in args.seeds:
        for stage in args.stages:
            cfg = _cfg(args.config, stage, args.ev_share)
            rid = make_run_id(cfg.scenario_id, stage, seed, cfg.policy.participation)
            if not (RUNS_DIR / rid / "charge_event.parquet").exists():
                missing.append(rid)
                continue
            s = summarize_kpis([rid]).set_index("metric")["mean"]
            rows.append({
                "scenario_id": cfg.scenario_id, "stage": stage,
                "stage_label": NAME.get(stage, stage), "seed": seed, "run_id": rid,
                "total_social_cost_h": social_cost_h(s),
                **{m: float(s[m]) for m in METRICS if m in s.index},
            })

    if missing:
        print(f"  ⚠ 저장된 결과가 없는 run {len(missing)}개 — 빼고 내보낸다")
        for rid in missing[:8]:
            print(f"      {rid}")
    if not rows:
        print("\n쓸 수 있는 run 이 없다.")
        return 1

    seeds_df = pd.DataFrame(rows)
    seeds_df.to_csv(out / "ladder_seeds.csv", index=False, encoding="utf-8-sig")
    print(f"  ladder_seeds.csv      {len(seeds_df):>4}행")

    # -- 2) 칸별 요약 --------------------------------------------------------
    w = seeds_df.pivot(index="seed", columns="stage", values="total_social_cost_h")
    floor = args.floor
    done = [s for s in args.stages if s in w.columns and w[s].notna().all()]
    summary = []
    for stage in done:
        d = seeds_df[seeds_df.stage == stage]
        rec = {"stage": stage, "stage_label": NAME.get(stage, stage),
               "n_seeds": len(d), "total_social_cost_h_mean": d.total_social_cost_h.mean()}
        if stage != floor and floor in w.columns:
            imp = [(w[floor][k] - w[stage][k]) / w[floor][k] * 100 for k in w.index]
            mu, lo, hi = ci95(imp)
            rec.update(vs_floor_pct=mu, ci95_lo_pct=lo, ci95_hi_pct=hi,
                       seeds_won=sum(1 for x in imp if x > 0),
                       verdict=("유의" if lo > 0 else "동점" if hi > 0 else "역전"))
        for m in ("dwell_total_h", "escape_cost_total_h", "renege_wait_total_h",
                  "n_escaped", "n_escaped_balked", "n_escaped_reneged",
                  "wait_mean_min", "wait_p95_min", "bottleneck_slots"):
            if m in d.columns:
                rec[f"{m}_mean"] = d[m].mean()
        if {"n_ev_charging", "n_escaped"} <= set(d.columns):
            rec["served_mean"] = (d.n_ev_charging - d.n_escaped).mean()
        summary.append(rec)
    pd.DataFrame(summary).to_csv(out / "ladder_summary.csv", index=False,
                                 encoding="utf-8-sig")
    print(f"  ladder_summary.csv    {len(summary):>4}행")

    # -- 3) 계단별 짝지은 차이 ----------------------------------------------
    steps = []
    for a, b in zip(done, done[1:], strict=False):
        d = [(w[a][k] - w[b][k]) / w[a][k] * 100 for k in w.index]
        mu, lo, hi = ci95(d)
        steps.append({"from_stage": a, "to_stage": b,
                      "from_label": NAME.get(a, a), "to_label": NAME.get(b, b),
                      "n_seeds": len(d), "effect_pct": mu,
                      "ci95_lo_pct": lo, "ci95_hi_pct": hi,
                      "seeds_won": sum(1 for x in d if x > 0),
                      "verdict": "유의" if lo > 0 else "동점" if hi > 0 else "역전"})
    pd.DataFrame(steps).to_csv(out / "ladder_steps.csv", index=False,
                               encoding="utf-8-sig")
    print(f"  ladder_steps.csv      {len(steps):>4}행")

    # -- 4) README -----------------------------------------------------------
    d = base.demand
    readme = f"""사다리 결과 CSV 묶음
{'=' * 72}

무대: {base.label}
  scenario_id        {base.scenario_id}
  config             {args.config}
  기간 · 방향         {d.period} · {base.direction}
  수요 프로파일       {d.volume_profile}
  기온               {base.environment.temp_c}°C
  EV 레이어          {base.demand_label}
  우회 비용           {d.escape_cost_min:g}분 (산포 sigma {d.escape_cost_sigma:g})
  포기(renege)       {d.renege}
  인내               {d.patience_kind}(shape {d.patience_shape:g}) ·
                     중앙값 {d.patience_median_min or d.escape_cost_min:g}분
  통행시간           {d.travel_time} · {d.cruise_speed_kmh:g} km/h
  시드               {sorted(seeds_df.seed.unique())}
  커밋               {git_commit()}

파일
  ladder_seeds.csv     시드 × 칸 한 줄씩 — **원자료**. 다시 집계할 때 이걸 쓴다
  ladder_summary.csv   칸별 평균 · S0 대비 개선율 · 95% 신뢰구간 · 이긴 시드
  ladder_steps.csv     계단별(S0→S1, S1→S2 …) 짝지은 차이와 신뢰구간
  run_params.json      무대 설정을 기계가 읽을 수 있게

핵심 열
  total_social_cost_h   **총 사회적 비용** = 체류 + 이탈 비용 + 포기 전 대기.
                        칸 비교는 **이 열로만** 한다
  dwell_total_h         총 체류
  escape_cost_total_h   이탈 비용 합 (차마다 제 우회 비용을 더한 값)
  renege_wait_total_h   포기하기 전까지 기다린 시간 합
  n_escaped             이탈 합계 = balked + reneged + stranded
    _balked             줄이 길어서 애초에 안 섰다
    _reneged            줄에서 기다리다 인내가 끝나 떠났다
    _stranded           정책이 계획 불가인 곳으로 몰아넣었다
  bottleneck_slots      포화된 (휴게소 × 시간칸) 수
  served_mean           충전 마친 차 = n_ev_charging − n_escaped

신뢰구간 읽는 법
  시드별로 **짝지어** 차이를 내고 그 평균의 95% 구간이다.
  구간이 0 을 포함하면 **"동점"** 이라고 쓴다. "조금 낫다" 라고 쓰지 않는다.

{'=' * 72}
⚠ 이 표로 하면 안 되는 것
{'=' * 72}

1. **평균 대기로 칸을 비교하지 말 것.**
   S0 는 가장 긴 줄로 갈 차를 가장 많이 흘린다. 그래서 S0 의 평균 대기는
   **생존자 평균**이고, 더 많은 차를 붙잡은 칸보다 좋아 보인다.
   총 사회적 비용과 `served_mean` 으로만 비교한다.

2. **다른 인내·이탈 설정끼리 총비용을 비교하지 말 것.**
   설정이 바뀌면 충전 마친 차 수가 바뀐다 — 모집단이 다른 두 수를 견주는 것이
   된다. 이 묶음은 **한 설정 안에서만** 유효하다.

3. **'바닥–천장 격차 축소율(구간의 N%)' 을 쓰지 말 것.**
   바닥과 천장의 이탈 대수가 다르면 분모가 같은 모집단이 아니다. 지금 무대는
   모든 칸이 그 허용(5대)을 넘는다.

4. **민폐(한계 외부비용) 합계를 총비용과 섞지 말 것.**
   한계값은 더해지지 않는다. 합계가 총 체류의 몇 배로 나온다
   (`scripts/externality_rank.py` 머리말 참조).

5. **완전예측은 S2 의 천장이지 S4 의 천장이 아니다.**
   `runner.py` 에서 `PERFECT_ROLLING` 은 S2Policy 로 떨어진다 — **S2 구조 +
   완전한 미래**다. S4 가 그걸 넘어도 고장이 아니다.

{'=' * 72}
다시 집계하려면 (pandas 만 있으면 된다)
{'=' * 72}

    import pandas as pd
    t = pd.read_csv("ladder_seeds.csv")
    w = t.pivot(index="seed", columns="stage", values="total_social_cost_h")
    imp = (w["S0"] - w["S4"]) / w["S0"] * 100      # 시드별 개선율
    print(imp.mean(), imp.std(ddof=1) / len(imp) ** .5 * 1.96)
"""
    (out / "README.txt").write_text(readme, encoding="utf-8")
    (out / "run_params.json").write_text(json.dumps({
        "scenario_id": base.scenario_id, "config": args.config,
        "label": base.label, "period": d.period, "direction": base.direction,
        "temp_c": base.environment.temp_c, "demand_label": base.demand_label,
        "escape_cost_min": d.escape_cost_min, "escape_cost_sigma": d.escape_cost_sigma,
        "renege": d.renege, "patience_kind": d.patience_kind,
        "patience_shape": d.patience_shape,
        "patience_median_min": d.patience_median_min or d.escape_cost_min,
        "travel_time": d.travel_time, "cruise_speed_kmh": d.cruise_speed_kmh,
        "seeds": sorted(int(s) for s in seeds_df.seed.unique()),
        "commit": git_commit(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  README.txt · run_params.json")

    print(f"\n내보냈다: {out}")
    if missing:
        print(f"⚠ run {len(missing)}개가 빠졌다 — 사다리를 마저 돌린 뒤 다시 내보낼 것")
    return 0


if __name__ == "__main__":
    sys.exit(main())
