"""표준노드링크 캐시에서 특정 링크의 원본 속성을 그대로 들여다본다.

build_lane_profile.py 가 "본선일 수 없는 차로수" 를 보고했을 때, 그 링크가 실제로
무엇인지(램프인지, 원본 LANES 가 정말 1인지, 어디쯤인지) 확인하기 위한 진단용이다.
파이프라인을 다시 돌리지 않고 lane_mapping_audit.parquet 만 읽는다.

사용법:
    python scripts/inspect_links.py 3520811800 3520811801
    python scripts/inspect_links.py --km 78.2 81.9 --direction UP
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
import pandas as pd

from evdt.paths import DATA_PROCESSED_DIR

CACHE_PATH = DATA_PROCESSED_DIR / "lane_mapping_audit.parquet"

COLUMNS = [
    "link_id",
    "lanes",
    "connect",
    "road_type",
    "max_spd",
    "movement",
    "m_start",
    "m_end",
    "source_length_m",
    "projected_length_km",
    "max_snap_m",
    "f_node",
    "t_node",
    "mid_lat",
    "mid_lon",
]

MOVEMENT_TO_DIRECTION = {"INCREASE": "UP", "DECREASE": "DOWN"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("link_ids", nargs="*", help="들여다볼 LINK_ID 목록")
    parser.add_argument(
        "--km",
        nargs=2,
        type=float,
        metavar=("START", "END"),
        help="이정 구간으로 찾기 (km)",
    )
    parser.add_argument("--direction", choices=("UP", "DOWN"), help="--km 과 함께 사용")
    parser.add_argument(
        "--context-km",
        type=float,
        default=2.0,
        help="찾은 링크 앞뒤로 함께 보여줄 범위 (기본 2 km)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.link_ids and args.km is None:
        raise SystemExit("LINK_ID 를 주거나 --km START END 를 주세요.")

    if not CACHE_PATH.exists():
        raise SystemExit(
            f"캐시가 없습니다: {CACHE_PATH}\n먼저 실행할 것:  python scripts/audit_lane_mapping.py"
        )

    audit = pd.read_parquet(CACHE_PATH)
    audit["direction"] = audit["movement"].map(MOVEMENT_TO_DIRECTION)
    audit["km_start"] = audit[["m_start", "m_end"]].min(axis=1)
    audit["km_end"] = audit[["m_start", "m_end"]].max(axis=1)

    if args.link_ids:
        wanted = audit[audit["link_id"].astype(str).isin([str(x) for x in args.link_ids])]

        missing = set(map(str, args.link_ids)) - set(wanted["link_id"].astype(str))
        if missing:
            print(f"캐시에 없는 LINK_ID: {sorted(missing)}")
        if wanted.empty:
            return 1

        low = float(wanted["km_start"].min()) - args.context_km
        high = float(wanted["km_end"].max()) + args.context_km
        directions = sorted(wanted["direction"].dropna().unique())
    else:
        low, high = min(args.km) - args.context_km, max(args.km) + args.context_km
        directions = [args.direction] if args.direction else ["UP", "DOWN"]

    columns = [c for c in COLUMNS if c in audit.columns]

    for direction in directions:
        part = audit[
            (audit["direction"] == direction)
            & (audit["km_end"] > low)
            & (audit["km_start"] < high)
        ].sort_values("km_start")

        print()
        print(f"=== {direction}  {low:.3f} ~ {high:.3f} km  ({len(part)}개 링크) ===")

        if part.empty:
            continue

        print(part[columns].to_string(index=False))

        print()
        print("  CONNECT 분포:", part["connect"].astype(str).value_counts().to_dict())
        print("  LANES 분포  :", part["lanes"].value_counts().sort_index().to_dict())

    print()
    print("읽는 법: connect='0' 이면 원본이 본선이라고 말하는 링크입니다.")
    print("         그런데 lanes=1 이면 원본 속성이 틀린 것이므로 이웃 차로수로 메웁니다.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
