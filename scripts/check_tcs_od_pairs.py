"""TCS 경부선 OD에서 의심스러운 OD 쌍을 진단한다.

입력:
    data/processed/tcs_od_gyeongbu.parquet

출력:
    data/processed/tcs_od_pair_diagnostics.parquet

목적:
    Issue #61의 "의심 OD 쌍 조사"를 위한 진단 자료를 만든다.

주의:
    여기서 '의심'은 데이터 오류라는 뜻이 아니다.
    추가 확인이 필요한 패턴을 자동으로 표시하는 용도다.
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.paths import DATA_PROCESSED_DIR  # noqa: E402


OD_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_od_gyeongbu.parquet"
)

OUTPUT_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_od_pair_diagnostics.parquet"
)


# 장거리 OD 기준
LONG_DISTANCE_KM = 250.0

# 장거리인데 일평균이 이 값보다 작으면 진단 대상으로 표시
LOW_DAILY_VOLUME = 100.0

# 양방향 차이가 5배 이상이면 진단 대상으로 표시
ASYMMETRY_RATIO = 5.0

# holiday / normal 일평균 차이가 3배 이상이면 표시
PERIOD_RATIO = 3.0

MIN_MEAN_DAILY_FOR_RATIO = 10.0
# 방향 제한 영업소
#
# 상서 하이패스IC:
# 서울 방향(UP)만 진입·진출 가능.
# 부산 방향(DOWN)은 진입·진출 불가.
DIRECTION_LIMITED_OFFICES = {
    "상서": {"UP"},
}

SPECIAL_OD_MARKERS = {
    "금강휴게소",
}


def load_od() -> pd.DataFrame:
    if not OD_PATH.exists():
        raise FileNotFoundError(
            f"{OD_PATH}가 없습니다.\n"
            "먼저 python scripts/build_tcs_od_gyeongbu.py 를 실행하세요."
        )

    df = pd.read_parquet(OD_PATH)

    required = {
        "period",
        "date",
        "start_office_code",
        "start_office",
        "end_office_code",
        "end_office",
        "direction",
        "volume_veh",
        "distance_km",
    }

    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            "tcs_od_gyeongbu.parquet에 필요한 컬럼이 없습니다: "
            + ", ".join(sorted(missing))
        )

    return df


def print_seoul_busan(df: pd.DataFrame) -> None:
    """Issue #61에서 언급한 서울→부산 OD를 확인한다."""

    target = df[
        (df["start_office"] == "서울")
        & (df["end_office"] == "부산")
    ].copy()

    print(
        "\n=== 서울 → 부산 날짜별 ==="
    )

    if target.empty:
        print(
            "서울 → 부산 OD를 찾지 못했습니다."
        )
        return

    print(
        target[
            [
                "period",
                "date",
                "direction",
                "volume_veh",
                "distance_km",
            ]
        ]
        .sort_values("date")
        .to_string(index=False)
    )

    print(
        "\n서울 → 부산 기간별 요약:"
    )

    summary = (
        target.groupby("period")
        .agg(
            days=("date", "nunique"),
            total_veh=("volume_veh", "sum"),
            mean_per_day=("volume_veh", "mean"),
            min_per_day=("volume_veh", "min"),
            max_per_day=("volume_veh", "max"),
        )
        .round(2)
    )

    print(
        summary.to_string()
    )


def build_pair_summary(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """OD pair × period 단위 일평균 요약."""

    summary = (
        df.groupby(
            [
                "period",
                "start_office_code",
                "start_office",
                "end_office_code",
                "end_office",
                "direction",
            ],
            as_index=False,
        )
        .agg(
            days=("date", "nunique"),
            total_veh=("volume_veh", "sum"),
            mean_daily_veh=("volume_veh", "mean"),
            min_daily_veh=("volume_veh", "min"),
            max_daily_veh=("volume_veh", "max"),
            distance_km=("distance_km", "first"),
        )
    )

    return summary


def add_long_distance_flag(
    summary: pd.DataFrame,
) -> pd.DataFrame:
    out = summary.copy()

    out["flag_long_low"] = (
            (out["distance_km"] >= LONG_DISTANCE_KM)
            & (out["mean_daily_veh"] > 0)
            & (
                    out["mean_daily_veh"]
                    < LOW_DAILY_VOLUME
            )
    )

    return out


def is_structural_direction_zero(
    start_office: str,
    end_office: str,
    direction: str,
) -> bool:
    """IC 구조상 해당 방향 OD가 발생할 수 없는지를 판단한다.

    예:
        상서는 UP 방향만 진입·진출 가능하므로
        상서가 포함된 DOWN OD는 구조적으로 0일 수 있다.
    """

    for office_name in (
        start_office,
        end_office,
    ):
        allowed = DIRECTION_LIMITED_OFFICES.get(
            office_name
        )

        if (
            allowed is not None
            and direction not in allowed
        ):
            return True

    return False

def build_direction_asymmetry(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """A→B와 B→A의 기간별 일평균 차이를 계산한다."""

    # --------------------------------------------------
    # 1. OD pair × period 단위 일평균
    # --------------------------------------------------
    pair = (
        df.groupby(
            [
                "period",
                "start_office_code",
                "start_office",
                "end_office_code",
                "end_office",
                "direction",
            ],
            as_index=False,
        )
        .agg(
            mean_daily_veh=(
                "volume_veh",
                "mean",
            ),
            distance_km=(
                "distance_km",
                "first",
            ),
        )
    )

    # --------------------------------------------------
    # 2. 구조적으로 불가능한 방향 표시
    #
    # 예:
    # 상서는 UP 방향만 진입·진출 가능하므로
    # 상서가 포함된 DOWN OD는 구조적 0으로 본다.
    # --------------------------------------------------
    pair["structural_zero"] = pair.apply(
        lambda row: is_structural_direction_zero(
            row["start_office"],
            row["end_office"],
            row["direction"],
        ),
        axis=1,
    )

    # --------------------------------------------------
    # 3. 특수 OD 표기 포함 여부
    #
    # 금강휴게소처럼 일반 영업소 OD와 동일하게
    # 해석하면 안 되는 항목을 따로 표시한다.
    # --------------------------------------------------
    pair["special_od_marker"] = (
        pair["start_office"].isin(
            SPECIAL_OD_MARKERS
        )
        | pair["end_office"].isin(
            SPECIAL_OD_MARKERS
        )
    )

    # --------------------------------------------------
    # 4. 역방향 OD 생성
    #
    # A → B 행에
    # B → A 평균 교통량을 붙이기 위한 테이블
    # --------------------------------------------------
    reverse = pair.rename(
        columns={
            "start_office_code":
                "end_office_code",

            "start_office":
                "end_office",

            "end_office_code":
                "start_office_code",

            "end_office":
                "start_office",

            "mean_daily_veh":
                "reverse_mean_daily_veh",

            "structural_zero":
                "reverse_structural_zero",

            "special_od_marker":
                "reverse_special_od_marker",
        }
    )[
        [
            "period",
            "start_office_code",
            "start_office",
            "end_office_code",
            "end_office",
            "reverse_mean_daily_veh",
            "reverse_structural_zero",
            "reverse_special_od_marker",
        ]
    ]

    # --------------------------------------------------
    # 5. 원방향 + 역방향 결합
    # --------------------------------------------------
    out = pair.merge(
        reverse,
        on=[
            "period",
            "start_office_code",
            "start_office",
            "end_office_code",
            "end_office",
        ],
        how="left",
        validate="one_to_one",
    )

    # --------------------------------------------------
    # 6. 두 방향 중 작은 값 / 큰 값
    # --------------------------------------------------
    smaller = out[
        [
            "mean_daily_veh",
            "reverse_mean_daily_veh",
        ]
    ].min(axis=1)

    larger = out[
        [
            "mean_daily_veh",
            "reverse_mean_daily_veh",
        ]
    ].max(axis=1)

    # 둘 다 0이면 ratio = 1
    out["direction_ratio"] = 1.0

    # 둘 다 0보다 크면 큰 값 / 작은 값
    positive = smaller > 0

    out.loc[
        positive,
        "direction_ratio",
    ] = (
        larger[positive]
        / smaller[positive]
    )

    # 한쪽만 0이면 무한대로 처리
    one_zero = (
        (smaller == 0)
        & (larger > 0)
    )

    out.loc[
        one_zero,
        "direction_ratio",
    ] = float("inf")

    # --------------------------------------------------
    # 7. 구조적 비대칭 여부
    #
    # 두 방향 중 하나라도 IC 구조상 불가능한 방향이면
    # 일반적인 이상치 분석 대상에서 제외한다.
    # --------------------------------------------------
    out["structural_asymmetry"] = (
        out["structural_zero"]
        | out["reverse_structural_zero"]
    )

    # --------------------------------------------------
    # 8. 특수 표기 때문에 생긴 비대칭 여부
    #
    # 금강휴게소 등이 포함된 OD는 일반 영업소 간
    # 비대칭 분석에서 제외한다.
    # --------------------------------------------------
    out["special_asymmetry"] = (
        out["special_od_marker"]
        | out["reverse_special_od_marker"]
    )

    # --------------------------------------------------
    # 9. 실제 추가 검토가 필요한 비대칭
    #
    # 조건:
    # - 구조적으로 설명되는 경우 제외
    # - 특수 OD 표기 제외
    # - 두 방향 중 큰 쪽의 일평균 >= 10대
    # - 양방향 차이 >= 5배
    # --------------------------------------------------
    out["flag_asymmetry"] = (
        ~out["structural_asymmetry"]
        & ~out["special_asymmetry"]
        & (
            out[
                [
                    "mean_daily_veh",
                    "reverse_mean_daily_veh",
                ]
            ]
            .max(axis=1)
            >= MIN_MEAN_DAILY_FOR_RATIO
        )
        & (
            out["direction_ratio"]
            >= ASYMMETRY_RATIO
        )
    )

    return out


def build_period_change(
    summary: pd.DataFrame,
) -> pd.DataFrame:
    """같은 OD의 holiday / normal 일평균 차이를 비교한다."""

    pivot = summary.pivot_table(
        index=[
            "start_office_code",
            "start_office",
            "end_office_code",
            "end_office",
            "direction",
            "distance_km",
        ],
        columns="period",
        values="mean_daily_veh",
        aggfunc="first",
    ).reset_index()

    pivot.columns.name = None

    if "holiday" not in pivot:
        pivot["holiday"] = pd.NA

    if "normal" not in pivot:
        pivot["normal"] = pd.NA

    smaller = pivot[
        [
            "holiday",
            "normal",
        ]
    ].min(axis=1)

    larger = pivot[
        [
            "holiday",
            "normal",
        ]
    ].max(axis=1)

    pivot["period_ratio"] = 1.0

    positive = smaller > 0

    pivot.loc[
        positive,
        "period_ratio",
    ] = (
        larger[positive]
        / smaller[positive]
    )

    one_zero = (
        (smaller == 0)
        & (larger > 0)
    )

    pivot.loc[
        one_zero,
        "period_ratio",
    ] = float("inf")

    pivot["flag_period_change"] = (
            (
                    pivot[
                        [
                            "holiday",
                            "normal",
                        ]
                    ]
                    .max(axis=1)
                    >= MIN_MEAN_DAILY_FOR_RATIO
            )
            & (
                    pivot["period_ratio"]
                    >= PERIOD_RATIO
            )
    )

    return pivot


def main() -> int:
    print(
        "=== TCS OD 쌍 진단 ==="
    )

    df = load_od()

    print(
        f"\nOD 행 수: "
        f"{len(df):,}"
    )

    print(
        f"날짜: "
        f"{df['date'].min().date()} "
        f"~ "
        f"{df['date'].max().date()}"
    )

    # --------------------------------------------------
    # 1. Issue #61 서울 → 부산 확인
    # --------------------------------------------------
    print_seoul_busan(df)

    # --------------------------------------------------
    # 2. OD pair 요약
    # --------------------------------------------------
    summary = build_pair_summary(df)

    summary = add_long_distance_flag(
        summary
    )

    long_low = summary[
        summary["flag_long_low"]
    ].copy()

    print(
        "\n=== 장거리 + 저교통량 OD ==="
    )

    print(
        f"기준: 거리 >= "
        f"{LONG_DISTANCE_KM:.0f} km, "
        f"일평균 < "
        f"{LOW_DAILY_VOLUME:.0f}대"
    )

    print(
        f"해당 OD: "
        f"{len(long_low):,}개"
    )

    if len(long_low):
        print(
            long_low[
                [
                    "period",
                    "start_office",
                    "end_office",
                    "direction",
                    "distance_km",
                    "mean_daily_veh",
                    "min_daily_veh",
                    "max_daily_veh",
                ]
            ]
            .sort_values(
                [
                    "mean_daily_veh",
                    "distance_km",
                ],
                ascending=[
                    True,
                    False,
                ],
            )
            .head(40)
            .to_string(index=False)
        )

    # --------------------------------------------------
    # 3. A→B / B→A 차이
    # --------------------------------------------------
    asymmetry = build_direction_asymmetry(
        df
    )

    strange_direction = asymmetry[
        asymmetry["flag_asymmetry"]
    ].copy()

    print(
        "\n=== 양방향 차이가 큰 OD ==="
    )

    print(
        f"기준: "
        f"{ASYMMETRY_RATIO:.1f}배 이상"
    )

    print(
        f"해당 OD: "
        f"{len(strange_direction):,}개"
    )

    if len(strange_direction):
        print(
            strange_direction[
                [
                    "period",
                    "start_office",
                    "end_office",
                    "distance_km",
                    "mean_daily_veh",
                    "reverse_mean_daily_veh",
                    "direction_ratio",
                ]
            ]
            .sort_values(
                "direction_ratio",
                ascending=False,
            )
            .head(40)
            .to_string(index=False)
        )

    # --------------------------------------------------
    # 4. 설 / 평시 차이
    # --------------------------------------------------
    period_change = build_period_change(
        summary
    )

    strange_period = period_change[
        period_change[
            "flag_period_change"
        ]
    ].copy()

    print(
        "\n=== 설 / 평시 차이가 큰 OD ==="
    )

    print(
        f"기준: "
        f"{PERIOD_RATIO:.1f}배 이상"
    )

    print(
        f"해당 OD: "
        f"{len(strange_period):,}개"
    )

    if len(strange_period):
        print(
            strange_period[
                [
                    "start_office",
                    "end_office",
                    "direction",
                    "distance_km",
                    "holiday",
                    "normal",
                    "period_ratio",
                ]
            ]
            .sort_values(
                "period_ratio",
                ascending=False,
            )
            .head(40)
            .to_string(index=False)
        )

    # --------------------------------------------------
    # 5. 진단 결과 저장
    # --------------------------------------------------
    diagnostics = summary.merge(
        asymmetry[
            [
                "period",
                "start_office_code",
                "start_office",
                "end_office_code",
                "end_office",
                "reverse_mean_daily_veh",
                "direction_ratio",
                "structural_zero",
                "reverse_structural_zero",
                "structural_asymmetry",
                "special_od_marker",
                "reverse_special_od_marker",
                "special_asymmetry",
                "flag_asymmetry",
            ]
        ],
        on=[
            "period",
            "start_office_code",
            "start_office",
            "end_office_code",
            "end_office",
        ],
        how="left",
        validate="one_to_one",
    )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    diagnostics.to_parquet(
        OUTPUT_PATH,
        index=False,
    )

    print(
        f"\n저장 완료: "
        f"{OUTPUT_PATH}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())