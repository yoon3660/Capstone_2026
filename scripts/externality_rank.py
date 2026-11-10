"""쏠림을 만드는 차를 **실제로 식별할 수 있나** (Q3 차별점의 검정).

    python scripts/externality_rank.py
    python scripts/externality_rank.py --stage S2 --seed 3 --top-pct 5 --reps 20
    python scripts/externality_rank.py --config config/scenario_seollal_down_adoption.yaml

## 무엇을 재나

설계문서 §6.1 의 한계 외부비용을 **실현된 하루**에 대해 차마다 낸다.

    민폐(i) = Σ_j≠i (j 의 종료 시각 | i 가 있음) − Σ_j≠i (j 의 종료 시각 | i 가 없음)

`StationLedger.externality_parts` 를 그대로 부른다 — 식은 한 곳에만 둔다
(`ledger.py:155`). 대기가 아니라 **종료 시각**인 이유도 거기 적혀 있다:
내가 100kW 자리를 빼앗아 뒤차를 50kW 로 밀어낸 몫이 대기에는 안 담긴다.

FIFO 라서 **내 앞 차들은 정확히 0 을 기여한다.** 그래서 랭킹은 "뒤에 누구를
얼마나 밀었나" 의 순서다.

## ⚠ 랭킹이 나오는 것만으로는 증명이 아니다

차를 빼면 비용은 **언제나** 줄어든다. 그러니 "상위 5% 를 빼니 좋아졌다" 는
아무것도 말하지 않는다. 물어야 할 것은 이것이다.

    **같은 대수를 무작위로 뺐을 때보다 유의하게 더 줄어드나?**

대조군을 둘 둔다.

    대조 A (느슨)  전체에서 무작위 k 대
                   → 이기면 "쏠리는 **휴게소**를 골랐다" 까지만 말할 수 있다
    대조 B (엄격)  **휴게소마다 상위집합과 같은 대수**를 무작위로
                   → 이기면 "같은 휴게소 안에서도 **맞는 차**를 골랐다"

B 를 이겨야 Q3 의 "차량을 타깃한다" 가 성립한다. A 만 이기면 그건
증설 랭킹(휴게소 단위) 이야기다.

## ⚠ 민폐를 **합치지 말 것** — 총 사회적 비용이 아니다

차마다의 민폐는 **그 차만 뺐을 때**의 차이다. 한계값이라 더해지지 않는다.
설 상행 S0 에서 합계가 30,649.9시간인데 총 체류는 5,641.8시간이다 (**5.4배**).
상위 5% 의 민폐 합이 12,842시간인데 실제로 빼 보면 체류가 1,685.7시간 줄 뿐이다.

틀린 게 아니다 — FIFO 큐에서 한계 기여의 합은 원래 총량을 크게 넘는다
(앞차를 빼면 뒤차가 당겨오는 몫을 모두가 각자 자기 몫으로 센다).
**랭킹 신호로만 쓴다.** 회계 항등식으로 쓰면 비용이 다섯 배로 부풀고,
`social_cost_h` 와 나란히 놓는 순간 그 표는 틀린다.

**칸끼리 비교하는 데도 쓰면 안 된다.** 같은 시드에서

    S0   민폐 합계 30,649.9시간   총 사회적 비용 6,999.8시간
    S2   민폐 합계 42,548.9시간   총 사회적 비용 **더 낮다**

S2 가 더 "민폐가 많다" 는 게 아니다. S2 는 발길질을 줄여 **더 많은 차를
줄에 남긴다** (S0 은 425대를 흘린다). 줄에 남은 차가 많으면 서로 미는 쌍이
늘어난다. 즉 이 합계는 혼잡의 크기가 아니라 **큐에 함께 있는 차의 수**를
따라간다. 칸 비교는 `social_cost_h` 로만 한다.

## ⚠ 사후 재계산은 완전한 반사실이 아니다

차를 빼면 줄이 짧아져 **포기했던 차가 남는다.** 여기서는 그걸 반영하지
않는다 (실현된 충전 기록만 다시 쌓는다). 그래서 남은 차들의 체류 감소를
**과대평가**한다.

그래도 쓸모가 있다. 그 편향이 **상위집합과 대조군에 똑같이** 걸리므로
둘의 **차이**는 그대로다 — 그리고 우리가 묻는 것이 바로 그 차이다.
(`breakeven_escape.py` 의 사후 재가격과 같은 성격이다)

제대로 된 반사실은 수요에서 그 차들을 지우고 다시 돌리는 것이다.
그건 선택까지 바뀌므로 별도 실험이다.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.engine.ledger import StationLedger  # noqa: E402
from evdt.io.db import get_conn  # noqa: E402
from evdt.io.stations import read_station_chargers  # noqa: E402
from evdt.io.vehicles import curve_segments, load_from_db  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, make_run_id, with_seed  # noqa: E402
from evdt.world.queue_rule import Arrival, Charger, assign  # noqa: E402
from evdt.world.sim import station_specs  # noqa: E402

#: 재구성이 원본과 이만큼 안에서 같아야 한다 (분). 부동소수 재현 오차만 허용한다
RECON_TOL = 1e-6


# ---------------------------------------------------------------------------
# 실현된 하루를 다시 쌓는다
# ---------------------------------------------------------------------------
def rebuild(rid: str, corridor_id: str, db_path) -> tuple[
    dict[str, tuple[Charger, ...]], dict[str, list[Arrival]],
    dict[tuple[str, str], int], pd.DataFrame
]:
    """`(휴게소별 충전기, 휴게소별 도착열, 정차 차례, 원본 충전 기록)`.

    충전기 대수·출력은 **DB 에서** 온다 (`io/stations.py` 의 경고). 기록에 있는
    charger_id 만 모으면 **쉬던 충전기가 사라져** 대기가 통째로 틀린다.
    """

    path = RUNS_DIR / rid / "charge_event.parquet"
    if not path.exists():
        raise SystemExit(f"충전 기록이 없다: {path}\n먼저 run 을 돌릴 것")

    ev = pd.read_parquet(path)

    with get_conn(db_path, readonly=True) as conn:
        station_rows, charger_rows = read_station_chargers(conn, corridor_id=corridor_id)
        vclasses, curves, _temps = load_from_db(conn)

    chargers = {s.station_id: s.chargers for s in station_specs(station_rows, charger_rows)}

    spec = {}
    for row in vclasses:
        vid = str(row["vclass_id"])
        spec[vid] = (float(row["battery_kwh"]), float(row["vmax_kw"]),
                     tuple(curve_segments(curves, vid)))

    by_station: dict[str, list[Arrival]] = defaultdict(list)
    stop_seq: dict[tuple[str, str], int] = {}
    for r in ev.itertuples(index=False):
        battery_kwh, vmax_kw, curve = spec[str(r.vclass_id)]
        stop_seq[(str(r.ev_id), str(r.station_id))] = int(r.stop_seq)
        by_station[str(r.station_id)].append(Arrival(
            ev_id=str(r.ev_id),
            arrival_min=float(r.t_arrive_min),
            soc_from=float(r.soc_in),
            soc_to=float(r.soc_out),
            battery_kwh=battery_kwh,
            vmax_kw=vmax_kw,
            curve=curve,
            charge_power_factor=float(r.cold_factor),
        ))

    for sid, arrs in by_station.items():
        arrs.sort(key=lambda a: (a.arrival_min, a.ev_id))
        dup = [k for k, n in Counter(a.ev_id for a in arrs).items() if n > 1]
        if dup:
            raise SystemExit(
                f"{sid} 에 같은 차가 두 번 있다: {dup[:3]} — 원장은 한 휴게소에 한 번만 "
                "선다고 본다 (ledger.py commit). 이 run 은 이 분석으로 못 잰다")
        if sid not in chargers:
            raise SystemExit(f"{sid} 의 충전기를 DB 에서 못 찾았다 — 공급이 바뀐 DB 다")

    return chargers, dict(by_station), stop_seq, ev


def check_reconstruction(chargers, by_station, ev: pd.DataFrame) -> None:
    """다시 쌓은 결과가 **원본과 같은지** 확인한다. 틀리면 멈춘다.

    ★ 이 관문이 이 스크립트의 전부다. 재구성이 어긋나면 민폐 수치는 숫자가
      아니라 잡음이다. 조용히 넘어가면 "쏠림 유발 차를 찾았다" 가 거짓이 된다.
    """

    # ★ 차 하나가 여러 휴게소에 선다 (설 상행 S0: 6,831대가 7,375번 정차).
    #   그래서 열쇠는 ev_id 가 아니라 **(ev_id, 휴게소)** 다. ev_id 로만 찾으면
    #   두 정차가 한 칸에 겹쳐 비교 자체가 안 된다
    want = {(str(r.ev_id), str(r.station_id)): (float(r.t_end_min), float(r.wait_min))
            for r in ev.itertuples(index=False)}
    worst_end = worst_wait = 0.0
    worst_id = ""

    for sid, arrs in by_station.items():
        for a in assign(arrs, chargers[sid]):
            end_min, wait_min = want[(a.ev_id, sid)]
            de = abs(float(a.end_min) - end_min)
            dw = abs(float(a.wait_min) - wait_min)
            if max(de, dw) > max(worst_end, worst_wait):
                worst_id = f"{a.ev_id}@{sid}"
            worst_end, worst_wait = max(worst_end, de), max(worst_wait, dw)

    if max(worst_end, worst_wait) > RECON_TOL:
        raise SystemExit(
            f"재구성이 원본과 다르다 — 종료 {worst_end:.6g}분 · 대기 {worst_wait:.6g}분 "
            f"(가장 큰 차 {worst_id}).\n"
            "원인 후보: DB 공급이 run 이후 바뀌었다 / 차종·충전곡선이 바뀌었다 / "
            "이 스테이지가 같은 휴게소에 두 번 세운다. 민폐 수치를 믿을 수 없다")

    print(f"  재구성 확인 — 종료·대기 최대 오차 {max(worst_end, worst_wait):.2e}분 "
          f"({len(ev):,}건 전부)")


# ---------------------------------------------------------------------------
# 정차마다의 민폐
# ---------------------------------------------------------------------------
def externality_by_visit(chargers, by_station, stop_seq) -> pd.DataFrame:
    """**정차**마다 `밀린 시간(분)`. 식은 `StationLedger` 에만 있다.

    ★ 단위가 차가 아니라 **정차**인 이유. 한 차가 여러 휴게소에 선다. 검정에서
      대조군을 휴게소마다 같은 대수로 맞추려면(대조 B) 빼는 단위가 휴게소 안에
      있어야 한다 — 차를 빼면 그 차의 **다른 휴게소 정차**까지 같이 빠져서
      두 팔의 층이 어긋난다. 차 단위 랭킹은 이 표를 ev_id 로 합쳐서 낸다.
    """

    rows = []
    for sid, arrs in by_station.items():
        led = StationLedger(chargers[sid])
        for a in arrs:
            led.commit(a)
        for a in arrs:
            total, parts = led.externality_parts(a, exclude=a.ev_id)
            rows.append({"ev_id": a.ev_id, "station_id": sid,
                         "stop_seq": stop_seq[(a.ev_id, sid)],
                         "arrival_min": a.arrival_min,
                         "ext_min": float(total),
                         "n_pushed": sum(1 for v in parts.values() if v > 1e-9)})

    return pd.DataFrame(rows).sort_values("ext_min", ascending=False, ignore_index=True)


# ---------------------------------------------------------------------------
# 반사실 — 빼면 남은 차들이 얼마나 덜 머무나
# ---------------------------------------------------------------------------
def dwell_of_rest(chargers, by_station, removed: set[tuple[str, str]]) -> float:
    """`removed`(정차 열쇠) 를 뺐을 때 **남은 정차들의** 총 체류 (시간).

    빠진 정차 자신의 체류는 세지 않는다 — 그건 혼잡 완화가 아니라 수요 감소다.
    두 팔이 같은 수의 정차를 빼므로 이 정의에서만 비교가 공정하다.
    """

    total = 0.0
    for sid, arrs in by_station.items():
        rest = [a for a in arrs if (a.ev_id, sid) not in removed]
        if not rest:
            continue
        total += sum(a.end_min - a.arrival_min for a in assign(rest, chargers[sid]))

    return total / 60.0


def ci95(xs: Sequence[float]) -> tuple[float, float, float]:
    n = len(xs)
    mu = sum(xs) / n
    if n < 2:
        return mu, float("nan"), float("nan")
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    return mu, mu - half, mu + half


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--stage", default="S0")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--top-pct", type=float, default=5.0,
                    help="상위 몇 %% 를 빼 보나 (기본 5)")
    ap.add_argument("--reps", type=int, default=20, help="대조군 반복 횟수")
    ap.add_argument("--rng-seed", type=int, default=12345)
    args = ap.parse_args()

    cfg = with_seed(load_config(args.config, stage=args.stage), args.seed)
    rid = make_run_id(cfg.scenario_id, args.stage, args.seed, cfg.policy.participation)

    print(f"\n민폐 랭킹 · {cfg.label}")
    print(f"  {rid}\n")

    chargers, by_station, stop_seq, ev = rebuild(rid, cfg.corridor_id, default_db_path())
    check_reconstruction(chargers, by_station, ev)

    t = externality_by_visit(chargers, by_station, stop_seq)
    n = len(t)
    k = max(1, round(n * args.top_pct / 100.0))

    # -- 분포 ----------------------------------------------------------------
    pos = t[t.ext_min > 1e-9]
    n_cars = t.ev_id.nunique()
    print(f"\n=== 민폐의 분포 (정차 {n:,}건 · 차 {n_cars:,}대) ===")
    print(f"  아무도 밀지 않은 정차    {n - len(pos):>6,}건 ({(n - len(pos)) / n * 100:4.1f}%)")
    print(f"  민폐 합계              {t.ext_min.sum() / 60:>8,.1f}시간")
    for p in (50, 75, 90, 95, 99):
        print(f"  P{p:<3}                  {t.ext_min.quantile(p / 100):>8.2f}분")
    print(f"  최대                   {t.ext_min.max():>8.2f}분")
    share = t.ext_min.head(k).sum() / t.ext_min.sum() * 100 if t.ext_min.sum() else float("nan")
    print(f"\n  상위 {args.top_pct:g}% ({k:,}건) 가 민폐 합계의 **{share:.1f}%** 를 낸다")

    # 차 단위 랭킹 — Q3 가 묻는 단위다 (정차 표를 ev_id 로 합친다)
    per_car = (t.groupby("ev_id")
                .agg(ext_min=("ext_min", "sum"), n_stops=("ext_min", "size"),
                     n_pushed=("n_pushed", "sum"),
                     station_id=("station_id", lambda s: "+".join(sorted(set(s)))))
                .sort_values("ext_min", ascending=False))
    print("\n=== 민폐 상위 10대 (차 단위) ===")
    print(f"  {'ev_id':<12}{'정차':>5}{'밀린 시간':>11}{'밀린 차':>8}  휴게소")
    for ev_id, r in per_car.head(10).iterrows():
        print(f"  {ev_id:<12}{int(r.n_stops):>5}{r.ext_min:>11.1f}{int(r.n_pushed):>8}"
              f"  {r.station_id}")

    # -- 반사실 --------------------------------------------------------------
    base = dwell_of_rest(chargers, by_station, set())
    top = set(zip(t.ev_id.head(k), t.station_id.head(k), strict=True))
    cut_top = base - dwell_of_rest(chargers, by_station, top)

    pool = list(zip(t.ev_id, t.station_id, strict=True))
    per_station = Counter(sid for _, sid in top)
    by_sid = {sid: [(a.ev_id, sid) for a in arrs] for sid, arrs in by_station.items()}

    rng = random.Random(args.rng_seed)
    cut_a, cut_b = [], []
    for _ in range(args.reps):
        pick_a = set(rng.sample(pool, k))
        cut_a.append(base - dwell_of_rest(chargers, by_station, pick_a))

        pick_b: set[tuple[str, str]] = set()
        for sid, m in per_station.items():
            pick_b.update(rng.sample(by_sid[sid], m))
        cut_b.append(base - dwell_of_rest(chargers, by_station, pick_b))

    print(f"\n=== {k:,}건({args.top_pct:g}%)을 빼면 남은 차들의 체류가 얼마나 줄나 ===")
    print(f"  빼기 전 남은 차 체류 (전원)   {base:>9,.1f}시간\n")
    print(f"  {'팔':<34}{'체류 감소':>11}{'95% CI':>22}{'상위 대비':>10}")
    print(f"  {'상위 ' + f'{args.top_pct:g}% (민폐 순)':<34}{cut_top:>11,.1f}{'—':>22}{'—':>10}")
    for name, xs in (("대조 A · 전체에서 무작위", cut_a),
                     ("대조 B · 휴게소마다 같은 건수", cut_b)):
        mu, lo, hi = ci95(xs)
        ratio = cut_top / mu if mu else float("nan")
        print(f"  {name:<34}{mu:>11,.1f}[{lo:>9,.1f}, {hi:>8,.1f}]{ratio:>9.2f}배")

    # -- 판정 ----------------------------------------------------------------
    print("\n=== 판정 ===")
    verdict_b = False
    for name, xs, claim in (
        ("A", cut_a, "쏠리는 **휴게소**를 골랐다"),
        ("B", cut_b, "같은 휴게소 안에서도 **맞는 차**를 골랐다"),
    ):
        _mu, _lo, hi = ci95(xs)
        won = cut_top > hi
        if name == "B":
            verdict_b = won
        mark = "★ 이긴다" if won else "✗ 못 이긴다"
        print(f"  대조 {name}: {mark} — {claim if won else '주장할 수 없다'}")

    if verdict_b:
        print("\n  → Q3 의 '차량을 타깃한다' 가 성립한다. 민폐 랭킹이 무작위보다 "
              f"{cut_top / ci95(cut_b)[0]:.2f}배 효율적이다")
    else:
        print("\n  → 대조 B 를 못 이겼다. **'차를 뺐으니 줄었다' 와 구별되지 않는다.**\n"
              "     랭킹 자체는 맞을 수 있지만 이 run 에서는 증거가 되지 않는다")

    out = ROOT / "runs" / "sweeps" / f"externality_{rid}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    t.to_csv(out, index=False)
    print(f"\nCSV: {out}")
    if args.reps < 10:
        print("⚠ 대조군 반복이 적다. **판정이 아니다.**")

    return 0


if __name__ == "__main__":
    sys.exit(main())
