import argparse
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402

from evdt.world.geometry import haversine_km  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()

    # 1. 원본 CSV 읽기
    df = pd.read_csv(
        args.csv_path,
        encoding="cp949",
        dtype=str,
    )

    # 2. 경부선만 추출
    gyeongbu = df[
        df["노선번호"].str.strip().isin(["10", "0010"])
    ].copy()

    if gyeongbu.empty:
        raise ValueError("경부선 데이터를 찾지 못했습니다.")

    # 3. 필요한 컬럼을 숫자로 변환
    gyeongbu["official_offset_km"] = pd.to_numeric(
        gyeongbu["이정"], errors="raise"
    )
    gyeongbu["lat"] = pd.to_numeric(
        gyeongbu["X좌표값"], errors="raise"
    )
    gyeongbu["lon"] = pd.to_numeric(
        gyeongbu["Y좌표값"], errors="raise"
    )

    gyeongbu = gyeongbu.sort_values(
        "official_offset_km"
    ).reset_index(drop=True)

    # 4. 좌표 검증
    if not gyeongbu["lat"].between(33, 39).all():
        raise ValueError("위도 범위가 이상합니다.")

    if not gyeongbu["lon"].between(124, 132).all():
        raise ValueError("경도 범위가 이상합니다.")

    # 5. 이정 중복 및 간격 검증
    offsets = gyeongbu["official_offset_km"]

    if offsets.duplicated().any():
        raise ValueError("중복된 이정이 있습니다.")

    gaps = offsets.diff().round(3)

    abnormal = [
        (
            float(offsets.iloc[i - 1]),
            float(offsets.iloc[i]),
            float(gaps.iloc[i]),
        )
        for i in range(1, len(gyeongbu))
        if gaps.iloc[i] != 0.1
    ]

    print("경부선 행 수:", len(gyeongbu))
    print("원본 이정 범위:", offsets.min(), "~", offsets.max())
    print("비정상 이정 간격:", abnormal)

    # 기존에 발견한 문제와 일치하는 경우에만 보정
    if abnormal != [(241.1, 241.7, 0.6)]:
        raise ValueError(
            "예상했던 원본 이정 상태와 다릅니다. "
            "이미 보정된 파일이거나 다른 이상 구간이 있는지 확인하세요."
        )

    # 6. 원본 이정은 보존하고, 작업용 거리만 보정
    gyeongbu["offset_km"] = gyeongbu["official_offset_km"]

    mask = gyeongbu["official_offset_km"] >= 241.7
    gyeongbu.loc[mask, "offset_km"] -= 0.5

    gyeongbu["offset_km"] = gyeongbu["offset_km"].round(3)

    # 7. 보정 후 검증
    corrected_gaps = gyeongbu["offset_km"].diff().dropna()

    if not corrected_gaps.round(3).eq(0.1).all():
        raise ValueError("보정 후에도 이정 간격이 일정하지 않습니다.")


    # 8. 인접 이정 좌표의 거리 품질 검사
    segment_rows = []

    for i in range(1, len(gyeongbu)):
        a = gyeongbu.iloc[i - 1]
        b = gyeongbu.iloc[i]

        distance_m = haversine_km(
            a["lat"], a["lon"],
            b["lat"], b["lon"],
        ) * 1000

        expected_m = (
            b["offset_km"] - a["offset_km"]
        ) * 1000

        segment_rows.append({
            "start_km": a["offset_km"],
            "end_km": b["offset_km"],
            "distance_m": distance_m,
            "deviation_m": distance_m - expected_m,
        })

    segments = pd.DataFrame(segment_rows)

    # 기준: 100m 대비 편차가 ±50m를 초과
    outliers = segments[
        segments["deviation_m"].abs() > 50
    ]

    print("\n=== 중심선 좌표 품질 검사 ===")
    print("전체 인접 구간:", len(segments))
    print("±50m 초과 구간:", len(outliers))

    if not outliers.empty:
        print(outliers.to_string(index=False))

    # 추가 점검용 임시 기준: 직선거리가 120m를 넘는 구간
    long_segments = segments[
        segments["distance_m"] > 120
    ]

    print("\n직선거리 120m 초과 구간:", len(long_segments))

    if not long_segments.empty:
        print(long_segments.to_string(index=False))

    # 이상 구간을 발견해도 좌표는 임의로 변경하지 않는다.


    # 9. 처리된 파일 저장
    result = gyeongbu[
        ["offset_km", "lat", "lon", "official_offset_km"]
    ]

    output = Path("data/processed/centerline_gyeongbu.parquet")
    output.parent.mkdir(parents=True, exist_ok=True)

    result.to_parquet(output, index=False)

    print("보정 후 거리 범위:",
          result["offset_km"].min(),
          "~",
          result["offset_km"].max())
    print("저장 완료:", output)


if __name__ == "__main__":
    main()