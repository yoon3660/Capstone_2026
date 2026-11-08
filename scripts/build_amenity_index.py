"""휴게소 "크기" 지수 — **대리변수판** (#146 · D1 대기 중).

    python scripts/build_amenity_index.py
    python scripts/build_amenity_index.py --out data/processed/amenity_index_proxy.csv

진짜 지표는 공공데이터포털 `노선별·방향별 휴게소 편의시설 현황`(apiId=0316)이다.
인증키 발급이 외부 차단이라, 그동안 **충전기 수**를 크기의 대리변수로 쓴다.

⚠ **순환 논리 위험.** 충전기가 많아서 선호되는 건지 휴게소가 커서 선호되는 건지
   이 지수로는 구분하지 못한다. 그래서 이것은 *"선호가 혼잡을 만들 수 있는가"* 라는
   **존재 질문**에만 쓴다. 크기 효과의 **크기**를 재는 데는 쓰면 안 된다.
   실제 편의시설 지표가 오면 갈아끼우고, 결론이 바뀌는지 보는 것 자체가 민감도다.

지수 = 충전기 수의 min-max 정규화 (코리도 안에서). 0 = 가장 작은 휴게소, 1 = 가장 큰 곳.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import default_db_path  # noqa: E402

SQL = """
SELECT s.station_id, s.corridor_id, s.name, s.n_parking,
       COALESCE(SUM(c.n_units), 0) AS n_chargers,
       COALESCE(MAX(c.power_kw), 0) AS max_kw
  FROM station s
  LEFT JOIN charger c ON c.station_id = s.station_id AND c.is_active = 1
 GROUP BY s.station_id, s.corridor_id, s.name
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/processed/amenity_index_proxy.csv")
    ap.add_argument("--corridor", default=None, help="비우면 전부")
    args = ap.parse_args()

    con = sqlite3.connect(default_db_path())
    t = pd.read_sql_query(SQL, con)
    con.close()

    if args.corridor:
        t = t[t["corridor_id"] == args.corridor]
    t = t[t["n_chargers"] > 0].copy()

    # ⚠ `station.n_parking` 이 **전부 NULL** 이다 — 스키마에 자리는 있는데 채워진
    #   적이 없다. 주차면수가 들어오면 충전기 수보다 **훨씬 나은** 대리변수다
    #   (순환 논리가 없다). D1 의 수집이 이 칸도 같이 채워야 한다.
    if t["n_parking"].notna().any():
        print("  ※ n_parking 이 채워져 있다 — 그쪽을 쓰도록 이 스크립트를 고칠 것")

    # 코리도 **안에서** 정규화한다. 상·하행을 섞으면 한쪽의 큰 휴게소가 다른 쪽
    # 전체를 눌러 버린다
    def norm(g: pd.DataFrame) -> pd.DataFrame:
        lo, hi = g["n_chargers"].min(), g["n_chargers"].max()
        g = g.copy()
        g["amenity_index"] = 0.0 if hi == lo else (g["n_chargers"] - lo) / (hi - lo)
        return g

    t = (t.groupby("corridor_id", group_keys=False)[t.columns.tolist()]
         .apply(norm).sort_values(["corridor_id", "amenity_index"], ascending=[True, False]))

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    t[["station_id", "corridor_id", "name", "n_chargers", "max_kw",
       "amenity_index"]].to_csv(out, index=False, encoding="utf-8-sig")

    print(f"썼다: {out}  ({len(t)}개 휴게소)")
    for cid, g in t.groupby("corridor_id"):
        print(f"\n  [{cid}] {len(g)}곳 · 충전기 {g['n_chargers'].sum():.0f}기")
        print(g[["name", "n_chargers", "amenity_index"]].head(5)
              .to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
        print("    ...")
        print(g[["name", "n_chargers", "amenity_index"]].tail(3)
              .to_string(index=False, header=False,
                         float_format=lambda v: f"{v:,.3f}"))
    print("\n⚠ 대리변수다. 진짜 편의시설 지표(data.go.kr 0316)가 오면 갈아끼울 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
