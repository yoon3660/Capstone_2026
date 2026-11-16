"""VDS 두 출처 대조 — 지점 단위 페이지 vs 콘존 단위 API (#179).

    python scripts/compare_vds_sources_179.py

출처
    page   data/raw/ex_vdszone_quiet202603/vdszone_<날짜>.json   (`fetch_vdszone_179.py`)
           conzoneStart(상행 CZE) · conzoneEnd(하행 CZS) : 콘존 × 시간 교통량·속도
           vdsStart · vdsEnd                              : VDS 지점 × 시간
    api    data/processed/issue178_quiet202603/traffic_gyeongbu_quiet.parquet · speed_…  (#178)
    old    data/processed/traffic_gyeongbu.parquet period=base202603 (2026-09-25 수집, 3/6~3/15)

검사
    1  page 콘존 값 = api 콘존 값 (교통량·속도, 콘존 × 시간, 10일)
       page 는 상행 교통량에 부호를 붙여 그린다(음수) → 절댓값으로 비교.
       page 의 |값| ≤ 2 는 결측 코드로 본다(api 의 결측 코드 −1·−2 와 같은 자리인지 같이 센다)
    2  api(2026-11-16 수집) = old(2026-09-25 수집) — 같은 날짜(3/6~3/11)를 두 번 받은 값이 같은가
    3  VDS 지점 완결성 — 지점 × 방향 × 날짜 중 24시간이 모두 있는 비율 (#179 체크리스트)
    4  지점 → 콘존 관계 — 콘존 값과 그 콘존 VDS 지점 평균의 비 (참고: 콘존 값의 정의 확인용)

출력 (data/processed/issue179_quiet202603/)
    conzone_match.csv   날짜 × 방향 × 지표: 비교 칸, 완전 일치, 결측 자리 일치, MAPE
    api_vs_old.csv      날짜 × 방향: 비교 칸, 완전 일치, MAPE
    vds_completeness.csv 날짜 × 방향: 지점 수, 24시간 완결 지점 수·비율, 유효 칸 비율
    vds_to_conzone.csv  날짜 × 방향: 콘존 값 / 지점 평균 의 중앙값·사분위
    summary.json
"""

from __future__ import annotations

import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "data/raw/ex_vdszone_quiet202603"
API = ROOT / "data/processed/issue178_quiet202603"
OLD = ROOT / "data/processed/traffic_gyeongbu.parquet"
OUT = ROOT / "data/processed/issue179_quiet202603"
DAYS = pd.date_range("2026-03-02", "2026-03-11")
KEYS = {"UP": ("conzoneStart", "vdsStart"), "DOWN": ("conzoneEnd", "vdsEnd")}


def page_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    cz, vds = [], []
    for day in DAYS:
        j = json.loads((PAGE / f"vdszone_{day:%Y%m%d}.json").read_text(encoding="utf-8"))
        for direction, (ck, vk) in KEYS.items():
            c = pd.DataFrame(j[ck]).assign(date=day, direction=direction)
            v = pd.DataFrame(j[vk]).assign(date=day, direction=direction)
            cz.append(c)
            vds.append(v)
    cz = pd.concat(cz, ignore_index=True)
    vds = pd.concat(vds, ignore_index=True)
    for f in (cz, vds):
        f["hour"] = f["collect_hour"].astype(int)
        f["traffic"] = pd.to_numeric(f["traffic"], errors="coerce").abs()
        f["avg_speed"] = pd.to_numeric(f["avg_speed"], errors="coerce")
    cz["page_missing"] = cz["traffic"] <= 2
    return cz, vds


def mape(a: pd.Series, b: pd.Series) -> float:
    m = (a > 0) & (b > 0)
    return float(((a[m] - b[m]).abs() / b[m]).mean() * 100) if m.any() else float("nan")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cz, vds = page_frames()
    vol = pd.read_parquet(API / "traffic_gyeongbu_quiet.parquet")
    spd = pd.read_parquet(API / "speed_gyeongbu_quiet.parquet")
    api = vol.merge(spd[["date", "hour", "direction", "conzone_id", "speed_kmh"]],
                    on=["date", "hour", "direction", "conzone_id"], how="left", validate="one_to_one")
    key = ["date", "direction", "conzone_id", "hour"]
    m = cz.merge(api, on=key, how="outer", indicator=True, validate="one_to_one")

    rows = []
    for (day, direction), g in m.groupby(["date", "direction"]):
        both = g[g["_merge"] == "both"]
        valid = both[~both.page_missing & both.volume_veh.notna()]
        rows.append({
            "date": day.date(), "direction": direction,
            "page_only_keys": int((g["_merge"] == "left_only").sum()),
            "api_only_keys": int((g["_merge"] == "right_only").sum()),
            "cells_both": len(both),
            "missing_page": int(both.page_missing.sum()),
            "missing_api": int(both.volume_veh.isna().sum()),
            "missing_same_place": int((both.page_missing == both.volume_veh.isna()).sum()),
            "volume_cells": len(valid),
            "volume_exact": int((valid.traffic == valid.volume_veh).sum()),
            "volume_mape_pct": mape(valid.traffic, valid.volume_veh),
            "speed_cells": int((valid.avg_speed > 0).sum()),
            "speed_abs_diff_le_0_1": int(((valid.avg_speed - valid.speed_kmh).abs() <= 0.1).sum()),
            "speed_mape_pct": mape(valid.avg_speed, valid.speed_kmh)})
    match = pd.DataFrame(rows)
    match.to_csv(OUT / "conzone_match.csv", index=False, encoding="utf-8-sig")

    old = pd.read_parquet(OLD)
    old = old[old.period == "base202603"]
    ao = api.merge(old[key + ["volume_veh"]], on=key, suffixes=("", "_old"), validate="one_to_one")
    rows = []
    for (day, direction), g in ao.groupby(["date", "direction"]):
        both = g[g.volume_veh.notna() & g.volume_veh_old.notna()]
        rows.append({"date": day.date(), "direction": direction, "cells": len(both),
                     "exact": int((both.volume_veh == both.volume_veh_old).sum()),
                     "mape_pct": mape(both.volume_veh, both.volume_veh_old),
                     "missing_new_only": int((g.volume_veh.isna() & g.volume_veh_old.notna()).sum()),
                     "missing_old_only": int((g.volume_veh.notna() & g.volume_veh_old.isna()).sum())})
    api_old = pd.DataFrame(rows)
    api_old.to_csv(OUT / "api_vs_old.csv", index=False, encoding="utf-8-sig")

    comp = (vds.assign(ok=vds.traffic > 0)
            .groupby(["date", "direction", "vds_id"]).ok.sum().reset_index())
    vc = (comp.groupby(["date", "direction"])
          .agg(vds=("vds_id", "nunique"), complete_24h=("ok", lambda s: int((s == 24).sum())),
               valid_cells=("ok", "sum")).reset_index())
    vc["complete_share"] = vc.complete_24h / vc.vds
    vc["valid_cell_share"] = vc.valid_cells / (vc.vds * 24)
    vc.to_csv(OUT / "vds_completeness.csv", index=False, encoding="utf-8-sig")

    vm = (vds[vds.traffic > 0].groupby(key).traffic.agg(["mean", "size"])
          .rename(columns={"mean": "vds_mean", "size": "n_vds"}).reset_index())
    r = cz[~cz.page_missing].merge(vm, on=key, validate="one_to_one")
    r["ratio"] = r.traffic / r.vds_mean
    rel = (r.groupby(["date", "direction"]).ratio
           .describe(percentiles=[.25, .5, .75])[["count", "25%", "50%", "75%"]].reset_index())
    rel.to_csv(OUT / "vds_to_conzone.csv", index=False, encoding="utf-8-sig")

    summary = {
        "conzone_keys_page": int(cz[["direction", "conzone_id"]].drop_duplicates().shape[0]),
        "conzone_keys_api": int(api[["direction", "conzone_id"]].drop_duplicates().shape[0]),
        "volume_cells": int(match.volume_cells.sum()),
        "volume_exact": int(match.volume_exact.sum()),
        "missing_same_place": int(match.missing_same_place.sum()),
        "cells_both": int(match.cells_both.sum()),
        "speed_within_0_1": int(match.speed_abs_diff_le_0_1.sum()),
        "speed_cells": int(match.speed_cells.sum()),
        "api_vs_old_cells": int(api_old.cells.sum()), "api_vs_old_exact": int(api_old.exact.sum()),
        "vds_points_per_day": vc.groupby("direction").vds.max().to_dict(),
        "vds_complete_share_mean": vc.groupby("direction").complete_share.mean().round(4).to_dict(),
        "vds_valid_cell_share_mean": vc.groupby("direction").valid_cell_share.mean().round(4).to_dict(),
        "conzone_over_vds_mean_median": float(r.ratio.median()),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                      encoding="utf-8")
    pd.set_option("display.width", 200)
    print(match.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(api_old.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(vc.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
