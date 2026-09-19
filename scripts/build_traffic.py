"""포털 원본을 정리본 parquet 로 만든다 (T-07 / T-08).

    python scripts/build_traffic.py

입력
    data/raw/ex_vds_*/                 scripts/fetch_traffic.py 수집본 (label 별 최신)
    data/processed/gyeongbu_route.json scripts/build_route.py 노선
    data/raw/ex_route_*/ic_all.json    전국 IC 좌표 (콘존 끝점 위치)
출력
    data/processed/traffic_gyeongbu.parquet
    data/processed/speed_gyeongbu.parquet
    data/processed/conzone_gyeongbu.parquet   콘존 위치표

검사 (하나라도 어긋나면 멈춘다)
    - 24시간이 다 있는 날의 시간대 합계 == 포털 일 합계
    - 값 범위 (교통량 음수·과대, 속도 0 이하·200 초과) — 파싱 단계에서
"""

from __future__ import annotations

import json
import sys

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402

from evdt.io import traffic  # noqa: E402
from evdt.io.route import ROUTE_PATH, GyeongbuRoute  # noqa: E402
from evdt.paths import DATA_PROCESSED_DIR, DATA_RAW_DIR  # noqa: E402


def main() -> int:
    collections = traffic.latest_collections(DATA_RAW_DIR)

    if not collections:
        print("수집본이 없습니다. 먼저: python scripts/fetch_traffic.py --help")
        return 1

    route = GyeongbuRoute.load()
    route_meta = json.loads(ROUTE_PATH.read_text(encoding="utf-8"))["meta"]
    ics = json.loads(
        (DATA_RAW_DIR / route_meta["raw_dir"] / "ic_all.json").read_text(encoding="utf-8")
    )

    outputs: dict[str, list[pd.DataFrame]] = {"volume": [], "speed": []}
    positions_all = []

    for label, raw_dir in sorted(collections.items()):
        manifest = traffic.read_manifest(raw_dir)
        print(f"[{label}] {raw_dir.name}")

        conzones = traffic.conzones_from_raw(raw_dir)
        positions = traffic.conzone_positions(conzones, route, ics)
        positions["period"] = label
        positions_all.append(positions)

        n_in = int(positions["in_corridor"].sum())
        n_interp = int((positions["in_corridor"] & (positions["position_source"] != "ic")).sum())
        print(f"  콘존 {len(positions)}개 중 코리도 안 {n_in}개 (보간 위치 {n_interp}개)")

        for dataset in ("volume", "speed"):
            if dataset not in manifest["datasets"]:
                continue
            hourly = traffic.read_dataset(raw_dir, dataset)

            if dataset == "volume" and "volume_daily" in manifest["datasets"]:
                daily = traffic.read_dataset(raw_dir, "volume_daily")
                bad = traffic.check_daily_totals(hourly, daily)
                if len(bad):
                    print(bad.head(20).to_string(index=False))
                    raise SystemExit(f"[{label}] 시간 합계 != 일 합계: {len(bad)}건")
                print("  시간 합계 == 포털 일 합계 (결측 없는 날 전부)")

            dates = pd.date_range(manifest["start"], manifest["end"], freq="D")
            tidy = traffic.tidy(hourly, positions, dataset, label, dates)
            no_data = tidy.loc[tidy["missing_code"] == traffic.NO_DATA_CODE, "conzone_name"]
            if len(no_data):
                print(
                    f"  {dataset}: 포털이 데이터를 주지 않은 콘존·시간 {len(no_data):,} "
                    f"({no_data.nunique()}개 콘존: {', '.join(sorted(set(no_data))[:8])})"
                )
            outputs[dataset].append(tidy)

    DATA_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    for dataset, name in (("volume", "traffic_gyeongbu"), ("speed", "speed_gyeongbu")):
        if not outputs[dataset]:
            continue
        df = pd.concat(outputs[dataset], ignore_index=True)
        path = DATA_PROCESSED_DIR / f"{name}.parquet"
        df.to_parquet(path, index=False)
        print(f"\n{path.name}: {len(df):,}행")
        print(traffic.missing_summary(df, traffic.VALUE_COLUMN[dataset]).to_string(index=False))

    pd.concat(positions_all, ignore_index=True).to_parquet(
        DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet", index=False
    )

    # T-08: 교통량과 속도가 같은 지점·같은 시각에 짝지어지는가
    if outputs["volume"] and outputs["speed"]:
        key = ["period", "conzone_id", "date", "hour"]
        vol = pd.concat(outputs["volume"])
        spd = pd.concat(outputs["speed"])
        paired = vol.merge(spd[key + ["speed_kmh"]], on=key, how="outer")
        has_v = paired["volume_veh"].notna()
        has_s = paired["speed_kmh"].notna()
        only_one = paired[has_v ^ has_s]
        print(
            f"\n교통량·속도 짝 (전체 {len(paired):,} 시간): 둘 다 있음 {int((has_v & has_s).sum()):,}"
            f" / 둘 다 결측 {int((~has_v & ~has_s).sum()):,}"
            f" / 한쪽만 {len(only_one):,} (콘존 {only_one['conzone_id'].nunique()}개)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
