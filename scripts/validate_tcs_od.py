"""TCS 경부선 OD를 하류 conzone 실측 교통량과 비교한다.

입력:
    data/processed/tcs_od_gyeongbu.parquet
    data/processed/tcs_office_conzone_map.parquet
    data/processed/traffic_gyeongbu.parquet

출력:
    data/processed/tcs_od_validation.parquet

검증 의미:
    특정 영업소에서 특정 방향으로 출발한 TCS 차량 중
    경부선 내부 목적지로 끝나는 차량 합을,
    해당 영업소 직후 conzone의 일 교통량과 비교한다.

coverage_ratio =
    TCS 경부선 내부 목적지 출발량
    / 하류 conzone 일 교통량
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.paths import DATA_PROCESSED_DIR  # noqa: E402

#: traffic 과 OD 의 period 라벨이 다르다. 여기서 맞춘다 (#97).
#:   traffic_gyeongbu.parquet : seollal2026 · base202603   (수집 라벨)
#:   tcs_od_gyeongbu.parquet  : holiday     · normal       (분석 라벨)
#: 예전엔 "202602"/"202603" 으로 바꾸려 했는데 **양쪽 어디에도 없는 값**이라
#: 조인이 전부 NaN 이 되고도 조용히 끝났다.
PERIOD_ALIAS = {
    "seollal2026": "holiday",
    "base202603": "normal",
}

OD_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_od_gyeongbu.parquet"
)

MAP_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_office_conzone_map.parquet"
)

TRAFFIC_PATH = (
    DATA_PROCESSED_DIR
    / "traffic_gyeongbu.parquet"
)

OUTPUT_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_od_validation.parquet"
)


def load_inputs():
    for path in (
        OD_PATH,
        MAP_PATH,
        TRAFFIC_PATH,
    ):
        if not path.exists():
            raise FileNotFoundError(
                f"{path}가 없습니다."
            )

    od = pd.read_parquet(OD_PATH)
    mapping = pd.read_parquet(MAP_PATH)
    traffic = pd.read_parquet(TRAFFIC_PATH)

    return od, mapping, traffic


def build_tcs_daily(
    od: pd.DataFrame,
) -> pd.DataFrame:
    """영업소·날짜·방향별 경부선 내부 목적지 차량 합."""

    out = (
        od.groupby(
            [
                "period",
                "date",
                "start_office_code",
                "start_office",
                "direction",
            ],
            as_index=False,
        )["volume_veh"]
        .sum()
        .rename(
            columns={
                "volume_veh":
                    "tcs_internal_veh"
            }
        )
    )

    return out


def build_traffic_daily(
    traffic: pd.DataFrame,
) -> pd.DataFrame:
    """conzone 일 교통량 합."""

    # 결측 시간이 하나라도 있는 날짜는
    # 일합계를 그대로 신뢰하지 않는다.
    grouped = (
        traffic.groupby(
            [
                "period",
                "date",
                "direction",
                "conzone_id",
                "conzone_name",
            ]
        )
        .agg(
            traffic_daily_veh=(
                "volume_veh",
                "sum",
            ),
            valid_hours=(
                "volume_veh",
                "count",
            ),
        )
        .reset_index()
    )

    # 24시간이 모두 있는 날짜만 유효한 일교통량으로 사용
    grouped.loc[
        grouped["valid_hours"] != 24,
        "traffic_daily_veh",
    ] = pd.NA

    return grouped


def main() -> int:
    print(
        "=== TCS OD / conzone 교통량 검증 ===\n"
    )

    od, mapping, traffic = load_inputs()
    traffic = traffic.copy()

    traffic["period"] = (
        traffic["period"]
        .astype(str)
        .replace(PERIOD_ALIAS)
    )

    # 라벨을 못 맞췄으면 **여기서 멈춘다.** 아래로 내려가면 NaN 만 남는다 (#97)
    od_periods = set(od["period"].astype(str).unique())
    tr_periods = set(traffic["period"].unique())

    if not (od_periods & tr_periods):
        raise SystemExit(
            "\n[중단] traffic 과 OD 의 period 가 하나도 겹치지 않는다.\n"
            f"  OD      : {sorted(od_periods)}\n"
            f"  traffic : {sorted(tr_periods)}\n"
            "  PERIOD_ALIAS 를 실제 값으로 고칠 것 (#97)."
        )

    # 코드형 통일
    od["start_office_code"] = (
        od["start_office_code"]
        .astype(str)
        .str.strip()
    )

    mapping["office_code"] = (
        mapping["office_code"]
        .astype(str)
        .str.strip()
    )

    tcs_daily = build_tcs_daily(od)
    traffic_daily = build_traffic_daily(
        traffic
    )

    print(
        f"TCS 일별 집계: "
        f"{len(tcs_daily):,}행"
    )

    print(
        f"conzone 일별 집계: "
        f"{len(traffic_daily):,}행"
    )

    # --------------------------------------------------
    # 영업소 → 하류 conzone 붙이기
    # --------------------------------------------------
    map_small = mapping[
        [
            "office_code",
            "office_name",
            "direction",
            "conzone_id",
            "conzone_name",
            "mapping_method",
            "office_to_conzone_start_km",
        ]
    ].copy()

    result = tcs_daily.merge(
        map_small,
        left_on=[
            "start_office_code",
            "direction",
        ],
        right_on=[
            "office_code",
            "direction",
        ],
        how="left",
        validate="many_to_one",
    )

    if result["conzone_id"].isna().any():
        bad = result[
            result["conzone_id"].isna()
        ]

        raise RuntimeError(
            "하류 conzone 매핑이 없는 TCS 행이 있습니다:\n"
            + bad.head(20).to_string(index=False)
        )

    # --------------------------------------------------
    # 날짜별 conzone 교통량 붙이기
    # --------------------------------------------------
    result = result.merge(
        traffic_daily,
        on=[
            "period",
            "date",
            "direction",
            "conzone_id",
            "conzone_name",
        ],
        how="left",
        validate="many_to_one",
    )

    # --------------------------------------------------
    # coverage 계산
    # --------------------------------------------------
    result["coverage_ratio"] = (
        result["tcs_internal_veh"]
        / result["traffic_daily_veh"]
    )

    result["coverage_pct"] = (
        result["coverage_ratio"]
        * 100
    )

    result = result[
        [
            "period",
            "date",
            "start_office_code",
            "start_office",
            "direction",
            "tcs_internal_veh",
            "conzone_id",
            "conzone_name",
            "traffic_daily_veh",
            "valid_hours",
            "coverage_ratio",
            "coverage_pct",
            "mapping_method",
            "office_to_conzone_start_km",
        ]
    ].copy()

    # --------------------------------------------------
    # 요약
    # --------------------------------------------------
    valid = result[
        result["coverage_ratio"].notna()
    ]

    print(
        f"\n검증 가능 행: "
        f"{len(valid):,} / "
        f"{len(result):,}"
    )

    # ⚠ 0 행을 검증하고 "저장 완료" 를 찍으면 아무도 못 잡는다 (#97).
    #   검증할 게 없으면 **저장하지 않고 실패한다.**
    if valid.empty:
        raise SystemExit(
            "\n[중단] 검증 가능한 행이 없다 — 조인이 전부 비었다.\n"
            "  period·date·direction·conzone_id 네 키 중 무엇이 어긋났는지 확인하라.\n"
            "  결과 파일은 저장하지 않는다."
        )

    print(
        "\ncoverage 요약:"
    )

    print(
        valid["coverage_pct"]
        .describe()
        .to_string()
    )

    print(
        "\n기간별 평균:"
    )

    print(
        valid.groupby(
            "period"
        )["coverage_pct"]
        .agg(
            [
                "count",
                "mean",
                "median",
                "min",
                "max",
            ]
        )
        .round(2)
        .to_string()
    )

    # --------------------------------------------------
    # 서울 예시 출력
    # --------------------------------------------------
    seoul = result[
        (
            result["start_office"]
            == "서울"
        )
        & (
            result["date"]
            == pd.Timestamp(
                "2026-02-14"
            )
        )
    ]

    print(
        "\n=== 2026-02-14 서울 예시 ==="
    )

    if len(seoul):
        print(
            seoul.to_string(
                index=False
            )
        )
    else:
        print(
            "해당 행을 찾지 못했습니다."
        )

    # --------------------------------------------------
    # coverage > 100% 진단
    # --------------------------------------------------
    over_100 = valid[
        valid["coverage_ratio"] > 1.0
    ]

    print(
        f"\ncoverage > 100%: "
        f"{len(over_100):,}행"
    )

    if len(over_100):
        print(
            over_100[
                [
                    "period",
                    "date",
                    "start_office",
                    "direction",
                    "tcs_internal_veh",
                    "traffic_daily_veh",
                    "coverage_pct",
                    "conzone_name",
                ]
            ]
            .sort_values(
                "coverage_pct",
                ascending=False,
            )
            .head(30)
            .to_string(index=False)
        )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    result.to_parquet(
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