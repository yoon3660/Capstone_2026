"""#145 CSV 번들을 교통량·속도 정리본에 **갈아끼운다** (#146).

    python scripts/load_issue145_csv.py --bundle <번들 폴더>
    python scripts/load_issue145_csv.py --bundle <번들 폴더> --dry-run

왜 별도 스크립트인가
    `build_traffic.py` 는 포털 원본 JSON(`data/raw/ex_vds_*`)에서 만든다. 이번 추석
    자료는 팀원이 **이미 그 산출 규격 그대로** CSV 로 내려줬다 (열 13개 동일,
    `offset_km` 이 기존 parquet 과 소수 9자리까지 같다). 원본 JSON 이 우리 쪽에
    없으므로 CSV 를 직접 받아 적재한다.

넣는 것
    chuseok2026   9/23~28 · 132 콘존 · 교통량 + 속도
    base202609    9/10~19 평시 대조군 · 교통량 + 속도

속도·평시 CSV 에는 `offset_km` · `period` 열이 없다 — 교통량 파일의 콘존 기하를
`conzone_id` 로 조인해서 채운다. 코리도 밖 콘존(속도 파일에 3개 더 있다)은 버린다.

⚠ 원본은 `data/processed/_backup_<타임스탬프>/` 로 복사해 둔다.
⚠ 같은 period 가 이미 있으면 **지우고 다시 넣는다** (두 번 돌려도 같은 결과).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

PROC = ROOT / "data" / "processed"
TRAFFIC = PROC / "traffic_gyeongbu.parquet"
SPEED = PROC / "speed_gyeongbu.parquet"

COLS = ["period", "date", "hour", "direction", "conzone_id", "conzone_name",
        "offset_km", "offset_km_start", "offset_km_end", "position_source",
        "{value}", "missing_code", "source"]

SRC = {"volume_veh": "ex_portal_vds_volume", "speed_kmh": "ex_portal_vds_speed"}

#: (period, 교통량 CSV, 속도 CSV, 날짜 범위)
BATCHES = [
    ("chuseok2026", "vds_hourly_volume_chuseok_20260923_28.csv", "chuseok_speed.csv",
     ("2026-09-23", "2026-09-28")),
    ("base202609", "ordinary_volume.csv", "ordinary_speed.csv",
     ("2026-09-10", "2026-09-19")),
]


def rd(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)


def geometry(bundle: Path) -> pd.DataFrame:
    """코리도 콘존의 기하. 추석 교통량 파일이 유일하게 전부 갖고 있다."""

    v = rd(bundle / "vds_hourly_volume_chuseok_20260923_28.csv")
    g = (v.groupby(["conzone_id", "direction"], as_index=False)
         .agg(conzone_name=("conzone_name", "first"),
              offset_km=("offset_km", "first"),
              offset_km_start=("offset_km_start", "first"),
              offset_km_end=("offset_km_end", "first"),
              position_source=("position_source", "first")))
    return g


def shape(raw: pd.DataFrame, period: str, value: str, geo: pd.DataFrame,
          span: tuple[str, str]) -> pd.DataFrame:
    """CSV 한 장 → 정리본 열 13개. 기하가 없으면 조인해서 채운다."""

    t = raw.copy()
    t["date"] = pd.to_datetime(t["date"])
    lo, hi = pd.Timestamp(span[0]), pd.Timestamp(span[1])
    t = t[(t["date"] >= lo) & (t["date"] <= hi)]

    if "offset_km" not in t.columns:
        before = t["conzone_id"].nunique()
        t = t.drop(columns=[c for c in ("conzone_name",) if c in t.columns])
        t = t.merge(geo, on=["conzone_id", "direction"], how="inner")
        after = t["conzone_id"].nunique()
        if before != after:
            print(f"    코리도 밖 콘존 {before - after}개 버림 ({before} → {after})")

    t["period"] = period
    t["source"] = SRC[value]
    if "missing_code" not in t.columns:
        t["missing_code"] = pd.NA

    cols = [c.format(value=value) for c in COLS]
    t = t[cols].astype({"hour": "int64", value: "float64",
                        "missing_code": "float64"})
    return t.sort_values(["date", "direction", "conzone_id", "hour"]).reset_index(drop=True)


def check(t: pd.DataFrame, period: str, value: str, ref: pd.DataFrame) -> None:
    """하나라도 어긋나면 멈춘다."""

    if list(t.columns) != list(ref.columns):
        raise SystemExit(f"[{period}/{value}] 열이 기존 정리본과 다르다:\n"
                         f"  새것: {list(t.columns)}\n  기존: {list(ref.columns)}")

    key = ["period", "date", "hour", "direction", "conzone_id"]
    dup = t.duplicated(key).sum()
    if dup:
        raise SystemExit(f"[{period}/{value}] 중복 키 {dup}건")

    bad = set(t["hour"]) - set(range(24))
    if bad:
        raise SystemExit(f"[{period}/{value}] hour 범위 밖: {sorted(bad)}")

    new_cz = set(t["conzone_id"])
    ref_cz = set(ref["conzone_id"])
    if not new_cz <= ref_cz:
        raise SystemExit(f"[{period}/{value}] 기존에 없는 콘존 {sorted(new_cz - ref_cz)[:5]}")

    # offset_km 이 기존과 같은 자를 쓰는지 (좌표계 검사)
    a = ref.groupby("conzone_id")["offset_km"].first()
    b = t.groupby("conzone_id")["offset_km"].first()
    d = (a.reindex(b.index) - b).abs().max()
    if pd.notna(d) and d > 1e-6:
        raise SystemExit(f"[{period}/{value}] offset_km 좌표계 불일치: 최대 {d} km")

    obs = t[value].notna().sum()
    print(f"    {period}/{value}: {len(t):,}행 · 콘존 {len(new_cz)} · "
          f"{t['date'].min():%m-%d}~{t['date'].max():%m-%d} · "
          f"관측 {obs:,} (결측 {(1 - obs / len(t)) * 100:.1f}%) · "
          f"offset 최대차 {d:.1e} km")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", required=True, help="#145 CSV 번들 폴더")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 검사만")
    args = ap.parse_args()

    bundle = Path(args.bundle)
    if not bundle.is_dir():
        print(f"번들 폴더가 없다: {bundle}")
        return 1
    if not TRAFFIC.is_file() or not SPEED.is_file():
        print("정리본이 없다. 먼저 build_traffic.py 를 돌릴 것.")
        return 1

    vol0 = pd.read_parquet(TRAFFIC)
    spd0 = pd.read_parquet(SPEED)
    print(f"기존: 교통량 {len(vol0):,}행 · 속도 {len(spd0):,}행 · "
          f"period {sorted(set(vol0['period']) | set(spd0['period']))}")

    geo = geometry(bundle)
    print(f"콘존 기하 {len(geo)}개 (추석 교통량 파일에서)\n")

    new_vol, new_spd = [], []
    for period, vcsv, scsv, span in BATCHES:
        print(f"  [{period}]  {span[0]} ~ {span[1]}")
        v = shape(rd(bundle / vcsv), period, "volume_veh", geo, span)
        check(v, period, "volume_veh", vol0)
        new_vol.append(v)
        s = shape(rd(bundle / scsv), period, "speed_kmh", geo, span)
        check(s, period, "speed_kmh", spd0)
        new_spd.append(s)
        print()

    added = {p for p, *_ in BATCHES}

    def merge(old: pd.DataFrame, parts: list[pd.DataFrame]) -> pd.DataFrame:
        kept = old[~old["period"].isin(added)]
        out = pd.concat([kept, *parts], ignore_index=True)
        return out.sort_values(["period", "date", "direction", "conzone_id", "hour"]
                               ).reset_index(drop=True)

    vol1 = merge(vol0, new_vol)
    spd1 = merge(spd0, new_spd)

    print("=" * 72)
    for name, a, b in (("교통량", vol0, vol1), ("속도", spd0, spd1)):
        print(f"{name}: {len(a):,} → {len(b):,}행  ({len(b) - len(a):+,})")
        print(f"   period: {b.groupby('period').size().to_dict()}")

    if args.dry_run:
        print("\n--dry-run — 쓰지 않았다")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = PROC / f"_backup_{stamp}"
    bak.mkdir(parents=True, exist_ok=True)
    for p in (TRAFFIC, SPEED):
        shutil.copy2(p, bak / p.name)
    print(f"\n원본 백업 → {bak}")

    vol1.to_parquet(TRAFFIC, index=False)
    spd1.to_parquet(SPEED, index=False)
    print(f"썼다: {TRAFFIC}\n     {SPEED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
