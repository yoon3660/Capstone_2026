"""2026 평시(3/2~3/11) 충전이력 실측표 — #167 설과 같은 규칙 (#178).

    python scripts/issue178_observed_march.py

입력
    data/raw/issue178_charging/20260402 2026년3월 기후부 충전기 충전이력(상세내역).xlsx
    data/processed/sp3_64_current_charger_supply.csv   경부선 35곳 (방향, 이름)

휴게소 붙이기는 #167(`issue167_seollal_s0_source_scope_v3.extract_observed`)과 같다.
    - 원본 행을 `analyze_sp3_64_charging_history.iter_xlsx_rows` 로 그대로 읽는다
    - `evdt.io.charger_ingest.resolve_charger_key` 로 충전소명 → (휴게소, 방향)
    - 이름은 `sp3_64_validate_20day_base215.norm` (공백 제거) 으로 정규화
    - 시작 시각이 [3/2 00:00, 3/12 00:00) 이면 시작 건수에 넣는다
    - 기간 전에 시작해 기간 안까지 이어진 충전은 점유시간에만 넣는다(이월)

출력 (data/processed/issue178_quiet202603/)
    observed_sessions.csv            기간과 겹치는 세션 전부 (원본 행 번호 포함)
    observed_station_day_hour.csv    direction, station_name, date, hour, starts, occupied_min
                                     (#167 seollal_observed_station_day_hour.csv 와 같은 키)
    observed_direction_day.csv       방향 × 날짜 시작 건수
    observed_devices.csv             휴게소별 실제로 찍힌 충전기 ID 와 건수 (공급 판단 재료)
    source_manifest.json             원본 해시·행 수·검산

총량은 판정에 쓰지 않는다(환경부·기후부 충전기만 담는다 — 민간 없음).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_sp3_64_charging_history import iter_xlsx_rows  # noqa: E402
from sp3_64_validate_20day_base215 import norm  # noqa: E402

from evdt.io.charger_ingest import resolve_charger_key  # noqa: E402

RAW = ROOT / "data/raw/issue178_charging/20260402 2026년3월 기후부 충전기 충전이력(상세내역).xlsx"
SUPPLY = ROOT / "data/processed/sp3_64_current_charger_supply.csv"
OUT = ROOT / "data/processed/issue178_quiet202603"
START = pd.Timestamp("2026-03-02")
END = pd.Timestamp("2026-03-12")
REQUIRED = {"충전소명", "충전기ID", "충전시작일시", "충전종료일시", "충전량"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def extract(xlsx: Path, known: set[tuple[str, str]]) -> tuple[pd.DataFrame, dict]:
    rows = iter_xlsx_rows(xlsx)
    hdr = [str(x).strip() for x in next(rows)]
    cols = {x: i for i, x in enumerate(hdr)}
    if REQUIRED - set(cols):
        raise SystemExit(f"[중단] 원본에 필요한 열이 없다: {REQUIRED - set(cols)}")

    def get(row, k):
        i = cols.get(k)
        return "" if i is None or i >= len(row) else str(row[i]).strip()

    found, inspected, bad, months = [], 0, 0, {}
    for source_row, row in enumerate(rows, start=2):
        inspected += 1
        st_text = get(row, "충전시작일시").split(".")[0]
        months[st_text[:6]] = months.get(st_text[:6], 0) + 1
        key = resolve_charger_key(get(row, "충전소명"), known)
        if key is None:
            continue
        try:
            st = datetime.strptime(st_text, "%Y%m%d%H%M%S")
            et = datetime.strptime(get(row, "충전종료일시").split(".")[0], "%Y%m%d%H%M%S")
        except ValueError:
            bad += 1
            continue
        if et <= START.to_pydatetime() or st >= END.to_pydatetime():
            continue
        found.append({
            "source_row": source_row, "direction": key[1], "station_name": norm(key[0]),
            "source_station_name": get(row, "충전소명"),
            "observed_charger_id": get(row, "충전기ID").zfill(2),
            "observed_charger_type": get(row, "충전기타입"),
            "observed_charger_power": get(row, "충전기용량(KW)"),
            "start": pd.Timestamp(st), "end": pd.Timestamp(et),
            "energy_kwh": pd.to_numeric(get(row, "충전량"), errors="coerce"),
            "reported_charge_raw": get(row, "충전시간"),
            "elapsed_min": (et - st).total_seconds() / 60,
            "in_start_window": bool(START <= pd.Timestamp(st) < END)})
        if inspected % 200000 == 0:
            print(f"  원본 {inspected:,}행 읽음", flush=True)
    obs = pd.DataFrame(found)
    if obs.empty:
        raise SystemExit("[중단] 35곳에 붙은 세션이 없다")
    obs["reported_charge_min"] = (pd.to_timedelta(obs["reported_charge_raw"], errors="coerce")
                                  .dt.total_seconds() / 60)
    obs["date"] = obs["start"].dt.strftime("%Y-%m-%d")
    obs["hour"] = obs["start"].dt.hour
    info = {"original_rows_scanned": inspected, "bad_time_rows": bad,
            "start_month_counts": dict(sorted(months.items()))}
    return obs, info


def occupied(obs: pd.DataFrame) -> pd.DataFrame:
    """세션을 기간 안 시간 칸으로 잘라 점유 분을 더한다(이월 포함)."""

    rec = {}
    for r in obs.itertuples():
        a, b = max(r.start, START), min(r.end, END)
        t = a.floor("h")
        while t < b:
            seg = (min(b, t + pd.Timedelta(hours=1)) - max(a, t)).total_seconds() / 60
            k = (r.direction, r.station_name, t.strftime("%Y-%m-%d"), t.hour)
            rec[k] = rec.get(k, 0.0) + seg
            t += pd.Timedelta(hours=1)
    return pd.Series(rec, name="occupied_min").rename_axis(
        ["direction", "station_name", "date", "hour"]).reset_index()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, default=RAW)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    supply = pd.read_csv(SUPPLY, encoding="utf-8-sig")
    known = set(zip(supply.station_name.astype(str), supply.direction.astype(str), strict=True))
    obs, info = extract(args.raw, known)
    sel = obs[obs["in_start_window"]]

    # 관측 휴게소의 시간 칸은 0 을 채운다 (#167 과 같다). 관측 없는 휴게소는 N/A 로 뺀다
    seen = sel[["direction", "station_name"]].drop_duplicates()
    days = pd.date_range(START, END - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    grid = seen.merge(pd.DataFrame({"date": days}), how="cross").merge(
        pd.DataFrame({"hour": range(24)}), how="cross")
    starts = sel.groupby(["direction", "station_name", "date", "hour"]).size().rename("starts")
    occ = occupied(obs).set_index(["direction", "station_name", "date", "hour"])["occupied_min"]
    sdh = grid.set_index(["direction", "station_name", "date", "hour"])
    sdh = sdh.join(starts).join(occ).fillna(0.0).reset_index()
    sdh["starts"] = sdh["starts"].astype(int)
    if int(sdh["starts"].sum()) != len(sel):
        raise SystemExit("[중단] 시간표 합계가 세션 수와 다르다")

    obs.to_csv(args.out / "observed_sessions.csv", index=False, encoding="utf-8-sig")
    sdh.to_csv(args.out / "observed_station_day_hour.csv", index=False, encoding="utf-8-sig")
    dd = sel.groupby(["direction", "date"]).size().rename("starts").reset_index()
    dd.to_csv(args.out / "observed_direction_day.csv", index=False, encoding="utf-8-sig")
    dev = (sel.groupby(["direction", "station_name", "observed_charger_id",
                        "observed_charger_power"]).size().rename("starts").reset_index())
    dev.to_csv(args.out / "observed_devices.csv", index=False, encoding="utf-8-sig")

    all_sites = supply[["direction", "station_name"]].assign(station_name=lambda x: x.station_name.map(norm))
    unseen = all_sites.merge(seen, how="left", indicator=True)
    unseen = unseen[unseen["_merge"] == "left_only"]
    gap = (sel["reported_charge_min"] - sel["elapsed_min"]).abs()
    manifest = {
        "issue": 178, "script": "scripts/issue178_observed_march.py",
        "raw_xlsx": str(args.raw.relative_to(ROOT)) if args.raw.is_relative_to(ROOT) else str(args.raw),
        "raw_xlsx_bytes": args.raw.stat().st_size, "raw_xlsx_sha256": digest(args.raw),
        "window": [str(START), str(END)], "window_rule": "start in [start, end)",
        **info,
        "sessions_overlapping": len(obs), "starts_in_window": len(sel),
        "carry_in_sessions": int((~obs["in_start_window"] & (obs["start"] < START)).sum()),
        "starts_by_direction": sel.groupby("direction").size().to_dict(),
        "observed_sites": len(seen), "unobserved_sites": unseen[["direction", "station_name"]]
        .apply(lambda r: f"{r.station_name} {r.direction}", axis=1).tolist(),
        "duplicates_exact": int(sel.duplicated(["station_name", "direction", "observed_charger_id",
                                                "start", "end"]).sum()),
        "zero_energy": int((sel["energy_kwh"] == 0).sum()),
        "missing_energy": int(sel["energy_kwh"].isna().sum()),
        "elapsed_over_120min": int((sel["elapsed_min"] > 120).sum()),
        "reported_vs_elapsed_gap_over_1min": int((gap > 1).sum()),
        "reported_vs_elapsed_gap_over_1min_share": float((gap > 1).mean()),
        "scope": "환경부·기후부 충전기만 (민간 미포함). 총량 판정에 쓰지 않는다",
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (args.out / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("starts_in_window", "starts_by_direction",
                                               "observed_sites", "unobserved_sites",
                                               "carry_in_sessions", "duplicates_exact")},
                     ensure_ascii=False, indent=1))
    print(dd.pivot(index="date", columns="direction", values="starts").to_string())
    return 0


if __name__ == "__main__":
    np.seterr(all="ignore")
    raise SystemExit(main())
