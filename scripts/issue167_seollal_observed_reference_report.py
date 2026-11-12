
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
#167 — 2026년 설 충전이력 실측 기준값 상세 집계기

실측 데이터만 사용하여 #153 형식의 검증 기준표를 작성한다.
S0/UE 모델값이나 모델 오차는 계산하지 않는다.

실행:
    python scripts/issue167_seollal_observed_reference_report.py --mode check
    python scripts/issue167_seollal_observed_reference_report.py --mode export

출력:
    docs/data_validation/issue167_seollal_observed_reference/
        issue167_seollal_observed_reference.md

    data/processed/issue167_seollal/
        집계 CSV 9개
        source_manifest.json

원칙:
- 기간 내 시작한 11,405건으로 시작 건수와 세션 분포 계산
- 이전 시작 이월 8건은 점유시간과 동시 세션에만 포함
- 충전이력 원본을 삭제하거나 수정하지 않음
- 중복 의심 및 장시간 세션은 감사 자료로 별도 출력
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# 1. 경로 및 검증 기준
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SOURCE = (
    ROOT
    / "runs/issue167_seollal_s0_history_source_scope_v3"
    / "observed_seollal_source_all_charging_history.csv"
)

DEFAULT_RAW = (
    ROOT
    / "data/raw/sp3_64"
    / "20260306_2026년2월_환경부_충전기_충전이력_상세내역.xlsx"
)

# Markdown 보고서 전용
DEFAULT_REPORT_OUT = (
    ROOT / "docs/data_validation/issue167_seollal_observed_reference"
)

# CSV 및 JSON 전용
DEFAULT_DATA_OUT = (
    ROOT / "data/processed/issue167_seollal"
)

START = pd.Timestamp("2026-02-13")
END = pd.Timestamp("2026-02-23")

DAYS = pd.date_range(START, periods=10, freq="D")

SITES_EXPECTED = 29
STARTS_EXPECTED = 11405

DIRECTION_EXPECTED = {
    "DOWN": 4828,
    "UP": 6577,
}

START_ONLY_OCCUPIED_EXPECTED = 308398.87
FULL_OCCUPIED_EXPECTED = 308475.37
CARRY_MIN_EXPECTED = 76.50


# ============================================================
# 2. 공통 함수
# ============================================================

def fmt(value, nd=2):
    if pd.isna(value):
        return "N/A"
    return f"{float(value):,.{nd}f}"


def med(values, q):
    if len(values) == 0:
        return np.nan
    return float(values.quantile(q))


def sig(path: Path) -> str:
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def mtable(df, fields):
    """DataFrame을 Markdown 테이블로 변환."""

    head = (
        "| " + " | ".join(fields) + " |\n"
        + "| " + " | ".join(["---"] * len(fields)) + " |\n"
    )

    rows = []

    for values in df[fields].itertuples(index=False, name=None):
        row = []
        for value in values:
            row.append("N/A" if pd.isna(value) else str(value))

        rows.append("| " + " | ".join(row) + " |")

    return head + "\n".join(rows)


# ============================================================
# 3. 설 기간 시작 세션 원장 읽기
# ============================================================

def read_source(path: Path):
    if not path.is_file():
        raise FileNotFoundError(
            f"실측 세션 CSV가 없습니다: {path}\n"
            "기존 #167 전처리 원장을 먼저 확인하세요."
        )

    df = pd.read_csv(
        path,
        encoding="utf-8-sig",
        low_memory=False,
        dtype={
            "observed_station_id": str,
            "observed_charger_id": str,
            "source_row": str,
        },
    )

    required = {
        "source_row",
        "direction",
        "station_name",
        "source_station_name",
        "start",
        "end",
        "energy_kwh",
    }

    missing = required - set(df.columns)

    if missing:
        raise ValueError(f"필수 컬럼 없음: {sorted(missing)}")

    optional = {
        "observed_charger_id": "",
        "observed_station_id": "",
        "observed_charger_power": "",
        "observed_charger_type": "",
        "elapsed_min": np.nan,
        "reported_charge_min": np.nan,
    }

    for column, default in optional.items():
        if column not in df.columns:
            df[column] = default

    for column in ["start", "end"]:
        df[column] = pd.to_datetime(
            df[column],
            errors="coerce",
        )

    df["direction"] = (
        df["direction"].astype(str).str.strip().str.upper()
    )

    df["station_name"] = (
        df["station_name"].astype(str).str.strip()
    )

    for column in [
        "energy_kwh",
        "elapsed_min",
        "reported_charge_min",
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    if len(df) != STARTS_EXPECTED:
        raise ValueError(
            f"실측 건수 불일치: {len(df)} != {STARTS_EXPECTED}"
        )

    if df[["start", "end"]].isna().any().any():
        raise ValueError("시작/종료시각 파싱 실패")

    if (df["end"] <= df["start"]).any():
        raise ValueError("종료시각 <= 시작시각인 세션 존재")

    if not (
        (df["start"] >= START) & (df["start"] < END)
    ).all():
        raise ValueError("설 기간 시작 범위 밖의 세션 존재")

    if not df["direction"].isin(["DOWN", "UP"]).all():
        raise ValueError("UP/DOWN 이외 방향 존재")

    counts = df["direction"].value_counts().to_dict()

    if counts != DIRECTION_EXPECTED:
        raise ValueError(
            f"방향별 시작 건수 불일치: {counts}"
        )

    sites = df[
        ["direction", "station_name"]
    ].drop_duplicates()

    if len(sites) != SITES_EXPECTED:
        raise ValueError(
            f"관측 휴게소 수 불일치: {len(sites)}"
        )

    if df["source_row"].duplicated().any():
        raise ValueError("source_row 중복 발생")

    if (
        df["energy_kwh"].dropna() < 0
    ).any():
        raise ValueError("음수 충전량 존재")

    df["date"] = df["start"].dt.strftime("%Y-%m-%d")
    df["hour"] = df["start"].dt.hour

    df["elapsed_from_ts_min"] = (
        df["end"] - df["start"]
    ).dt.total_seconds() / 60

    return df


# ============================================================
# 4. 설 시작 이전 이월 세션 확인
# ============================================================

def read_carryover(raw_file: Path, started: pd.DataFrame):
    """
    2월 13일 이전에 시작했지만
    2월 13일 이후에도 충전 중이었던 세션 확인.

    시작 건수에는 포함하지 않는다.
    점유시간과 동시 세션에만 반영한다.
    """

    if not raw_file.is_file():
        raise FileNotFoundError(
            f"원본 XLSX가 없습니다: {raw_file}"
        )

    raw = pd.read_excel(
        raw_file,
        usecols=[
            "충전소명",
            "충전기ID",
            "충전시작일시",
            "충전종료일시",
            "충전량",
        ],
    )

    raw["excel_row_number"] = raw.index + 2

    raw["충전소명"] = (
        raw["충전소명"]
        .astype("string")
        .str.strip()
    )

    def parse_time(column):
        digits = (
            column.astype("string")
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"\D", "", regex=True)
        )

        return pd.to_datetime(
            digits.where(digits.str.len() == 14),
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )

    raw["start"] = parse_time(raw["충전시작일시"])
    raw["end"] = parse_time(raw["충전종료일시"])

    names = started[
        [
            "source_station_name",
            "direction",
            "station_name",
        ]
    ].copy()

    names["source_station_name"] = (
        names["source_station_name"]
        .astype("string")
        .str.strip()
    )

    names = names.drop_duplicates()

    if (
        names["source_station_name"].isna().any()
        or names["source_station_name"].duplicated().any()
    ):
        raise ValueError("원본 충전소 이름 매핑 중복 또는 결측")

    in_scope = raw["충전소명"].isin(
        names["source_station_name"]
    )

    raw_started_count = int(
        (
            in_scope
            & (raw["start"] >= START)
            & (raw["start"] < END)
        ).sum()
    )

    if raw_started_count != len(started):
        raise ValueError(
            "원본과 정제 CSV 시작 건수 불일치: "
            f"{raw_started_count} != {len(started)}"
        )

    carry = raw.loc[
        in_scope
        & (raw["start"] < START)
        & (raw["end"] > START)
        & (raw["start"] < raw["end"])
    ].copy()

    carry = carry.merge(
        names,
        left_on="충전소명",
        right_on="source_station_name",
        validate="many_to_one",
        how="left",
    )

    if carry[["direction", "station_name"]].isna().any().any():
        raise AssertionError("이월 세션 휴게소 매핑 실패")

    carry["occupied_min_in_window"] = (
        carry["end"].clip(upper=END) - START
    ).dt.total_seconds() / 60

    carry["occupied_min_in_window"] = (
        carry["occupied_min_in_window"].clip(lower=0)
    )

    carry["source_kind"] = "carry_in_before_start"

    carry = carry.sort_values(
        [
            "direction",
            "station_name",
            "start",
            "excel_row_number",
        ]
    )

    if len(carry) != 8:
        raise ValueError(
            f"이월 세션 수 불일치: {len(carry)} != 8"
        )

    carry_minutes = carry["occupied_min_in_window"].sum()

    if abs(carry_minutes - CARRY_MIN_EXPECTED) > 0.011:
        raise ValueError(
            f"이월 점유시간 불일치: {carry_minutes}"
        )

    return carry


# ============================================================
# 5. 중복·중첩·장시간 세션 품질 검사
# ============================================================

def audit_quality(df):
    identity = [
        "direction",
        "station_name",
        "observed_charger_id",
        "observed_charger_type",
        "observed_charger_power",
        "start",
        "end",
        "energy_kwh",
    ]

    exact = df.loc[
        df.duplicated(identity, keep=False)
    ].copy()

    work = df.copy()

    work["_charger_id_normalized"] = (
        work["observed_charger_id"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"^0+(?=\d)", "", regex=True)
    )

    signature = [
        "direction",
        "station_name",
        "_charger_id_normalized",
        "observed_charger_type",
        "observed_charger_power",
    ]

    overlaps = []

    valid = work[
        work["end"] > work["start"]
    ].sort_values("start")

    for _, group in valid.groupby(
        signature,
        dropna=False,
        sort=False,
    ):
        active = []

        for row in group.itertuples(index=False):
            active = [
                prev for prev in active
                if prev.end > row.start
            ]

            for prev in active:
                overlap_min = (
                    min(prev.end, row.end) - row.start
                ).total_seconds() / 60

                if overlap_min > 0:
                    overlaps.append({
                        "direction": row.direction,
                        "station_name": row.station_name,
                        "source_row_1": prev.source_row,
                        "source_row_2": row.source_row,
                        "overlap_min": overlap_min,
                    })

            active.append(row)

    overlap_df = pd.DataFrame(
        overlaps,
        columns=[
            "direction",
            "station_name",
            "source_row_1",
            "source_row_2",
            "overlap_min",
        ],
    )

    long_sessions = df.loc[
        df["elapsed_from_ts_min"] > 120
    ].copy()

    long_sessions = long_sessions.sort_values(
        "elapsed_from_ts_min",
        ascending=False,
    )

    gimcheon = df.loc[
        (df["direction"] == "UP")
        & (df["station_name"] == "김천휴게소")
    ]

    durations = df["elapsed_from_ts_min"]
    gimcheon_durations = gimcheon["elapsed_from_ts_min"]

    overlap_by_station = {
        f"{direction} {station}": int(count)
        for (direction, station), count
        in overlap_df.groupby(
            ["direction", "station_name"]
        ).size().items()
    }

    quality = {
        "exact_duplicate_rows": int(len(exact)),

        "same_display_device_overlap_pairs":
            int(len(overlap_df)),

        "overlap_by_station": overlap_by_station,

        "overlap_pair_min_sum_not_deduction":
            round(float(overlap_df["overlap_min"].sum()), 3),

        "over_120_min_sessions":
            int((durations > 120).sum()),

        "over_1440_min_sessions":
            int((durations > 1440).sum()),

        "elapsed_mean_all_min":
            round(float(durations.mean()), 3),

        "elapsed_mean_le_120_min":
            round(float(durations[durations <= 120].mean()), 3),

        "gimcheon_up_n":
            int(len(gimcheon)),

        "gimcheon_up_elapsed_mean_all_min":
            round(float(gimcheon_durations.mean()), 3),

        "gimcheon_up_elapsed_mean_le_120_min":
            round(
                float(
                    gimcheon_durations[
                        gimcheon_durations <= 120
                    ].mean()
                ),
                3,
            ),
    }

    return exact, overlap_df, long_sessions, quality


# ============================================================
# 6. 휴게소×날짜×시간 집계
# ============================================================

def hourly_from_sessions(
    sessions,
    sites,
    start_sessions=None,
):
    """
    점유시간과 동시 세션:
        입력된 모든 세션을 사용한다.

    시작 건수:
        start_sessions만 사용한다.

    이월 세션은 점유시간에만 반영된다.
    """

    events = defaultdict(list)

    for row in sessions.itertuples(index=False):
        start = max(row.start, START)
        end = min(row.end, END)

        if end <= start:
            continue

        cursor = start.floor("h")

        while cursor < end:
            lower = max(start, cursor)
            upper = min(
                end,
                cursor + timedelta(hours=1),
            )

            if lower < upper:
                key = (
                    row.direction,
                    row.station_name,
                    cursor.strftime("%Y-%m-%d"),
                    cursor.hour,
                )

                events[key].append((lower, 1))
                events[key].append((upper, -1))

            cursor += timedelta(hours=1)

    rows = []

    for key, timeline in events.items():
        busy = 0.0
        concurrent = 0
        peak = 0
        previous_time = None

        # 같은 시각이면 종료(-1)를 먼저 처리
        for clock, delta in sorted(
            timeline,
            key=lambda item: (item[0], item[1]),
        ):
            if previous_time is not None:
                busy += (
                    concurrent
                    * (clock - previous_time).total_seconds()
                    / 60
                )

            concurrent += delta
            peak = max(peak, concurrent)
            previous_time = clock

        if concurrent != 0:
            raise AssertionError(
                f"동시 세션 이벤트 종결 불일치: {key}"
            )

        rows.append((*key, busy, peak))

    occupied = pd.DataFrame(
        rows,
        columns=[
            "direction",
            "station_name",
            "date",
            "hour",
            "occupied_min",
            "max_concurrent_sessions_in_hour",
        ],
    )

    if start_sessions is None:
        start_sessions = sessions

    starts = (
        start_sessions.groupby(
            ["direction", "station_name", "date", "hour"],
            sort=False,
        )
        .size()
        .rename("starts")
        .reset_index()
    )

    site_grid = sites[
        ["direction", "station_name"]
    ].copy()

    site_grid["_join"] = 1

    time_grid = (
        pd.MultiIndex.from_product(
            [
                DAYS.strftime("%Y-%m-%d"),
                range(24),
            ],
            names=["date", "hour"],
        )
        .to_frame(index=False)
    )

    time_grid["_join"] = 1

    grid = site_grid.merge(
        time_grid,
        on="_join",
    ).drop(columns="_join")

    grid = grid.merge(
        starts,
        on=[
            "direction",
            "station_name",
            "date",
            "hour",
        ],
        how="left",
        validate="one_to_one",
    )

    grid = grid.merge(
        occupied,
        on=[
            "direction",
            "station_name",
            "date",
            "hour",
        ],
        how="left",
        validate="one_to_one",
    )

    for column in [
        "starts",
        "occupied_min",
        "max_concurrent_sessions_in_hour",
    ]:
        grid[column] = grid[column].fillna(0)

    grid["starts"] = grid["starts"].astype(int)

    grid["max_concurrent_sessions_in_hour"] = (
        grid["max_concurrent_sessions_in_hour"].astype(int)
    )

    if len(grid) != 29 * 10 * 24:
        raise AssertionError("시간별 집계 격자 크기 불일치")

    if int(grid["starts"].sum()) != 11405:
        raise AssertionError("시간별 시작 건수 불일치")

    return grid


# ============================================================
# 7. 충전량·충전시간 분포
# ============================================================

def agg_percentiles(df, group_columns):
    results = []

    for key, group in df.groupby(
        group_columns,
        sort=True,
        dropna=False,
    ):
        if not isinstance(key, tuple):
            key = (key,)

        # groupby 키는 group_columns 와 길이가 같다 — 어긋나면 그게 버그다
        row = dict(zip(group_columns, key, strict=True))

        row["starts"] = len(group)

        row["energy_kwh_total"] = (
            group["energy_kwh"].sum(min_count=1)
        )

        metrics = [
            ("energy_kwh", "energy_kwh"),
            ("elapsed_min", "elapsed_from_ts_min"),
            ("reported_charge_min", "reported_charge_min"),
        ]

        for metric_name, column in metrics:
            values = group[column].dropna()

            row[f"{metric_name}_n"] = len(values)

            row[f"{metric_name}_mean"] = (
                values.mean() if len(values) else np.nan
            )

            row[f"{metric_name}_p50"] = med(values, 0.5)
            row[f"{metric_name}_p90"] = med(values, 0.9)

        results.append(row)

    return pd.DataFrame(results)


# ============================================================
# 8. 피크 집계
# ============================================================

def peak_hours(group, metric):
    maximum = group[metric].max()

    if pd.isna(maximum) or maximum <= 0:
        return "N/A", maximum

    matching = np.isclose(
        group[metric],
        maximum,
        atol=1e-8,
        rtol=1e-9,
    )

    hours = (
        group.loc[matching, "hour"]
        .sort_values()
        .tolist()
    )

    hour_text = ",".join(
        f"{int(hour):02d}" for hour in hours
    )

    return hour_text, maximum


def direction_day_peaks(hourly):
    aggregated = (
        hourly.groupby(
            ["date", "direction", "hour"],
            as_index=False,
        )[["starts", "occupied_min"]]
        .sum()
    )

    results = []

    for (date, direction), group in aggregated.groupby(
        ["date", "direction"]
    ):
        start_hour, start_count = peak_hours(
            group,
            "starts",
        )

        busy_hour, busy_minutes = peak_hours(
            group,
            "occupied_min",
        )

        results.append({
            "date": date,
            "direction": direction,
            "peak_starts_hours": start_hour,
            "peak_starts_count": int(start_count),
            "peak_occupied_hours": busy_hour,
            "peak_occupied_min": busy_minutes,
        })

    return pd.DataFrame(results)


def station_day_peaks(hourly):
    results = []

    for (direction, station, date), group in hourly.groupby(
        ["direction", "station_name", "date"],
        sort=True,
    ):
        start_hour, start_count = peak_hours(
            group,
            "starts",
        )

        busy_hour, busy_minutes = peak_hours(
            group,
            "occupied_min",
        )

        results.append({
            "direction": direction,
            "station_name": station,
            "date": date,
            "peak_starts_hours": start_hour,
            "peak_starts_count": int(start_count),
            "peak_occupied_hours": busy_hour,
            "peak_occupied_min": busy_minutes,
            "max_concurrent_sessions": int(
                group["max_concurrent_sessions_in_hour"].max()
            ),
        })

    return pd.DataFrame(results)


# ============================================================
# 9. 최종 Markdown 보고서 생성
# ============================================================

def report(
    df,
    hourly,
    day,
    site,
    peak_day,
    peak_site,
    source,
    raw_source,
    carry,
    carry_impact,
    quality,
):
    all_stats = agg_percentiles(
        df.assign(scope="ALL_OBSERVED_29"),
        ["scope"],
    ).iloc[0]

    total_busy = float(hourly["occupied_min"].sum())
    carry_busy = float(carry["occupied_min_in_window"].sum())
    start_busy = total_busy - carry_busy

    if (
        abs(start_busy - START_ONLY_OCCUPIED_EXPECTED) > 0.015
        or abs(total_busy - FULL_OCCUPIED_EXPECTED) > 0.015
    ):
        raise AssertionError("보고서 점유시간 검산 실패")

    direction_summary = (
        hourly.groupby("direction", as_index=False)
        [["starts", "occupied_min"]]
        .sum()
    )

    daily = day.copy()
    daily["occupied_min"] = daily["occupied_min"].round(2)

    stations = site.copy().sort_values(
        ["starts", "direction", "station_name"],
        ascending=[False, True, True],
    )

    stations["occupied_min"] = (
        stations["occupied_min"].round(2)
    )

    top_daily = daily.sort_values(
        "starts",
        ascending=False,
    ).head(8)

    top_stations = stations.head(12)

    peak_day_view = peak_day.copy()

    peak_day_view["peak_occupied_min"] = (
        peak_day_view["peak_occupied_min"].round(2)
    )

    bad_id = int(
        df["observed_charger_id"]
        .fillna("")
        .astype(str)
        .str.strip()
        .eq("")
        .sum()
    )

    overlap_text = ", ".join(
        f"{name}: {count}쌍"
        for name, count in sorted(
            quality["overlap_by_station"].items(),
            key=lambda item: -item[1],
        )
    )

    lines = [
        "# #167 — 2026년 설 충전이력 실측 기준값·검증 리스트",
        "",
        "> 대상: 경부고속도로 UP/DOWN, "
        "2026-02-13 00:00 ~ 2026-02-23 00:00 미만(KST)",
        "",
        "> 용도: #153과 동일하게 향후 시뮬레이션 검증을 위한 "
        "**실측 기준값만** 기록한다. "
        "S0/UE 모델값과 모델 오차는 포함하지 않는다.",
        "",
        "> 집계 구분: 충전 시작·충전량·충전시간은 "
        "**11,405개 시작 세션**, 점유시간·최대 동시 세션은 "
        "**이월 8건 포함**.",
        "",

        "## 1. 자료 범위·집계 정의",
        "",
        f"- 전처리 원장: `{source.name}`",
        f"- 전처리 원장 SHA-256: `{sig(source)}`",
        f"- 원본 XLSX: `{raw_source.name}`",
        f"- 원본 XLSX SHA-256: `{sig(raw_source)}`",
        f"- 설 기간 충전 시작: **{len(df):,}건**",
        f"- 관측 방향별 휴게소: **{len(site)}개소**",
        "- 미관측 6곳: DOWN 망향·죽암·황간·경주, "
        "UP 칠곡·옥천. **N/A로 처리하며 0건으로 간주하지 않는다.**",
        "- `starts`: 해당 시간에 충전을 시작한 세션 수.",
        "- `occupied_min`: 세션의 시작~종료 구간과 검증 시간칸이 "
        "겹치는 분의 합계(세션-분). "
        "동시 세션 때문에 한 시간에 60분을 초과할 수 있다.",
        "- `max_concurrent_sessions`: 동시에 진행 중인 "
        "충전 세션의 최대 수. 실제 충전기 대수나 대기 차량 수가 아니다.",
        "- 시작 건수·충전량·충전시간 분포에는 기간 내 시작 세션만 사용한다.",
        "- 점유시간·동시 세션에는 설 시작 이전 이월 8건을 추가한다.",
        "- 자료 제공기관과 실제 충전 운영사는 구분한다.",
        "",

        "## 2. 기간 전체 실측 핵심값",
        "",
        "| 검증 항목 | 실측 기준값 | 의미 |",
        "|---|---:|---|",

        f"| 충전 시작 (10일, 29곳) | "
        f"**{len(df):,}건** | 시작 시각 기준 |",

        f'| DOWN 시작 | **{int(direction_summary.loc[direction_summary.direction == "DOWN", "starts"].iloc[0]):,}건** | 하행 |',

        f'| UP 시작 | **{int(direction_summary.loc[direction_summary.direction == "UP", "starts"].iloc[0]):,}건** | 상행 |',

        f"| 기간 시작 세션 점유시간 | "
        f"**{start_busy:,.2f}분** | 11,405건의 기간 내 점유 |",

        f"| 이월 세션 점유시간 | "
        f"**{carry_busy:,.2f}분 (8건)** | 이전 시작 세션 |",

        f"| **기간 전체 점유시간** | "
        f"**{total_busy:,.2f}분** | 이월 8건 포함 |",

        f"| 총 세션 점유시간 | "
        f"**{total_busy / 60:,.2f}시간** | 대기시간 아님 |",

        f"| 세션당 충전량 평균/P50/P90 | "
        f'**{fmt(all_stats["energy_kwh_mean"], 3)} / '
        f'{fmt(all_stats["energy_kwh_p50"], 3)} / '
        f'{fmt(all_stats["energy_kwh_p90"], 3)} kWh** | 시작 세션 분포 |',

        f"| 세션 경과시간 평균/P50/P90 | "
        f'**{fmt(all_stats["elapsed_min_mean"], 3)} / '
        f'{fmt(all_stats["elapsed_min_p50"], 3)} / '
        f'{fmt(all_stats["elapsed_min_p90"], 3)}분** | 종료−시작 |',

        f"| 원본 기록 충전시간 평균/P50/P90 | "
        f'**{fmt(all_stats["reported_charge_min_mean"], 3)} / '
        f'{fmt(all_stats["reported_charge_min_p50"], 3)} / '
        f'{fmt(all_stats["reported_charge_min_p90"], 3)}분** | 원본 기록 기준 |',

        f"| 충전기 ID 공란 | **{bad_id}건** | "
        "ID의 물리 포트 고유성은 별도 문제 |",
        "",

        "## 3. 날짜×방향별 실측 시작·점유 — 20개 사례",
        "",
        mtable(
            daily.sort_values(["date", "direction"]),
            ["date", "direction", "starts", "occupied_min"],
        ),
        "",

        "### 3-1. 충전 시작 최다 날짜×방향 TOP 8",
        "",
        mtable(
            top_daily,
            ["date", "direction", "starts", "occupied_min"],
        ),
        "",

        "## 4. 휴게소별 10일 실측 수요·이용 — 29개소",
        "",
        "관측이 존재하는 방향별 휴게소만 집계한다. "
        "점유시간과 최대 동시 세션은 이월 세션을 포함한다.",
        "",

        mtable(
            stations.rename(
                columns={
                    "station_name": "station",
                    "max_concurrent_sessions": "max_concurrent",
                }
            ),
            [
                "direction",
                "station",
                "starts",
                "occupied_min",
                "max_concurrent",
            ],
        ),
        "",

        "### 4-1. 충전 시작 최다 휴게소 TOP 12",
        "",
        mtable(
            top_stations,
            [
                "direction",
                "station_name",
                "starts",
                "occupied_min",
            ],
        ),
        "",

        "## 5. 날짜×방향별 충전 시작·점유 피크",
        "",
        "충전 시작 피크와 점유 피크는 서로 다른 시간일 수 있다. "
        "복수의 피크 시간은 동률을 뜻한다.",
        "",

        mtable(
            peak_day_view,
            [
                "date",
                "direction",
                "peak_starts_hours",
                "peak_starts_count",
                "peak_occupied_hours",
                "peak_occupied_min",
            ],
        ),
        "",

        "### 5-1. 최대 동시 충전 세션 대표 지점",
        "",
        mtable(
            stations.sort_values(
                "max_concurrent_sessions",
                ascending=False,
            ).head(12),
            [
                "direction",
                "station_name",
                "starts",
                "max_concurrent_sessions",
                "max_concurrent_peak_times",
            ],
        ),
        "",

        "## 6. 세션별 충전량·시간 분포 — 휴게소별",
        "",
        "아래 분포는 설 기간에 시작한 세션만 계산한다. "
        "원본 기록 충전시간과 시작~종료 경과시간은 구분한다.",
        "",

        mtable(
            stations[
                [
                    "direction",
                    "station_name",
                    "energy_kwh_mean",
                    "energy_kwh_p50",
                    "energy_kwh_p90",
                    "elapsed_min_mean",
                    "elapsed_min_p50",
                    "elapsed_min_p90",
                ]
            ].round(3),
            [
                "direction",
                "station_name",
                "energy_kwh_mean",
                "energy_kwh_p50",
                "energy_kwh_p90",
                "elapsed_min_mean",
                "elapsed_min_p50",
                "elapsed_min_p90",
            ],
        ),
        "",

        "## 7. 원본 재검산 — 경계·중복·장시간 세션",
        "",

        "### 7-1. 이월 세션 및 점유시간",
        "",
        f"- 기간 시작 세션 점유시간: **{start_busy:,.2f}분**",
        f"- 이월 8건 점유시간: **{carry_busy:,.2f}분**",
        f"- 기간 전체 점유시간: **{total_busy:,.2f}분**",
        "- 기존 #167과의 76.50분 차이는 이월 8건으로 설명된다.",
        "- 이월분은 2월 13일 00시 시간칸의 점유·동시 세션에만 반영한다.",
        f"- 이월 영향 휴게소×시간 셀: **{len(carry_impact)}개**",
        "",

        "### 7-2. 중복·동일 장비 표시의 중첩",
        "",
        f'- 완전 동일 필드 중복: '
        f'**{quality["exact_duplicate_rows"]}건**',

        f'- 동일 장비 표시의 세션 중첩: '
        f'**{quality["same_display_device_overlap_pairs"]}쌍**',

        f"- 휴게소별 중첩: {overlap_text}",
        "- 실제 장비 ID의 물리 포트 고유성을 확인하지 못했으므로 "
        "중첩을 중복 거래로 확정하지 않는다.",
        "- 중첩 의심 기록은 삭제하거나 점유시간에서 차감하지 않는다.",
        "",

        "### 7-3. 장시간 세션",
        "",
        f'- 120분 초과 세션: '
        f'**{quality["over_120_min_sessions"]}건**',

        f'- 24시간 초과 세션: '
        f'**{quality["over_1440_min_sessions"]}건**',

        f'- 전체 경과시간 평균: '
        f'**{quality["elapsed_mean_all_min"]:.3f}분**',

        f'- 120분 이하만의 평균: '
        f'**{quality["elapsed_mean_le_120_min"]:.3f}분**',

        f'- 김천 UP {quality["gimcheon_up_n"]}건 '
        f'평균 **{quality["gimcheon_up_elapsed_mean_all_min"]:.3f}분**, '
        f'120분 이하 평균 '
        f'**{quality["gimcheon_up_elapsed_mean_le_120_min"]:.3f}분**',

        "- 장시간 세션도 원본에서 삭제하지 않고 "
        "전체 통계와 민감도 참고값을 구분한다.",
        "",

        "## 8. 실측 기반 대표 검증 사례",
        "",

        f'- 시작 최다 날짜×방향: '
        f'**{top_daily.iloc[0]["date"]} '
        f'{top_daily.iloc[0]["direction"]}, '
        f'{int(top_daily.iloc[0]["starts"]):,}건**',

        f'- 시작 최다 휴게소: '
        f'**{top_stations.iloc[0]["direction"]} '
        f'{top_stations.iloc[0]["station_name"]}, '
        f'{int(top_stations.iloc[0]["starts"]):,}건**',

        f'- 최대 동시 충전 휴게소: '
        f'**{stations.sort_values("max_concurrent_sessions", ascending=False).iloc[0]["direction"]} '
        f'{stations.sort_values("max_concurrent_sessions", ascending=False).iloc[0]["station_name"]}, '
        f'{int(stations["max_concurrent_sessions"].max())}세션**',

        "- 대표 사례는 실측 데이터만을 기준으로 선정한다.",
        "",

        "## 9. 실측 검증의 한계",
        "",
        "| 항목 | 실측 상태 | 판정 |",
        "|---|---|---|",
        "| 충전 시작·공간·시간대 분포 | 확보 | 기준값 산출 |",
        "| 충전량·지속시간 | 확보 | 기준값 산출 |",
        "| 최대 동시 충전 세션 | 세션 중첩에서 계산 | 실제 대기열과 구분 |",
        "| 실제 충전 대기시간 | 없음 | N/A |",
        "| 실제 대기 차량 수 | 없음 | N/A |",
        "| 실제 가동 포트·운영사 전수 | 미확정 | 조건부 |",
        "| 미관측 6개소 충전 수요 | 미관측 | N/A |",
        "",

        "## 10. 근거 및 산출물",
        "",
        f"- 기간 시작 세션 원장: `{source.relative_to(ROOT) if source.is_relative_to(ROOT) else source}`",
        f"- 원본 XLSX: `{raw_source.relative_to(ROOT) if raw_source.is_relative_to(ROOT) else raw_source}`",
        "- 집계 코드: `scripts/issue167_seollal_observed_reference_report.py`",
        "- 집계 CSV 및 품질 감사 CSV: `data/processed/issue167_seollal/`",
        "- 품질 감사 파일: 이월 세션·시간별 영향·중첩 의심·장시간 세션.",
        "- 관련 이슈: #64, #153, #167",
        "",

        "## 11. 최종 검증 결과",
        "",
        "- [x] 설 기간 충전 시작 11,405건 검산",
        "- [x] 날짜×방향별 20개 사례 집계",
        "- [x] 휴게소별 29개 사례 집계",
        "- [x] 충전 시작·점유 피크 및 최대 동시 세션 집계",
        "- [x] 충전량·충전시간 평균/P50/P90 산출",
        "- [x] 이월 세션 8건 및 점유시간 정합성 검산",
        "- [x] 중첩 의심 65쌍·장시간 세션 품질 감사",
        "- [x] 원본 데이터 임의 삭제·수정 없음",
        "",

        "**최종 판정: 설 충전이력 실측 기준값 추출 및 "
        "집계 정합성 검증 완료.**",
        "",
        "운영사·장비 신원과 실제 대기시간 등 미확정 항목은 "
        "검증 한계로 유지한다.",
        "",
    ]

    return "\n".join(lines)


# ============================================================
# 10. 전체 실행
# ============================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--mode",
        choices=["check", "export"],
        default="check",
    )

    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
    )

    parser.add_argument(
        "--raw",
        type=Path,
        default=DEFAULT_RAW,
    )

    parser.add_argument(
        "--report-out",
        type=Path,
        default=DEFAULT_REPORT_OUT,
    )

    parser.add_argument(
        "--data-out",
        type=Path,
        default=DEFAULT_DATA_OUT,
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # 원본 읽기 및 감사
    # --------------------------------------------------------

    df = read_source(args.source)
    carry = read_carryover(args.raw, df)

    exact, overlaps, long_sessions, quality = audit_quality(df)

    sites = (
        df[["direction", "station_name"]]
        .drop_duplicates()
        .sort_values(["direction", "station_name"])
    )

    # --------------------------------------------------------
    # 시작 세션만의 점유시간
    # --------------------------------------------------------

    start_only = hourly_from_sessions(
        df,
        sites,
    )

    # --------------------------------------------------------
    # 이월 8건 추가 후 전체 점유시간 및 동시 충전 집계
    # --------------------------------------------------------

    full_sessions = pd.concat(
        [
            df,
            carry[
                ["direction", "station_name", "start", "end"]
            ],
        ],
        ignore_index=True,
        sort=False,
    )

    hourly = hourly_from_sessions(
        full_sessions,
        sites,
        start_sessions=df,
    )

    hourly["occupied_min_start_sessions"] = (
        start_only["occupied_min"]
    )

    hourly["occupied_min_carry_in"] = (
        hourly["occupied_min"]
        - hourly["occupied_min_start_sessions"]
    )

    hourly.loc[
        hourly["occupied_min_carry_in"].abs() < 1e-8,
        "occupied_min_carry_in",
    ] = 0.0

    hourly["max_concurrent_without_carry_in"] = (
        start_only["max_concurrent_sessions_in_hour"]
    )

    hourly["max_concurrent_delta_from_carry_in"] = (
        hourly["max_concurrent_sessions_in_hour"]
        - hourly["max_concurrent_without_carry_in"]
    )

    # 이월로 점유시간이 증가한 시간칸
    affected = hourly.loc[
        hourly["occupied_min_carry_in"] > 1e-6,
        [
            "date",
            "direction",
            "station_name",
            "hour",
            "starts",
            "occupied_min_start_sessions",
            "occupied_min_carry_in",
            "occupied_min",
            "max_concurrent_without_carry_in",
            "max_concurrent_sessions_in_hour",
            "max_concurrent_delta_from_carry_in",
        ],
    ].copy()

    # --------------------------------------------------------
    # 점유시간 교차검산
    # --------------------------------------------------------

    started_busy = float(
        start_only["occupied_min"].sum()
    )

    carry_busy = float(
        carry["occupied_min_in_window"].sum()
    )

    full_busy = float(
        hourly["occupied_min"].sum()
    )

    if abs(started_busy - START_ONLY_OCCUPIED_EXPECTED) > 0.015:
        raise AssertionError("시작 세션 점유시간 검산 실패")

    if abs(carry_busy - CARRY_MIN_EXPECTED) > 0.015:
        raise AssertionError("이월 점유시간 검산 실패")

    if abs(full_busy - FULL_OCCUPIED_EXPECTED) > 0.015:
        raise AssertionError("전체 점유시간 검산 실패")

    if abs(full_busy - started_busy - carry_busy) > 1e-6:
        raise AssertionError("점유시간 합산 검산 실패")

    if int(hourly["starts"].sum()) != STARTS_EXPECTED:
        raise AssertionError("이월 세션이 시작 건수에 포함됨")

    if len(hourly) != 6960:
        raise AssertionError("시간별 집계 격자 불일치")

    # --------------------------------------------------------
    # 휴게소별 통계
    # --------------------------------------------------------

    station_stats = agg_percentiles(
        df,
        ["direction", "station_name"],
    )

    station_busy = (
        hourly.groupby(
            ["direction", "station_name"],
            as_index=False,
        )
        .agg(
            occupied_min=("occupied_min", "sum"),
            max_concurrent_sessions=(
                "max_concurrent_sessions_in_hour",
                "max",
            ),
        )
    )

    station = station_stats.merge(
        station_busy,
        on=["direction", "station_name"],
        validate="one_to_one",
    )

    # --------------------------------------------------------
    # 최대 동시 충전 발생 시간
    # --------------------------------------------------------

    peak_time_rows = []

    for (direction, station_name), group in hourly.groupby(
        ["direction", "station_name"],
        sort=True,
    ):
        maximum = group[
            "max_concurrent_sessions_in_hour"
        ].max()

        matching = group.loc[
            group["max_concurrent_sessions_in_hour"] == maximum
        ].sort_values(["date", "hour"])

        peak_times = [
            f"{row.date} {int(row.hour):02d}시"
            for row in matching.head(10).itertuples(index=False)
        ]

        peak_time_rows.append({
            "direction": direction,
            "station_name": station_name,
            "max_concurrent_peak_times": ", ".join(peak_times),
        })

    peak_times_df = pd.DataFrame(peak_time_rows)

    station = station.merge(
        peak_times_df,
        on=["direction", "station_name"],
        validate="one_to_one",
        how="left",
    )

    # --------------------------------------------------------
    # 날짜×방향별 통계
    # --------------------------------------------------------

    direction_day = (
        hourly.groupby(
            ["date", "direction"],
            as_index=False,
        )
        .agg(
            starts=("starts", "sum"),
            occupied_min=("occupied_min", "sum"),
        )
    )

    direction_peaks = direction_day_peaks(hourly)
    station_peaks = station_day_peaks(hourly)

    # --------------------------------------------------------
    # Markdown 내용 생성
    # --------------------------------------------------------

    markdown = report(
        df,
        hourly,
        direction_day,
        station,
        direction_peaks,
        station_peaks,
        args.source,
        args.raw,
        carry,
        affected,
        quality,
    )

    print(
        "[CHECK PASS]",
        "starts=", len(df),
        "observed_sites=", len(sites),
        "hour_cells=", len(hourly),
        "direction_totals=",
        df["direction"].value_counts().to_dict(),
    )

    print(
        "[OCCUPIED]",
        f"start_only={started_busy:.2f}",
        f"carry_in={carry_busy:.2f}",
        f"full={full_busy:.2f}",
    )

    print(
        "[CARRY]",
        len(carry),
        "sessions, affected site-hours=",
        len(affected),
    )

    print(
        "[QUALITY]",
        json.dumps(
            quality,
            ensure_ascii=False,
        ),
    )

    # --------------------------------------------------------
    # check: 읽기 전용
    # --------------------------------------------------------

    if args.mode == "check":
        print("[READ ONLY] no files created")
        return 0

    # ========================================================
    # 11. export: 경로별 파일 저장
    # ========================================================

    args.report_out.mkdir(
        parents=True,
        exist_ok=True,
    )

    args.data_out.mkdir(
        parents=True,
        exist_ok=True,
    )

    carry_export = carry[
        [
            "excel_row_number",
            "direction",
            "station_name",
            "충전소명",
            "충전기ID",
            "start",
            "end",
            "occupied_min_in_window",
            "충전량",
            "source_kind",
        ]
    ].copy()

    long_export = long_sessions[
        [
            "source_row",
            "direction",
            "station_name",
            "observed_charger_id",
            "start",
            "end",
            "elapsed_from_ts_min",
            "energy_kwh",
        ]
    ].copy()

    # CSV는 전부 data/processed 아래에 저장
    outputs = {
        "seollal_observed_direction_day.csv":
            direction_day,

        "seollal_observed_station_10day.csv":
            station,

        "seollal_observed_station_day_hour.csv":
            hourly,

        "seollal_observed_direction_day_peaks.csv":
            direction_peaks,

        "seollal_observed_station_day_peaks.csv":
            station_peaks,

        "seollal_observed_carryover_sessions.csv":
            carry_export,

        "seollal_observed_carryover_hourly_impact.csv":
            affected,

        "seollal_observed_quality_overlap_pairs.csv":
            overlaps,

        "seollal_observed_quality_long_sessions.csv":
            long_export,
    }

    for filename, output_df in outputs.items():
        output_path = args.data_out / filename

        output_df.to_csv(
            output_path,
            index=False,
            encoding="utf-8-sig",
        )

        print(
            "[WRITE]",
            output_path,
            len(output_df),
            "rows",
        )

    # Markdown은 docs 아래에 저장
    report_path = (
        args.report_out
        / "issue167_seollal_observed_reference.md"
    )

    report_path.write_text(
        markdown,
        encoding="utf-8",
    )

    print("[REPORT]", report_path)

    # JSON은 data/processed 아래에 저장
    manifest = {
        "source": str(args.source),
        "source_sha256": sig(args.source),
        "raw_xlsx": str(args.raw),
        "raw_xlsx_sha256": sig(args.raw),

        "period": [
            "2026-02-13",
            "2026-02-23",
        ],

        "start_sessions": len(df),
        "carry_in_sessions": len(carry),

        "occupied_min_start_sessions":
            round(started_busy, 6),

        "occupied_min_carry_in":
            round(carry_busy, 6),

        "occupied_min_full":
            round(full_busy, 6),

        "observed_sites": len(sites),
        "model_values_included": False,
        "quality_summary": quality,

        "report_path": str(report_path),
        "data_path": str(args.data_out),
    }

    manifest_path = (
        args.data_out / "source_manifest.json"
    )

    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print("[MANIFEST]", manifest_path)
    print("[DONE]", report_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
