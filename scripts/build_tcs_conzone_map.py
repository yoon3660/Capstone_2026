"""TCS 경부선 영업소를 방향별 하류 conzone에 매핑한다.

입력:
    data/processed/tcs_offices_gyeongbu.parquet
    data/processed/conzone_gyeongbu.parquet

출력:
    data/processed/tcs_office_conzone_map.parquet

목적:
    Issue #61의 TCS OD 검증을 위해 각 영업소에서 출발한 차량을
    해당 영업소 직후의 VDS conzone 교통량과 비교할 수 있도록 한다.

매핑 원칙:
    1. 영업소명과 conzone 시작 경계명이 같으면 해당 conzone 사용
    2. conzone 시작점이 영업소 위치와 충분히 가까우면 해당 conzone 사용
    3. 영업소가 특정 conzone 내부에 있으면 해당 conzone 사용
    4. 그 외에는 주행 방향으로 가장 가까운 다음 conzone 사용

주의:
    UP/DOWN 모두 offset_km가 주행 방향으로 증가한다.
"""

from __future__ import annotations

import re
import sys

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.paths import DATA_PROCESSED_DIR  # noqa: E402

OFFICES_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_offices_gyeongbu.parquet"
)

CONZONES_PATH = (
    DATA_PROCESSED_DIR
    / "conzone_gyeongbu.parquet"
)

OUTPUT_PATH = (
    DATA_PROCESSED_DIR
    / "tcs_office_conzone_map.parquet"
)

# 일반 영업소는 IC와 거의 같은 위치이므로
# conzone 시작점과 2 km 이내면 같은 진입 경계로 본다.
START_MATCH_TOL_KM = 2.0

def normalize_endpoint_name(name: str) -> str:
    """TCS 영업소명과 conzone 경계 이름을 비교하기 위한 정규화."""

    name = str(name or "").strip()
    name = re.sub(r"\s+", "", name)

    # 긴 접미사부터 제거
    # 예:
    # 서울TG       -> 서울
    # 수원신갈IC   -> 수원신갈
    # 옥산하이패스IC -> 옥산
    # 서울주JC     -> 서울주
    name = re.sub(
        r"(하이패스IC|하이패스|JCT|JC|IC|TG|T/G|영업소)$",
        "",
        name,
        flags=re.IGNORECASE,
    )

    return name
def conzone_start_name(conzone_name: str) -> str:
    """주행방향 기준 conzone의 시작 경계 이름을 반환한다."""

    parts = str(conzone_name).split("-", 1)

    if not parts:
        return ""

    return normalize_endpoint_name(parts[0])


def load_conzones() -> pd.DataFrame:
    if not CONZONES_PATH.exists():
        raise FileNotFoundError(
            f"{CONZONES_PATH}가 없습니다."
        )

    df = pd.read_parquet(CONZONES_PATH)

    required = {
        "direction",
        "conzone_id",
        "name",
        "seq",
        "offset_km_start",
        "offset_km_end",
        "offset_km",
        "position_source",
        "in_corridor",
    }

    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            "conzone_gyeongbu.parquet에 필요한 컬럼이 없습니다: "
            + ", ".join(sorted(missing))
        )

    # build_traffic.py에서 기간별 위치표를 붙이므로
    # holiday / normal에 같은 conzone이 중복될 수 있다.
    position_cols = [
        "direction",
        "conzone_id",
        "name",
        "seq",
        "offset_km_start",
        "offset_km_end",
        "offset_km",
        "position_source",
        "in_corridor",
    ]

    df = (
        df[position_cols]
        .drop_duplicates()
        .copy()
    )

    # 같은 conzone_id / direction에 서로 다른 위치가 있으면
    # 조용히 하나를 고르지 않고 중단한다.
    counts = (
        df.groupby(
            ["direction", "conzone_id"]
        )
        .size()
    )

    bad = counts[counts > 1]

    if len(bad):
        raise RuntimeError(
            "기간별 conzone 위치가 서로 다릅니다:\n"
            + bad.to_string()
        )

    df = df[df["in_corridor"]].copy()

    df = df.dropna(
        subset=[
            "offset_km_start",
            "offset_km_end",
        ]
    )

    # 방향별 offset은 주행방향으로 증가해야 한다.
    bad_order = (
        df["offset_km_end"]
        <= df["offset_km_start"]
    )

    if bad_order.any():
        raise RuntimeError(
            "offset 시작/끝 순서가 잘못된 conzone이 있습니다:\n"
            + df.loc[bad_order].to_string(index=False)
        )

    return df


def choose_downstream_conzone(
    office_name: str,
    office_offset: float,
    conzones: pd.DataFrame,
) -> dict | None:
    """영업소 직후에 사용할 conzone 하나를 고른다.

    우선순위:
    1. 영업소 이름 == conzone 시작 경계 이름
    2. conzone 시작점이 영업소 offset과 2 km 이내
    3. 영업소가 포함된 conzone
    4. 주행방향으로 다음 conzone
    """

    temp = conzones.copy()

    # --------------------------------------------------
    # 공통 거리 계산
    # --------------------------------------------------
    temp["start_gap_km"] = (
        temp["offset_km_start"]
        - office_offset
    )

    temp["abs_start_gap_km"] = (
        temp["start_gap_km"].abs()
    )

    # --------------------------------------------------
    # 1. 이름 기준:
    #    영업소와 conzone 시작 경계가 같은 구간을 최우선
    #
    # 서울 + 서울TG-신갈JC
    # → 서울TG-신갈JC 선택
    # --------------------------------------------------
    office_key = normalize_endpoint_name(
        office_name
    )

    temp["start_name_key"] = (
        temp["name"]
        .map(conzone_start_name)
    )

    name_matches = temp[
        temp["start_name_key"] == office_key
    ].copy()

    if len(name_matches):
        # 혹시 같은 시작 이름이 여러 개 있으면
        # 실제 영업소 offset과 가장 가까운 것을 사용
        chosen = (
            name_matches
            .sort_values(
                [
                    "abs_start_gap_km",
                    "seq",
                ]
            )
            .iloc[0]
        )

        return {
            **chosen.to_dict(),
            "mapping_method": "name_start",
            "office_to_conzone_start_km":
                float(
                    chosen["start_gap_km"]
                ),
        }

    # --------------------------------------------------
    # 2. conzone 시작점과 2 km 이내
    # --------------------------------------------------
    nearest_start = (
        temp.sort_values(
            [
                "abs_start_gap_km",
                "seq",
            ]
        )
        .iloc[0]
    )

    if (
        nearest_start["abs_start_gap_km"]
        <= START_MATCH_TOL_KM
    ):
        return {
            **nearest_start.to_dict(),
            "mapping_method": "nearest_start",
            "office_to_conzone_start_km":
                float(
                    nearest_start[
                        "start_gap_km"
                    ]
                ),
        }

    # --------------------------------------------------
    # 3. 영업소가 특정 conzone 안에 있는 경우
    # --------------------------------------------------
    containing = temp[
        (
            temp["offset_km_start"]
            <= office_offset
        )
        & (
            office_offset
            <= temp["offset_km_end"]
        )
    ].copy()

    if len(containing):
        chosen = (
            containing
            .sort_values("seq")
            .iloc[0]
        )

        return {
            **chosen.to_dict(),
            "mapping_method": "containing",
            "office_to_conzone_start_km":
                float(
                    chosen["offset_km_start"]
                    - office_offset
                ),
        }

    # --------------------------------------------------
    # 4. 주행방향으로 다음에 시작하는 conzone
    # --------------------------------------------------
    downstream = temp[
        temp["offset_km_start"]
        > office_offset
    ].copy()

    if len(downstream):
        chosen = (
            downstream
            .sort_values(
                "offset_km_start"
            )
            .iloc[0]
        )

        return {
            **chosen.to_dict(),
            "mapping_method": "next_downstream",
            "office_to_conzone_start_km":
                float(
                    chosen["offset_km_start"]
                    - office_offset
                ),
        }

    return None


def build_mapping(
    offices: pd.DataFrame,
    conzones: pd.DataFrame,
) -> pd.DataFrame:

    rows = []

    for office in offices.itertuples(
        index=False
    ):
        for direction in (
            "UP",
            "DOWN",
        ):
            if direction == "UP":
                office_offset = float(
                    office.offset_up_km
                )
            else:
                office_offset = float(
                    office.offset_down_km
                )

            candidates = conzones[
                conzones["direction"]
                == direction
            ].copy()

            chosen = choose_downstream_conzone(
                office.office_name,
                office_offset,
                candidates,
            )

            if chosen is None:
                print(
                    f"  매핑 실패: "
                    f"{office.office_name} / "
                    f"{direction}"
                )

                continue

            rows.append(
                {
                    "office_code":
                        office.office_code,

                    "office_name":
                        office.office_name,

                    "direction":
                        direction,

                    "office_offset_km":
                        round(
                            office_offset,
                            3,
                        ),

                    "conzone_id":
                        chosen["conzone_id"],

                    "conzone_name":
                        chosen["name"],

                    "conzone_seq":
                        int(
                            chosen["seq"]
                        ),

                    "conzone_start_km":
                        float(
                            chosen[
                                "offset_km_start"
                            ]
                        ),

                    "conzone_end_km":
                        float(
                            chosen[
                                "offset_km_end"
                            ]
                        ),

                    "conzone_position_source":
                        chosen[
                            "position_source"
                        ],

                    "mapping_method":
                        chosen[
                            "mapping_method"
                        ],

                    "office_to_conzone_start_km":
                        round(
                            chosen[
                                "office_to_conzone_start_km"
                            ],
                            3,
                        ),
                }
            )

    return pd.DataFrame(rows)


def main() -> int:
    print(
        "=== TCS 영업소 → 하류 conzone 매핑 ===\n"
    )

    if not OFFICES_PATH.exists():
        raise FileNotFoundError(
            f"{OFFICES_PATH}가 없습니다."
        )

    offices = pd.read_parquet(
        OFFICES_PATH
    )

    conzones = load_conzones()

    print(
        f"TCS 영업소: "
        f"{len(offices):,}개"
    )

    print(
        "경부선 conzone:"
    )

    print(
        conzones.groupby(
            "direction"
        )["conzone_id"]
        .nunique()
        .to_string()
    )

    mapped = build_mapping(
        offices,
        conzones,
    )

    # 42개 × 2방향이 기본 기대값
    print(
        f"\n매핑 결과: "
        f"{len(mapped):,}행"
    )

    print(
        "\n방향별:"
    )

    print(
        mapped.groupby(
            "direction"
        ).size().to_string()
    )

    print(
        "\n매핑 방식:"
    )

    print(
        mapped.groupby(
            [
                "direction",
                "mapping_method",
            ]
        ).size().to_string()
    )

    # --------------------------------------------------
    # 이상치 확인
    # --------------------------------------------------
    suspicious = mapped[
        mapped[
            "office_to_conzone_start_km"
        ].abs() > 3.0
    ]

    if len(suspicious):
        print(
            "\n[확인 필요] 영업소와 선택된 "
            "conzone 시작점이 3 km 이상 떨어진 항목:"
        )

        print(
            suspicious[
                [
                    "office_code",
                    "office_name",
                    "direction",
                    "office_offset_km",
                    "conzone_name",
                    "conzone_start_km",
                    "conzone_end_km",
                    "mapping_method",
                    "office_to_conzone_start_km",
                ]
            ].to_string(index=False)
        )

    print(
        "\n=== 매핑표 ==="
    )

    print(
        mapped[
            [
                "office_name",
                "direction",
                "office_offset_km",
                "conzone_name",
                "conzone_start_km",
                "conzone_end_km",
                "mapping_method",
                "office_to_conzone_start_km",
            ]
        ].to_string(index=False)
    )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    mapped.to_parquet(
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