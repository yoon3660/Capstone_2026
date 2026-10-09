"""충전기 이용률을 실측과 대조한다 (#112).

    python scripts/validate_utilization.py --run <run_id>
    python scripts/validate_utilization.py --run <a> --vs <b>

## 왜 이 지표인가

교통 쪽은 실측으로 검증했지만 **충전 쪽은 대조할 숫자가 없었다** (#64). 그런데
충전사업자 **워터**가 **2026 설 연휴** — 우리 재현 기간 그대로 — 고속도로 충전소
49곳의 이용률을 공개했다.

**이용률은 우리가 바로 계산할 수 있다.** 스냅샷에 `chargers_busy` 와
`chargers_total` 이 있다.

그리고 이 지표는 **체류시간과 도착수를 한꺼번에** 본다 —

    이용률 = 도착수 × 체류시간 ÷ 충전기수

둘 중 하나만 맞으면 안 맞는다. 그래서 *"충전 수요를 제대로 만들고 있나"* 에 대한
단일 검사로 쓸 만하다.

## ⚠ 통과 기준 — 결과를 보기 전에 정한다

| | 실측 (2026 설, 오후 12~6시) | 기준 |
|---|---:|---|
| 충전기 **7기 이상** | **33.5%** | 상대오차 **±30%** 안 (23.5~43.6%) |
| 충전기 **2기 이하** | **50.3%** | 같은 기준 (35.2~65.4%) |
| **작은 쪽 ÷ 큰 쪽** | **1.50** | **1.2 이상** — 작은 충전소가 더 포화돼야 한다 |

±30% 는 넉넉하다. **우리 코리도 18곳을 전국 49곳 통계와 맞대는 것**이라 구성이
다르고, 그보다 좁게 잡으면 측정할 수 없는 정밀도를 요구하게 된다.

**세 번째가 구조 검사다.** 수준은 모수 하나로 맞출 수 있지만 **비율은 그렇지 않다** —
작은 충전소가 먼저 포화되는 것은 쏠림이 제대로 일어나야 나온다.

## 읽을 때

- 실측은 **전국 고속도로 49곳**, 우리는 **경부 한 방향**이다. 노선별 편차가 크다
  (실측 영동 인천방향: 용인 46.8% · 문막 13.7%). **수준을 일대일로 믿지 말 것.**
- 이용률은 **평균 점유**다. 100% 가 아니어도 특정 시각에 줄이 설 수 있다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402

#: 실측 (워터, 2026 설 고속도로 49곳, 오후 12~6시). inews24 보도.
#: 보도 통계 기본값. **실측 파일이 오면 `--measured` 로 갈아끼운다** (#64 준비).
PUBLISHED = {"small": 50.3, "big": 33.5}
PUBLISHED_SOURCE = "워터, 2026 설 고속도로 49곳 (inews24 보도)"

#: 결과를 보기 전에 정한 기준 (#112)
TOLERANCE = 0.30
MIN_RATIO = 1.2

PM_FROM, PM_TO = 12, 18
SMALL_MAX, BIG_MIN = 2, 7


def _ratio(measured: dict) -> float:
    if "small" in measured and measured.get("big"):
        return measured["small"] / measured["big"]
    return float("nan")

def load_measured(path: Path | None) -> tuple[dict, str, pd.DataFrame | None]:
    """실측 이용률. 파일이 없으면 보도 통계를 쓴다 (#64 준비).

    ## 기대하는 형식 — 팀원이 받아오는 자료를 이 모양으로만 맞춰 주면 된다

    CSV 한 장, 최소 세 열:

        station,hour,util_pct        휴게소 이름(또는 코드) · 0~23 · 0~100
        안성,13,46.8
        안성,14,51.2
        ...

    `util_pct` 대신 `busy,total` 을 줘도 된다 (우리가 나눈다).

    > **충전 세션 로그(시작·종료 시각)를 받았다면** 위 표는 groupby 한 번이다 —
    > 각 (휴게소, 시) 에서 동시에 꽂혀 있던 충전기 수의 평균 ÷ 총 충전기 수.
    > 로그 자체를 이 스크립트에 넣지 않는 이유는, **점유를 세는 규칙이 하나여야**
    > 하기 때문이다 (설계 규칙 1 과 같은 이유).

    휴게소 이름은 우리 `station.name` 과 맞아야 한다. 안 맞는 이름은 **버리지 않고
    세어서 알린다** — 조용히 빠지면 비교 대상이 줄어든 걸 아무도 모른다.
    """

    if path is None:
        return dict(PUBLISHED), PUBLISHED_SOURCE, None

    if not path.exists():
        raise SystemExit(f"\n[중단] 실측 파일이 없다: {path}")

    m = pd.read_csv(path)
    need = {"station", "hour"}
    if not need <= set(m.columns):
        raise SystemExit(
            f"\n[중단] {path} 에 {sorted(need)} 가 있어야 한다.\n"
            f"  있는 열: {list(m.columns)}\n"
            "  형식은 load_measured 의 설명을 볼 것."
        )
    if "util_pct" not in m.columns:
        if not {"busy", "total"} <= set(m.columns):
            raise SystemExit(
                f"\n[중단] {path} 에 util_pct 가 없으면 busy·total 이 있어야 한다."
            )
        m["util_pct"] = m["busy"] / m["total"].replace(0, pd.NA) * 100

    pm = m[(m["hour"] >= PM_FROM) & (m["hour"] < PM_TO)]
    if pm.empty:
        raise SystemExit(
            f"\n[중단] {path} 에 {PM_FROM}~{PM_TO}시 자료가 없다.\n"
            "  비교 시간대가 다르면 PM_FROM/PM_TO 를 같이 고치고 **문서에 적을 것.**"
        )

    by_station = pm.groupby("station")["util_pct"].mean()
    return {}, f"{path.name} (휴게소 {len(by_station)}곳)", by_station.rename("util_pct").reset_index()


def utilization(run_id: str) -> pd.DataFrame:
    path = RUNS_DIR / run_id / "snapshot.parquet"
    if not path.exists():
        raise SystemExit(f"\n[중단] 스냅샷이 없다: {path}")

    s = pd.read_parquet(path)
    s = s[s["entity_type"] == "station"]
    w = s.pivot_table(index=["t_min", "entity_id"], columns="state",
                      values="value").reset_index()
    w["hour"] = (w["t_min"] // 60).astype(int) % 24
    pm = w[(w["hour"] >= PM_FROM) & (w["hour"] < PM_TO)]

    g = pm.groupby("entity_id").agg(busy=("chargers_busy", "mean"),
                                    total=("chargers_total", "first"))
    g["util_pct"] = g["busy"] / g["total"] * 100

    with get_conn(default_db_path(), readonly=True) as conn:
        names = dict(conn.execute(
            f"SELECT station_id, name FROM station WHERE station_id IN "  # noqa: S608
            f"({','.join('?' * len(g))})", list(g.index)).fetchall())
    g["name"] = [names.get(i, i) for i in g.index]
    return g


def report(g: pd.DataFrame, label: str, measured: dict) -> dict:
    small = g[g["total"] <= SMALL_MAX]
    big = g[g["total"] >= BIG_MIN]
    out = {
        "small": float(small["util_pct"].mean()) if len(small) else float("nan"),
        "big": float(big["util_pct"].mean()) if len(big) else float("nan"),
        "n_small": len(small), "n_big": len(big),
        "all": float(g["util_pct"].mean()),
        "spread": float(g["util_pct"].max() / max(g["util_pct"].min(), 1e-9)),
    }
    out["ratio"] = out["small"] / out["big"] if out["big"] else float("nan")

    print(f"\n=== {label} ===")
    print(f"  충전기 {SMALL_MAX}기 이하 {out['n_small']:2d}곳  "
          f"{out['small']:5.1f}%   실측 {measured.get('small', float('nan')):5.1f}%")
    print(f"  충전기 {BIG_MIN}기 이상 {out['n_big']:2d}곳  "
          f"{out['big']:5.1f}%   실측 {measured.get('big', float('nan')):5.1f}%")
    print(f"  전체 {len(g):2d}곳        {out['all']:5.1f}%")
    print(f"  작은÷큰 {out['ratio']:5.2f}        실측 "
          f"{_ratio(measured):.2f}  "
          f"(기준 ≥ {MIN_RATIO})")
    print(f"  휴게소 편차 {out['spread']:.1f}배   실측 영동 인천방향 3.4배")
    return out


def verdict(out: dict, measured: dict) -> None:
    """⚠ **못 잰 검사를 통과로 세지 않는다.**

    기준 셋 중 둘이 "판정 불가" 인데 남은 하나가 통과했다고 "기준 통과" 라고 하면,
    **아무것도 검증하지 않고 합격증을 내주는 것**이다. #97 에서 0행을 검증하고
    "저장 완료" 를 찍던 것과 같은 고장이다.
    """

    print("\n  판정 (기준은 결과 보기 전에 정했다)")
    passed, failed, skipped = 0, 0, 0

    for key, name in (("big", f"{BIG_MIN}기 이상"), ("small", f"{SMALL_MAX}기 이하")):
        if key not in measured:
            print(f"    {name:9} 실측 없음 — 판정 불가")
            skipped += 1
            continue
        if out[f"n_{key}"] == 0:
            print(f"    {name:9} 해당 휴게소 없음 — 판정 불가")
            skipped += 1
            continue
        m = measured[key]
        lo, hi = m * (1 - TOLERANCE), m * (1 + TOLERANCE)
        v = out[key]
        good = lo <= v <= hi
        passed, failed = passed + good, failed + (not good)
        print(f"    {name:9} {v:5.1f}%  범위 {lo:.1f}~{hi:.1f}%  "
              f"{'통과' if good else '미달'}")

    r = out["ratio"]
    good = r >= MIN_RATIO
    passed, failed = passed + good, failed + (not good)
    print(f"    구조(비율) {r:5.2f}   기준 ≥ {MIN_RATIO}        {'통과' if good else '미달'}")

    if failed:
        print(f"\n  ⇒ 기준 미달 ({failed}개 미달 · {passed}개 통과"
              + (f" · {skipped}개 판정 불가)" if skipped else ")"))
    elif skipped:
        print(f"\n  ⇒ **판정 보류** — {skipped}개를 재지 못했다 ({passed}개만 통과).\n"
              "     못 잰 것을 통과로 세지 않는다.")
    else:
        print(f"\n  ⇒ 기준 통과 ({passed}개 전부)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--vs", help="비교할 run_id")
    ap.add_argument("--stations", action="store_true", help="휴게소별로 펼친다")
    ap.add_argument("--measured", type=Path,
                    help="실측 이용률 CSV (#64). 없으면 보도 통계를 쓴다")
    args = ap.parse_args()

    measured, source, by_station = load_measured(args.measured)
    print(f"\n실측 출처: {source}")

    g = utilization(args.run)
    out = report(g, args.run, measured)

    if args.vs:
        g2 = utilization(args.vs)
        out2 = report(g2, args.vs, measured)
        print(f"\n  변화  {BIG_MIN}기 이상 {out['big']:.1f}% → {out2['big']:.1f}%"
              f"  ·  전체 {out['all']:.1f}% → {out2['all']:.1f}%")
        verdict(out2, measured)
    else:
        verdict(out, measured)

    if by_station is not None:
        joined = g.merge(by_station, left_on="name", right_on="station",
                         how="left", suffixes=("", "_meas"))
        hit = joined["util_pct_meas"].notna()
        print(f"\n  휴게소 이름 대조: {int(hit.sum())} / {len(joined)} 곳 일치")
        if (~hit).any():
            print("    못 맞춘 우리 휴게소:",
                  ", ".join(joined.loc[~hit, "name"].head(8)))
        if hit.any():
            err = (joined.loc[hit, "util_pct"] - joined.loc[hit, "util_pct_meas"]).abs()
            print(f"    맞춘 곳 평균절대오차 {err.mean():.1f}%p")

    if args.stations:
        print("\n  휴게소별")
        print(g.sort_values("util_pct", ascending=False)
              [["name", "total", "busy", "util_pct"]]
              .to_string(float_format=lambda v: f"{v:,.2f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
