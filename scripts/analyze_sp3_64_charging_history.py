from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

from evdt.io.charger_ingest import resolve_charger_key

# ============================================================
# 경로
# ============================================================

XLSX_PATH = Path("data/raw/sp3_64/20260306_2026년2월_환경부_충전기_충전이력_상세내역.xlsx")

SUPPLY_PATH = Path("data/processed/sp3_64_current_charger_supply.csv")

OUT_STATION = Path("data/processed/sp3_64_charging_validation_by_station.csv")

OUT_HOURLY = Path("data/processed/sp3_64_charging_hourly.csv")


FEB_START = datetime(2026, 2, 1)
MAR_START = datetime(2026, 3, 1)

SEOLLAL_START = datetime(2026, 2, 13)
SEOLLAL_END = datetime(2026, 2, 23)  # 2/18 23:59:59까지


# ============================================================
# XLSX XML 관련
# ============================================================

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def col_index(cell_ref: str) -> int:
    """A -> 0, B -> 1, AA -> 26"""
    letters = re.match(r"[A-Z]+", cell_ref)

    if not letters:
        return 0

    result = 0

    for ch in letters.group():
        result = result * 26 + (ord(ch) - ord("A") + 1)

    return result - 1


def load_shared_strings(z: ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"

    if path not in z.namelist():
        return []

    strings = []

    with z.open(path) as f:
        for _event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == f"{{{MAIN_NS}}}si":
                text = "".join(node.text or "" for node in elem.iter(f"{{{MAIN_NS}}}t"))
                strings.append(text)
                elem.clear()

    return strings


def find_first_sheet_path(z: ZipFile) -> str:
    workbook = ET.fromstring(z.read("xl/workbook.xml"))

    sheets = workbook.find(f"{{{MAIN_NS}}}sheets")

    if sheets is None or len(sheets) == 0:
        raise RuntimeError("XLSX에 시트가 없습니다.")

    sheet = sheets[0]

    rid = sheet.attrib[f"{{{REL_NS}}}id"]

    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))

    target = None

    for rel in rels:
        if rel.attrib.get("Id") == rid:
            target = rel.attrib.get("Target")
            break

    if not target:
        raise RuntimeError("워크시트 경로를 찾을 수 없습니다.")

    target = target.replace("\\", "/")

    if target.startswith("/"):
        target = target.lstrip("/")

    if not target.startswith("xl/"):
        target = "xl/" + target

    return target


def cell_value(cell, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")

    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))

    value_node = cell.find(f"{{{MAIN_NS}}}v")

    if value_node is None or value_node.text is None:
        return ""

    value = value_node.text

    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return value

    return value


def iter_xlsx_rows(path: Path):
    with ZipFile(path) as z:
        shared = load_shared_strings(z)
        sheet_path = find_first_sheet_path(z)

        with z.open(sheet_path) as f:
            for _event, elem in ET.iterparse(
                f,
                events=("end",),
            ):
                if elem.tag != f"{{{MAIN_NS}}}row":
                    continue

                row_dict = {}

                for cell in elem.findall(f"{{{MAIN_NS}}}c"):
                    ref = cell.attrib.get("r", "A1")
                    idx = col_index(ref)

                    row_dict[idx] = cell_value(
                        cell,
                        shared,
                    )

                if row_dict:
                    max_idx = max(row_dict)

                    row = [row_dict.get(i, "") for i in range(max_idx + 1)]

                    yield row

                elem.clear()


# ============================================================
# 프로젝트 station
# ============================================================


def load_supply():
    stations = []

    with SUPPLY_PATH.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        for row in reader:
            row["offset_km"] = float(row["offset_km"])

            row["current_total_chargers"] = int(row["current_total_chargers"])

            row["current_moe_chargers"] = int(row["current_moe_chargers"])

            row["current_other_chargers"] = int(row["current_other_chargers"])

            stations.append(row)

    return stations


def parse_dt(value: str) -> datetime | None:
    value = str(value or "").strip()

    if not value:
        return None

    value = value.split(".")[0]

    try:
        return datetime.strptime(
            value,
            "%Y%m%d%H%M%S",
        )
    except ValueError:
        return None


def parse_energy(value: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(
        minute=0,
        second=0,
        microsecond=0,
    )


def hour_range(start: datetime, end: datetime):
    cur = floor_hour(start)

    while cur < end:
        yield cur
        cur += timedelta(hours=1)


# ============================================================
# 메인
# ============================================================


def main():
    if not XLSX_PATH.exists():
        raise FileNotFoundError(XLSX_PATH)

    if not SUPPLY_PATH.exists():
        raise FileNotFoundError(SUPPLY_PATH)

    stations = load_supply()

    known_keys = {(s["station_name"], s["direction"]) for s in stations}



    # station별 실제 세션
    sessions = defaultdict(list)

    # source에 한 번이라도 등장했는지
    observed_keys = set()

    # 표시 ID / signature
    display_ids = defaultdict(set)
    signatures = defaultdict(set)

    # 매칭되지 않은 고속도로 휴게소명 감사용
    unmatched_names = defaultdict(int)

    rows = iter_xlsx_rows(XLSX_PATH)

    try:
        header = next(rows)
    except StopIteration as exc:
        raise RuntimeError("XLSX가 비어 있습니다.") from exc

    header_map = {str(name).strip(): idx for idx, name in enumerate(header)}

    required = [
        "충전소명",
        "충전기ID",
        "충전기타입",
        "충전기용량(KW)",
        "충전시작일시",
        "충전종료일시",
        "충전량",
    ]

    missing = [col for col in required if col not in header_map]

    if missing:
        raise RuntimeError(f"필수 컬럼이 없습니다: {missing}")

    def get(row, name):
        idx = header_map[name]

        if idx >= len(row):
            return ""

        return str(row[idx]).strip()

    total_xlsx_rows = 0
    matched_rows = 0
    bad_datetime_rows = 0

    print("XLSX 분석 중...")

    for row in rows:
        total_xlsx_rows += 1

        station_raw = get(row, "충전소명")

        key = resolve_charger_key(
            station_raw,
            known_keys,
        )

        if key is None:
            if "휴게소" in station_raw:
                unmatched_names[station_raw] += 1
            continue

        start = parse_dt(get(row, "충전시작일시"))

        end = parse_dt(get(row, "충전종료일시"))

        if start is None or end is None:
            bad_datetime_rows += 1
            continue

        # 이상 행 방지
        if end <= start:
            bad_datetime_rows += 1
            continue

        # 이번 파일은 2월 파일이지만 안전하게 범위 확인
        if end <= FEB_START or start >= MAR_START:
            continue

        matched_rows += 1
        observed_keys.add(key)

        charger_id = get(row, "충전기ID")
        charger_type = get(row, "충전기타입")
        charger_power = get(row, "충전기용량(KW)")
        energy = parse_energy(get(row, "충전량"))

        display_ids[key].add(charger_id)

        signatures[key].add(
            (
                charger_id,
                charger_type,
                charger_power,
            )
        )

        sessions[key].append(
            {
                "start": start,
                "end": end,
                "energy": energy,
                "charger_id": charger_id,
                "charger_type": charger_type,
                "charger_power": charger_power,
            }
        )

        if total_xlsx_rows % 100000 == 0:
            print(f"  {total_xlsx_rows:,}행 읽음 / 경부선 매칭 {matched_rows:,}행")

    # ========================================================
    # 시간별 집계
    # ========================================================

    hourly = defaultdict(
        lambda: {
            "session_starts": 0,
            "energy_kwh_started": 0.0,
            "charge_minutes_started": 0.0,
            "occupied_minutes": 0.0,
            "peak_concurrent_sessions": 0,
        }
    )

    # 세션 시작 / 점유시간
    for key, rows_ in sessions.items():
        for s in rows_:
            start = s["start"]
            end = s["end"]

            clipped_start = max(start, FEB_START)
            clipped_end = min(end, MAR_START)

            if clipped_end <= clipped_start:
                continue

            start_hour = floor_hour(start)

            if FEB_START <= start_hour < MAR_START:
                bucket = hourly[(key, start_hour)]

                bucket["session_starts"] += 1

                bucket["energy_kwh_started"] += s["energy"]

                bucket["charge_minutes_started"] += (end - start).total_seconds() / 60.0

            # 해당 충전 세션이 각 시간대에 차지한 분
            for hour_start in hour_range(
                clipped_start,
                clipped_end,
            ):
                hour_end = hour_start + timedelta(hours=1)

                overlap_start = max(
                    clipped_start,
                    hour_start,
                )

                overlap_end = min(
                    clipped_end,
                    hour_end,
                )

                minutes = max(
                    0.0,
                    (overlap_end - overlap_start).total_seconds() / 60.0,
                )

                hourly[(key, hour_start)]["occupied_minutes"] += minutes

    # ========================================================
    # 최대 동시 충전 세션 수
    #
    # 주의:
    # 이것은 "물리 충전기 대수"가 아니라
    # 동시에 진행 중인 charging session 수이다.
    # ========================================================

    for key, rows_ in sessions.items():
        events = []

        for s in rows_:
            start = max(s["start"], FEB_START)
            end = min(s["end"], MAR_START)

            if end <= start:
                continue

            events.append((start, +1))
            events.append((end, -1))

        # 같은 시각에 종료와 시작이 있으면 종료부터 처리
        events.sort(key=lambda x: (x[0], x[1]))

        current = 0
        prev_time = FEB_START

        for event_time, delta in events:
            event_time = min(
                max(event_time, FEB_START),
                MAR_START,
            )

            # prev_time ~ event_time 구간에는
            # current개의 세션이 계속 충전 중
            if current > 0 and event_time > prev_time:
                for hour_start in hour_range(
                    prev_time,
                    event_time,
                ):
                    hourly[(key, hour_start)]["peak_concurrent_sessions"] = max(
                        hourly[(key, hour_start)]["peak_concurrent_sessions"],
                        current,
                    )

            current += delta

            # 시작 이벤트 이후의 순간값도 반영
            if delta > 0 and FEB_START <= event_time < MAR_START:
                h = floor_hour(event_time)

                hourly[(key, h)]["peak_concurrent_sessions"] = max(
                    hourly[(key, h)]["peak_concurrent_sessions"],
                    current,
                )

            prev_time = event_time

    # ========================================================
    # 시간별 CSV
    #
    # 관측된 station은 2월의 모든 시간대를 출력한다.
    # 관측되지 않은 station은 출력하되 값은 빈칸으로 둔다.
    #
    # 따라서:
    #   observed=1 + session_starts=0
    #       → 관측 범위 안에서 그 시간에는 시작 세션 0
    #
    #   observed=0
    #       → 이 XLSX만으로 판단 불가
    # ========================================================

    OUT_HOURLY.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUT_HOURLY.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        fieldnames = [
            "direction",
            "station_name",
            "date",
            "hour",
            "hour_start",
            "is_seollal",
            "xlsx_observed_station",
            "session_starts",
            "energy_kwh_started",
            "charge_minutes_started",
            "occupied_minutes_in_hour",
            "avg_concurrent_sessions",
            "peak_concurrent_sessions",
        ]

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        cur = FEB_START

        while cur < MAR_START:
            for s in stations:
                key = (
                    s["station_name"],
                    s["direction"],
                )

                observed = key in observed_keys

                if observed:
                    b = hourly[(key, cur)]

                    values = {
                        "session_starts": b["session_starts"],
                        "energy_kwh_started": round(
                            b["energy_kwh_started"],
                            3,
                        ),
                        "charge_minutes_started": round(
                            b["charge_minutes_started"],
                            3,
                        ),
                        "occupied_minutes_in_hour": round(
                            b["occupied_minutes"],
                            3,
                        ),
                        "avg_concurrent_sessions": round(
                            b["occupied_minutes"] / 60.0,
                            4,
                        ),
                        "peak_concurrent_sessions": b["peak_concurrent_sessions"],
                    }

                else:
                    values = {
                        "session_starts": "",
                        "energy_kwh_started": "",
                        "charge_minutes_started": "",
                        "occupied_minutes_in_hour": "",
                        "avg_concurrent_sessions": "",
                        "peak_concurrent_sessions": "",
                    }

                writer.writerow(
                    {
                        "direction": s["direction"],
                        "station_name": s["station_name"],
                        "date": cur.strftime("%Y-%m-%d"),
                        "hour": cur.hour,
                        "hour_start": cur.strftime("%Y-%m-%d %H:00:00"),
                        "is_seollal": int(SEOLLAL_START <= cur < SEOLLAL_END),
                        "xlsx_observed_station": int(observed),
                        **values,
                    }
                )

            cur += timedelta(hours=1)

    # ========================================================
    # 휴게소별 요약
    # ========================================================

    station_rows = []

    for s in stations:
        key = (
            s["station_name"],
            s["direction"],
        )

        rows_ = sessions.get(key, [])

        feb_sessions = len(rows_)

        seollal_rows = [x for x in rows_ if SEOLLAL_START <= x["start"] < SEOLLAL_END]

        def energy_sum(data):
            return sum(x["energy"] for x in data)

        def avg_minutes(data):
            if not data:
                return 0.0

            return sum((x["end"] - x["start"]).total_seconds() / 60.0 for x in data) / len(data)

        station_hourly = [(h, v) for (k, h), v in hourly.items() if k == key]

        feb_peak_start_count = 0
        feb_peak_start_hour = ""

        feb_peak_concurrent = 0
        feb_peak_concurrent_hour = ""

        seollal_peak_start_count = 0
        seollal_peak_start_hour = ""

        seollal_peak_concurrent = 0
        seollal_peak_concurrent_hour = ""

        for h, b in station_hourly:
            if b["session_starts"] > feb_peak_start_count:
                feb_peak_start_count = b["session_starts"]

                feb_peak_start_hour = h.strftime("%Y-%m-%d %H:00")

            if b["peak_concurrent_sessions"] > feb_peak_concurrent:
                feb_peak_concurrent = b["peak_concurrent_sessions"]

                feb_peak_concurrent_hour = h.strftime("%Y-%m-%d %H:00")

            if SEOLLAL_START <= h < SEOLLAL_END:
                if b["session_starts"] > seollal_peak_start_count:
                    seollal_peak_start_count = b["session_starts"]

                    seollal_peak_start_hour = h.strftime("%Y-%m-%d %H:00")

                if b["peak_concurrent_sessions"] > seollal_peak_concurrent:
                    seollal_peak_concurrent = b["peak_concurrent_sessions"]

                    seollal_peak_concurrent_hour = h.strftime("%Y-%m-%d %H:00")

        observed = key in observed_keys

        station_rows.append(
            {
                "direction": s["direction"],
                "station_name": s["station_name"],
                "offset_km": s["offset_km"],
                "current_total_chargers": s["current_total_chargers"],
                "current_moe_chargers": s["current_moe_chargers"],
                "current_other_chargers": s["current_other_chargers"],
                "xlsx_observed_station": int(observed),
                "feb_sessions": feb_sessions if observed else "",
                "seollal_sessions": (len(seollal_rows) if observed else ""),
                "feb_energy_kwh": (
                    round(
                        energy_sum(rows_),
                        3,
                    )
                    if observed
                    else ""
                ),
                "seollal_energy_kwh": (
                    round(
                        energy_sum(seollal_rows),
                        3,
                    )
                    if observed
                    else ""
                ),
                "feb_avg_charge_min": (
                    round(
                        avg_minutes(rows_),
                        3,
                    )
                    if observed
                    else ""
                ),
                "seollal_avg_charge_min": (
                    round(
                        avg_minutes(seollal_rows),
                        3,
                    )
                    if observed
                    else ""
                ),
                # 주의: 물리 충전기 대수가 아님
                "feb_unique_display_charger_ids": (len(display_ids[key]) if observed else ""),
                # 주의: 물리 충전기 대수가 아님
                "feb_charger_signatures": (len(signatures[key]) if observed else ""),
                "feb_peak_start_sessions_per_hour": (feb_peak_start_count if observed else ""),
                "feb_peak_start_hour": (feb_peak_start_hour if observed else ""),
                "seollal_peak_start_sessions_per_hour": (
                    seollal_peak_start_count if observed else ""
                ),
                "seollal_peak_start_hour": (seollal_peak_start_hour if observed else ""),
                # 동시에 진행 중인 충전 세션 수.
                # 물리 충전기 대수로 해석하지 말 것.
                "feb_peak_concurrent_sessions": (feb_peak_concurrent if observed else ""),
                "feb_peak_concurrent_hour": (feb_peak_concurrent_hour if observed else ""),
                "seollal_peak_concurrent_sessions": (seollal_peak_concurrent if observed else ""),
                "seollal_peak_concurrent_hour": (seollal_peak_concurrent_hour if observed else ""),
            }
        )

    with OUT_STATION.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(station_rows[0].keys()),
        )

        writer.writeheader()
        writer.writerows(station_rows)

    # ========================================================
    # 요약 출력
    # ========================================================

    observed_station_count = len(observed_keys)

    total_feb = sum(len(x) for x in sessions.values())

    total_seollal = sum(
        1 for rows_ in sessions.values() for x in rows_ if SEOLLAL_START <= x["start"] < SEOLLAL_END
    )

    down_seollal = sum(
        1
        for key, rows_ in sessions.items()
        if key[1] == "DOWN"
        for x in rows_
        if SEOLLAL_START <= x["start"] < SEOLLAL_END
    )

    up_seollal = sum(
        1
        for key, rows_ in sessions.items()
        if key[1] == "UP"
        for x in rows_
        if SEOLLAL_START <= x["start"] < SEOLLAL_END
    )

    print()
    print("=== SP3 #64 분석 완료 ===")
    print(
        "XLSX 전체 데이터 행:",
        f"{total_xlsx_rows:,}",
    )
    print(
        "경부선 매칭 세션:",
        f"{matched_rows:,}",
    )
    print(
        "관측된 프로젝트 휴게소:",
        f"{observed_station_count} / {len(stations)}",
    )
    print(
        "2월 전체 세션:",
        f"{total_feb:,}",
    )
    print(
        "설 기간 세션:",
        f"{total_seollal:,}",
    )
    print(
        "  DOWN:",
        f"{down_seollal:,}",
    )
    print(
        "  UP:",
        f"{up_seollal:,}",
    )
    print(
        "날짜 오류 행:",
        bad_datetime_rows,
    )

    print()
    print("저장:")
    print(" ", OUT_STATION)
    print(" ", OUT_HOURLY)

    print()
    print("=== XLSX 미관측 프로젝트 휴게소 ===")

    for s in stations:
        key = (
            s["station_name"],
            s["direction"],
        )

        if key not in observed_keys:
            print(f"{s['direction']:4} {s['station_name']}")

    print()
    print(
        "주의: unique charger ID, signature, "
        "동시 세션 수는 물리 충전기 대수로 "
        "직접 해석하지 않습니다."
    )


if __name__ == "__main__":
    main()
