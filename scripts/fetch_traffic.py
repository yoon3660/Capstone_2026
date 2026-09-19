"""경부선 구간(콘존) 교통량·속도 원본을 포털에서 받는다 (T-07 / T-08).

    python scripts/fetch_traffic.py --start 2026-02-13 --end 2026-02-22 --label seollal2026
    python scripts/fetch_traffic.py --start 2026-03-06 --end 2026-03-15 --label base202603
    python scripts/fetch_traffic.py --start 2026-09-21 --end 2026-10-04 --label chuseok2026 \\
        --dataset volume speed volume_daily

인증키가 필요 없다. 포털 조회 한도(30일)를 넘는 기간은 알아서 나눠 받는다.

저장 (원본은 손대지 않는다, T-05 원칙)
    data/raw/ex_vds_<label>_<시작>_<끝>_<수집시각>/
        conzones_DOWN.json, conzones_UP.json   콘존 목록 응답
        <dataset>/<conzoneId>_<창시작>_<창끝>.json   포털 응답 그대로
        manifest.json                           요청 파라미터·수집 시각·건수

포털 화면의 CSV 버튼은 이 JSON 을 브라우저에서 표로 바꾼 것이라 JSON 이 원본이다.
정리본은 scripts/build_traffic.py 가 만든다.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)

from evdt.io import ex_portal
from evdt.paths import DATA_RAW_DIR

#: 요청 사이 간격 (초). 공용 포털이라 몰아서 두드리지 않는다.
REQUEST_INTERVAL_S = 0.2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, type=date.fromisoformat, help="시작일 YYYY-MM-DD")
    ap.add_argument("--end", required=True, type=date.fromisoformat, help="종료일 YYYY-MM-DD (포함)")
    ap.add_argument("--label", required=True, help="기간 이름 (예: seollal2026, base202603)")
    ap.add_argument(
        "--dataset",
        nargs="+",
        default=["volume", "speed", "volume_daily"],
        choices=sorted(ex_portal.DATASETS),
    )
    args = ap.parse_args()

    if args.end >= date.today():
        raise SystemExit(f"아직 끝나지 않은 날짜가 들어 있습니다: {args.end} (오늘 {date.today()})")

    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    out = DATA_RAW_DIR / (
        f"ex_vds_{args.label}_{args.start:%Y%m%d}_{args.end:%Y%m%d}_{stamp}"
    )
    out.mkdir(parents=True, exist_ok=False)

    conzones = []

    for direction in ("DOWN", "UP"):
        parsed, raw = ex_portal.fetch_conzones(direction)
        (out / f"conzones_{direction}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        conzones.extend(parsed)

    windows = ex_portal.date_windows(args.start, args.end)
    n_files = 0

    for dataset in args.dataset:
        (out / dataset).mkdir()

        for i, conzone in enumerate(conzones, start=1):
            for w_start, w_end in windows:
                raw = ex_portal.fetch_window(dataset, conzone.conzone_id, w_start, w_end)
                name = f"{conzone.conzone_id}_{w_start:%Y%m%d}_{w_end:%Y%m%d}.json"
                (out / dataset / name).write_text(
                    json.dumps(raw, ensure_ascii=False), encoding="utf-8"
                )
                n_files += 1
                time.sleep(REQUEST_INTERVAL_S)

            if i % 20 == 0 or i == len(conzones):
                print(f"  {dataset}: {i}/{len(conzones)} 콘존")

    manifest = {
        "label": args.label,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "datasets": args.dataset,
        "windows": [[a.isoformat(), b.isoformat()] for a, b in windows],
        "n_conzones": len(conzones),
        "n_files": n_files,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": ex_portal.PORTAL_URL,
        "endpoints": {k: {"path": v[0], "xAxis": v[1]} for k, v in ex_portal.DATASETS.items()},
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print("저장:", out)
    print(f"콘존 {len(conzones)}개 × 창 {len(windows)}개 × {len(args.dataset)} 종 = 파일 {n_files}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
