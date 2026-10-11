"""CTM 속도를 실측 VDS 와 대조한다 (#58).

    python scripts/validate_ctm_speed.py --run <ctm run_id>
    python scripts/validate_ctm_speed.py --run <ctm run_id> --plot

## 왜 이게 먼저인가

CTM 은 구현만 돼 있고(#56) **보정(#57)도 검증(#58)도 안 됐다.** 그 위에서 계산한
도착 시각과 쏠림은 아직 근거가 없다.

그런데 보정보다 **검증이 먼저다.** #56 에서 실측 서울→부산 4.3시간 vs CTM 4.4시간이
나왔으니 **이미 쓸 만할 수도 있다.** 그러면 보정을 건너뛴다. 못 넘으면 그때
**무엇을** 보정해야 하는지 알게 된다. 보정부터 하면 무엇을 고치는지 모른 채 돌린다.

## ⚠ 통과 기준 — **결과를 보기 전에 정한다**

#58 의 요구다. 결과를 보고 기준을 바꾸면 검증이 아니다.

| | 기준 | 왜 이 값인가 |
|---|---|---|
| **속도 MAPE** | **≤ 15%** | 거시 교통류 모형 보정의 통상 허용치 |
| **정체 재현율** | **≥ 0.50** | 실측이 60 km/h 미만인 칸에서 CTM 도 60 미만일 비율 |
| **정체 오경보율** | **≤ 0.30** | 실측이 자유흐름인데 CTM 이 정체라고 한 비율 |

**세 번째가 중요하다.** 재현율만 보면 **전 구간을 정체라고 우기는 모형이 만점**을 받는다.

> 이 프로젝트가 CTM 에서 필요로 하는 것은 평균 속도가 아니라 **정체가 언제 어디서
> 생기는가**다. 그게 휴게소 도착 시각을 흔들고, 그 도착이 쏠림을 만든다.
> 그래서 평균 오차(MAPE)보다 **정체 판정**을 같은 무게로 본다.

## 비교 방법

CTM 은 1 km 셀 396개, VDS 는 콘존 142개다. **셀을 콘존으로 묶어** 콘존×시간으로
맞춘다 (겹치는 길이로 가중). 자료 없는 콘존은 **점수에서 빼고 몇 개인지 적는다.**
"""

from __future__ import annotations

import argparse
import sqlite3

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import DATA_PROCESSED_DIR, RUNS_DIR, default_db_path  # noqa: E402

#: 결과를 보기 전에 정한 기준 (#58). 바꾸려면 **왜 바꾸는지** 를 같이 적을 것.
PASS_MAPE = 15.0
PASS_RECALL = 0.50
PASS_FALSE_ALARM = 0.30

#: 이 아래면 정체로 본다 (km/h). 자유속도 100 대비 40% 감속.
JAM_KMH = 60.0


def cell_to_conzone(corridor_id: str, direction: str) -> pd.DataFrame:
    """셀 → 콘존 대응과 가중치. 겹치는 **길이**로 가중한다."""

    with sqlite3.connect(default_db_path()) as conn:
        cells = pd.read_sql_query(
            "SELECT cell_id, offset_km_start, offset_km_end FROM cell WHERE corridor_id = ?",
            conn, params=(corridor_id,))

    z = pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet")
    z = z[z["direction"].astype(str) == direction]
    z = z[["conzone_id", "offset_km_start", "offset_km_end"]].drop_duplicates("conzone_id")

    rows = []
    for c in cells.itertuples(index=False):
        lo = np.maximum(c.offset_km_start, z["offset_km_start"].to_numpy())
        hi = np.minimum(c.offset_km_end, z["offset_km_end"].to_numpy())
        overlap = np.clip(hi - lo, 0.0, None)
        if overlap.sum() <= 0:
            continue
        for zid, w in zip(z["conzone_id"], overlap, strict=True):
            if w > 0:
                rows.append({"cell_id": c.cell_id, "conzone_id": zid, "w": float(w)})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="travel_time: ctm 으로 돌린 run_id")
    ap.add_argument("--plot", action="store_true", help="시공간 속도 그림 (실측 vs CTM)")
    ap.add_argument("--out", default="docs/figures/ctm_speed_validation.png")
    args = ap.parse_args()

    snap = pd.read_parquet(RUNS_DIR / args.run / "snapshot.parquet")
    cell = snap[(snap["entity_type"] == "cell") & (snap["state"] == "speed_kmh")]
    if cell.empty:
        raise SystemExit(
            f"\n[중단] {args.run} 에 cell 속도 스냅샷이 없다.\n"
            "  demand.travel_time 이 'ctm' 인지 확인하라 — 'fixed' 면 셀이 안 나온다."
        )

    corridor = str(cell["entity_id"].iloc[0]).rsplit("_", 1)[0]
    direction = "UP" if corridor.endswith("_up") else "DOWN"

    # 기간은 run 이 저장한 config 전문에서 읽는다 — scenario 표에는 열이 없고,
    # 이름으로 짐작하면 설 시나리오가 평시 속도와 대조될 수 있다 (#97 이 그랬다)
    import yaml
    with sqlite3.connect(default_db_path()) as conn:
        row = conn.execute(
            "SELECT s.config_yaml FROM run r JOIN scenario s ON s.scenario_id = r.scenario_id "
            "WHERE r.run_id = ?", (args.run,)).fetchone()
    if row is None:
        raise SystemExit(f"\n[중단] run 이 DB 에 없다: {args.run}")
    period = str(yaml.safe_load(row[0])["demand"]["period"])

    m = cell_to_conzone(corridor, direction)
    cell = cell.assign(hour=(cell["t_min"] // 60).astype(int) % 24)
    cell = cell.merge(m, left_on="entity_id", right_on="cell_id")
    # 콘존 속도 = 겹치는 길이로 가중한 조화평균이 아니라 산술평균을 쓴다.
    # VDS 가 보고하는 값도 구간 대표 속도라 같은 성격이다
    model = (cell.assign(wv=cell["value"] * cell["w"])
             .groupby(["conzone_id", "hour"])[["wv", "w"]].sum())
    model = (model["wv"] / model["w"]).rename("ctm_kmh").reset_index()

    # ⚠ **같은 날과 비교한다.** 수요 프로파일은 `peak_date` 한 날에서 나온다
    # (`io/demand_profile.entry_hourly_volume`). 그런데 속도를 기간 평균과 비교하면
    # 서로 다른 날을 맞대는 것이고, 게다가 **평균이 정체를 지운다** — 설 하행은
    # 날짜별 정체칸 2.96% 가 10일 평균 뒤 0.44% 로, 최저속도는 13 → 50 km/h 가 된다.
    import evdt.io.demand_profile as dp
    traffic = pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet")
    day = dp.peak_date(traffic, period=period, direction=direction)

    vds = pd.read_parquet(DATA_PROCESSED_DIR / "speed_gyeongbu.parquet")
    vds = vds[(vds["direction"].astype(str) == direction)
              & (vds["period"].astype(str) == period)
              & (pd.to_datetime(vds["date"]).dt.normalize() == day)]
    vds = (vds.groupby(["conzone_id", "hour"])["speed_kmh"].mean()
           .rename("vds_kmh").reset_index())

    cmp = model.merge(vds, on=["conzone_id", "hour"], how="left")
    total = len(cmp)
    missing = cmp["vds_kmh"].isna() | (cmp["vds_kmh"] <= 0)
    gap_zones = sorted(cmp.loc[missing, "conzone_id"].unique())
    cmp = cmp[~missing]

    err = (cmp["ctm_kmh"] - cmp["vds_kmh"]).abs()
    mape = float((err / cmp["vds_kmh"]).mean() * 100)
    rmse = float(np.sqrt((err ** 2).mean()))
    bias = float((cmp["ctm_kmh"] - cmp["vds_kmh"]).mean())

    jam_true = cmp["vds_kmh"] < JAM_KMH
    jam_model = cmp["ctm_kmh"] < JAM_KMH
    recall = float((jam_true & jam_model).sum() / max(1, jam_true.sum()))
    false_alarm = float((~jam_true & jam_model).sum() / max(1, (~jam_true).sum()))

    print(f"\n{args.run}   {direction} · {period}")
    print(f"  점수에 쓴 칸 {len(cmp):,} / {total:,}   "
          f"(VDS 속도 없음 {int(missing.sum()):,}칸 · 콘존 {len(gap_zones)}곳)")
    print(f"\n  속도 MAPE   {mape:6.1f}%   기준 ≤ {PASS_MAPE:.0f}%   "
          f"{'통과' if mape <= PASS_MAPE else '미달'}")
    print(f"  RMSE        {rmse:6.1f} km/h")
    print(f"  편의        {bias:+6.1f} km/h   (+ 면 CTM 이 빠르다)")
    print(f"\n  실측 정체 칸 {int(jam_true.sum()):,} ({jam_true.mean():.1%})")
    print(f"  정체 재현율  {recall:6.2f}    기준 ≥ {PASS_RECALL:.2f}   "
          f"{'통과' if recall >= PASS_RECALL else '미달'}")
    print(f"  정체 오경보  {false_alarm:6.2f}    기준 ≤ {PASS_FALSE_ALARM:.2f}   "
          f"{'통과' if false_alarm <= PASS_FALSE_ALARM else '미달'}")

    ok = mape <= PASS_MAPE and recall >= PASS_RECALL and false_alarm <= PASS_FALSE_ALARM
    print(f"\n  ⇒ {'세 기준 모두 통과' if ok else '기준 미달 — #57 보정이 필요하다'}")

    if args.plot:
        from evdt.viz.ctm_speed import plot_speed_fields
        path = plot_speed_fields(cmp, DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet",
                                 direction, ROOT / args.out,
                                 title=f"속도 시공간 — 실측 vs CTM ({direction} · {period})",
                                 subtitle=f"MAPE {mape:.1f}% · 정체 재현율 {recall:.2f} · "
                                          f"오경보 {false_alarm:.2f}")
        print(f"\n  그림: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
