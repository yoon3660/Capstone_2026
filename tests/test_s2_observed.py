"""관측 가능한 것만으로 하는 예측 (#119).

두 테스트가 이 파일의 근거다.

    1. strength 0 이면 S1 과 **비트 단위로** 같다
    2. 목적지·SoC·계획을 뒤섞어도 예측이 **한 글자도 안 바뀐다**

2번이 *"시뮬레이터의 미래를 안 본다"* 를 **장식이 아니라 검사로** 만든다. 주석으로
"안 봅니다" 라고 적는 것은 아무것도 보장하지 않는다 — 실제로 안 보는지는 **보여줄 값을
바꿔 보고 결과가 같은지**로만 확인된다.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from evdt.engine.forecast import (
    ObservedSpec,
    observed_from_entries,
)
from evdt.engine.reservation import ReservationLedger
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.engine.s2 import S2Policy, S2Settings
from evdt.engine.ue_demand import TripDemand
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
BATTERY = 60.0
CONSUMPTION = 0.2
AMOUNT = ChargeAmount(buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8)
KW = {"buffer_km": 30.0, "reserve_soc": 0.2, "target_soc_cap": 0.8, "max_stops": 1}
OFFSETS = {"A": 100.0, "B": 150.0}


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


STATIONS = [_spec("A", 200.0), _spec("B", 150.0)]
CHARGERS = {s.station_id: len(s.chargers) for s in STATIONS}


def _trip(ev_id: str, entry_min: float, entry_km: float = 0.0, **over) -> TripDemand:
    base = dict(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=entry_km,
        dest_offset_km=300.0, battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT,
        cold_factor=1.0, plans=(), soc0=0.65, consumption_kwh_km=CONSUMPTION,
    )
    base.update(over)
    return TripDemand(**base)


def _ev(ev_id: str, entry_min: float, soc0: float = 0.65) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=0.0,
        dest_offset_km=300.0, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
    )


def _ledger() -> ReservationLedger:
    return ReservationLedger.build({s.station_id: s.chargers for s in STATIONS})


def _forecast(trips, strength: float = 1.0):
    return observed_from_entries(
        trips, OFFSETS, CHARGERS, speed_kmh=SPEED,
        soc_from=0.2, soc_to=0.8, consumption_kwh_km=CONSUMPTION,
        entry_soc_mean=0.65, spec=ObservedSpec(strength=strength))


def _run(policy, evs, *, ledger=None, forecast=None):
    return run_corridor(
        STATIONS, OFFSETS, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
        ledger=ledger, forecast=forecast,
    )


# ---------------------------------------------------------------------------
# ★ 1 — 예측을 안 쓰면 S1 이다
# ---------------------------------------------------------------------------
def test_strength_zero_is_bit_identical_to_s1():
    """**예측이 빗나가도 정책이 무너지지 않는다**의 극단 (#119 완료조건).

    strength 0 은 "예측을 전혀 믿지 않는다" 다. 그러면 S1 과 같은 질문을 같은 원장에
    해야 한다 — 한 글자라도 다르면 배선이 섞인 것이다.
    """

    evs = [_ev(f"e{i}", float(i)) for i in range(5)]
    trips = [_trip(f"e{i}", float(i)) for i in range(5)]

    s1 = _run(S1Policy(S1Settings(**KW)), evs, ledger=_ledger())
    s2 = _run(S2Policy(S2Settings(**KW)), evs, ledger=_ledger(),
              forecast=_forecast(trips, strength=0.0))

    assert s2.charge_events == s1.charge_events
    assert s2.escape_events == s1.escape_events


# ---------------------------------------------------------------------------
# ★ 2 — 시뮬레이터의 미래를 안 본다
# ---------------------------------------------------------------------------
def test_observed_forecast_never_sees_the_future():
    """**이 테스트가 #118 과 #119 를 가른다.**

    목적지·출발 SoC·계획을 통째로 뒤섞고, 배터리·최대출력을 차들 사이에서 **섞는다**
    (평균은 보존 — 집계값은 관측 가능하다고 본 것이니까).

    예측이 **완전히 같아야** 한다. 하나라도 달라지면, 아직 아무도 물어보지 않은 차의
    정보를 읽고 있다는 뜻이다.
    """

    rng = random.Random(0)
    trips = [_trip(f"e{i:03d}", float(i) * 3.0, entry_km=float(i % 4) * 10.0,
                   battery_kwh=50.0 + i % 5 * 10, vmax_kw=100.0 + i % 3 * 50)
             for i in range(60)]

    batteries = [t.battery_kwh for t in trips]
    vmaxes = [t.vmax_kw for t in trips]
    rng.shuffle(batteries)
    rng.shuffle(vmaxes)

    scrambled = [
        dataclasses.replace(
            t,
            # ↓ 예측이 봐서는 안 되는 것들 — 아직 아무도 안 물어봤다
            dest_offset_km=rng.uniform(50.0, 400.0),
            soc0=rng.uniform(0.1, 0.9),
            plans=(),
            # ↓ 개별 차 단위로 섞는다 (평균은 그대로)
            battery_kwh=b, vmax_kw=v,
        )
        for t, b, v in zip(trips, batteries, vmaxes, strict=True)
    ]

    assert _forecast(trips) == _forecast(scrambled), (
        "목적지·SoC·계획을 바꿨는데 예측이 달라졌다 — 관측 불가능한 정보를 읽고 있다")


def test_forecast_changes_when_entries_change():
    """위 테스트가 '아무것도 안 읽어서' 통과하는 것을 막는다.

    **진입 시각은 관측 가능하고, 예측의 유일한 재료다.** 그걸 바꾸면 반드시 달라져야
    한다.
    """

    a = _forecast([_trip(f"e{i}", float(i) * 3.0) for i in range(30)])
    b = _forecast([_trip(f"e{i}", float(i) * 3.0 + 500.0) for i in range(30)])
    assert a != b, "진입 시각을 500분 늦췄는데 예측이 같다 — 진입을 안 읽고 있다"


def test_window_only_counts_cars_that_already_entered():
    """**이 예측이 현실에서 구현 가능한 이유** (#119).

    내가 `t` 에 offset `y` 에서 offset `x` 의 휴게소를 보면 도착은 `t + (x−y)/v` 다.
    그 창에 `x` 를 지나갈 차는 진입 시각이

        t − (x−e)/v  <  진입  <  t − (y−e)/v

    인데, 나보다 상류에서 진입했으면(`e ≤ y`) 오른쪽 경계가 **t 이하**다. 즉
    **미래 진입량을 추정할 필요가 없다** — VDS 가 지금까지 센 값만으로 된다.

    여기서는 그걸 숫자로 확인한다: 창 안에 잡힌 통과 시각을 진입 시각으로 되돌려서
    **전부 지금(t) 이전**인지 본다.
    """

    trips = [_trip(f"e{i}", float(i) * 2.0) for i in range(200)]   # 전부 offset 0 진입
    fc = _forecast(trips)

    x, y, t = OFFSETS["A"], 40.0, 300.0
    eta = t + (x - y) / SPEED * 60.0

    counted = [p for p in fc.pass_times["A"] if t < p < eta]
    assert counted, "전제: 이 창에 통과 차가 있어야 검사가 의미 있다"

    for p in counted:
        entered_at = p - x / SPEED * 60.0        # e = 0 이므로
        assert entered_at <= t + 1e-9, (
            f"통과 {p:.1f}분은 진입 {entered_at:.1f}분에서 왔는데 지금은 {t}분이다 — "
            "아직 진입하지도 않은 차를 세고 있다")


# ---------------------------------------------------------------------------
# 가상 도착이 원장을 오염시키지 않는다
# ---------------------------------------------------------------------------
def test_synthetic_ids_cannot_collide_with_real_cars():
    """겹치면 원장이 진짜 예약으로 오인해 중복 제거에 걸린다 — 조용히 과소예측된다."""

    fc = _forecast([_trip(f"e{i}", float(i)) for i in range(100)])
    got = fc.arrivals_between("A", after_min=0.0, before_min=400.0)
    assert got, "전제: 예측이 뭔가 돌려줘야 한다"
    assert all(a.ev_id.startswith("~fc:") for a in got)


def test_forecast_is_spread_across_the_window_not_at_the_pipe_times():
    """**어느 차가 설지는 모른다.** 통과 시각을 그대로 쓰면 그걸 아는 셈이 된다."""

    fc = _forecast([_trip(f"e{i}", float(i)) for i in range(100)])
    got = fc.arrivals_between("A", after_min=100.0, before_min=200.0)
    assert len(got) >= 2
    assert all(100.0 < a.arrival_min < 200.0 for a in got)
    # 고르게 펴져 있다 — 간격이 전부 같다
    gaps = [b.arrival_min - a.arrival_min for a, b in zip(got, got[1:], strict=False)]
    assert max(gaps) - min(gaps) < 1e-9


# ---------------------------------------------------------------------------
# 손잡이가 의도대로 움직인다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("strength", [0.0, 0.5, 1.0, 2.0])
def test_strength_scales_the_number_of_forecast_arrivals(strength):
    """과대예측은 엉뚱한 곳으로 보내고, 과소예측은 S1 으로 돌아간다 (#119).

    둘을 가르려면 손잡이가 **단조**여야 한다.
    """

    trips = [_trip(f"e{i}", float(i)) for i in range(300)]
    n = len(_forecast(trips, strength).arrivals_between("A", after_min=0.0, before_min=400.0))
    base = len(_forecast(trips, 1.0).arrivals_between("A", after_min=0.0, before_min=400.0))
    assert n == pytest.approx(base * strength, abs=1.0)


def test_predicted_hourly_uses_the_same_model_as_the_policy_sees():
    """예측 오차 KPI 가 **정책이 실제로 본 예측**을 재야 한다.

    두 경로(`arrivals_between` · `predicted_hourly`)가 갈라지면, 보고하는 오차가
    엔진이 겪은 오차가 아니게 된다.
    """

    trips = [_trip(f"e{i}", float(i) * 2.0) for i in range(300)]
    fc = _forecast(trips)

    for h in (2, 5, 8):
        via_hourly = fc.predicted_hourly().get(("A", h), 0.0)
        via_policy = len(fc.arrivals_between("A", after_min=h * 60.0,
                                             before_min=(h + 1) * 60.0))
        assert via_policy == pytest.approx(via_hourly, abs=1.0)


# ---------------------------------------------------------------------------
# 예측이 실제로 선택을 바꾼다
# ---------------------------------------------------------------------------
def test_pipe_full_of_cars_moves_the_choice():
    """상류에 차가 가득하면 **빈 원장에도 불구하고** 다른 곳을 고른다.

    S1 은 원장이 비어 있으면 아무것도 못 본다 — 이게 관측 예측이 더하는 것이다.
    """

    me = [_ev("me", 0.0)]
    s1 = _run(S1Policy(S1Settings(**KW)), me, ledger=_ledger())
    assert [e["station_id"] for e in s1.charge_events] == ["A"], "전제: 빈 원장이면 A"

    # me 보다 앞서 진입해 A 를 먼저 지나갈 차를 잔뜩 둔다
    crowd = [_trip(f"x{i:03d}", -60.0 + i * 0.4) for i in range(400)]
    s2 = _run(S2Policy(S2Settings(**KW)), me, ledger=_ledger(),
              forecast=_forecast(crowd, strength=1.0))

    assert [e["station_id"] for e in s2.charge_events] == ["B"], (
        "상류 파이프가 가득한데도 A 를 골랐다 — 예측이 비용에 안 들어갔다")
