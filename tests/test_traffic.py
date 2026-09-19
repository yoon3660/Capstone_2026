"""T-07 / T-08 교통량·속도 수집·정리 규칙. 네트워크 없이 돈다."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from evdt.eval.speed_validation import compare_speeds
from evdt.io import ex_portal, flow_params, traffic
from evdt.io.conzone_position import locate_conzones, normalize_endpoint
from evdt.io.route import GyeongbuRoute

# ---------------------------------------------------------------------------
# 포털 응답 모양 (실제 응답에서 필요한 열만 남긴 것)
# ---------------------------------------------------------------------------


def _hourly_raw(dataset, days, value_fn, *, wrap=True):
    rows = []
    for hour in range(24):
        row = {"preTime": f"{hour:02d}"}
        for i, day in enumerate(days):
            row[f"day_{i}"] = day
            v = value_fn(day, hour)
            row[f"day_Cnt{i}"] = None if v is None else str(v)
        row[f"day_{len(days)}"] = None       # 포털은 안 쓰는 칸도 null 로 채운다
        rows.append(row)
    if dataset == "volume":
        return {"result": {"excelData": rows, "trafficVdsList": []}}
    return {"result": [], "excelData": rows}


def _daily_raw(totals):
    return {"result": {"excelData": [{"preDay": d, "trafficCnt": str(v)} for d, v in totals.items()]}}


def test_parse_hourly_keeps_missing_hours():
    raw = _hourly_raw("volume", ["20260217"], lambda d, h: None if h == 3 else 100 + h)
    rows = ex_portal.parse_hourly("volume", raw)

    assert len(rows) == 24
    assert [r["value"] for r in rows if r["hour"] == 3] == [None]
    assert sum(r["value"] for r in rows if r["value"] is not None) == sum(
        100 + h for h in range(24) if h != 3
    )


@pytest.mark.parametrize(
    ("dataset", "bad"),
    [("volume", -5), ("volume", 25_000), ("speed", -3), ("speed", 201)],
)
def test_parse_hourly_rejects_nonsense(dataset, bad):
    raw = _hourly_raw(dataset, ["20260217"], lambda d, h: bad if h == 8 else 90)
    with pytest.raises(ex_portal.PortalError, match="비상식"):
        ex_portal.parse_hourly(dataset, raw)


@pytest.mark.parametrize(("dataset", "code"), [("volume", -1), ("volume", -2), ("speed", 0)])
def test_portal_missing_codes_become_missing(dataset, code):
    raw = _hourly_raw(dataset, ["20260217"], lambda d, h: code if h == 5 else 90)
    rows = {r["hour"]: r for r in ex_portal.parse_hourly(dataset, raw)}

    assert rows[5]["value"] is None
    assert rows[5]["missing_code"] == code
    assert rows[6]["missing_code"] is None


def test_negative_daily_total_is_missing():
    rows = ex_portal.parse_daily(_daily_raw({"20260217": -24, "20260218": 1000}))
    assert [r["value"] for r in rows] == [None, 1000.0]


def test_zero_volume_is_kept_not_rejected():
    raw = _hourly_raw("volume", ["20260217"], lambda d, h: 0 if h == 4 else 50)
    rows = ex_portal.parse_hourly("volume", raw)
    assert [r["value"] for r in rows if r["hour"] == 4] == [0.0]


def test_date_windows_respect_portal_limit():
    windows = ex_portal.date_windows(date(2026, 2, 1), date(2026, 3, 17))
    assert windows[0][0] == date(2026, 2, 1)
    assert windows[-1][1] == date(2026, 3, 17)
    assert all((b - a).days + 1 <= ex_portal.MAX_WINDOW_DAYS for a, b in windows)
    assert all((w2[0] - w1[1]).days == 1 for w1, w2 in zip(windows, windows[1:], strict=False))


def _hourly_frame(values_by_day):
    rows = [
        {"conzone_id": "C1", "date": pd.Timestamp(d), "hour": h, "value": v}
        for d, values in values_by_day.items()
        for h, v in enumerate(values)
    ]
    return pd.DataFrame(rows)


def test_hourly_sum_equals_daily_total():
    hourly = _hourly_frame({"2026-02-17": [100.0] * 24, "2026-02-18": [50.0] * 24})
    daily = pd.DataFrame(
        {"conzone_id": ["C1", "C1"], "date": pd.to_datetime(["2026-02-17", "2026-02-18"]),
         "value": [2400.0, 1200.0]}
    )
    assert traffic.check_daily_totals(hourly, daily).empty

    daily.loc[1, "value"] = 1300.0
    bad = traffic.check_daily_totals(hourly, daily)
    assert list(bad["date"]) == [pd.Timestamp("2026-02-18")]


def test_day_with_missing_hour_is_not_compared():
    hourly = _hourly_frame({"2026-02-17": [100.0] * 23 + [None]})
    daily = pd.DataFrame({"conzone_id": ["C1"], "date": [pd.Timestamp("2026-02-17")], "value": [9.0]})
    assert traffic.check_daily_totals(hourly, daily).empty


# ---------------------------------------------------------------------------
# 노선 좌표계와 콘존 위치
# ---------------------------------------------------------------------------


def _straight_route():
    # 남(구서, UP 기점) → 북(양재, DOWN 기점) 으로 곧게 뻗은 가상 노선 (위도 1도 ≈ 111km)
    points = tuple((35.0 + 0.1 * i, 128.0) for i in range(11))
    mileposts = tuple(11.1195 * i for i in range(11))
    return GyeongbuRoute(points=points, mileposts=mileposts)


def test_offset_of_and_directions():
    route = _straight_route()
    lat = 35.5
    up = route.offset_of(lat, 128.0, "UP")
    down = route.offset_of(lat, 128.0, "DOWN")

    assert up == pytest.approx(55.6, abs=0.2)
    assert up + down == pytest.approx(route.length_km)

    with pytest.raises(ValueError, match="노선에서"):
        route.offset_of(35.5, 128.5, "UP")    # 동쪽으로 약 45km


def test_route_save_load_roundtrip(tmp_path):
    route = _straight_route()
    path = route.save(tmp_path / "route.json", note="test")
    assert GyeongbuRoute.load(path) == route


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("판교IC", "판교JC", False),
        ("판교JC", "판교JCT", True),
        ("서영천Hi", "서영천IC", True),
        ("북구미하이패스IC", "북구미IC", True),
        ("옥산IC", "옥산하이패스IC", True),
        ("통도사Hi", "통도사IC", False),
        ("구서나들목", "구서IC", True),
    ],
)
def test_endpoint_names(a, b, same):
    assert (normalize_endpoint(a) == normalize_endpoint(b)) is same


def _ic(name, lat):
    return {"icName": name, "yValue": str(lat), "xValue": "128.0"}


def test_locate_conzones_interpolates_and_resolves_duplicates():
    route = _straight_route()
    names = ["남쪽밖IC", "A IC", "B JC", "C IC", "D IC"]
    conzones = [
        ex_portal.Conzone(f"0010CZE{i:03d}", f"{names[i]}-{names[i + 1]}", "UP", i)
        for i in range(len(names) - 1)
    ]
    ics = [
        _ic("AIC", 35.1),
        # B JC 는 좌표가 없다 → A 와 C 사이 보간
        _ic("CIC", 35.5),
        _ic("CIC", 35.9),       # 같은 이름 중복. 이웃(D=35.7)에 가까운 쪽이 아니라
        _ic("DIC", 35.7),       #   체인 순서를 지키는 쪽이 남아야 한다
    ]
    rows = {r["name"]: r for r in locate_conzones(conzones, route, ics)}

    assert rows["남쪽밖IC-A IC"]["in_corridor"] is False
    b = rows["A IC-B JC"]
    assert b["position_source"] == "interpolated"
    assert b["offset_km_end"] == pytest.approx((11.12 + 55.6) / 2, abs=0.3)

    starts = [rows[n]["offset_km_start"] for n in ("A IC-B JC", "B JC-C IC", "C IC-D IC")]
    assert starts == sorted(starts)
    assert rows["C IC-D IC"]["offset_km_end"] > rows["C IC-D IC"]["offset_km_start"]


def test_broken_conzone_chain_stops():
    route = _straight_route()
    conzones = [
        ex_portal.Conzone("0010CZE001", "AIC-BIC", "UP", 0),
        ex_portal.Conzone("0010CZE002", "CIC-DIC", "UP", 1),
    ]
    with pytest.raises(RuntimeError, match="체인"):
        locate_conzones(conzones, route, [])


# ---------------------------------------------------------------------------
# 원본 폴더 → 정리본 (축소 데이터 종단 테스트)
# ---------------------------------------------------------------------------


def test_raw_collection_to_tidy(tmp_path):
    raw_dir = tmp_path / "ex_vds_test_20260217_20260218_000000"
    for d in ("volume", "speed", "volume_daily"):
        (raw_dir / d).mkdir(parents=True)

    conzones = {
        "DOWN": {"result": [{"value": "0010CZS100", "text": "DIC-AIC"}]},
        "UP": {"result": [{"value": "0010CZE100", "text": "AIC-DIC"}]},
    }
    for direction, raw in conzones.items():
        (raw_dir / f"conzones_{direction}.json").write_text(json.dumps(raw), encoding="utf-8")

    days = ["20260217", "20260218"]
    for cz in ("0010CZS100", "0010CZE100"):
        vol = _hourly_raw("volume", days, lambda d, h: 1000 + h)
        spd = _hourly_raw("speed", days, lambda d, h: 40 if h == 17 else 95)
        (raw_dir / "volume" / f"{cz}_20260217_20260218.json").write_text(json.dumps(vol))
        (raw_dir / "speed" / f"{cz}_20260217_20260218.json").write_text(json.dumps(spd))
        total = sum(1000 + h for h in range(24))
        (raw_dir / "volume_daily" / f"{cz}_20260217_20260218.json").write_text(
            json.dumps(_daily_raw({d: total for d in days}))
        )
    (raw_dir / "manifest.json").write_text(
        json.dumps({"label": "test", "datasets": ["volume", "speed", "volume_daily"]})
    )

    assert traffic.latest_collections(tmp_path) == {"test": raw_dir}

    route = _straight_route()
    ics = [_ic("AIC", 35.2), _ic("DIC", 35.8)]
    positions = traffic.conzone_positions(traffic.conzones_from_raw(raw_dir), route, ics)

    volume = traffic.read_dataset(raw_dir, "volume")
    assert traffic.check_daily_totals(volume, traffic.read_dataset(raw_dir, "volume_daily")).empty

    dates = pd.date_range("2026-02-17", "2026-02-19")   # 2/19 는 포털 응답에 없는 날
    tidy = traffic.tidy(volume, positions, "volume", "test", dates)
    assert len(tidy) == 2 * 3 * 24
    absent = tidy[tidy["date"] == pd.Timestamp("2026-02-19")]
    assert absent["volume_veh"].isna().all()
    assert (absent["missing_code"] == traffic.NO_DATA_CODE).all()
    tidy = tidy[tidy["date"] < pd.Timestamp("2026-02-19")]
    assert set(tidy["direction"]) == {"UP", "DOWN"}
    up = tidy[tidy["direction"] == "UP"]["offset_km"].iloc[0]
    down = tidy[tidy["direction"] == "DOWN"]["offset_km"].iloc[0]
    assert up + down == pytest.approx(route.length_km, abs=0.01)

    speed = traffic.tidy(
        traffic.read_dataset(raw_dir, "speed"), positions, "speed", "test", dates[:2]
    )
    params = flow_params.estimate_by_conzone(speed, tidy)
    # 2일 × 새벽 3시간 = 6표본 < 최소 12표본 → v_free 를 내지 않는다
    assert params["n_free_flow"].tolist() == [6, 6]
    assert params["v_free_kmh"].isna().all()
    assert params["q_p95_veh_h"].notna().all()


# ---------------------------------------------------------------------------
# T-08 파라미터와 검증 지표
# ---------------------------------------------------------------------------


def test_flow_params_use_quantiles_not_max():
    rows = []
    for day in range(10):
        for hour in range(24):
            rows.append(
                {"period": "p", "direction": "UP", "conzone_id": "C1", "conzone_name": "x",
                 "offset_km": 1.0, "date": day, "hour": hour,
                 "speed_kmh": 100.0 + day if hour in (3, 4, 5) else 60.0,
                 "volume_veh": 9000.0 if (day, hour) == (0, 8) else 1000.0 + hour}
            )
    df = pd.DataFrame(rows)
    est = flow_params.estimate_by_conzone(df, df).iloc[0]

    assert est["v_free_kmh"] == pytest.approx(pd.Series(range(100, 110)).repeat(3).quantile(0.85))
    assert est["q_observed_max_veh_h"] == 9000.0
    assert est["q_p95_veh_h"] < 2000.0          # 이상치 한 시간이 용량을 정하지 않는다


def test_breakdown_flow_is_the_hour_before_congestion():
    speeds = [95, 90, 85, 40, 30, 80, 95, 95]
    volumes = [2000, 3000, 5000, 4000, 3500, 4500, 3000, 2000]
    rows = []
    for day in range(2):
        for hour, (sp, vol) in enumerate(zip(speeds, volumes, strict=True)):
            rows.append(
                {"period": "p", "direction": "DOWN", "conzone_id": "C1", "conzone_name": "x",
                 "offset_km": 1.0, "date": day, "hour": hour,
                 "speed_kmh": float(sp), "volume_veh": float(vol + day * 100)}
            )
    df = pd.DataFrame(rows)

    b = flow_params.breakdown_flow(df, df, congestion_kmh=50).iloc[0]

    # 2시(85km/h, 다음 시간 40km/h)의 교통량. 이틀치 5000, 5100 의 중앙값
    assert b["n_breakdown"] == 2
    assert b["q_breakdown_veh_h"] == pytest.approx(5050.0)


def test_breakdown_flow_skips_gaps_in_hours():
    df = pd.DataFrame(
        {"period": "p", "direction": "DOWN", "conzone_id": "C1", "conzone_name": "x",
         "offset_km": 1.0, "date": 0, "hour": [1, 3], "speed_kmh": [90.0, 30.0],
         "volume_veh": [5000.0, 3000.0]}
    )
    # 1시 다음 기록이 3시다 (2시 결측) → 정체 직전으로 보지 않는다
    assert flow_params.breakdown_flow(df, df, congestion_kmh=50).empty


def test_derived_w_back_satisfies_triangular_fd():
    v, q, k = 100.0, 2000.0, 144.0
    w = flow_params.derived_w_back(v, q, k)
    assert q == pytest.approx(v * w * k / (v + w))

    with pytest.raises(ValueError, match="삼각형"):
        flow_params.derived_w_back(100.0, 20_000.0, 144.0)


def test_speed_comparison_confusion_and_onset():
    obs = pd.DataFrame(
        {"conzone_id": "C1", "date": "2026-02-17", "hour": range(6),
         "speed_kmh": [95, 90, 40, 30, 80, 95]}
    )
    sim = obs.assign(speed_kmh=[95, 95, 95, 35, 45, 95])

    result = compare_speeds(obs, sim, congestion_kmh=50)

    assert result.confusion == {"tp": 1, "fp": 1, "fn": 1, "tn": 3}
    assert result.recall == pytest.approx(0.5)
    assert result.onset_lag_h["lag_h"].tolist() == [1]
    assert result.mae_kmh == pytest.approx((0 + 5 + 55 + 5 + 35 + 0) / 6)
