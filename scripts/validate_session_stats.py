"""세션 수준 실측 대조 — 설 충전이력(#167) 7항목의 **모델값**을 채운다 (발표 §6).

    python scripts/validate_session_stats.py
    python scripts/validate_session_stats.py --up seollal_2026_up_base__S0__p100 --seeds 1-5

`docs/core_validation_table_153.md` 의 #167 대조표는 7항목 모두 모델값이
**"재집계 필요"**, 판정이 **"보류"** 다. 실측 기준값은 단단한데(원장 SHA-256 까지
적혀 있다) 모델값만 없다. 그걸 센다.

## ★ 무엇을 견줄 수 있고 무엇을 견줄 수 없나

이걸 먼저 가르지 않으면 §6 이 질의응답에서 무너진다.

  견줄 수 있다 — **세션당 분포**
    세션당 충전량 (kWh)      차 한 대가 한 번에 얼마나 넣나
    세션 경과시간 (분)        한 번 꽂으면 얼마나 걸리나

    둘 다 **대수에 무관**하다. EV 가 몇 대든 한 대가 넣는 양과 걸리는 시간은
    같아야 한다. 그래서 EV 보급률이 가정이어도 이 둘은 검증이 된다.

  견줄 수 없다 — **총량**
    충전 시작 건수 · 총 점유시간

    두 가지 이유로 막힌다:
      ① EV 보급률이 **가정**이다 (config label 에 "⚠ 실측 비율 아님")
      ② 실측은 환경부 제공 이력이고 모델은 **전체 운영사**다. #64 의 판정
         보류 사유가 그것이다 — "총량은 판정에 쓰지 않는다"

    총량을 맞추려고 EV 보급률을 조절하면 **맞춘 것으로 검증하는** 순환이 된다.

  반쯤 — **최대 동시 충전**
    대수에 딸려 가므로 총량과 같은 한계를 받는다. 다만 **충전기 수를 넘지
    않는가** 는 물리 검사로 쓸 수 있다 (넘으면 버그다).

## ★ 세션 분포가 안 맞는 것은 **이미 아는 결함**이다 — 새 발견이 아니다

  `docs/charge_amount.md` 의 #136 갱신이 같은 것을 쟀다 (base UE 40런 · 86,133 세션):

      세션 충전량   모델 13.358 kWh  vs 실측 22.093   −39.5%
      세션 시간     모델   9.60분    vs 실측  27.12분  −64.6%

  원인도 적혀 있다:

    · `habit_soc`(사람처럼 가득 채우는 습관)가 #113 때문에 **모든 시나리오에서
      꺼져 있다**. 그래서 차들이 "목적지까지 갈 만큼만" 채운다
    · 그렇다고 목표 SoC 를 0.80 으로 올리는 보정은 **기각됐다** (#136). 그러면
      세션당 34 kWh 가 되는데 실측은 22.093 이다 — 1.54배 과하다
    · 그리고 충전량을 실측에 맞춰도 시간의 **41.5% 가 설명되지 않는다.** 노브
      하나로 메울 수 없다는 뜻이다. 보정은 ① 충전량 ② 출력·세션 오버헤드 로 쪼갠다

  이 스크립트는 그 결함을 **지금 config 에서 다시 재서 숫자를 확정**한다. 발표
  §6 에서 "검증했다" 가 아니라 **"이만큼 어긋나 있고 원인을 안다"** 로 말해야
  하는 항목이다.

## 경과시간 ↔ 어느 열인가

실측 "세션 경과시간" 은 **충전기 원장의 종료 − 시작**이다. 충전기는 차가 꽂힌
뒤부터 안다 — 줄 서 있던 시간은 모른다. 그래서

    경과시간  ↔  `charge_min`      (충전만)
    ✗ 아니다  ↔  `dwell_min`       (= wait_min + charge_min, 줄 포함)

`dwell_min` 으로 견주면 모델이 실측보다 긴 것처럼 보인다. 대기를 실측에 없는
항목과 견주는 셈이다.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import RUNS_DIR  # noqa: E402

#: #167 실측 기준값 (docs/core_validation_table_153.md §2).
#: 기간 2026-02-13~22 (10일) · 경부 UP/DOWN 관측 29곳 · 시작 세션 11,405건
OBSERVED = {
    "energy_mean": 22.093, "energy_p50": 20.490, "energy_p90": 39.540,   # kWh
    "elapsed_mean": 27.117, "elapsed_p50": 26.667, "elapsed_p90": 40.650,  # 분
    "starts_total": 11405, "starts_up": 6577, "starts_down": 4828,
    "occupied_min_total": 308475.37,
    "max_concurrent_station": ("칠곡휴게소", "DOWN", 8),
}

#: 재현 무대가 되는 날 (`validate_mnl_seollal.py` 와 같다)
REPLAY_DAY = {"up": "2026-02-17", "down": "2026-02-13"}
#: 그 날의 실측 (core_validation_table_153.md §3)
OBSERVED_DAY = {"up": (930, 23796.45), "down": (552, 15321.15)}

#: 판정 기준 — 상대오차. 세션 분포는 10% 안이면 맞춘 것으로 본다
TOL_OK, TOL_WEAK = 0.10, 0.20


def sessions(run_id: str, seeds: list[int]) -> pd.DataFrame:
    """여러 시드의 충전 이벤트를 모은다. 시드 열을 남긴다."""

    frames = []
    for s in seeds:
        p = RUNS_DIR / f"{run_id}__s{s:04d}" / "charge_event.parquet"
        if not p.is_file():
            continue
        f = pd.read_parquet(p)
        f["seed"] = s
        frames.append(f)
    if not frames:
        raise SystemExit(f"[중단] {run_id}__s* 의 charge_event.parquet 를 못 찾았다")
    return pd.concat(frames, ignore_index=True)


def max_concurrent(f: pd.DataFrame) -> pd.Series:
    """휴게소별 **최대 동시 충전 세션 수** — 시드마다 따로 세고 평균한다.

    시작 +1 / 종료 −1 로 훑는다. 같은 시각에 끝나고 시작하면 **끝나는 것을
    먼저** 처리한다 — 충전기가 비는 순간에 다음 차가 꽂히는 것이 동시 2세션이
    아니기 때문이다 (정렬 키의 두 번째 항이 그 일을 한다).

    ★ **시드를 섞어서 세면 안 된다.** 10시드를 한 표에 쌓아 놓고 훑으면 서로
      다른 세계의 세션이 같은 시각에 겹쳐서, 동시 세션이 정확히 10배로 나온다
      (처음에 그렇게 세어서 "충전기 수를 넘었다 — 버그다" 로 잘못 읽었다.
      190/19 · 128/13 · 80/8 … 전부 시드 수 배수였다. 2026-11-14).
    """

    per_seed = []
    for _, fs in f.groupby("seed"):
        out = {}
        for sid, g in fs.groupby("station_id"):
            ev = [(t, +1) for t in g["t_start_min"]] + [(t, -1) for t in g["t_end_min"]]
            ev.sort(key=lambda x: (x[0], x[1]))      # -1 이 +1 보다 먼저
            cur = peak = 0
            for _, d in ev:
                cur += d
                peak = max(peak, cur)
            out[sid] = peak
        per_seed.append(pd.Series(out, dtype=float))
    return pd.concat(per_seed, axis=1).mean(axis=1)


def verdict(model: float, obs: float) -> tuple[str, float]:
    err = (model - obs) / obs
    mark = "통과" if abs(err) <= TOL_OK else ("약함" if abs(err) <= TOL_WEAK else "미달")
    return mark, err * 100.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--up", default="seollal_2026_up_base__S0__p100")
    ap.add_argument("--down", default="seollal_2026_down_base__S0__p100")
    ap.add_argument("--seeds", default="7", help="쉼표 또는 a-b")
    args = ap.parse_args()

    seeds = ([int(x) for x in args.seeds.split(",")] if "," in args.seeds
             else list(range(int(args.seeds.split("-")[0]),
                             int(args.seeds.split("-")[-1]) + 1)))

    both = {}
    for d, rid in (("up", args.up), ("down", args.down)):
        try:
            both[d] = sessions(rid, seeds)
        except SystemExit as exc:
            print(f"  [건너뜀] {d.upper()} — {exc}")
    if not both:
        return 1
    f = pd.concat(both.values(), ignore_index=True)

    print("\n" + "=" * 78)
    print("설 충전이력 (#167) 세션 수준 대조 — 모델값 재집계")
    print("=" * 78)
    for d, rid in (("up", args.up), ("down", args.down)):
        if d in both:
            n = both[d]["seed"].nunique()
            print(f"  {d.upper():<5}{rid}  시드 {n}개 · {REPLAY_DAY[d]} 재현")

    # --- 1. 견줄 수 있는 것 — 세션당 분포 ----------------------------------
    print("\n### 1. 세션당 분포 — **견줄 수 있다** (대수에 무관)\n")
    print(f"  {'항목':<22}{'실측':>10}{'모델':>10}{'오차':>9}  판정")
    rows = [
        ("세션당 충전량 평균 (kWh)", OBSERVED["energy_mean"], f["energy_kwh"].mean()),
        ("          P50", OBSERVED["energy_p50"], f["energy_kwh"].median()),
        ("          P90", OBSERVED["energy_p90"],
         float(np.percentile(f["energy_kwh"], 90))),
        ("세션 경과시간 평균 (분)", OBSERVED["elapsed_mean"], f["charge_min"].mean()),
        ("          P50", OBSERVED["elapsed_p50"], f["charge_min"].median()),
        ("          P90", OBSERVED["elapsed_p90"],
         float(np.percentile(f["charge_min"], 90))),
    ]
    for name, obs, mod in rows:
        mark, err = verdict(mod, obs)
        print(f"  {name:<22}{obs:>10.3f}{mod:>10.3f}{err:>+8.1f}%  {mark}")

    print(f"\n  참고 — 모델의 `dwell_min`(줄 포함) 평균 {f['dwell_min'].mean():.3f}분. "
          f"실측과 견주면 안 되는 열이다\n  (충전기 원장은 줄 서 있던 시간을 모른다)")

    # --- 2. 견줄 수 없는 것 — 총량 ------------------------------------------
    print("\n### 2. 총량 — **견줄 수 없다**\n")
    print("  ① EV 보급률이 가정이다   ② 실측은 환경부 제공, 모델은 전체 운영사")
    print("  → #64 의 판정 보류 사유 그대로. 참고로만 적는다\n")
    print(f"  {'항목':<22}{'실측':>12}{'모델':>12}{'비율':>9}")
    per_seed = len(f) / f["seed"].nunique()
    obs_day = sum(OBSERVED_DAY[d][0] for d in both)
    occ_day = sum(OBSERVED_DAY[d][1] for d in both)
    mod_occ = f["charge_min"].sum() / f["seed"].nunique()
    print(f"  {'충전 시작 (하루)':<22}{obs_day:>12,.0f}{per_seed:>12,.0f}"
          f"{per_seed / obs_day:>8.2f}x")
    print(f"  {'점유시간 (하루, 분)':<22}{occ_day:>12,.0f}{mod_occ:>12,.0f}"
          f"{mod_occ / occ_day:>8.2f}x")
    print(f"\n  실측 하루 = {' + '.join(f'{d.upper()} {REPLAY_DAY[d]}' for d in both)}")

    # --- 3. 물리 검사 — 최대 동시 ≤ 충전기 수 -------------------------------
    print("\n### 3. 최대 동시 충전 — 총량과 같은 한계. 다만 **물리 검사**로 쓴다\n")
    obs_name, obs_dir, obs_max = OBSERVED["max_concurrent_station"]
    print(f"  실측 최대: {obs_name} {obs_dir} {obs_max}세션")
    bad = []
    for d, g in both.items():
        peak = max_concurrent(g)
        # 쓴 충전기 수도 **시드마다** 세고 평균한다 (같은 이유)
        used = (g.groupby(["seed", "station_id"])["charger_id"].nunique()
                 .groupby("station_id").mean())
        top = peak.sort_values(ascending=False).head(3)
        detail = " · ".join(f"{s.rsplit('_', 1)[-1]} {v:.1f}세션 (충전기 {used[s]:.0f}기)"
                            for s, v in top.items())
        print(f"  모델 {d.upper():<5}{detail}")
        over = {s: (round(v, 1), round(float(used[s]), 1))
                for s, v in peak.items() if v > used[s] + 1e-9}
        if over:
            bad.append((d, over))
    if bad:
        print("\n  ★ 동시 세션이 쓴 충전기 수를 넘었다 — **버그다**:")
        for d, over in bad:
            print(f"    {d}: {over}")
        return 1
    print("\n  동시 세션이 충전기 수를 넘은 휴게소 없음 — 물리 검사 통과")

    print("\n" + "=" * 78)
    print("§6 에 쓸 문장:")
    e_mark, e_err = verdict(f["energy_kwh"].mean(), OBSERVED["energy_mean"])
    t_mark, t_err = verdict(f["charge_min"].mean(), OBSERVED["elapsed_mean"])
    print(f"  세션당 충전량은 실측 {OBSERVED['energy_mean']:.1f} kWh 대비 "
          f"{e_err:+.1f}% ({e_mark}),")
    print(f"  세션 경과시간은 실측 {OBSERVED['elapsed_mean']:.1f}분 대비 "
          f"{t_err:+.1f}% ({t_mark}) 입니다.")
    print("  총량(건수·점유시간)은 EV 보급률이 가정이고 실측이 환경부 제공 이력이라")
    print("  검증 대상에서 **뺍니다** — 맞추려고 보급률을 조절하면 순환이 됩니다.")
    print()
    print("  ★ 이 어긋남은 **이미 아는 결함**이다 (docs/charge_amount.md #136:")
    print("    -39.5% / -64.6%). 원인은 `habit_soc` 가 #113 때문에 꺼져 있어서")
    print("    차들이 '목적지까지 갈 만큼만' 채우는 것이다. 목표 SoC 를 0.80 으로")
    print("    올리는 보정은 세션당 34 kWh (실측의 1.54배) 가 되어 기각됐다.")
    print("    그래서 '검증했다' 가 아니라 **'이만큼 어긋나 있고 원인을 안다'** 로")
    print("    말해야 하는 항목이다.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
