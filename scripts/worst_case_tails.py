"""EV 보급률별 **최악의 상황** — 평균이 아니라 꼬리로 사다리를 읽는다.

    python scripts/worst_case_tails.py

`scripts/ev_share_stages.py` 가 **총 사회비용(평균)** 으로 재는 같은 네 칸을,
여기서는 **차 한 대가 받는 피해의 꼬리**로 다시 잰다. 둘의 결론이 다르다:

    EV 10% · 평균   S4 851.5h vs S2 848.0h  → **S4 가 0.30%p 나쁘다**
    EV 10% · 꼬리   S4 3.4대  vs S2 4.2대   → **S4 가 0.80대 낫다** (6/10 시드)

평균으로 고르면 S2, 최악의 상황으로 고르면 S4 다. **어느 쪽으로 고를지는
목적함수가 정하는 게 아니라 우리가 정한다** — 그래서 둘 다 찍는다.

## 왜 평균이 5·10% 에서 사다리를 숨기나

사회비용 3항은 (체류 · 이탈 · 포기대기) 이고 **체류가 거의 전부**다 — EV 5% 바닥에서
499.8h / 502.7h = 99.4%. 체류는 `대기 + 충전` 이고 저보급에서는 충전 시간이 지배한다.
충전 시간은 **엔진이 못 줄인다** (에너지가 정해져 있다). 그래서 평균을 보면 엔진이
건드릴 수 있는 몫 자체가 작아 보인다. 꼬리는 반대다 — 꼬리는 전부 대기와 이탈이다.

## 차 한 대가 받는 피해(분) 의 정의

    충전을 마친 차   그 차의 `wait_min` 합 (여러 번 섰으면 더한다)
    이탈한 차        `escape_cost_min` + `waited_min`
                     (줄 서다 포기했으면 기다린 시간까지 치른다)

**충전 시간은 안 넣는다.** 충전은 엔진이 줄일 수 없고, 넣으면 꼬리가 "배터리가 큰 차"
순위가 되어 혼잡을 안 본다.

## ⚠ 도로 정체는 이 표에 없다

config 가 `travel_time: fixed` 다 — CTM 이 추석 정체를 못 재현했고(#160: 저속 821칸
중 적중 1칸 · Recall 0.0012) 실측 속도장을 꽂는 입구가 아직 없다. 비전기차는
**통과 교통량**으로만 들어와 EV 대수를 정하고, 도로 지체는 만들지 않는다.

즉 이 표의 "최악"은 **휴게소 충전 대기·이탈**의 최악이고, 실제 추석에 사람들이
도로에서 보낸 시간은 **사회비용 3항 바깥**이다. 발표에서 그걸 먼저 말한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: F401

#: (보급률 라벨, run_id 꼬리표, MNL 바닥 디렉터리) — 15% 는 config 기본값이라 꼬리표가 없다
SHARES = (
    ("3.7% (실측 비율)", "ev04", "chuseok_ev037_floor"),
    ("5%", "ev05", "chuseok_ev05_floor"),
    ("10%", "ev10", "chuseok_ev10_floor"),
    ("15% (발표 본 무대)", None, "chuseok_congested_floor/theta0p05_w1p0"),
)
STAGES = ("MNL", "S0", "S2", "S4")
SEEDS = tuple(range(1, 11))

#: 꼬리를 세는 문턱(분). 15 는 "줄을 봤다", 30 은 "일정이 깨졌다", 60 은 "사실상 이탈"
THRESHOLDS = (15, 30, 60)


def paired_ci(x) -> tuple[float, float, float]:
    """짝지은 비교의 평균과 95% CI. 시드가 하나면 CI 가 없다 (nan)."""

    x = np.asarray(x, float)
    if len(x) < 2:
        return float(x.mean()), float("nan"), float("nan")
    se = x.std(ddof=1) / np.sqrt(len(x))
    return float(x.mean()), float(x.mean() - 1.96 * se), float(x.mean() + 1.96 * se)


def _read(path):
    """없으면 None — 이탈이 0이면 `escape_event.parquet` 를 안 쓴다."""

    return pd.read_parquet(path) if path.exists() else None


def load(tag: str | None, floor: str, stage: str, seed: int):
    """(충전 이벤트, 이탈 이벤트). MNL 바닥은 다른 곳에 다른 이름으로 있다."""

    if stage == "MNL":
        d = ROOT / "runs/mnl_world" / floor / f"seed{seed}"
        return _read(d / "MNL.parquet"), _read(d / "MNL__escape.parquet")
    name = "chuseok_2026_up_congested__" + (f"{tag}__" if tag else "") + f"{stage}__p100"
    d = ROOT / "runs" / f"{name}__s{seed:04d}"
    return _read(d / "charge_event.parquet"), _read(d / "escape_event.parquet")


def penalty(ce: pd.DataFrame, esc: pd.DataFrame | None) -> np.ndarray:
    """차 한 대가 받는 피해(분). 모듈 docstring 의 정의를 그대로 쓴다."""

    per_car = ce.groupby("ev_id").wait_min.sum()
    if esc is not None and len(esc):
        left = (esc.escape_cost_min.fillna(0) + esc.waited_min.fillna(0))
        left = left.groupby(esc.ev_id).sum()
        per_car = pd.concat([per_car, left]).groupby(level=0).sum()
    return per_car.to_numpy(dtype=float)


def metrics(ce: pd.DataFrame, esc: pd.DataFrame | None) -> dict[str, float]:
    v = penalty(ce, esc)
    # 최악 휴게소: 역별 p95 대기 중 최댓값. config 가 "평균보다 P95·최악 휴게소" 라고 적어 뒀다
    by_station = ce.groupby("station_id").wait_min.quantile(0.95)
    out = {f"over{t}": float((v > t).sum()) for t in THRESHOLDS}
    out.update(
        n_escaped=0.0 if esc is None else float(len(esc)),
        p95=float(np.percentile(v, 95)),
        worst_station=float(by_station.max()),
        n_bad_stations=float((by_station > 30).sum()),
        n_stations=float(len(by_station)),
        n_cars=float(len(v)),
    )
    return out


def collect(tag: str | None, floor: str) -> dict[str, dict[str, np.ndarray]]:
    """단계 → 지표 → 시드별 값."""

    out: dict[str, dict[str, np.ndarray]] = {}
    for stage in STAGES:
        rows = [metrics(*load(tag, floor, stage, s)) for s in SEEDS]
        out[stage] = {k: np.array([r[k] for r in rows], float) for k in rows[0]}
    return out


def main() -> int:
    print("추석 2026-09-25 상행 무대 · 차 한 대가 받는 피해(분) 의 꼬리 · 10시드 평균")
    print("바닥 = 각 보급률의 MNL (θ=0.05 · w=1.0 · ASC 없음)")
    print("⚠ 도로 정체는 무대에 없다 (travel_time: fixed) — 여기 혼잡은 충전 대기뿐이다")

    everything = {}
    for label, tag, floor in SHARES:
        g = everything[label] = collect(tag, floor)
        n_st = int(g["S0"]["n_stations"][0])
        n_cars = int(g["S0"]["n_cars"][0])
        print()
        print("=" * 88)
        print(f"EV {label}   — 충전한 차 {n_cars:,}대 · 휴게소 {n_st}곳")
        print(f"  {'단계':<5}{'>15분':>9}{'>30분':>9}{'>60분':>9}{'이탈':>9}"
              f"{'p95':>9}{'최악휴게소':>12}{'p95>30역':>10}")
        for stage in STAGES:
            m = g[stage]
            print(f"  {stage:<5}{m['over15'].mean():8.1f}대{m['over30'].mean():8.1f}대"
                  f"{m['over60'].mean():8.1f}대{m['n_escaped'].mean():8.1f}대"
                  f"{m['p95'].mean():8.1f}분{m['worst_station'].mean():11.1f}분"
                  f"{m['n_bad_stations'].mean():9.1f}곳")

        # 사다리가 꼬리에서 갈라지는지 — 짝지은 시드로만 말할 수 있다
        for lo, hi in (("MNL", "S0"), ("S0", "S2"), ("S2", "S4")):
            d = g[lo]["over15"] - g[hi]["over15"]
            mean, low, high = paired_ci(d)
            flag = "" if low > 0 or high < 0 else "   ⚠ 구간이 0 을 포함 — 못 가른다"
            print(f"  {lo} → {hi}  >15분 {-mean:+8.2f}대 [{-high:+.2f}, {-low:+.2f}]"
                  f"  {hi} 가 나은 시드 {int((d > 0).sum())}/10{flag}")

    print()
    print("=" * 88)
    print("★ 평균과 꼬리가 다른 것을 말한다")
    print("-" * 88)
    for label in everything:
        g = everything[label]
        base, s2, s4 = g["MNL"]["over15"], g["S2"]["over15"], g["S4"]["over15"]
        cut = 100.0 * (1.0 - s4.mean() / base.mean()) if base.mean() else float("nan")
        mean, low, high = paired_ci(s2 - s4)
        verdict = "S4" if low > 0 else ("S2" if high < 0 else "못 가른다")
        print(f"  EV {label:<18} 바닥 {base.mean():7.1f}대 → S4 {s4.mean():6.1f}대"
              f"  (꼬리 {cut:5.1f}% 감소)   꼬리로 고르면 {verdict}")
    print()
    print("  ⚠ `scripts/ev_share_stages.py` 는 **평균**으로 재고 EV 10% 에서 S2 를")
    print("    고른다. 같은 칸을 꼬리로 재면 S4 다. 어느 쪽을 쓸지는 우리가 정한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
