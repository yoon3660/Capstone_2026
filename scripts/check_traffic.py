"""T-07 교통량 정리본 검증과 설날 vs 평상시 곡선.

    python scripts/check_traffic.py --holiday seollal2026 --base base202603 \\
        --outbound 2026-02-14 2026-02-16 --return 2026-02-17 2026-02-18

검사 (어긋나면 종료 코드 1)
    1. 귀성일은 하행이, 귀경일은 상행이 커야 한다. 반대면 방향 매핑이 뒤집혔다.
    2. 명절 피크일 교통량이 같은 요일 평상시보다 확연히 커야 한다 (HOLIDAY_MIN_RATIO).
    3. 시간대 곡선: 하루 최저는 새벽(0~6시), 최고는 낮(7~21시)에 있어야 한다.
보고
    - 일교통량 비율표 (같은 요일끼리), 결측 시간 비율
    - docs/figures/t07_hourly_volume.png  설날 vs 평상시 시간대 곡선

"구간 평균 교통량" 은 코리도 안 콘존들의 단면 교통량 평균이다 (대/h).
두 기간 모두 결측이 MAX_MISSING_SHARE 이하인 콘존만 쓴다. 기간마다 검지기가
빠진 콘존이 달라서(설 8개, 3월 17개), 전부 쓰면 서로 다른 콘존 집합을 비교하게 된다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from evdt.io.traffic import missing_summary  # noqa: E402
from evdt.paths import DATA_PROCESSED_DIR, PROJECT_ROOT  # noqa: E402

HOLIDAY_MIN_RATIO = 1.10
MAX_MISSING_SHARE = 0.10
FIGURE = PROJECT_ROOT / "docs" / "figures" / "t07_hourly_volume.png"


def corridor_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """(period, direction, date, hour) 별 구간 평균 교통량."""

    return (
        df.groupby(["period", "direction", "date", "hour"])["volume_veh"]
        .mean()
        .reset_index()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--holiday", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--outbound", nargs=2, type=date.fromisoformat, required=True, help="귀성 기간")
    ap.add_argument("--return", dest="inbound", nargs=2, type=date.fromisoformat, required=True, help="귀경 기간")
    args = ap.parse_args()

    df = pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet")
    df = df[df["period"].isin([args.holiday, args.base])]

    share = df["volume_veh"].isna().groupby([df["period"], df["conzone_id"]]).mean().unstack(0)
    keep = share[(share <= MAX_MISSING_SHARE).all(axis=1)].index
    print(
        f"비교 콘존: {len(keep)} / {df['conzone_id'].nunique()} "
        f"(두 기간 모두 결측 {MAX_MISSING_SHARE:.0%} 이하)"
    )
    report_df = df
    df = df[df["conzone_id"].isin(keep)]
    hourly = corridor_hourly(df)
    daily = hourly.groupby(["period", "direction", "date"])["volume_veh"].sum().reset_index()
    daily["weekday"] = daily["date"].dt.day_name().str[:3]

    failures = []

    # --- 일교통량 비율 (같은 요일끼리) -----------------------------------------
    hol = daily[daily["period"] == args.holiday]
    base = daily[daily["period"] == args.base]
    base_by_wd = base.groupby(["direction", "weekday"])["volume_veh"].mean()

    table = hol.pivot(index=["date", "weekday"], columns="direction", values="volume_veh")
    for direction in ("DOWN", "UP"):
        table[f"{direction}_ratio"] = [
            v / base_by_wd[(direction, wd)] for (_, wd), v in table[direction].items()
        ]
    print("\n=== 설날 일교통량 (구간 평균, 대/일) / 같은 요일 평상시 ===")
    print(table.round(2).to_string())

    def days(a: date, b: date) -> pd.DataFrame:
        idx = table.index.get_level_values("date")
        return table[(idx >= pd.Timestamp(a)) & (idx <= pd.Timestamp(b))]

    out_days, in_days = days(*args.outbound), days(*args.inbound)

    # 1. 방향
    if not (out_days["DOWN"] > out_days["UP"]).all():
        failures.append("귀성 기간에 하행이 상행보다 작은 날이 있다 → 방향 매핑 확인")
    if not (in_days["UP"] > in_days["DOWN"]).all():
        failures.append("귀경 기간에 상행이 하행보다 작은 날이 있다 → 방향 매핑 확인")

    # 2. 명절 피크 > 평상시
    peak_out = out_days["DOWN_ratio"].max()
    peak_in = in_days["UP_ratio"].max()
    print(f"\n귀성 피크 하행 비율 {peak_out:.2f}, 귀경 피크 상행 비율 {peak_in:.2f}")
    if peak_out < HOLIDAY_MIN_RATIO or peak_in < HOLIDAY_MIN_RATIO:
        failures.append(f"명절 피크가 평상시의 {HOLIDAY_MIN_RATIO}배에 못 미친다 → 기간·방향 확인")

    # 3. 시간대 곡선 모양 (피크일)
    out_peak_day = out_days["DOWN_ratio"].idxmax()[0]
    in_peak_day = in_days["UP_ratio"].idxmax()[0]
    for direction, day in (("DOWN", out_peak_day), ("UP", in_peak_day)):
        curve = hourly[(hourly["direction"] == direction) & (hourly["date"] == day)]
        curve = curve.set_index("hour")["volume_veh"]
        low, high = int(curve.idxmin()), int(curve.idxmax())
        print(f"{direction} {day:%m/%d}: 최저 {low}시 {curve.min():,.0f} / 최고 {high}시 {curve.max():,.0f} 대/h")
        if not (0 <= low <= 6 and 7 <= high <= 21):
            failures.append(f"{direction} {day:%m/%d} 곡선 모양이 이상하다 (최저 {low}시, 최고 {high}시)")

    # --- 결측 ------------------------------------------------------------------
    print("\n=== 결측 시간 ===")
    print(missing_summary(report_df, "volume_veh").to_string(index=False))
    zeros = int((report_df["volume_veh"] == 0).sum())
    print(f"교통량 0 인 시간: {zeros}")

    # --- 그림 ------------------------------------------------------------------
    for family in ("Malgun Gothic", "AppleGothic", "NanumGothic"):
        if any(family in f.name for f in matplotlib.font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = family
            break
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    for ax, direction, day, title in (
        (axes[0], "DOWN", out_peak_day, "하행 (귀성)"),
        (axes[1], "UP", in_peak_day, "상행 (귀경)"),
    ):
        wd = day.day_name()
        base_curve = (
            hourly[(hourly["period"] == args.base) & (hourly["direction"] == direction)]
            .assign(wd=lambda d: d["date"].dt.day_name())
            .query("wd == @wd")
            .groupby("hour")["volume_veh"].mean()
        )
        hol_curve = hourly[(hourly["direction"] == direction) & (hourly["date"] == day)]
        ax.plot(base_curve.index, base_curve.values, color="#8a8a8a", lw=2,
                label=f"평상시 {wd[:3]} 평균 ({args.base})")
        ax.plot(hol_curve["hour"], hol_curve["volume_veh"], color="#c0392b", lw=2.5,
                label=f"설날 {day:%m/%d} ({wd[:3]})")
        ax.set_title(f"경부선 {title}")
        ax.set_xlabel("시각")
        ax.set_xticks(range(0, 24, 3))
        ax.grid(alpha=0.3)
        ax.legend(frameon=False)
    axes[0].set_ylabel("구간 평균 교통량 (대/h)")
    fig.tight_layout()
    FIGURE.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(FIGURE, dpi=150)
    print(f"\n그림: {FIGURE}")

    if failures:
        print("\n[FAIL]")
        for f in failures:
            print(" -", f)
        return 1

    print("\n[OK] 방향·명절 비율·곡선 모양 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
