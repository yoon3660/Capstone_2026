"""포털 원본(ex_vds_*) → 시간대별 교통량·속도 정리본 (T-07 / T-08).

정리본 컬럼
    traffic_gyeongbu.parquet
        period, date, hour, direction, conzone_id, conzone_name,
        offset_km, offset_km_start, offset_km_end, position_source,
        volume_veh, missing_code, source
    speed_gyeongbu.parquet
        (위와 같고 volume_veh 대신 speed_kmh)

hour 는 포털의 preTime 이다. "08" = 08:00~08:59 에 콘존 단면을 지난 대수(속도는 평균).
결측(검지기 이상)은 NaN 으로 남기고, 포털 결측 코드(교통량 -1/-2, 속도 0)를
missing_code 에 둔다. 포털이 콘존 데이터를 아예 주지 않으면(검지기 없는 짧은 구간)
missing_code = NO_DATA_CODE 로 채운다. 행을 빼면 결측률이 실제보다 낮게 보고된다.
보간 여부는 쓰는 쪽에서 정한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from evdt.io import ex_portal
from evdt.io.conzone_position import locate_conzones
from evdt.io.route import GyeongbuRoute

SOURCE = {
    "volume": "ex_portal_vds_volume",
    "speed": "ex_portal_vds_speed",
}
VALUE_COLUMN = {"volume": "volume_veh", "speed": "speed_kmh"}

#: 포털 응답에 그 콘존 데이터가 하나도 없음 (포털 결측 코드와 겹치지 않는 값)
NO_DATA_CODE = -9.0


def read_manifest(raw_dir: Path) -> dict:
    return json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))


def latest_collections(raw_root: Path) -> dict[str, Path]:
    """label 별로 가장 최근 수집본 폴더. 같은 기간을 다시 받았으면 새 것을 쓴다."""

    latest: dict[str, Path] = {}

    for d in sorted(raw_root.glob("ex_vds_*")):
        if not (d / "manifest.json").exists():
            continue   # 수집이 중간에 끊긴 폴더
        latest[read_manifest(d)["label"]] = d

    return latest


def conzones_from_raw(raw_dir: Path) -> list[ex_portal.Conzone]:
    conzones = []

    for direction in ("DOWN", "UP"):
        raw = json.loads((raw_dir / f"conzones_{direction}.json").read_text(encoding="utf-8"))
        code = {v: k for k, v in ex_portal.PORTAL_DIRECTION.items()}[direction]
        for seq, item in enumerate(raw["result"]):
            if not item["value"].startswith(f"{ex_portal.GYEONGBU_LINE_NO}CZ{code}"):
                raise ex_portal.PortalError(f"방향이 다른 콘존: {item['value']}")
            conzones.append(ex_portal.Conzone(item["value"], item["text"], direction, seq))

    return conzones


def conzone_positions(
    conzones: list[ex_portal.Conzone],
    route: GyeongbuRoute,
    ics: list[dict],
) -> pd.DataFrame:
    rows = []
    for direction in ("DOWN", "UP"):
        rows += locate_conzones([c for c in conzones if c.direction == direction], route, ics)
    return pd.DataFrame(rows)


def read_dataset(raw_dir: Path, dataset: str) -> pd.DataFrame:
    """한 수집본의 dataset 폴더를 (conzone_id, date, hour, value) 로 펼친다."""

    frames = []

    for path in sorted((raw_dir / dataset).glob("*.json")):
        conzone_id = path.name.split("_", 1)[0]
        raw = json.loads(path.read_text(encoding="utf-8"))

        if dataset == "volume_daily":
            rows = ex_portal.parse_daily(raw)
        else:
            rows = ex_portal.parse_hourly(dataset, raw)

        frame = pd.DataFrame(rows)
        if "missing_code" in frame:
            frame["missing_code"] = frame["missing_code"].astype("float64")
        frame["conzone_id"] = conzone_id
        frames.append(frame)

    if not frames:
        raise FileNotFoundError(f"{raw_dir / dataset} 에 원본이 없습니다")

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df


def check_daily_totals(hourly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """시간대 합계와 포털 일 합계가 다른 (conzone_id, date) 를 돌려준다.

    24시간이 모두 있는 날만 비교한다. 결측이 있는 날은 합계가 원래 다르다.
    """

    grouped = hourly.groupby(["conzone_id", "date"])["value"]
    complete = grouped.count() == 24
    hourly_sum = grouped.sum()[complete].rename("hourly_sum")

    merged = (
        hourly_sum.reset_index()
        .merge(daily.rename(columns={"value": "daily"}), on=["conzone_id", "date"], how="inner")
    )

    return merged[(merged["hourly_sum"] - merged["daily"]).abs() > 0.5]


def tidy(
    hourly: pd.DataFrame,
    positions: pd.DataFrame,
    dataset: str,
    period: str,
    dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    """코리도 안 콘존 × dates × 24시간 전체 격자에 값을 붙인 정리본.

    포털이 데이터를 주지 않은 (콘존, 날짜, 시간) 도 행으로 남긴다.
    """

    pos = positions[positions["in_corridor"]].rename(columns={"name": "conzone_name"})
    grid = pd.MultiIndex.from_product(
        [pos["conzone_id"], dates, range(24)], names=["conzone_id", "date", "hour"]
    ).to_frame(index=False)

    df = grid.merge(hourly, on=["conzone_id", "date", "hour"], how="left")
    absent = df["value"].isna() & df["missing_code"].isna()
    df.loc[absent, "missing_code"] = NO_DATA_CODE
    df = df.merge(pos, on="conzone_id", how="inner")

    df = df.rename(columns={"value": VALUE_COLUMN[dataset]})
    df["period"] = period
    df["source"] = SOURCE[dataset]

    columns = [
        "period", "date", "hour", "direction", "conzone_id", "conzone_name",
        "offset_km", "offset_km_start", "offset_km_end", "position_source",
        VALUE_COLUMN[dataset], "missing_code", "source",
    ]
    return df[columns].sort_values(["period", "direction", "offset_km", "date", "hour"])


def missing_summary(df: pd.DataFrame, value_column: str) -> pd.DataFrame:
    """기간·방향별 결측 시간 수와 비율."""

    g = df.groupby(["period", "direction"])[value_column]
    out = pd.DataFrame({"n": g.size(), "missing": g.apply(lambda s: s.isna().sum())})
    out["missing_pct"] = (100 * out["missing"] / out["n"]).round(2)
    return out.reset_index()
