import json
from pathlib import Path

import pandas as pd


od = pd.read_parquet("data/processed/tcs_od_all.parquet")

tcs_names = set(
    od["start_office"]
    .dropna()
    .astype(str)
    .str.strip()
)

raw_dirs = sorted(
    Path("data/raw").glob("ex_tcs_offices_*")
)

if not raw_dirs:
    raise RuntimeError("ex_tcs_offices_* raw 디렉터리가 없습니다.")

raw_path = raw_dirs[-1] / "offices_gyeongbu.json"

items = json.loads(
    raw_path.read_text(encoding="utf-8")
)

print("사용 raw:", raw_path)

print("\n=== API 항목 중 TCS Matrix에도 존재 ===")

matched = []

for item in items:
    name = str(item.get("unitName") or "").strip()

    if name in tcs_names:
        matched.append(name)

        print(
            f"{name:12s} "
            f"x={item.get('xValue')} "
            f"y={item.get('yValue')}"
        )

print("\n개수:", len(matched))


print("\n=== TCS에는 있지만 좌표가 없는 API 항목 ===")

for item in items:
    name = str(item.get("unitName") or "").strip()

    if name not in tcs_names:
        continue

    x = item.get("xValue")
    y = item.get("yValue")

    if x in (None, "") or y in (None, ""):
        print(" -", name)


print("\n=== API에는 있지만 TCS Matrix에는 없는 항목 ===")

for item in items:
    name = str(item.get("unitName") or "").strip()

    if name and name not in tcs_names:
        print(" -", name)