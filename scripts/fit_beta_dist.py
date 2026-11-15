"""N2 — 거리 항 β_d 를 **줄이 없는 무대**에서 적합한다.

    python scripts/fit_beta_dist.py
    python scripts/fit_beta_dist.py --grid -0.40 0.40 0.05 --seeds 5

## 왜 여기서 적합하나 — 식별

ASC 는 **13개**짜리라 무엇이든 맞출 수 있었고, 그래서 무대가 바뀌면 값이
따라 움직였다 (`docs/mnl_redesign_plan.txt` 5-8). β_d 는 **1개**다.

그리고 적합 무대를 실측 재현(`scenario_seollal_up.yaml`, 가정 레이어 없음)
으로 잡는다. 거기서는 대기 평균이 **1.2분**이라 비용의 wait 항이 사실상 0 이다.
남은 구조는 거의 전부 **위치와 출력**이고, 그중 위치를 β_d 가 가져간다.
혼잡한 무대에서 적합하면 큐 모형의 오차까지 β_d 가 뒤집어쓴다 — ASC 가 당한 일이다.

## 목표를 무엇으로 두나 — 평시 18일

#167 원장은 2026-02-01~28 **28일**이고, 그중 연휴는 10일(2/13~22)뿐이다.
**평시 18일(평일 14 · 주말 4)을 적합에 쓰고, 연휴 10일은 손대지 않는다.**

재 보면 두 모양이 거의 같다 (평일↔연휴 피어슨 +0.954). 그래서 이 분할은
"다른 패턴을 배운다" 가 아니라 **"혼잡 체제를 가로지르는 검증"** 이다 —
ASC 가 정확히 거기서 깨졌다.

## 분모 — 공공 100% 휴게소만

실측은 환경부 충전기만 담는다. 보급률이 휴게소마다 달라서 보정 없이 견주면
**모델 오차가 아니라 보급률 차이**를 잰다 (`diagnose_choice_fit.py` 머리말).
공공 100% 다섯 곳은 분모가 같아서 가정 없이 견줄 수 있다.

## ⚠ ASC 를 끈 채로 적합한다

ASC 13개가 거리를 흉내 낼 수 있어 같이 넣으면 β_d 가 식별되지 않는다.
N3 에서 β_d 를 고정한 **뒤에** ASC 를 다시 적합해 폭이 줄어드는지 본다 —
줄어든 몫이 "ASC 가 거리를 대신 들고 있던 양" 이다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

OBS_DAILY = "data/processed/observed_station_daily.csv"
OBS_STATIONS = "data/processed/observed_station_daily__stations.csv"
COVERAGE = "data/processed/me_coverage_gyeongbu.csv"

#: 연휴로 쓴 날. 나머지가 평시다
HOLIDAY = [f"2026-02-{d:02d}" for d in range(13, 23)]
#: 2월만 쓴다 (3/1 은 9곳뿐이라 하루가 반쪽이다)
LAST_DAY = "2026-02-28"

#: 원장 충전기 / API 충전기 가 이 위면 "분모가 같다"
PURE_FRAC = 0.999


def observed_shape(group: str) -> pd.Series:
    """`평시` 또는 `연휴` 의 **1기당 하루 세션** — 공공 100% 휴게소, 평균으로 나눈 모양."""

    d = pd.read_csv(ROOT / OBS_DAILY)
    st = pd.read_csv(ROOT / OBS_STATIONS)
    cov = pd.read_csv(ROOT / COVERAGE)

    pure = set(cov.loc[(cov.me_frac >= PURE_FRAC) & (cov["dir"] == "UP"), "name"])
    up = st[(st.direction == "UP") & st.name.isin(pure)]
    if len(up) < 3:
        raise SystemExit(f"[중단] 공공 100% 상행 휴게소가 {len(up)}곳뿐이다 — 적합 불가")

    d = d[(d.date <= LAST_DAY) & d.station_id.isin(set(up.station_id))]
    sel = d.date.isin(HOLIDAY) if group == "연휴" else ~d.date.isin(HOLIDAY)
    d = d[sel]

    units = up.set_index("station_id")["ledger_chargers_feb"]
    name = up.set_index("station_id")["name"]
    g = d.groupby("station_id").agg(s=("starts", "sum"), days=("date", "nunique"))
    load = g["s"] / units.reindex(g.index) / g["days"]
    load.index = load.index.map(name)
    return (load / load.mean()).sort_index()


def model_shape(out_dir: Path, units: pd.Series, names: pd.Series) -> pd.Series:
    """모델의 1기당 부하 모양 (휴게소 **이름** 색인). 시드를 평균한다.

    `units`·`names` 는 둘 다 station_id 색인이다 — 모델 parquet 가 id 를 쓰고
    실측 목표가 이름을 쓰기 때문에, 나누는 것은 id 로 하고 마지막에 이름으로 바꾼다.
    """

    acc = []
    for s in range(1, 41):
        p = out_dir / f"seed{s}" / "MNL.parquet"
        if p.is_file():
            acc.append(pd.read_parquet(p).groupby("station_id").size())
    if not acc:
        raise SystemExit(f"[중단] {out_dir} 에 MNL.parquet 가 없다")
    sess = pd.concat(acc, axis=1).sum(axis=1) / len(acc)
    load = sess.reindex(units.index).fillna(0.0).astype(float) / units
    load.index = load.index.map(names)
    return (load / load.mean()).sort_index()


def mape(model: pd.Series, obs: pd.Series) -> float:
    idx = [i for i in obs.index if i in model.index]
    m, o = model.loc[idx], obs.loc[idx]
    return float(((m - o).abs() / o).mean() * 100)


def run_one(beta: float, cfg: str, seeds: int, theta: float, root: Path) -> Path:
    out = root / f"b{beta:+.3f}".replace(".", "p")
    if (out / f"seed{seeds}" / "MNL.parquet").is_file():
        return out                                  # 이미 돌렸다
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "run_mnl_world.py"),
           "--config", cfg, "--seeds", f"1-{seeds}", "--p", "1.0",
           "--theta", str(theta), "--wait-weight", "1.0", "--floor-only",
           "--beta-dist", str(beta), "--out", str(out)]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,  # noqa: S603
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"[중단] β={beta} 실행 실패:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up.yaml",
                    help="줄이 없는 재현 무대. 혼잡한 무대로 적합하면 β_d 가 "
                         "큐 모형의 오차까지 뒤집어쓴다")
    ap.add_argument("--grid", type=float, nargs=3, default=[-0.40, 0.40, 0.05],
                    metavar=("LO", "HI", "STEP"))
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--theta", type=float, default=0.05)
    ap.add_argument("--out", default="runs/mnl_world/beta_dist_fit")
    args = ap.parse_args()

    st = pd.read_csv(ROOT / OBS_STATIONS)
    cov = pd.read_csv(ROOT / COVERAGE)
    pure = set(cov.loc[(cov.me_frac >= PURE_FRAC) & (cov["dir"] == "UP"), "name"])
    up = st[(st.direction == "UP") & st.name.isin(pure)]
    units = up.set_index("station_id")["ledger_chargers_feb"]
    units.index.name = "station_id"
    nm = up.set_index("station_id")["name"]

    fit_obs, val_obs = observed_shape("평시"), observed_shape("연휴")
    print(f"목표 — 공공 100% {len(up)}곳 · 평균 대비 1기당 부하")
    print(f"  {'휴게소':<14}{'평시(적합)':>12}{'연휴(검증)':>12}")
    for n in fit_obs.index:
        print(f"  {n[:13]:<14}{fit_obs[n]:>12.3f}{val_obs.get(n, float('nan')):>12.3f}")
    print(f"\n  적합 평시 18일 · 검증 연휴 10일 "
          f"(두 모양 상관 {fit_obs.corr(val_obs):+.3f})")

    lo, hi, step = args.grid
    betas = [round(lo + i * step, 4) for i in range(int(round((hi - lo) / step)) + 1)]
    root = ROOT / args.out
    root.mkdir(parents=True, exist_ok=True)

    print(f"\n격자 {len(betas)}점 · 시드 {args.seeds} · {args.config}")
    print(f"  {'β_d (분/km)':>12}{'적합 MAPE':>11}{'검증 MAPE':>11}   비고")
    rows = []
    for b in betas:
        out = run_one(b, args.config, args.seeds, args.theta, root)
        ms = model_shape(out, units, nm)
        rows.append({"beta": b, "fit": mape(ms, fit_obs), "val": mape(ms, val_obs)})
        print(f"  {b:>12.3f}{rows[-1]['fit']:>10.0f}%{rows[-1]['val']:>10.0f}%")

    t = pd.DataFrame(rows)
    best = t.loc[t["fit"].idxmin()]
    base = t.loc[(t["beta"] - 0.0).abs().idxmin()]
    print(f"\n★ 최적 β_d = **{best.beta:+.3f} 분/km**  "
          f"(적합 MAPE {best.fit:.0f}% · 검증 MAPE {best.val:.0f}%)")
    print(f"  거리 항 없음(β=0) 대비  적합 {base.fit:.0f}% → {best.fit:.0f}% · "
          f"검증 {base.val:.0f}% → {best.val:.0f}%")
    if best.beta < 0:
        print("  → 부호가 **음수**다: 운전자는 **첫 휴게소에서 안 서고 더 간다.**")
    elif best.beta > 0:
        print("  → 부호가 **양수**다: 운전자는 **가까운 데서 빨리 끊는다.**")
    else:
        print("  ⚠ 0 이 최적이다 — 거리 항이 설명하는 몫이 없다")
    print(f"  100 km 더 가는 값 = {abs(best.beta) * 100:.0f}분 "
          f"({'멀수록 싫다' if best.beta > 0 else '멀수록 좋다'})")

    csv = root / "grid.csv"
    t.to_csv(csv, index=False, encoding="utf-8-sig")
    (root / "best.json").write_text(
        json.dumps({"beta_dist_min_per_km": float(best.beta),
                    "fit_mape": float(best.fit), "val_mape": float(best.val),
                    "config": args.config, "seeds": args.seeds,
                    "fit_days": "평시 18일", "val_days": "연휴 10일"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  격자: {csv}\n  최적: {root / 'best.json'}")
    print("\n  ⚠ 다음은 N3 — 이 β_d 를 고정하고 ASC 를 다시 적합해 **폭이 191분에서**"
          "\n    얼마나 줄어드는지 본다. 줄어든 몫이 ASC 가 거리를 대신 들고 있던 양이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
