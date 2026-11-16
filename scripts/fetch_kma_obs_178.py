"""기상청 날씨누리 관측 페이지로 재현 기간 기온 표본을 받는다 (#178).

    python scripts/fetch_kma_obs_178.py --start 2026-03-02 --end 2026-03-11

#145·#146 추석과 같은 방식이다.
    지점   서울 108 · 대전 133 · 대구 143 · 부산 159 (경부선 축)
    시각   매일 00 / 06 / 12 / 18시 (KST)
    페이지 https://www.weather.go.kr/w/weather/land/aws-obs.do?db=MINDB_60M&tm=<YYYY.MM.DD HH:MM>&stnId=<지점>&sidoCode=asos
    값     표 첫 행(= 조회 시각)의 기온(°C)

저장
    data/raw/kma_<label>/kma_<지점>_<MMDD>_<HH>.html     받은 그대로
    data/processed/issue178_<label>/kma_observation_samples.csv
        station, time_kst, url, temp_c, path, sha256, error   (추석 표본과 같은 열)
    data/processed/issue178_<label>/kma_daily_mean.csv
        date, n, temp_c   — 4지점 × 4시각 = 16개 동일 가중 평균 (추석 config 와 같은 정의)

일평균은 24시간 연속 관측 평균이 아니라 16개 표본 평균이다.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import _bootstrap  # noqa: F401
import pandas as pd

from evdt.paths import DATA_PROCESSED_DIR, DATA_RAW_DIR

STATIONS = (108, 133, 143, 159)
HOURS = (0, 6, 12, 18)
BASE = "https://www.weather.go.kr/w/weather/land/aws-obs.do"
INTERVAL_S = 0.5


def url_for(stn: int, d: date, hour: int) -> str:
    q = {"db": "MINDB_60M", "tm": f"{d:%Y.%m.%d} {hour:02d}:00", "stnId": stn,
         "sidoCode": "asos", "sort": "", "config": ""}
    return BASE + "?" + urlencode(q)


def first_row_temp(html: str, hour: int) -> float:
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    body = html[html.find("<tbody>"):html.find("</tbody>")]
    row = re.search(r"<tr>(.*?)</tr>", body, re.S)
    if not row:
        raise ValueError("표 행 없음")
    cells = [re.sub(r"<[^>]+>", "", c).strip()
             for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
    if cells[0] != f"{hour:02d}:00":
        raise ValueError(f"첫 행 시각 불일치: {cells[0]}")
    return float(cells[3])          # 시:분 · 1시간강수 · 일강수 · 기온


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, type=date.fromisoformat)
    ap.add_argument("--end", required=True, type=date.fromisoformat)
    ap.add_argument("--label", default="quiet202603")
    args = ap.parse_args()

    raw_dir = DATA_RAW_DIR / f"kma_{args.label}"
    out_dir = DATA_PROCESSED_DIR / f"issue178_{args.label}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    d = args.start
    while d <= args.end:
        for stn in STATIONS:
            for h in HOURS:
                url = url_for(stn, d, h)
                path = raw_dir / f"kma_{stn}_{d:%m%d}_{h:02d}.html"
                rec = {"station": stn, "time_kst": f"{d:%Y.%m.%d} {h:02d}:00", "url": url,
                       "temp_c": None, "path": path.relative_to(DATA_RAW_DIR.parent.parent).as_posix(), "sha256": "", "error": ""}
                try:
                    if not path.is_file():
                        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urlopen(req, timeout=60) as r:
                            path.write_bytes(r.read())
                        time.sleep(INTERVAL_S)
                    data = path.read_bytes()
                    rec["sha256"] = hashlib.sha256(data).hexdigest()
                    rec["temp_c"] = first_row_temp(data.decode("utf-8", errors="replace"), h)
                except Exception as exc:  # noqa: BLE001 — 실패도 표본표에 남긴다
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(rec)
        print(f"  {d}: {sum(1 for r in rows if r['time_kst'].startswith(f'{d:%Y.%m.%d}') and r['temp_c'] is not None)}/16",
              flush=True)
        d += timedelta(days=1)

    s = pd.DataFrame(rows)
    s.to_csv(out_dir / "kma_observation_samples.csv", index=False, encoding="utf-8-sig")
    s["date"] = s["time_kst"].str[:10].str.replace(".", "-", regex=False)
    daily = (s.dropna(subset=["temp_c"]).groupby("date")["temp_c"]
             .agg(n="size", temp_c="mean").reset_index())
    daily.to_csv(out_dir / "kma_daily_mean.csv", index=False, encoding="utf-8-sig")
    print(daily.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(f"실패 {int((s['error'] != '').sum())}건 · 저장 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
