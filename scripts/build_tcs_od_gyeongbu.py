"""TCS 전체 OD Matrix에서 경부고속도로 내부 OD를 구축한다.

입력:
    data/processed/tcs_od_all.parquet
    data/processed/tcs_offices_gyeongbu.parquet

출력:
    data/processed/tcs_od_gyeongbu.parquet

하는 일:
    1. 출발/도착이 모두 유효 경부선 TCS 영업소인 OD만 남긴다.
    2. 출발/도착 영업소의 공통 milepost_km를 붙인다.
    3. milepost 증감으로 UP/DOWN 방향을 결정한다.
    4. 방향별 출발/도착 offset을 계산한다.

방향 기준:
    공통 milepost는 구서IC -> 양재IC 방향으로 증가한다.
    - end_milepost > start_milepost: UP
    - end_milepost < start_milepost: DOWN

동일 영업소(start == end)는 방향을 정의할 수 없으므로
진단 후 최종 경부선 OD에서는 제외한다.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.paths import DATA_PROCESSED_DIR  # noqa: E402


OD_ALL_PATH = DATA_PROCESSED_DIR / "tcs_od_all.parquet"
OFFICES_PATH = DATA_PROCESSED_DIR / "tcs_offices_gyeongbu.parquet"
OUTPUT_PATH = DATA_PROCESSED_DIR / "tcs_od_gyeongbu.parquet"


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    if not OD_ALL_PATH.exists():
        raise FileNotFoundError(
            f"{OD_ALL_PATH}가 없습니다.\n"
            "먼저 python scripts/build_tcs_od.py 를 실행하세요."
        )

    if not OFFICES_PATH.exists():
        raise FileNotFoundError(
            f"{OFFICES_PATH}가 없습니다.\n"
            "먼저 python scripts/build_tcs_offices.py 를 실행하세요."
        )

    od = pd.read_parquet(OD_ALL_PATH)
    offices = pd.read_parquet(OFFICES_PATH)

    return od, offices


def validate_offices(offices: pd.DataFrame) -> None:
    required = {
        "office_code",
        "office_name",
        "milepost_km",
        "offset_up_km",
        "offset_down_km",
    }

    missing = required - set(offices.columns)

    if missing:
        raise ValueError(
            "tcs_offices_gyeongbu.parquet에 필요한 컬럼이 없습니다: "
            + ", ".join(sorted(missing))
        )

    if offices["office_code"].duplicated().any():
        duplicated = offices[
            offices["office_code"].duplicated(keep=False)
        ]

        raise ValueError(
            "경부선 영업소 코드가 중복되어 있습니다:\n"
            + duplicated.to_string(index=False)
        )


def build_gyeongbu_od(
    od: pd.DataFrame,
    offices: pd.DataFrame,
) -> pd.DataFrame:
    required_od = {
        "period",
        "date",
        "start_office_code",
        "start_office",
        "end_office_code",
        "end_office",
        "volume_veh",
    }

    missing = required_od - set(od.columns)

    if missing:
        raise ValueError(
            "tcs_od_all.parquet에 필요한 컬럼이 없습니다: "
            + ", ".join(sorted(missing))
        )

    # 코드 타입 통일
    od = od.copy()

    od["start_office_code"] = (
        od["start_office_code"]
        .astype(str)
        .str.strip()
    )

    od["end_office_code"] = (
        od["end_office_code"]
        .astype(str)
        .str.strip()
    )

    offices = offices.copy()

    offices["office_code"] = (
        offices["office_code"]
        .astype(str)
        .str.strip()
    )

    valid_codes = set(offices["office_code"])

    # 출발/도착 모두 경부선 유효 영업소인 OD
    mask = (
        od["start_office_code"].isin(valid_codes)
        & od["end_office_code"].isin(valid_codes)
    )

    gyeongbu = od.loc[mask].copy()

    print(
        f"전체 OD: {len(od):,}행"
    )

    print(
        f"경부선 내부 OD: {len(gyeongbu):,}행"
    )

    print(
        f"전체 차량수: {od['volume_veh'].sum():,.0f}대"
    )

    print(
        f"경부선 내부 차량수: "
        f"{gyeongbu['volume_veh'].sum():,.0f}대"
    )

    # --------------------------------------------
    # 출발 영업소 공간정보
    # --------------------------------------------
    start_lookup = offices[
        [
            "office_code",
            "milepost_km",
            "offset_up_km",
            "offset_down_km",
        ]
    ].rename(
        columns={
            "office_code": "start_office_code",
            "milepost_km": "start_milepost_km",
            "offset_up_km": "start_offset_up_km",
            "offset_down_km": "start_offset_down_km",
        }
    )

    gyeongbu = gyeongbu.merge(
        start_lookup,
        on="start_office_code",
        how="left",
        validate="many_to_one",
    )

    # --------------------------------------------
    # 도착 영업소 공간정보
    # --------------------------------------------
    end_lookup = offices[
        [
            "office_code",
            "milepost_km",
            "offset_up_km",
            "offset_down_km",
        ]
    ].rename(
        columns={
            "office_code": "end_office_code",
            "milepost_km": "end_milepost_km",
            "offset_up_km": "end_offset_up_km",
            "offset_down_km": "end_offset_down_km",
        }
    )

    gyeongbu = gyeongbu.merge(
        end_lookup,
        on="end_office_code",
        how="left",
        validate="many_to_one",
    )

    # 공간정보 누락 검사
    spatial_cols = [
        "start_milepost_km",
        "end_milepost_km",
    ]

    if gyeongbu[spatial_cols].isna().any().any():
        bad = gyeongbu[
            gyeongbu[spatial_cols]
            .isna()
            .any(axis=1)
        ]

        raise RuntimeError(
            "경부선 OD 일부에 영업소 위치정보가 없습니다:\n"
            + bad.head(20).to_string(index=False)
        )

    # --------------------------------------------
    # 동일 영업소 OD 확인
    # --------------------------------------------
    same = gyeongbu[
        gyeongbu["start_office_code"]
        == gyeongbu["end_office_code"]
    ]

    print(
        f"\n동일 영업소 OD: "
        f"{len(same):,}행 / "
        f"{same['volume_veh'].sum():,.0f}대"
    )

    # 동일 영업소는 UP/DOWN 방향 정의 불가
    gyeongbu = gyeongbu[
        gyeongbu["start_office_code"]
        != gyeongbu["end_office_code"]
    ].copy()

    # --------------------------------------------
    # 방향 판정
    # --------------------------------------------
    gyeongbu["direction"] = "UP"

    gyeongbu.loc[
        gyeongbu["end_milepost_km"]
        < gyeongbu["start_milepost_km"],
        "direction",
    ] = "DOWN"

    # 방향 기준 offset
    gyeongbu["start_offset_km"] = gyeongbu[
        "start_offset_up_km"
    ]

    gyeongbu["end_offset_km"] = gyeongbu[
        "end_offset_up_km"
    ]

    down_mask = gyeongbu["direction"] == "DOWN"

    gyeongbu.loc[
        down_mask,
        "start_offset_km",
    ] = gyeongbu.loc[
        down_mask,
        "start_offset_down_km",
    ]

    gyeongbu.loc[
        down_mask,
        "end_offset_km",
    ] = gyeongbu.loc[
        down_mask,
        "end_offset_down_km",
    ]

    # 실제 이동거리
    gyeongbu["distance_km"] = (
        gyeongbu["end_milepost_km"]
        - gyeongbu["start_milepost_km"]
    ).abs()

    # 최종 컬럼
    gyeongbu = gyeongbu[
        [
            "period",
            "date",
            "start_office_code",
            "start_office",
            "end_office_code",
            "end_office",
            "direction",
            "volume_veh",
            "start_milepost_km",
            "end_milepost_km",
            "start_offset_km",
            "end_offset_km",
            "distance_km",
        ]
    ].copy()

    return gyeongbu


def validate_result(df: pd.DataFrame) -> None:
    if df.empty:
        raise RuntimeError(
            "경부선 OD 결과가 비어 있습니다."
        )

    if (df["volume_veh"] < 0).any():
        raise ValueError(
            "음수 OD 교통량이 존재합니다."
        )

    if df["direction"].isna().any():
        raise ValueError(
            "방향이 없는 OD가 존재합니다."
        )

    invalid_direction = ~df["direction"].isin(
        ["UP", "DOWN"]
    )

    if invalid_direction.any():
        raise ValueError(
            "UP/DOWN 이외의 방향값이 존재합니다."
        )

    if (df["distance_km"] <= 0).any():
        raise ValueError(
            "이동거리가 0 이하인 OD가 존재합니다."
        )

    # 방향별 offset은 항상 주행방향으로 증가해야 한다.
    bad_offset = (
        df["end_offset_km"]
        <= df["start_offset_km"]
    )

    if bad_offset.any():
        bad = df.loc[bad_offset].head(20)

        raise RuntimeError(
            "방향별 offset 순서가 잘못된 OD가 있습니다:\n"
            + bad.to_string(index=False)
        )


def main() -> int:
    print(
        "=== 경부선 TCS OD 구축 ===\n"
    )

    od, offices = load_inputs()

    validate_offices(offices)

    print(
        f"유효 경부선 영업소: "
        f"{len(offices):,}개"
    )

    result = build_gyeongbu_od(
        od,
        offices,
    )

    validate_result(result)

    print(
        "\n=== 최종 결과 ==="
    )

    print(
        f"OD 행 수: {len(result):,}"
    )

    print(
        f"차량수: "
        f"{result['volume_veh'].sum():,.0f}대"
    )

    print(
        "\n기간별:"
    )

    print(
        result.groupby("period").agg(
            dates=("date", "nunique"),
            rows=("volume_veh", "size"),
            vehicles=("volume_veh", "sum"),
        ).to_string()
    )

    print(
        "\n방향별:"
    )

    print(
        result.groupby("direction").agg(
            rows=("volume_veh", "size"),
            vehicles=("volume_veh", "sum"),
        ).to_string()
    )

    print(
        "\n거리 범위:"
    )

    print(
        result["distance_km"]
        .describe()
        .to_string()
    )

    DATA_PROCESSED_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    result.to_parquet(
        OUTPUT_PATH,
        index=False,
    )

    print(
        f"\n저장 완료: {OUTPUT_PATH}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())