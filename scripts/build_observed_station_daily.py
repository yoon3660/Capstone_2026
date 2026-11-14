"""환경부 충전이력 → **날짜 × 휴게소 × 방향** 집계 (#167 의 빠진 조각).

    python scripts/build_observed_station_daily.py
    python scripts/build_observed_station_daily.py --corridor gyeongbu --verify-only

`docs/core_validation_table_153.md` 에는 10일 **합계**만 있었다. 날짜별 표가
없어서 ASC 적합의 **날짜 분할 검증**(적합 6일 / 검증 4일)을 할 수 없었다.
이 스크립트가 그 표를 만든다.

## 원장

    data/raw/sp3_64/20260306_2026년2월_환경부_충전기_충전이력_상세내역.xlsx
    SHA-256 d3c18c83440debbfa3be02d018551e7a44bb175162ea1844b977995c5ce564f3

전국 **561,710건**(2026-02 전체) 중 `휴게시설 / 고속도로 휴게소` 가 129,847건,
전국 휴게소 255곳이다. 거기서 **경부선만** 골라낸다.

## ★ 휴게소를 어떻게 골라내나 — 이름이 **정확히** 맞는다

원장의 `충전소명` 이 충전기 API 의 `statNm` 과 같은 문자열이다:

    원장  천안삼거리(서울) 휴게소
    API   천안삼거리(서울) 휴게소   ME178011 · ME21A309  (기후에너지환경부)

그래서 **API 를 경유해서** 짝짓는다. API 는 좌표를 갖고 있으므로

    원장 충전소명  ──(문자열 일치)──  API statNm  ──(좌표)──  우리 휴게소

로 이어진다. 원장에 좌표가 없어서 직접 좌표 짝짓기를 할 수 없기 때문이다.
이 경로의 부수 효과로 **각 세션의 운영사도 알 수 있다** (API 의 `bnm`).

## 검산 — 통과하지 못하면 쓰지 않는다

`core_validation_table_153.md` 에 적힌 값을 **정확히** 재현해야 한다:

    설 기간(2026-02-13 00:00 ~ 02-23 00:00) 충전 시작   11,405건
    DOWN                                             4,828건
    UP                                               6,577건
    관측 방향별 휴게소                                  29개소
    기간 전체 점유시간                              308,475.37분

하나라도 어긋나면 우리가 휴게소를 다르게 골라낸 것이고, 그 표로 만든 검증은
팀 보고와 다른 자를 쓰게 된다.

## 점유시간의 정의 (#167 과 같게)

세션의 시작~종료 구간과 **그 날짜 칸이 겹치는 분**의 합이다. 자정을 넘는
세션은 두 날짜에 나뉘어 들어간다. 시작 건수는 **시작 시각**의 날짜로 센다 —
그래서 날짜별 `starts` 합과 `occupied_min` 합의 분모가 다르다.
"""

from __future__ import annotations

import argparse
import collections
import csv
import math
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import default_db_path  # noqa: E402

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

LEDGER = ("data/raw/sp3_64/"
          "20260306_2026년2월_환경부_충전기_충전이력_상세내역.xlsx")
LEDGER_SHA = "d3c18c83440debbfa3be02d018551e7a44bb175162ea1844b977995c5ce564f3"
API_GLOB = "data/raw/highway_chargers_20260918_120946_all/page_*.json"

#: 설 검증 기간 — #167 과 같다 (KST). 끝은 **미포함**
SEOLLAL = (datetime(2026, 2, 13), datetime(2026, 2, 23))

#: 검산 기준값 (core_validation_table_153.md §2)
EXPECT = {"starts": 11405, "up": 6577, "down": 4828, "stations": 29}

MATCH_KM = 2.0
NEEDED = ("충전소명", "충전소 유형(대분류)", "충전소 유형(소분류)",
          "충전시작일시", "충전종료일시", "충전량")


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def norm_name(raw: str) -> tuple[str, str] | None:
    """원장의 `충전소명` → (기준이름, 방향). 못 읽으면 None.

        '천안삼거리(서울) 휴게소'  → ('천안삼거리', 'UP')
        '평사(부산)휴게소'        → ('평사',       'DOWN')   ← 공백 없는 변형
        '서울하이패스센터(입구)'    → ('서울하이패스센터', '')   ← 방향 없음

    ★ **공백 변형을 반드시 합쳐야 한다.** 원장에 같은 휴게소가 두 표기로 들어
      있다 — `평사(부산)휴게소 361건` + `평사(부산) 휴게소 67건` = 428건
      (공개표와 일치). 한쪽만 세면 황간 -111 · 옥산 -86 · 평사 -67 이 빠진다.

    ★ **API 를 경유하지 않는다.** 처음에는 충전기 API(2026-09-18 덤프)의
      statNm 으로 짝지었는데, **2월과 9월 사이에 충전기 구성이 바뀌었다** —
      양산(서울)은 2월 원장에 환경부 세션이 있는데 9월 API 에는 한전만 있다.
      9월 덤프로 2월 범위를 판정하면 안 된다.
    """

    t = re.sub(r"\s+", "", raw or "")
    if not t:
        return None
    m = re.match(r"^(.+?)\((서울|부산|입구|출구)\)", t)
    if m:
        base, mark = m.group(1), m.group(2)
        return base, {"서울": "UP", "부산": "DOWN"}.get(mark, "")
    return re.sub(r"휴게소$|쉼터$", "", t), ""


def station_lookup(corridor: str) -> dict[tuple[str, str], tuple[str, str]]:
    """(기준이름, 방향) → (station_id, 이름). 우리 휴게소 표에서 만든다.

    방향 표기가 없는 원장 이름(서울하이패스센터(입구) 등)은 방향 없이도 찾을 수
    있게 ('이름', '') 키도 같이 둔다 — 그 이름이 한 방향에만 있을 때만이다.
    """

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        rows = list(con.execute(
            "SELECT station_id, name, direction FROM station "
            "WHERE station_id LIKE ?", (f"{corridor}_%",)))
    if not rows:
        raise SystemExit(f"[중단] 코리도 휴게소가 없다: {corridor}")

    out: dict[tuple[str, str], tuple[str, str]] = {}
    by_base: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for sid, name, direction in rows:
        base = re.sub(r"휴게소$|쉼터$", "", re.sub(r"\s+", "", name))
        out[(base, direction)] = (sid, name)
        by_base[base].append((sid, name))
    for base, lst in by_base.items():
        if len(lst) == 1:                 # 한 방향에만 있는 이름
            out[(base, "")] = lst[0]
    return out


def shared_strings(z: zipfile.ZipFile) -> list[str]:
    vals: list[str] = []
    with z.open("xl/sharedStrings.xml") as f:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag == f"{NS}si":
                vals.append("".join(t.text or "" for t in el.iter(f"{NS}t")))
                el.clear()
    return vals


def parse_dt(raw: str) -> datetime | None:
    """`20260213143000` → datetime. 14자리가 아니면 버린다."""

    s = re.sub(r"\D", "", raw or "")
    if len(s) != 14:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def overlap_minutes(start: datetime, end: datetime,
                    day: datetime) -> float:
    """세션이 그 **날짜 칸**과 겹치는 분. 자정을 넘는 세션은 두 날에 나뉜다."""

    lo, hi = max(start, day), min(end, day + timedelta(days=1))
    return max(0.0, (hi - lo).total_seconds() / 60.0)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corridor", default="gyeongbu")
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--out", default="data/processed/observed_station_daily.csv")
    ap.add_argument("--verify-only", action="store_true",
                    help="CSV 를 쓰지 않고 검산만 한다")
    ap.add_argument("--skip-hash", action="store_true")
    args = ap.parse_args()

    path = ROOT / args.ledger
    if not path.is_file():
        raise SystemExit(f"[중단] 원장이 없다: {path}")

    if not args.skip_hash:
        import hashlib
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got != LEDGER_SHA:
            raise SystemExit(
                f"[중단] 원장의 SHA-256 이 다르다\n  기대 {LEDGER_SHA}\n  실제 {got}\n"
                "  #167 이 쓴 파일과 다른 파일이면 집계가 팀 보고와 달라진다")
        print("  SHA-256 일치 — #167 과 같은 원장이다")

    look = station_lookup(args.corridor)
    print(f"  {args.corridor} 휴게소 이름표 {len(look)}개")

    z = zipfile.ZipFile(path)
    shared = shared_strings(z)
    print(f"  sharedStrings {len(shared):,}개")

    idx: dict[str, int] = {}
    rows = kept = bad_dt = 0
    # (날짜, station_id) → [starts, occupied_min, energy]
    daily: dict[tuple[str, str], list[float]] = collections.defaultdict(
        lambda: [0.0, 0.0, 0.0])
    seen_names: collections.Counter = collections.Counter()
    seollal_starts: collections.Counter = collections.Counter()
    seollal_occ = 0.0

    with z.open("xl/worksheets/sheet1.xml") as f:
        for _ev, el in ET.iterparse(f, events=("end",)):
            if el.tag != f"{NS}row":
                continue
            cells = []
            for c in el.iter(f"{NS}c"):
                v = c.find(f"{NS}v")
                raw = "" if v is None else (v.text or "")
                if c.get("t") == "s" and raw.isdigit():
                    raw = shared[int(raw)]
                cells.append(raw)
            el.clear()

            if not idx:
                idx = {h: i for i, h in enumerate(cells) if h in NEEDED}
                missing = set(NEEDED) - set(idx)
                if missing:
                    raise SystemExit(f"[중단] 컬럼이 없다: {sorted(missing)}")
                continue

            rows += 1
            # ★ `충전소 유형` 으로 거르지 **않는다.** 원장이 같은 휴게소를 두
            #   유형으로 적는다 — `양산(서울) 휴게소` 가
            #   [고속도로 휴게소] 72건 + [지방도로 휴게소] 68건 = 140건이고,
            #   공개표의 140 과 합쳐서만 맞는다. 유형으로 거르면 68건이 빠진다.
            #   이름 짝짓기(기준이름 + 방향표기)가 충분히 좁고, 틀리면 검산이 잡는다
            parsed = norm_name(cells[idx["충전소명"]])
            if parsed is None:
                continue
            hit = look.get(parsed) or look.get((parsed[0], ""))
            if hit is None:
                continue
            sid, pretty = hit

            start = parse_dt(cells[idx["충전시작일시"]])
            end = parse_dt(cells[idx["충전종료일시"]])
            if start is None or end is None or end < start:
                bad_dt += 1
                continue

            kept += 1
            seen_names[cells[idx["충전소명"]].strip()] += 1

            # 시작 건수 — 시작 시각의 날짜
            daily[(start.date().isoformat(), sid)][0] += 1
            daily[(start.date().isoformat(), sid)][2] += _num(
                cells[idx["충전량"]])
            # 점유시간 — 걸치는 모든 날짜에 나눠 넣는다
            day = datetime(start.year, start.month, start.day)
            while day <= end:
                m = overlap_minutes(start, end, day)
                if m > 0:
                    daily[(day.date().isoformat(), sid)][1] += m
                day += timedelta(days=1)

            if SEOLLAL[0] <= start < SEOLLAL[1]:
                seollal_starts[sid] += 1
            d0 = max(start, SEOLLAL[0])
            d1 = min(end, SEOLLAL[1])
            if d1 > d0:
                seollal_occ += (d1 - d0).total_seconds() / 60.0

    print(f"  원장 {rows:,}행 · 경부 휴게소 {kept:,}건 "
          f"(시각 불량 {bad_dt:,}건 제외)")

    with sqlite3.connect(f"file:{default_db_path()}?mode=ro", uri=True) as con:
        dirs = dict(con.execute("SELECT station_id, direction FROM station"))
        names = dict(con.execute("SELECT station_id, name FROM station"))

    # --- 검산 ---------------------------------------------------------------
    tot = sum(seollal_starts.values())
    up = sum(c for s, c in seollal_starts.items() if dirs.get(s) == "UP")
    down = sum(c for s, c in seollal_starts.items() if dirs.get(s) == "DOWN")
    n_st = len(seollal_starts)

    print(f"\n{'=' * 70}\n검산 — core_validation_table_153.md §2\n{'=' * 70}")
    ok = True
    for label, got, want in (("설 기간 충전 시작", tot, EXPECT["starts"]),
                             ("  UP", up, EXPECT["up"]),
                             ("  DOWN", down, EXPECT["down"]),
                             ("관측 휴게소 수", n_st, EXPECT["stations"])):
        mark = "일치" if got == want else f"★ 다르다 ({got - want:+,})"
        ok = ok and got == want
        print(f"  {label:<20}{got:>10,}   기대 {want:>10,}   {mark}")
    print(f"  {'기간 전체 점유시간':<20}{seollal_occ:>10,.2f}분  "
          f"기대 {308475.37:>10,.2f}분   "
          f"{'일치' if abs(seollal_occ - 308475.37) < 1.0 else '★ 다르다'}")

    print(f"\n  원장 표기 {len(seen_names)}개 (경부, 2월 전체 {kept:,}건) — 상위 6")
    for nm, c in seen_names.most_common(6):
        print(f"    {c:>8,}  {nm}")

    if not ok:
        print("\n  ★ 검산 실패 — 휴게소를 다르게 골라냈다. CSV 를 쓰지 않는다")
        return 1
    print("\n  검산 통과")

    if args.verify_only:
        return 0

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "station_id", "name", "direction",
                    "starts", "occupied_min", "energy_kwh"])
        for (date, sid), (st, occ, kwh) in sorted(daily.items()):
            w.writerow([date, sid, names.get(sid, sid), dirs.get(sid, "?"),
                        int(st), f"{occ:.2f}", f"{kwh:.3f}"])
    print(f"  내보냄 → {out}  ({len(daily):,}줄)")
    return 0


def _num(raw: str) -> float:
    try:
        return float(re.sub(r"[^0-9.\-]", "", raw or "") or 0.0)
    except ValueError:
        return 0.0


if __name__ == "__main__":
    sys.exit(main())
