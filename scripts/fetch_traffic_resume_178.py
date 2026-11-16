"""`fetch_traffic.py` 로 받다 끊긴 폴더를 이어 받는다 (#178).

    python scripts/fetch_traffic_resume_178.py data/raw/ex_vds_quiet202603_20260302_20260311_<시각>

왜
    포털이 응답 도중 연결을 끊으면(`http.client.IncompleteRead`) `ex_portal._post` 의
    재시도 목록에 없는 예외라 `fetch_traffic.py` 가 통째로 죽는다. 기존 파일은 고치지
    않고, 이 스크립트가 그 예외를 잡아 다시 묻는다.

하는 일
    - 폴더 이름에서 label·기간을 읽고, 이미 저장된 `<dataset>/<콘존>_<창>.json` 은 건너뛴다
    - 저장 형식·파일 이름·manifest 는 `fetch_traffic.py` 와 같다 (`build_traffic.py` 가 그대로 읽는다)
    - manifest 에 `resumed_at` 과 재시도 횟수를 더 적는다
"""

from __future__ import annotations

import argparse
import http.client
import json
import time
from datetime import date, datetime
from pathlib import Path

import _bootstrap  # noqa: F401

from evdt.io import ex_portal

REQUEST_INTERVAL_S = 0.2
RETRIES = 6


def _parse_dir(path: Path) -> tuple[str, date, date]:
    # ex_vds_<label>_<YYYYMMDD>_<YYYYMMDD>_<stamp>_<stamp>
    parts = path.name.split("_")
    label = "_".join(parts[2:-4])
    return label, date.fromisoformat(_iso(parts[-4])), date.fromisoformat(_iso(parts[-3]))


def _iso(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _fetch(dataset: str, conzone_id: str, a: date, b: date, stats: dict) -> object:
    for attempt in range(RETRIES):
        try:
            return ex_portal.fetch_window(dataset, conzone_id, a, b)
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                ConnectionError, ex_portal.PortalError) as exc:
            stats["retries"] += 1
            print(f"    재시도 {attempt + 1}/{RETRIES}: {dataset} {conzone_id} ({type(exc).__name__})",
                  flush=True)
            time.sleep(3 * (attempt + 1))
    raise SystemExit(f"[중단] {RETRIES}회 실패: {dataset} {conzone_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--dataset", nargs="+", default=["volume", "speed", "volume_daily"],
                    choices=sorted(ex_portal.DATASETS))
    args = ap.parse_args()
    out = Path(args.folder)
    label, start, end = _parse_dir(out)

    conzones = []
    for direction in ("DOWN", "UP"):
        parsed, raw = ex_portal.fetch_conzones(direction)
        f = out / f"conzones_{direction}.json"
        if not f.is_file():
            f.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        conzones.extend(parsed)

    windows = ex_portal.date_windows(start, end)
    stats = {"retries": 0, "fetched": 0, "skipped": 0}
    for dataset in args.dataset:
        (out / dataset).mkdir(exist_ok=True)
        for i, cz in enumerate(conzones, start=1):
            for a, b in windows:
                name = out / dataset / f"{cz.conzone_id}_{a:%Y%m%d}_{b:%Y%m%d}.json"
                if name.is_file() and name.stat().st_size > 0:
                    stats["skipped"] += 1
                    continue
                raw = _fetch(dataset, cz.conzone_id, a, b, stats)
                name.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                stats["fetched"] += 1
                time.sleep(REQUEST_INTERVAL_S)
            if i % 20 == 0 or i == len(conzones):
                print(f"  {dataset}: {i}/{len(conzones)} 콘존", flush=True)

    n_files = sum(len(list((out / d).glob("*.json"))) for d in args.dataset)
    manifest = {
        "label": label, "start": start.isoformat(), "end": end.isoformat(),
        "datasets": args.dataset,
        "windows": [[a.isoformat(), b.isoformat()] for a, b in windows],
        "n_conzones": len(conzones), "n_files": n_files,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "resumed_by": "scripts/fetch_traffic_resume_178.py", "resume_stats": stats,
        "source": ex_portal.PORTAL_URL,
        "endpoints": {k: {"path": v[0], "xAxis": v[1]} for k, v in ex_portal.DATASETS.items()},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print("저장:", out, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
