"""#62 OD 추정을 **날짜별로** VDS 와 대조한다 (hotfix/62).

    python scripts/check_od_by_date.py --od data/processed/od_gyeongbu_DOWN_202602.parquet

## 왜 따로 필요한가

`docs/od_design.md` 의 성능표는 검증 6일을 **합쳐서** 낸다. 그래서
*"2/17·2/18 을 써도 되는가"* 에 답할 수 없다 — 그 둘은 설 연휴 당일·다음날이고
**물량이 가장 큰 날**인데, 보정에 들어간 연휴일은 2/16 하루뿐이다.

> 합산 MAPE 가 48% 라고 해서 모든 날이 48% 인 것은 아니다. 쓸 수 있는 날과 못 쓰는
> 날이 섞여 있을 수 있고, 그걸 가르지 않으면 **가장 중요한 날을 모르고 쓴다.**

## 무엇을 재나

OD 한 줄은 *"이 시각에 여기서 떠나 저기까지 가는 N 대"* 다. 일정 속도를 가정하면
**어느 콘존을 언제 지나는지**가 정해진다. 그걸 다 더하면 구간×시간 통과 대수가 나오고,
그것을 VDS 실측과 비교한다.

`estimate_od_daily.passage_operator` 와 **같은 규칙**을 쓴다 — 시간 안에서 출발이
균등하다고 보고 도착 시간대를 인접 두 칸에 나눈다. 다르게 재면 다른 수가 나온다.

## ⚠ 읽을 때 주의

- **워밍업**: 코리도를 한 번 통과하는 데 걸리는 시간만큼은 앞 날짜에서 들어온 차가
  빠져 있다. 그 구간은 과소집계되므로 **첫 몇 시간은 점수에서 뺀다.**
- **꼬리**: 마지막 날 늦게 떠난 차는 다음 날로 넘어간다. 창 밖으로 나간 몫은 버린다.
- 그래서 이 수치는 그들의 성능표와 **표본이 다르다.** 날짜 사이 **상대 비교**로 읽는다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import DATA_PROCESSED_DIR  # noqa: E402


def zone_positions(direction: str) -> pd.DataFrame:
    """콘존 중간점. `passage_operator` 가 쓰는 것과 같은 정의다."""

    z = pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet")
    z = z[z["direction"].astype(str) == direction].copy()
    z["position_km"] = (z["offset_km_start"] + z["offset_km_end"]) / 2.0
    return z[["conzone_id", "position_km"]].drop_duplicates("conzone_id").sort_values("position_km")


def passage(od: pd.DataFrame, zones: pd.DataFrame, speed_kmh: float) -> pd.DataFrame:
    """OD → (콘존, 날짜, 시) 통과 대수."""

    pos = zones["position_km"].to_numpy(dtype=float)
    zid = zones["conzone_id"].to_numpy()

    days = np.sort(od["date"].unique())
    day_index = {d: i for i, d in enumerate(days)}
    n_hours = len(days) * 24

    start = od["start_offset_km"].to_numpy(dtype=float)
    end = od["end_offset_km"].to_numpy(dtype=float)
    vol = od["volume_veh"].to_numpy(dtype=float)
    dep = od["departure_hour"].to_numpy(dtype=int)
    day = od["date"].map(day_index).to_numpy(dtype=int)
    t0 = day * 24 + dep

    out = np.zeros((len(pos), n_hours + 48), dtype=float)

    # 콘존마다 한 번에 — 행 단위 파이썬 반복을 피한다 (53만 행)
    for j, p in enumerate(pos):
        hit = (start <= p) & (p < end)
        if not hit.any():
            continue
        lag = (p - start[hit]) / speed_kmh
        whole = np.floor(lag).astype(int)
        frac = lag - whole
        arrive = t0[hit] + whole
        v = vol[hit]
        np.add.at(out[j], np.clip(arrive, 0, n_hours + 47), v * (1.0 - frac))
        np.add.at(out[j], np.clip(arrive + 1, 0, n_hours + 47), v * frac)

    out = out[:, :n_hours]
    idx = pd.MultiIndex.from_product([zid, range(n_hours)], names=["conzone_id", "t"])
    flat = pd.DataFrame({"model_veh": out.ravel()}, index=idx).reset_index()
    flat["date"] = pd.to_datetime(days[flat["t"] // 24])
    flat["hour"] = flat["t"] % 24
    return flat[["conzone_id", "date", "hour", "model_veh"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--od", type=Path, required=True)
    ap.add_argument("--speed-kmh", type=float, default=90.0)
    ap.add_argument("--warmup-hours", type=int, default=5,
                    help="앞 구간은 들어오는 차가 빠져 있어 과소집계된다")
    args = ap.parse_args()

    od = pd.read_parquet(args.od)
    od["date"] = pd.to_datetime(od["date"]).dt.normalize()
    direction = str(od["direction"].dropna().iloc[0])

    zones = zone_positions(direction)
    model = passage(od, zones, args.speed_kmh)

    vds = pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet")
    vds = vds[vds["direction"].astype(str) == direction]
    vds = vds[["conzone_id", "date", "hour", "volume_veh"]].rename(
        columns={"volume_veh": "vds_veh"})
    vds["date"] = pd.to_datetime(vds["date"]).dt.normalize()

    cmp = model.merge(vds, on=["conzone_id", "date", "hour"], how="inner")
    cmp = cmp[cmp["vds_veh"].notna() & (cmp["vds_veh"] > 0)]

    first_day = cmp["date"].min()
    warm = (cmp["date"] == first_day) & (cmp["hour"] < args.warmup_hours)
    cmp = cmp[~warm]

    cmp["abs_err"] = (cmp["model_veh"] - cmp["vds_veh"]).abs()
    cmp["ape"] = cmp["abs_err"] / cmp["vds_veh"] * 100

    split = od.groupby(od["date"])["split"].first()

    rows = []
    for d, g in cmp.groupby("date"):
        dawn = g[g["hour"] < 6]
        rows.append({
            "날짜": d.date(),
            "split": split.get(d, "-"),
            "관측칸": len(g),
            "MAPE%": g["ape"].mean(),
            "중위APE%": g["ape"].median(),
            "RMSE": float(np.sqrt((g["abs_err"] ** 2).mean())),
            "새벽MAPE%": dawn["ape"].mean() if len(dawn) else float("nan"),
            "모델/실측": g["model_veh"].sum() / g["vds_veh"].sum(),
        })

    table = pd.DataFrame(rows)
    print(f"\n{args.od.name}  방향 {direction}  속도 {args.speed_kmh:g} km/h")
    print(f"워밍업 {args.warmup_hours}시간 제외 · 콘존 {len(zones)}개\n")
    print(table.to_string(index=False, float_format=lambda v: f"{v:,.2f}"))

    tr = table[table["split"] == "train"]
    va = table[table["split"] == "validation"]
    if len(tr) and len(va):
        print(f"\n  보정일 평균 MAPE {tr['MAPE%'].mean():.1f}%  ·  "
              f"검증일 평균 MAPE {va['MAPE%'].mean():.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
