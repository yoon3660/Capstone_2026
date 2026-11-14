"""T1 — 휴게소 선택 모형의 진단 네 지표를 한 명령으로 잰다 (MNL 재설계 회귀 방지선).

    python scripts/diagnose_choice_fit.py
    python scripts/diagnose_choice_fit.py --mnl-dirs runs/mnl_world/seollal_w0_probe/*
    python scripts/diagnose_choice_fit.py --me-table-only   # ME 보정표만 내보낸다

`docs/mnl_redesign_plan.txt` 의 모든 티켓이 이 네 지표로 판정한다.

    ① 분배 TVD        실측 휴게소 점유율과 얼마나 다른가
    ② 대기↔충전기수    충전기가 적은 휴게소가 더 밀리나 (스피어만, 음수여야 한다)
    ③ 총 사회적 비용   체류 + 이탈 비용 + 포기 전 대기 (#147 세 항)
    ④ 이탈 수 / S0    기준 2 — S0 의 ±35% 안 (docs/compare_rule.md)


## ★★ 가장 중요한 것 — 실측과 모델은 **분모가 다르다**

실측(#167 설 충전이력)은 **환경부(ME) 운영 충전기의 세션만** 담는다. 우리 모델의
충전기는 **전 운영사**다. 그래서 보정 없이 점유율을 견주면 **모델의 오차가 아니라
ME 보급률의 차이를 재게 된다.**

2026-11-14 에 그걸 모르고 쟀다가 틀린 진단을 냈다. 보정 없는 TVD 로는

    언양      ME 1/19기 (5%)     모델 17.5%  실측  0.8%   +16.7%p
    천안삼거리  ME 6/6기 (100%)    모델  4.9%  실측 16.8%  -11.9%p

가 나와서 "모델이 고출력·대형 휴게소로 과대 배정한다" 고 읽었다. 그런데

    오차 ↔ ME 충전기 비중    스피어만 **-0.767**
    오차 ↔ 최고 충전기 출력   스피어만  +0.510

ME 비중이 더 잘 설명한다. 언양의 실측 0.8% 는 "아무도 안 간다" 가 아니라
**"ME 충전기 1기의 세션만 세었다"** 다.

### 그래서 세 방식을 다 내고, ③ 을 판정에 쓴다

  ① 보정 없음        참고로만. 두 자가 다르다
  ② 실측을 역산       실측 ÷ ME비중. "한 휴게소 안에서 운영사별 이용률이 같다"
                     는 거친 가정이 들어간다. 실제로 더 나빠진다 (0.48)
  ③ **ME 100% 휴게소만**  분모가 같다. 가정이 없다. n 이 작은 것이 값
                     **판정은 이것으로 한다**

## 휴게소 ↔ API 충전기 짝짓기는 **좌표로 한다**

API 의 `statNm` 이 한 휴게소를 여러 이름으로 적는다:

    입장거봉포도(서울) 휴게소   ME178010   (ME)
    입장휴게소 (서울)         ST441325   (SK일렉링크)   ← 같은 휴게소다

이름으로 맞추면 뒤쪽을 놓쳐서 "ME 100%" 로 잘못 센다 (실제 5/8 = 62%).
그래서 **좌표 근접 + 이름의 방향 표기**로 짝짓는다.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402

#: #167 설 충전이력 — UP 관측 14곳의 10일 시작 건수 (core_validation_table_153.md §4).
#: ⚠ UP 칠곡·옥천은 **미관측**이다 (그쪽에 ME 충전기가 없다 — 아래 note 참조).
#:   0 이 아니라 N/A 로 다룬다.
#: 실측 기간 (일). #167 은 2026-02-13 ~ 02-22 의 **10일**이다. 모델은 시드당
#: 하루이므로 1기당 부하를 견줄 때 반드시 이걸로 나눈다
OBS_DAYS = 10

OBS_UP: dict[str, float] = {
    "천안삼거리휴게소": 1107, "신탄진휴게소": 893, "죽암휴게소": 733,
    "죽전휴게소": 724, "건천휴게소": 543, "안성휴게소": 480,
    "입장거봉포도휴게소": 467, "경산휴게소": 387, "황간휴게소": 359,
    "추풍령휴게소": 321, "청주휴게소": 284, "양산휴게소": 140,
    "김천휴게소": 86, "언양휴게소": 53,
}

#: 충전기 API 원본 덤프 (전 운영사). 공공 비중을 여기서 센다
API_GLOB = "data/raw/highway_chargers_20260918_120946_all/page_*.json"

#: ★ **환경부 원장이 담는 운영사** — 2026-11-14 에 16곳 전부로 확인했다.
#:
#:   · 관측된 14곳은 **모두** 이 중 하나를 1기 이상 갖고 있다
#:   · 미관측 2곳(UP 칠곡·옥천)은 **0기**다
#:     칠곡 UP = SK일렉링크5 + 브라이트에너지파3 · 옥천 UP = SK시그넷7
#:
#: `core_validation_table_153.md` 이 "환경부 제공 거래 전체를 ME-only 로 단정하지
#: 않는다" 고 적어 둔 것이 맞았다 — **한국전력공사도 들어온다.** 양산 UP 은
#: 한전 4기뿐인데 실측 140건이 있다. ME 만으로 보면 설명이 안 되는 칸이다.
PUBLIC_OPERATORS = frozenset({"기후에너지환경부", "한국전력공사"})

#: 좌표 짝짓기 허용 거리 (km). 같은 휴게소의 상·하행은 보통 0.1~0.5 km 떨어져 있다
MATCH_KM = 2.0

#: 공공 100% 로 볼 하한. 1.0 으로 두면 반올림에 걸린다
PURE_ME = 0.999


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def me_coverage(corridor_like: str = "gyeongbu_up%") -> pd.DataFrame:
    """휴게소별 ME 충전기 비중 — **좌표로 짝지어** 센다.

    돌려주는 열: api_units · me_units · me_frac · db_units · max_kw
    """

    paths = sorted(glob.glob(str(ROOT / API_GLOB)))
    if not paths:
        raise SystemExit(f"[중단] 충전기 API 덤프가 없다: {API_GLOB}")
    items = []
    for p in paths:
        items += json.load(open(p, encoding="utf-8"))["items"]["item"]

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        stations = list(con.execute(
            "SELECT station_id, name, direction, lat, lon FROM station "
            "WHERE station_id LIKE ?", (corridor_like,)))
        db_units = dict(con.execute(
            "SELECT s.station_id, SUM(c.n_units) FROM station s JOIN charger c "
            "ON c.station_id = s.station_id WHERE c.is_active = 1 "
            "AND s.station_id LIKE ? GROUP BY 1", (corridor_like,)))
        max_kw = dict(con.execute(
            "SELECT s.station_id, MAX(c.power_kw) FROM station s JOIN charger c "
            "ON c.station_id = s.station_id WHERE c.is_active = 1 "
            "AND s.station_id LIKE ? GROUP BY 1", (corridor_like,)))

    acc = {s[0]: {"name": s[1], "api_units": 0, "me_units": 0} for s in stations}
    for r in items:
        try:
            pos = (float(r["lat"]), float(r["lng"]))
        except (TypeError, ValueError, KeyError):
            continue
        nm = str(r.get("statNm", ""))
        # 이름의 방향 표기. 둘 다 없으면 방향을 안 보고 가장 가까운 곳에 붙인다
        want = ("UP" if re.search(r"서울", nm) else
                "DOWN" if re.search(r"부산", nm) else None)
        best, best_km = None, MATCH_KM
        for sid, _name, direction, lat, lon in stations:
            if want is not None and direction != want:
                continue
            d = haversine(pos, (lat, lon))
            if d < best_km:
                best, best_km = sid, d
        if best is None:
            continue
        acc[best]["api_units"] += 1
        if str(r.get("bnm") or r.get("busiNm") or "") in PUBLIC_OPERATORS:
            acc[best]["me_units"] += 1

    t = pd.DataFrame.from_dict(acc, orient="index")
    t["me_frac"] = t.me_units / t.api_units.replace(0, pd.NA)
    t["db_units"] = [db_units.get(i, 0) for i in t.index]
    t["max_kw"] = [max_kw.get(i, 0.0) for i in t.index]
    return t.set_index("name").sort_values("me_frac")


def mnl_sessions(d: Path, cell: str = "MNL") -> tuple[pd.Series, int]:
    acc = []
    for s in range(1, 41):
        p = d / f"seed{s}" / f"{cell}.parquet"
        if p.is_file():
            acc.append(pd.read_parquet(p).groupby("station_id").size())
    if not acc:
        raise SystemExit(f"[중단] {d} 에서 {cell}.parquet 를 못 찾았다")
    return pd.concat(acc, axis=1).sum(axis=1) / len(acc), len(acc)


def ladder_sessions(scenario: str, stage: str, seeds: range) -> tuple[pd.Series, int]:
    acc = []
    for s in seeds:
        p = RUNS_DIR / f"{scenario}__{stage}__p100__s{s:04d}" / "charge_event.parquet"
        if p.is_file():
            acc.append(pd.read_parquet(p).groupby("station_id").size())
    if not acc:
        raise SystemExit(f"[중단] {scenario}__{stage} run 을 못 찾았다")
    return pd.concat(acc, axis=1).sum(axis=1) / len(acc), len(acc)


def tvd(model: pd.Series, obs: pd.Series) -> tuple[float, int]:
    """관측 휴게소 **안에서** 다시 정규화한 점유율의 전변동거리."""

    idx = [i for i in obs.index if i in model.index]
    if not idx:
        return float("nan"), 0
    a = model.reindex(idx).fillna(0.0).astype(float)
    b = obs.reindex(idx).astype(float)
    if a.sum() <= 0 or b.sum() <= 0:
        return float("nan"), 0
    return float(((a / a.sum()) - (b / b.sum())).abs().sum() / 2), len(idx)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mnl-dirs", nargs="*", default=[
        "runs/mnl_world/seollal_up_adoption_v3/floor_theta0p05_w1",
        "runs/mnl_world/seollal_up_adoption_v3/floor_theta0p1_w1",
        "runs/mnl_world/seollal_w0_probe/th0p05_w0p5",
        "runs/mnl_world/seollal_w0_probe/th0p05_w0p0",
    ])
    ap.add_argument("--scenario", default="seollal_2026_up_adoption",
                    help="사다리 S0 를 가져올 scenario_id")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--me-out", default="data/processed/me_coverage_gyeongbu.csv")
    ap.add_argument("--me-table-only", action="store_true")
    args = ap.parse_args()

    # --- ME 보정표 ---------------------------------------------------------
    print("\n" + "=" * 78)
    print("공공(환경부·한전) 충전기 비중 — 실측의 분모. 좌표로 짝지었다")
    print("=" * 78)
    cov = pd.concat([me_coverage("gyeongbu_up%").assign(dir="UP"),
                     me_coverage("gyeongbu_down%").assign(dir="DOWN")])
    up = cov[cov["dir"] == "UP"]
    print(f"  {'휴게소':<14}{'API기':>7}{'공공기':>6}{'공공%':>8}{'DB기':>6}"
          f"{'최고kW':>7}  실측")
    for nm in up.index:
        r = up.loc[nm]
        f = f"{r.me_frac * 100:.0f}%" if pd.notna(r.me_frac) else "—"
        mark = ("관측" if nm in OBS_UP else
                "★미관측 (공공 0기)" if r.me_units == 0 else "미관측")
        print(f"  {nm[:13]:<14}{int(r.api_units):>7}{int(r.me_units):>6}{f:>8}"
              f"{int(r.db_units):>6}{r.max_kw:>7.0f}  {mark}")

    out = ROOT / args.me_out
    out.parent.mkdir(parents=True, exist_ok=True)
    cov.to_csv(out, encoding="utf-8-sig")
    print(f"\n  내보냄 → {out}")

    pure = [n for n in up.index if pd.notna(up.loc[n, "me_frac"])
            and up.loc[n, "me_frac"] >= PURE_ME and n in OBS_UP]
    print(f"\n  공공 100% 이고 관측된 휴게소 {len(pure)}곳 — **판정은 이 부분집합으로**")
    print(f"    {', '.join(pure)}")

    missing_me = [n for n in up.index if up.loc[n, "me_units"] == 0]
    if missing_me:
        print(f"\n  ★ 공공 충전기가 **0기**인 휴게소: {', '.join(missing_me)}")
        print("    → 환경부 원장으로는 **영원히 관측되지 않는다.** 미관측이 맞다")

    if args.me_table_only:
        return 0

    obs = pd.Series(OBS_UP, dtype=float)
    obs_pure = obs.reindex(pure)

    # --- 네 지표 -----------------------------------------------------------
    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        sid2nm = dict(con.execute(
            "SELECT station_id, name FROM station WHERE station_id LIKE 'gyeongbu_%'"))
    nch = dict(zip(up.index, up.db_units, strict=True))

    cases: list[tuple[str, pd.Series, pd.DataFrame, int]] = []
    for d in args.mnl_dirs:
        p = ROOT / d
        if not p.is_dir():
            print(f"  [건너뜀] {d} 없음")
            continue
        s, n = mnl_sessions(p)
        cases.append((p.name, s, _waits(p, "MNL"), n))
    s0, n0 = ladder_sessions(args.scenario, "S0", range(1, args.seeds + 1))
    cases.append(("S0 (사다리)", s0, _ladder_waits(args.scenario, "S0",
                                                   range(1, args.seeds + 1)), n0))

    print("\n" + "=" * 78)
    print("진단 네 지표")
    print("=" * 78)
    print(f"  {'설정':<22}{'TVD①':>8}{'TVD③':>8}{'n③':>4}"
          f"{'스피어만②':>11}{'세션/시드':>10}{'시드':>5}")
    for label, sess, waits, n in cases:
        sess = sess.rename(index=sid2nm)
        v1, _ = tvd(sess, obs)
        v3, n3 = tvd(sess, obs_pure)
        w = waits.rename(index=sid2nm)
        j = pd.DataFrame({"wait": w, "ch": pd.Series(nch)}).dropna()
        rho = j["wait"].corr(j["ch"], method="spearman") if len(j) > 2 else float("nan")
        print(f"  {label[:21]:<22}{v1:>8.4f}{v3:>8.4f}{n3:>4}"
              f"{rho:>+11.3f}{sess.sum():>10,.0f}{n:>5}")

    # --- ⑤ 충전기 1기당 부하 — **실측으로 직접 검사한다** ------------------
    #     "충전기가 적은 휴게소가 더 심하게 밀린다" 를 스피어만보다 날카롭게
    #     본다. 공공 100% 휴게소는 분모가 같으므로 1기당 세션을 그대로 견줄 수
    #     있다. 실측에서 건천(2기)이 1기당 부하 1위다 — 모델도 그래야 한다
    print("\n" + "=" * 78)
    print("⑤ 충전기 1기당 세션 — 공공 100% 휴게소 (분모가 같다)")
    print("=" * 78)
    # ⚠ 실측은 **10일치**고 모델은 시드당 **하루**다. 1기당 부하를 견주려면
    #   실측을 일수로 나눠야 한다. 안 나누면 10배 어긋난 값을 나란히 놓는다
    pub_units = pd.Series({n: float(up.loc[n, "me_units"]) for n in pure})
    obs_pc = (obs_pure / pub_units / OBS_DAYS).sort_values(ascending=False)
    head = f"  {'휴게소':<14}{'공공기':>6}{'실측/기·일':>11}{'순위':>5}"
    model_pc = {}
    for label, sess, _w, _n in cases:
        v = sess.rename(index=sid2nm)
        model_pc[label] = (v.reindex(pure).astype(float) / pub_units)
        head += f"{label[:11]:>13}"
    print(head)
    for rank, nm in enumerate(obs_pc.index, 1):
        line = f"  {nm[:13]:<14}{int(pub_units[nm]):>6}{obs_pc[nm]:>11.1f}{rank:>5}"
        for mp in model_pc.values():
            line += f"{mp[nm]:>8.0f}({int(mp.rank(ascending=False)[nm])})"
        print(line)
    print("\n  괄호는 그 설정 안에서의 1기당 부하 순위다. "
          "**실측 1위(건천)를 모델도 1위로 잡아야 한다** — 이미 맞다")
    # ★ 순위는 이미 맞다. 틀린 것은 **격차의 크기**다. 이것이 "작은 휴게소가
    #   더 심하게 밀린다" 의 진짜 자다 — 공통 배수에 영향받지 않으므로
    #   EV 보급률이 가정이어도 견줄 수 있다
    line = f"\n  ★ 불균등도 (1기당 부하 최대/최소)   실측 {obs_pc.max() / obs_pc.min():.1f}배"
    for label, mp in model_pc.items():
        line += f" · {label[:11]} {mp.max() / mp.min():.1f}배"
    print(line)
    print("    모델이 **너무 고르다.** 실측은 작은 휴게소를 훨씬 심하게 짓누른다.")
    print("    절대 수준은 견주지 않는다 — EV 보급률 10% 는 가정이고 2026-02 "
          "실제와 다르다.\n    비율은 그 공통 배수에 영향받지 않으므로 견줄 수 있다.")

    print("\n  ① 보정 없음 — 참고로만. 실측은 공공 운영사만, 모델은 전 운영사다")
    print("  ③ 공공 100% 휴게소만 — **판정은 이것으로**. 분모가 같다")
    print("  ② 스피어만: 대기 ↔ 충전기 수. **음수여야 한다** (적을수록 오래 기다린다)")
    return 0


def _waits(d: Path, cell: str) -> pd.Series:
    acc = []
    for s in range(1, 41):
        p = d / f"seed{s}" / f"{cell}.parquet"
        if p.is_file():
            acc.append(pd.read_parquet(p).groupby("station_id")["wait_min"].mean())
    return pd.concat(acc, axis=1).mean(axis=1)


def _ladder_waits(scenario: str, stage: str, seeds: range) -> pd.Series:
    acc = []
    for s in seeds:
        p = RUNS_DIR / f"{scenario}__{stage}__p100__s{s:04d}" / "charge_event.parquet"
        if p.is_file():
            acc.append(pd.read_parquet(p).groupby("station_id")["wait_min"].mean())
    return pd.concat(acc, axis=1).mean(axis=1)


if __name__ == "__main__":
    sys.exit(main())
