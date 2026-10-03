"""도로공사 TCS 전체 영업소간 교통량 Matrix를 OD long format으로 정리한다.

실행:
    python scripts/build_tcs_od.py

입력:
    data/raw/tcs_od/tcs_od_202602.csv
    data/raw/tcs_od/tcs_od_202603.csv

사용 기간:
    holiday: 2026-02-13 ~ 2026-02-22
    normal : 2026-03-06 ~ 2026-03-15

출력:
    data/processed/tcs_od_all.parquet

출력 컬럼:
    period
    date
    start_office_code
    start_office
    end_office_code
    end_office
    volume_veh

주의:
    이 단계에서는 아직 경부선 영업소 필터링과 offset_km 매핑을 하지 않는다.
    전체 OD를 long format으로 정상 변환하는 것이 목적이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.paths import DATA_PROCESSED_DIR, DATA_RAW_DIR  # noqa: E402

RAW_DIR = DATA_RAW_DIR / "tcs_od"

INPUTS = {
    "holiday": {
        "path": RAW_DIR / "tcs_od_202602.csv",
        "start": "2026-02-13",
        "end": "2026-02-22",
    },
    "normal": {
        "path": RAW_DIR / "tcs_od_202603.csv",
        "start": "2026-03-06",
        "end": "2026-03-15",
    },
}


BASE_COLUMNS = {
    "기준일자",
    "출발영업소코드",
    "출발영업소명",
}


def read_csv_robust(path: Path) -> pd.DataFrame:
    """도로공사 CSV를 인코딩 차이에 대응해 읽는다."""
    if not path.exists():
        raise FileNotFoundError(f"원본 파일이 없습니다: {path}")

    errors = []

    # 공공데이터 CSV는 utf-8-sig 또는 cp949인 경우가 많다.
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            df = pd.read_csv(
                path,
                encoding=encoding,
                sep=None,
                engine="python",
            )

            # 컬럼명 앞뒤 공백 제거
            df.columns = [str(col).strip() for col in df.columns]

            print(f"  읽기 성공: {path.name} ({encoding})")
            return df

        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            errors.append(f"{encoding}: {exc}")

    raise RuntimeError(
        f"{path} 파일을 읽을 수 없습니다.\n" + "\n".join(errors)
    )


def validate_source_columns(df: pd.DataFrame, path: Path) -> None:
    """필수 컬럼이 있는지 확인한다."""
    missing = BASE_COLUMNS - set(df.columns)

    if missing:
        raise ValueError(
            f"{path.name}: 필수 컬럼이 없습니다: {sorted(missing)}"
        )


def normalize_office_code(series: pd.Series) -> pd.Series:
    """영업소 코드를 문자열로 통일한다."""
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )


def destination_columns(df: pd.DataFrame) -> list[str]:
    """101, 102, 103 ... 형태의 도착 영업소 코드 열만 찾는다."""
    result = []

    for col in df.columns:
        col_str = str(col).strip()

        if col_str.isdigit():
            result.append(col_str)

    if not result:
        raise ValueError(
            "도착 영업소 코드 열을 찾지 못했습니다. "
            "CSV 컬럼 구조를 확인해주세요."
        )

    return result


def build_office_map(frames: list[pd.DataFrame]) -> dict[str, str]:
    """출발영업소코드 → 영업소명 대응표를 만든다."""
    office_rows = []

    for df in frames:
        temp = df[
            ["출발영업소코드", "출발영업소명"]
        ].copy()

        temp["출발영업소코드"] = normalize_office_code(
            temp["출발영업소코드"]
        )

        temp["출발영업소명"] = (
            temp["출발영업소명"]
            .astype(str)
            .str.strip()
        )

        office_rows.append(temp)

    offices = pd.concat(office_rows, ignore_index=True).drop_duplicates()

    # 하나의 코드에 서로 다른 이름이 연결되어 있으면 데이터 문제
    conflicts = (
        offices.groupby("출발영업소코드")["출발영업소명"]
        .nunique()
    )

    bad_codes = conflicts[conflicts > 1]

    if len(bad_codes):
        raise ValueError(
            "같은 영업소 코드에 여러 이름이 존재합니다:\n"
            + bad_codes.to_string()
        )

    return (
        offices.drop_duplicates("출발영업소코드")
        .set_index("출발영업소코드")["출발영업소명"]
        .to_dict()
    )


def matrix_to_long(
    df: pd.DataFrame,
    *,
    period: str,
    start_date: str,
    end_date: str,
    office_map: dict[str, str],
) -> pd.DataFrame:
    """Matrix 형태를 OD long format으로 바꾼다."""
    df = df.copy()

    df["기준일자"] = pd.to_datetime(
        df["기준일자"],
        errors="raise",
    )

    df["출발영업소코드"] = normalize_office_code(
        df["출발영업소코드"]
    )

    df["출발영업소명"] = (
        df["출발영업소명"]
        .astype(str)
        .str.strip()
    )

    # 필요한 기간만 남긴다.
    mask = df["기준일자"].between(
        pd.Timestamp(start_date),
        pd.Timestamp(end_date),
    )

    selected = df.loc[mask].copy()

    if selected.empty:
        raise ValueError(
            f"{period}: {start_date} ~ {end_date} 데이터가 없습니다."
        )

    dest_cols = destination_columns(selected)

    long_df = selected.melt(
        id_vars=[
            "기준일자",
            "출발영업소코드",
            "출발영업소명",
        ],
        value_vars=dest_cols,
        var_name="end_office_code",
        value_name="volume_veh",
    )

    long_df["end_office_code"] = normalize_office_code(
        long_df["end_office_code"]
    )

    long_df["volume_veh"] = pd.to_numeric(
        long_df["volume_veh"],
        errors="coerce",
    )

    # 숫자가 아닌 이상한 값이 있는지 확인
    bad_volume = long_df["volume_veh"].isna()

    if bad_volume.any():
        sample = long_df.loc[
            bad_volume,
            [
                "기준일자",
                "출발영업소코드",
                "end_office_code",
            ],
        ].head(10)

        raise ValueError(
            "교통량을 숫자로 변환하지 못한 행이 있습니다:\n"
            + sample.to_string(index=False)
        )

    if (long_df["volume_veh"] < 0).any():
        raise ValueError("음수 교통량이 존재합니다.")

    long_df["volume_veh"] = long_df["volume_veh"].astype("int64")

    long_df["end_office"] = (
        long_df["end_office_code"].map(office_map)
    )

    unknown = (
        long_df.loc[
            long_df["end_office"].isna(),
            "end_office_code",
        ]
        .drop_duplicates()
        .sort_values()
    )

    if len(unknown):
        print(
            "  경고: 이름을 찾지 못한 도착 영업소 코드 "
            f"{len(unknown)}개: "
            + ", ".join(unknown.head(20))
        )

    result = long_df.rename(
        columns={
            "기준일자": "date",
            "출발영업소코드": "start_office_code",
            "출발영업소명": "start_office",
        }
    )

    result.insert(0, "period", period)

    return result[
        [
            "period",
            "date",
            "start_office_code",
            "start_office",
            "end_office_code",
            "end_office",
            "volume_veh",
        ]
    ]


def print_summary(df: pd.DataFrame) -> None:
    print("\n=== TCS OD 변환 결과 ===")
    print(f"전체 행 수: {len(df):,}")
    print(
        f"날짜: {df['date'].min().date()} "
        f"~ {df['date'].max().date()}"
    )
    print(
        f"출발 영업소: "
        f"{df['start_office_code'].nunique():,}개"
    )
    print(
        f"도착 영업소: "
        f"{df['end_office_code'].nunique():,}개"
    )

    print("\n기간별:")
    print(
        df.groupby("period")
        .agg(
            dates=("date", "nunique"),
            rows=("volume_veh", "size"),
            vehicles=("volume_veh", "sum"),
        )
        .to_string()
    )

    print("\n샘플:")
    sample = df[
        (df["start_office"] == "서울")
        & (df["volume_veh"] > 0)
    ].head(15)

    print(sample.to_string(index=False))


def main() -> int:
    print("TCS OD Matrix 전처리 시작\n")

    raw_frames: dict[str, pd.DataFrame] = {}

    # 1. 원본 읽기
    for period, config in INPUTS.items():
        path = config["path"]

        print(f"[{period}] {path}")

        df = read_csv_robust(path)
        validate_source_columns(df, path)

        print(
            f"  원본: {len(df):,}행, "
            f"{len(df.columns):,}열"
        )

        raw_frames[period] = df

    # 2. 코드 → 이름 대응표
    office_map = build_office_map(
        list(raw_frames.values())
    )

    print(
        f"\n영업소 코드-이름 대응표: "
        f"{len(office_map):,}개"
    )

    # 3. Matrix → long
    outputs = []

    for period, config in INPUTS.items():
        long_df = matrix_to_long(
            raw_frames[period],
            period=period,
            start_date=config["start"],
            end_date=config["end"],
            office_map=office_map,
        )

        print(
            f"[{period}] 변환 완료: "
            f"{len(long_df):,}행"
        )

        outputs.append(long_df)

    result = pd.concat(
        outputs,
        ignore_index=True,
    )

    result = result.sort_values(
        [
            "date",
            "start_office_code",
            "end_office_code",
        ]
    ).reset_index(drop=True)

    # 4. 저장
    DATA_PROCESSED_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        DATA_PROCESSED_DIR
        / "tcs_od_all.parquet"
    )

    result.to_parquet(
        output_path,
        index=False,
    )

    print_summary(result)

    print(
        f"\n저장 완료: {output_path}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())