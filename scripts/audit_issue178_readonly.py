"""#178 평시(3/2~3/11) 입력 읽기 전용 감사 — #146 추석 감사와 같은 검사 (#178).

    python scripts/audit_issue178_readonly.py --raw data/raw/ex_vds_quiet202603_<...> \\
        --report-dir data/processed/issue178_quiet202603/audit

`audit_issue134_readonly.py`(설)·`audit_issue146_readonly.py`(추석)와 같은 검사를 한다.
기존 감사 파일은 고치지 않고, 같은 검사 함수(`key_difference`, `audit_coordinates`)를 가져다 쓴다.

    VDS      저장본 = 원본 재정리, 교통량·속도 키 일치
    날짜·방향 진입·진출 재계산 = 저장본, 시간 0~23, 진입 원장 = 재계산, 진입량 = 고정 기대값
    OD       중복 키 없음, 진입량 보존 (build 단계 check_od 재실행)
    기온     날마다 16개 표본, 일평균 = 저장본
    실측     시간표 합계 = 시작 건수 고정값, 관측 휴게소 29곳
    DB       코리도 길이·셀·휴게소 수, 원본 좌표 재투영 (#146 과 같다)

고정 기대값(ENTRIES·OBS_STARTS)은 2026-11-16 첫 생성 결과다. 어긋나면 실패한다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT
from audit_issue146_readonly import audit_coordinates, key_difference
from build_chuseok_demand import check_od, vds_profiles
from build_quiet_demand_178 import DAYS, DIRECTIONS, PERIOD, positions, tidy

from evdt.io.route import GyeongbuRoute
from evdt.paths import default_db_path

OUT = ROOT / "data/processed/issue178_quiet202603"

#: 첫 생성 결과로 고정 (build_quiet_demand_178.py, 2026-11-16)
ENTRIES: dict[str, list[int]] = {
    "DOWN": [255391, 309550, 318576, 312116, 339614, 304896, 259393, 319983, 312698, 314327],
    "UP": [285291, 290592, 296973, 300068, 306813, 269602, 251487, 300101, 294674, 300784],
}
OBS_STARTS = {"DOWN": 4299, "UP": 6674}


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def check(self, name: str, got, expected) -> None:
        ok = got == expected
        self.rows.append({"check": name, "got": repr(got), "expected": repr(expected), "ok": bool(ok)})
        if not ok:
            print(f"  [실패] {name}: {got!r} != {expected!r}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--report-dir", type=Path, default=OUT / "audit")
    args = ap.parse_args()
    a = Audit()
    processed = ROOT / "data/processed"
    length = GyeongbuRoute.load().length_km
    differences: list[pd.DataFrame] = []

    vds, speed = tidy(args.raw, positions(processed), PERIOD, DAYS, check_daily=True)
    stored = pd.read_parquet(OUT / "traffic_gyeongbu_quiet.parquet")
    stored_speed = pd.read_parquet(OUT / "speed_gyeongbu_quiet.parquet")
    keys = ["date", "direction", "conzone_id", "hour"]
    key_difference(a, "VDS stored vs recomputed", stored, vds, keys, differences)
    key_difference(a, "VDS volume vs speed", stored, stored_speed, keys, differences)
    a.check("VDS stored rows match raw recomputation",
            stored.reset_index(drop=True).equals(vds.reset_index(drop=True)), True)

    entries: dict[str, list[int]] = {d: [] for d in DIRECTIONS}
    for direction in DIRECTIONS:
        for day in DAYS:
            tag = f"{day.date()} {direction}"
            ramps, head, points = vds_profiles(vds, PERIOD, direction, day)
            folder = OUT / str(day.date()) / direction
            s_ramps = pd.read_csv(folder / "entry_exit.csv")
            key_difference(a, f"{tag} ramps", s_ramps.round({"offset_km": 6}),
                           ramps.round({"offset_km": 6}), ["hour", "offset_km"], differences)
            a.check(f"{tag} ramp keys and values", s_ramps.shape == ramps.shape and
                    bool(np.allclose(s_ramps, ramps, rtol=0, atol=1e-8)), True)
            volume = pd.read_csv(folder / "volume.csv")
            a.check(f"{tag} volume hours", volume.hour.tolist(), list(range(24)))
            a.check(f"{tag} volume recomputation",
                    bool(np.array_equal(volume.volume_veh, head.entry_veh)), True)
            ledger = pd.read_parquet(folder / "ctm_entries.parquet")
            key_difference(a, f"{tag} entry ledger", ledger, points, ["hour", "offset_km"], differences)
            a.check(f"{tag} entry keys and values", ledger.equals(points), True)
            n = int(round(ledger.entry_veh.sum()))
            entries[direction].append(n)
            if ENTRIES:
                a.check(f"{tag} entries (frozen)", n, ENTRIES[direction][len(entries[direction]) - 1])
            od = pd.read_parquet(folder / "hourly_od.parquet")
            a.check(f"{tag} OD duplicate keys",
                    int(od.duplicated(["hour", "start_offset_km", "end_offset_km"]).sum()), 0)
            try:
                check_od(od.rename(columns={}), points, length)
                a.check(f"{tag} OD conserves entries", True, True)
            except AssertionError as exc:
                a.check(f"{tag} OD conserves entries", str(exc)[:80], True)

    weather = pd.read_csv(OUT / "kma_observation_samples.csv")
    daily = pd.read_csv(OUT / "kma_daily_mean.csv")
    a.check("KMA sample errors", int(weather["error"].fillna("").ne("").sum()), 0)
    for day in DAYS:
        s = weather[weather.time_kst.str[:10].eq(f"{day:%Y.%m.%d}")]
        a.check(f"{day.date()} KMA sample size", int(s.temp_c.notna().sum()), 16)
        a.check(f"{day.date()} KMA mean", round(float(s.temp_c.mean()), 6),
                round(float(daily.loc[daily.date.eq(str(day.date())), "temp_c"].iloc[0]), 6))

    obs = pd.read_csv(OUT / "observed_station_day_hour.csv")
    for direction, n in OBS_STARTS.items():
        a.check(f"{direction} observed starts", int(obs.loc[obs.direction.eq(direction), "starts"].sum()), n)
    a.check("observed sites", int(obs[["direction", "station_name"]].drop_duplicates().shape[0]), 29)
    a.check("observed hour keys duplicate",
            int(obs.duplicated(["direction", "station_name", "date", "hour"]).sum()), 0)

    with sqlite3.connect(default_db_path().as_uri() + "?mode=ro", uri=True) as conn:
        zones, stations_cmp = audit_coordinates(a, ROOT, args.raw, processed, conn, differences)
        for direction, stations in (("DOWN", 19), ("UP", 16)):
            cid = f"gyeongbu_{direction.lower()}"
            db_len = conn.execute("select length_km from corridor where corridor_id=?",
                                  (cid,)).fetchone()[0]
            # DB 는 소수 셋째 자리로 저장한다 — 좌표 검사와 같은 1 m 허용
            a.check(f"{direction} DB length within 1 m", abs(db_len - length) <= 0.001, True)
            a.check(f"{direction} DB cells", conn.execute(
                "select count(*) from cell where corridor_id=?", (cid,)).fetchone()[0], 396)
            a.check(f"{direction} DB stations", conn.execute(
                "select count(*) from station where corridor_id=?", (cid,)).fetchone()[0], stations)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    rep = pd.DataFrame(a.rows)
    rep.to_csv(args.report_dir / "checks.csv", index=False, encoding="utf-8-sig")
    if differences:
        pd.concat(differences, ignore_index=True).to_csv(args.report_dir / "key_differences.csv", index=False)
    zones.to_csv(args.report_dir / "conzone_coordinates.csv", index=False)
    stations_cmp.to_csv(args.report_dir / "station_coordinates.csv", index=False)
    if not ENTRIES:
        print("고정할 진입량:", entries)
    print(f"검사 {len(rep)}개 · 통과 {int(rep.ok.sum())} · 실패 {int((~rep.ok).sum())}")
    return 0 if rep.ok.all() else 1


if __name__ == "__main__":
    sys.exit(main())
