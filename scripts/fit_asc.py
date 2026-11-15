"""휴게소 고정효과(ASC) 적합 — 적합 6일 / 검증 4일 (MNL 재설계 T5).

    python scripts/fit_asc.py --rounds 6
    python scripts/fit_asc.py --rounds 6 --theta 0.1
    python scripts/fit_asc.py --eval-only runs/mnl_world/asc_fit/asc.json

## 왜 ASC 인가 — 다른 축이 모두 닫혔다

  수요 가정 (기회충전)   비용의 82% 를 지지만 포화를 만들어 **모양을 지운다** (5-5)
  세션 양 (habit_soc)   올리면 충전량이 +37% 로 벗어난다. #136 의 기각이 옳았다 (5-6)
  충전 속도 (출력)       같은 출력대에서 **이미 맞는다** (49.9 vs 49.0 kW) (5-7)

남은 오차는 **천안삼거리 실측 2.38 vs 모델 0.83** 이고, 바로 옆 입장거봉포도와
충전기 수·출력·위치가 같다. 용량으로도 출력으로도 지리로도 설명되지 않는다.
**휴게소 선호 말고 설명할 것이 없다.**

## ★ 목표 점유율을 어떻게 만드나 — 원장은 **환경부 충전기만** 담는다

실측 점유율을 그대로 목표로 쓰면 안 된다. 환경부 보급률이 휴게소마다 달라서
(언양 1/19기 · 천안삼거리 3/6기 · 건천 2/2기) 그 차이가 그대로 목표에 섞인다.

그래서 **1기당 부하**로 환산한 뒤 전체 충전기 수로 되곱한다:

    1기당 부하_s   = 실측 세션_s / 원장 충전기_s          (2월 기준)
    환산 총세션_s  = 1기당 부하_s × 전체 충전기_s          (DB, 전 운영사)
    목표 점유율_s  = 환산 총세션_s / Σ 환산 총세션

⚠ **가정**: 한 휴게소 **안에서** 운영사별 1기당 이용률이 같다. 휴게소 **사이**
  의 균일성은 가정하지 않는다 — 그쪽이 우리가 재려는 것이기 때문이다.
  (휴게소 사이까지 균일하다고 가정한 변형은 5-2 ② 에서 더 나빴다)

## 적합 방법 — 로짓 ASC 보정 반복

    ASC_s ← ASC_s − (1/θ) · ln(목표 점유율_s / 모델 점유율_s)

모델이 많이 보내면(모델 > 목표) 로그가 음수라 ASC 가 **올라가고**(= 멀게
느껴지고) 덜 가게 된다. 로짓의 표준 보정이고 보통 몇 번에 수렴한다.

매 번 평균을 0 으로 맞춘다 — 전체에 같은 값을 더해도 선택이 안 바뀌므로
(식별 문제) 그대로 두면 값이 떠돌아다닌다.

## ★ 날짜를 가른다 — 안 가르면 검증이 아니다

    적합  2026-02-13 ~ 18 (6일)
    검증  2026-02-19 ~ 22 (4일)

검증일 점유율은 **적합에 쓰지 않는다.** 적합일 TVD 가 0 에 가까워지는 것은
자유모수가 13개니 당연하다 — **검증일 TVD 만 판정에 쓴다.**
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import default_db_path  # noqa: E402

OBS_DAILY = "data/processed/observed_station_daily.csv"
OBS_STATIONS = "data/processed/observed_station_daily__stations.csv"

#: 적합 / 검증 날짜. #167 의 10일을 6 : 4 로 가른다
FIT_DAYS = [f"2026-02-{d:02d}" for d in range(13, 19)]
VAL_DAYS = [f"2026-02-{d:02d}" for d in range(19, 23)]

#: 보정 한 번에 움직일 수 있는 최대 폭 (분). 첫 회차에 폭주하는 것을 막는다
MAX_STEP_MIN = 25.0


def targets(direction: str = "UP") -> tuple[pd.Series, pd.Series, pd.Series]:
    """(적합일 목표 점유율, 검증일 목표 점유율, 전체 충전기 수) — station_id 색인."""

    daily = pd.read_csv(ROOT / OBS_DAILY)
    summ = pd.read_csv(ROOT / OBS_STATIONS).set_index("station_id")
    daily = daily[daily["direction"] == direction]

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        total = pd.Series(dict(con.execute(
            "SELECT s.station_id, SUM(c.n_units) FROM station s JOIN charger c "
            "ON c.station_id = s.station_id WHERE c.is_active = 1 GROUP BY 1")),
            dtype=float)

    def share(days: list[str]) -> pd.Series:
        g = daily[daily["date"].isin(days)].groupby("station_id")["starts"].sum()
        led = summ["ledger_chargers_feb"].reindex(g.index)
        tot = total.reindex(g.index)
        # 1기당 부하 → 전체 충전기로 환산 (위 docstring 의 가정)
        implied = (g / led) * tot
        implied = implied.dropna()
        return implied / implied.sum()

    return share(FIT_DAYS), share(VAL_DAYS), total


def tvd(model: pd.Series, obs: pd.Series) -> tuple[float, int]:
    idx = [i for i in obs.index if i in model.index]
    if not idx:
        return float("nan"), 0
    a = model.reindex(idx).fillna(0.0).astype(float)
    b = obs.reindex(idx).astype(float)
    if a.sum() <= 0:
        return float("nan"), 0
    return float(((a / a.sum()) - (b / b.sum())).abs().sum() / 2), len(idx)


def run_floor(cfg_path: str, seeds: str, theta: float, wait_weight: float,
              asc: dict[str, float], out: Path, *, quiet: bool = True) -> pd.Series:
    """MNL 바닥을 한 번 돌리고 휴게소별 세션 수를 돌려준다."""

    import run_mnl_world as W

    cfg0 = W.R.load_config(cfg_path)
    acc: list[pd.Series] = []
    log = (lambda *_: None) if quiet else print
    for seed in W.R.parse_seeds(seeds):
        world = W.prepare(cfg0, seed, root=ROOT, log=log)
        mnl = W.MnlSettings(seed=seed, theta=theta, wait_weight=wait_weight,
                            escape_mode="s0", asc=dict(asc))
        _row, events, _esc = W.run_cell(
            world, W.BASE, p=0.0, mnl=mnl, strength=W.DEFAULT_STRENGTH,
            oracle=None, s3_window=None, log=log)
        acc.append(pd.DataFrame(events).groupby("station_id").size())
    out.mkdir(parents=True, exist_ok=True)
    return pd.concat(acc, axis=1).sum(axis=1) / len(acc)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--theta", type=float, default=0.05)
    ap.add_argument("--wait-weight", type=float, default=1.0)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--out", default="runs/mnl_world/asc_fit")
    ap.add_argument("--eval-only", default=None,
                    help="적합하지 않고 이 asc.json 으로 검증만 한다")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / "scripts"))
    out = ROOT / args.out
    fit_t, val_t, _total = targets("UP")

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        nm = dict(con.execute("SELECT station_id, name FROM station"))

    print(f"\n{'=' * 74}")
    print("ASC 적합 — 적합 6일 / 검증 4일")
    print(f"{'=' * 74}")
    print(f"  적합일 {FIT_DAYS[0]} ~ {FIT_DAYS[-1]}  ({len(fit_t)}곳)")
    print(f"  검증일 {VAL_DAYS[0]} ~ {VAL_DAYS[-1]}  ({len(val_t)}곳)")
    print(f"  θ {args.theta:g} · w {args.wait_weight:g} · 시드 {args.seeds}")

    asc: dict[str, float] = {}
    if args.eval_only:
        asc = {k: float(v) for k, v in
               json.loads(Path(args.eval_only).read_text(encoding="utf-8")).items()}
        rounds = 1
    else:
        rounds = args.rounds

    hist = []
    for it in range(rounds + 1):
        sess = run_floor(args.config, args.seeds, args.theta, args.wait_weight,
                         asc, out)
        share = sess / sess.sum()
        f_tvd, f_n = tvd(share, fit_t)
        v_tvd, v_n = tvd(share, val_t)
        spread = (max(asc.values()) - min(asc.values())) if asc else 0.0
        hist.append({"round": it, "fit_tvd": f_tvd, "val_tvd": v_tvd,
                     "asc_spread_min": spread})
        print(f"  회차 {it}  적합 TVD {f_tvd:.4f} (n={f_n})  "
              f"검증 TVD {v_tvd:.4f} (n={v_n})  ASC 폭 {spread:5.1f}분")

        if it == rounds:
            break
        # --- 보정 ----------------------------------------------------------
        idx = [s for s in fit_t.index if s in share.index]
        step = {}
        for s in idx:
            m = max(float(share[s]), 1e-9)
            delta = -(1.0 / args.theta) * math.log(float(fit_t[s]) / m)
            step[s] = max(-MAX_STEP_MIN, min(MAX_STEP_MIN, delta))
        asc = {s: asc.get(s, 0.0) + step[s] for s in idx}
        mean = sum(asc.values()) / len(asc)
        asc = {s: v - mean for s, v in asc.items()}      # 식별: 평균 0

    out.mkdir(parents=True, exist_ok=True)
    (out / "asc.json").write_text(
        json.dumps({k: round(v, 4) for k, v in asc.items()},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(hist).to_csv(out / "history.csv", index=False,
                              encoding="utf-8-sig")

    print("\n=== 적합된 ASC (분 · 양수 = 덜 간다) ===")
    for s, v in sorted(asc.items(), key=lambda x: x[1]):
        print(f"  {nm.get(s, s)[:14]:<15}{v:>+8.1f}분")
    print(f"\n  저장 → {out / 'asc.json'}")

    best = min(hist, key=lambda h: h["val_tvd"])
    print(f"\n  ★ 검증 TVD 가 가장 낮은 회차: {best['round']} "
          f"({best['val_tvd']:.4f}). 회차 0 은 ASC 없는 값이다")
    if best["round"] == 0:
        print("    → **ASC 가 검증일을 개선하지 못했다.** 과적합이다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
