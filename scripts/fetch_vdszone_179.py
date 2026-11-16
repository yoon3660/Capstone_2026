"""도로공사 「주요노선 구간별 교통량」에서 경부선 VDS 지점 단위 시간 교통량·속도를 받는다 (#179).

    python scripts/fetch_vdszone_179.py --start 2026-03-02 --end 2026-03-11

페이지
    https://data.ex.co.kr/visual/vdszone?date=YYYYMMDD
    화면 그래프용 자료가 페이지 안 `initVdsRangeA('<JSON>')` 에 들어 있다. CSV 버튼은 없다.
    JSON 키: vdsStart · vdsEnd (VDS 지점 × 시간), conzoneStart · conzoneEnd (콘존 × 시간)
    VDS 행: vds_id, conzone_id, collect_hour, traffic(대/시), avg_speed(km/h), vds_length(m)

저장 (원본은 고치지 않는다)
    data/raw/ex_vdszone_<label>/vdszone_<YYYYMMDD>.json   페이지에서 떼어 낸 JSON 그대로
    data/raw/ex_vdszone_<label>/manifest.json            날짜별 URL·페이지 크기·SHA-256·JSON SHA-256

인증키가 필요 없다. 날짜 하나에 페이지 하나(약 3MB)라 요청 사이에 간격을 둔다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen

import _bootstrap  # noqa: F401

from evdt.paths import DATA_RAW_DIR

URL = "https://data.ex.co.kr/visual/vdszone?date={d:%Y%m%d}"
MARK = "initVdsRangeA('"
INTERVAL_S = 1.0


def extract(page: str) -> str:
    i = page.find(MARK)
    if i < 0:
        raise ValueError("페이지에 initVdsRangeA 자료가 없다")
    i += len(MARK)
    j = page.find("')", i)
    text = page[i:j]
    json.loads(text)            # 형식 확인만
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--label", default="quiet202603")
    args = ap.parse_args()
    out = DATA_RAW_DIR / f"ex_vdszone_{args.label}"
    out.mkdir(parents=True, exist_ok=True)

    records = []
    d = args.start
    while d <= args.end:
        url = URL.format(d=d)
        path = out / f"vdszone_{d:%Y%m%d}.json"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (evdt capstone data collection)"})
        for attempt in range(4):
            try:
                with urlopen(req, timeout=120) as r:
                    raw = r.read()
                break
            except Exception as exc:  # noqa: BLE001 — 끊김·시간초과 모두 다시 묻는다
                if attempt == 3:
                    raise
                print(f"  재시도 {d}: {type(exc).__name__}", flush=True)
                time.sleep(3 * (attempt + 1))
        text = extract(raw.decode("utf-8", errors="replace"))
        path.write_text(text, encoding="utf-8")
        j = json.loads(text)
        records.append({
            "date": d.isoformat(), "url": url, "page_bytes": len(raw),
            "page_sha256": hashlib.sha256(raw).hexdigest(),
            "json_file": path.name, "json_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "rows": {k: len(v) for k, v in j.items() if isinstance(v, list)}})
        print(f"  {d}: {records[-1]['rows']}", flush=True)
        time.sleep(INTERVAL_S)
        d += timedelta(days=1)

    (out / "manifest.json").write_text(json.dumps({
        "issue": 179, "script": "scripts/fetch_vdszone_179.py", "label": args.label,
        "start": args.start.isoformat(), "end": args.end.isoformat(),
        "source": "https://data.ex.co.kr/visual/vdszone (주요노선 구간별 교통량)",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "records": records}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("저장:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
