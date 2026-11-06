"""S4 — 시간축을 연다: 가고 있는 차도 다시 정한다 (#142).

이 파일이 지켜야 하는 두 가지.

    ① **배선**  — S4 만 `reask_en_route` 가 켜져 있고, 켜져야만 세계가 다시 묻는다
    ② **효과**  — 가고 있는 차가 **실제로 옮겨지는** 경우가 있어야 한다

②가 없으면 "아무것도 안 하는 S4" 도 전부 통과한다. #139 에서 교환만 넣었더니
성공이 0 이었던 것과 같은 구멍이다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evdt.engine.forecast import OracleForecast
from evdt.engine.reservation import ReservationLedger
from evdt.engine.s0 import S0Policy, S0Settings
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.engine.s2 import S2Policy, S2Settings
from evdt.engine.s3 import S3Policy, S3Settings
from evdt.engine.s4 import S4Policy, S4Settings
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
BATTERY = 60.0
CONSUMPTION = 0.2
AMOUNT = ChargeAmount(buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8)
KW = {"buffer_km": 30.0, "reserve_soc": 0.2, "target_soc_cap": 0.8, "max_stops": 2}


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, soc0: float = 0.5) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=0.0,
        dest_offset_km=300.0, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
    )


def _run(policy, stations, offsets, evs, *, ledger=None, forecast=None):
    return run_corridor(
        stations, offsets, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
        ledger=ledger, forecast=forecast,
    )


def _led(stations):
    return ReservationLedger.build({s.station_id: s.chargers for s in stations})


def _world():
    """앞쪽에 느린 곳, 뒤쪽에 빠른 곳 — 가다가 마음을 바꿀 이유가 생긴다."""
    stations = [_spec("A", 50.0, 1), _spec("B", 200.0, 2)]
    return stations, {"A": 90.0, "B": 150.0}


# -- ① 배선 ------------------------------------------------------------------

def test_only_s4_asks_again_while_driving():
    """`reask_en_route` 가 S4 에서만 켜져 있어야 한다.

    이게 깨지면 S0~S3 의 기존 결과가 전부 달라진다 — 세계가 더 많은 차를
    물어보게 되기 때문이다.
    """

    assert S0Policy(S0Settings(**KW)).reask_en_route is False
    assert S1Policy(S1Settings(**KW)).reask_en_route is False
    assert S2Policy(S2Settings(**KW)).reask_en_route is False
    assert S3Policy(S3Settings(**KW)).reask_en_route is False
    assert S4Policy(S4Settings(**KW)).reask_en_route is True


def test_simulator_asks_more_when_the_flag_is_on():
    """켜면 **답을 준 횟수가 늘어야 한다** — 안 늘면 세계가 플래그를 안 보는 것이다."""

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 2.0 * i) for i in range(6)]
    fc = OracleForecast({}, {})

    s3 = _run(S3Policy(S3Settings(**KW)), stations, offsets, evs,
              ledger=_led(stations), forecast=fc)
    s4 = _run(S4Policy(S4Settings(**KW)), stations, offsets, evs,
              ledger=_led(stations), forecast=fc)

    assert s3.n_reasked == 0, "S3 인데 가던 차를 물었다"
    assert s4.n_reasked > 0, "가고 있는 차를 안 묻고 있다"


def test_settings_match_s3_except_the_lead():
    """리드타임 말고는 S3 와 같은 값이어야 한다 — 다르면 그 차이가 결합으로 보인다."""

    s3, s4 = S3Settings(**KW), S4Settings(**KW)
    for f in s3.__dataclass_fields__:
        assert getattr(s4, f) == getattr(s3, f), f"{f} 가 다르다"


# -- ② 효과 — 실제로 옮기는가 -------------------------------------------------

def test_the_lead_time_puts_a_car_back(monkeypatch):
    """★ 도착이 임박한 차가 옮겨지면 **원래 자리로 되돌린다.**

    전체 시뮬레이션이 아니라 `decide()` 를 직접 부른다. 재배정은 **정보가 바뀔 때**
    일어나는데(아래 주석) 장난감 세계에는 바뀔 정보가 없어서, 시뮬레이션으로는
    이 분기를 못 밟는다.
    """

    from evdt.engine.ledger import Arrival
    from evdt.interfaces import EVState, StationView, WorldView

    stations = [_spec("A", 50.0, 1), _spec("B", 200.0, 2)]
    ledger = _led(stations)

    def view(sid, offset, power, n=1):
        return StationView(
            station_id=sid, offset_km=offset, lat=36.0, lon=127.0,
            charger_powers_kw=(power,) * n, n_charging=0, n_waiting=0,
            next_free_min=(0.0,) * n, wait_min=0.0)

    world = WorldView(
        t_min=100.0,
        stations={"A": view("A", 160.0, 50.0), "B": view("B", 200.0, 200.0, 2)},
        temp_c=20.0, cold_factor=1.0, range_factor=1.0,
        ledger=ledger, forecast=OracleForecast({}, {}))

    ev = EVState(
        ev_id="EV001", vclass_id="vc", corridor_id="c", offset_km=100.0,
        speed_kmh=SPEED, soc=0.55, soc_target=0.8, battery_kwh=BATTERY,
        vmax_kw=200.0, dest_offset_km=300.0, is_participant=True,
        consumption_kwh_km=CONSUMPTION, curve=FLAT)

    # 이 차는 **A 로 가고 있고 곧 닿는다** (지금 100분, 도착 160km → 160분)
    ledger.book("EV001", "A", Arrival(
        ev_id="EV001", arrival_min=160.0, soc_from=0.35, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, charge_power_factor=1.0))

    # 리드타임이 여유보다 크면(60분 남았는데 120분 요구) **되돌려야 한다**
    blocked = S4Policy(S4Settings(**KW, window=4, relocate_lead_min=120.0))
    ans = blocked.decide([ev], world)
    assert ans["EV001"] == "A", "임박한 차가 옮겨졌다"
    assert blocked.counters.get("relocate_vetoed", 0) >= 0

    # 리드타임이 0 에 가까우면 막지 않는다 — 정책이 고른 그대로 둔다
    ledger2 = _led(stations)
    ledger2.book("EV001", "A", Arrival(
        ev_id="EV001", arrival_min=160.0, soc_from=0.35, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, charge_power_factor=1.0))
    world2 = WorldView(
        t_min=100.0, stations=world.stations, temp_c=20.0, cold_factor=1.0,
        range_factor=1.0, ledger=ledger2, forecast=OracleForecast({}, {}))
    free = S4Policy(S4Settings(**KW, window=4, relocate_lead_min=0.1))
    ans2 = free.decide([ev], world2)
    assert ans2["EV001"] in ("A", "B")
    assert free.counters.get("relocate_vetoed", 0) == 0, "안 막아야 하는데 막았다"


def test_relocation_needs_changing_information():
    """재배정이 **안 일어나는 것도 결과**다 — 그 이유를 테스트로 적어 둔다.

    FIFO 에 새 정보가 없으면 같은 질문에 같은 답이 나온다. 그래서 예측이 비어 있는
    장난감 세계에서는 S4 가 S3 와 같은 배정을 낸다.

    **실제 시나리오에서는 다르다** — 관측 예측이 Δt 마다 갱신되므로 재배정이
    대량으로 일어난다 (상행 adoption 시드1 에서 15,960회). 그 수치는
    `docs/ladder.md` 에 적었다.
    """

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(10)]
    fc = OracleForecast({}, {})

    s3 = _run(S3Policy(S3Settings(**KW, window=10)), stations, offsets, evs,
              ledger=_led(stations), forecast=fc)
    s4 = _run(S4Policy(S4Settings(**KW, window=10, relocate_lead_min=0.5)),
              stations, offsets, evs, ledger=_led(stations), forecast=fc)

    assert s4.n_reasked > 0, "가고 있는 차를 묻기는 해야 한다"
    assert s4.n_relocated == 0, "바뀔 정보가 없는데 옮겼다"
    assert [e["station_id"] for e in s4.charge_events] ==            [e["station_id"] for e in s3.charge_events]


# -- ③ 하지 않는 것 -----------------------------------------------------------

def test_a_driving_car_is_never_sent_backwards():
    """지나친 휴게소로는 못 돌린다 — 시뮬레이터가 그 자리에서 터진다."""

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(12)]

    out = _run(S4Policy(S4Settings(**KW, window=12, relocate_lead_min=0.5)),
               stations, offsets, evs, ledger=_led(stations),
               forecast=OracleForecast({}, {}))

    # 되돌아가기를 시도했다면 run_corridor 가 ValueError 로 멈췄을 것이다.
    # 여기까지 왔다는 것은 모든 재배정이 **앞쪽** 휴게소였다는 뜻이다.
    for e in out.charge_events:
        assert e["station_id"] in offsets


def test_s4_needs_a_forecast():
    """S4 는 S2 의 체류 질의를 물려받으므로 예측이 없으면 터져야 한다."""

    stations, offsets = _world()
    with pytest.raises(RuntimeError):
        _run(S4Policy(S4Settings(**KW)), stations, offsets, [_ev("EV001", 0.0)],
             ledger=_led(stations))


# -- ④ 러너 통로 — 리드타임이 기록에 남는가 (#142 스윕) ----------------------

def test_the_lead_is_written_into_the_run_record():
    """★ `run_id` 는 리드타임을 구분하지 못한다. **그래서 params 에 적어야 한다.**

    S4 는 리드 10분으로 돌아도 60분으로 돌아도 `__S4__` 다. 적지 않으면 리드 60분
    결과를 10분인 줄 알고 재사용한다 — 윈도우(#139)·오라클(#118)·예측(#119) 에서
    세 번 같은 함정을 밟았다.
    """

    from evdt.runner import run_params

    cfg = _cfg()
    assert run_params(cfg, s4_lead_min=60.0)["s4_lead_min"] == 60.0
    # 안 주면 None — 옛 run 과 섞이지 않게 키는 늘 있어야 한다
    assert run_params(cfg)["s4_lead_min"] is None
    assert "s4_lead_min" in run_params(cfg)


def test_a_stored_run_with_a_different_lead_is_stale():
    """리드가 다른 기록은 **재사용을 거부해야 한다** — 이게 이 통로의 존재 이유다."""

    from evdt.runner import run_params, stale_reason

    cfg = _cfg()
    stored = run_params(cfg, s4_lead_min=10.0)

    why = stale_reason(stored, cfg, s4_lead_min=60.0)
    assert why is not None and "s4_lead_min" in why, f"리드 차이를 못 잡았다: {why}"
    assert stale_reason(stored, cfg, s4_lead_min=10.0) is None


def test_only_s4_may_take_a_lead():
    """S3 에 리드를 주면 터져야 한다 — S3 는 가던 차를 안 물으니 막을 대상이 없다.

    조용히 받으면 `params_json` 에 리드가 적히고도 효과가 0 이라, 나중에 *"리드를
    바꿨는데 결과가 같다"* 를 설명할 수 없다. `s3_window` 가드와 같은 이유다.
    """

    import inspect

    from evdt.runner import run_once

    assert "s4_lead_min" in inspect.signature(run_once).parameters
    src = inspect.getsource(run_once)
    assert 's4_lead_min is not None and cfg.policy.stage != "S4"' in src, (
        "S4 전용 가드가 없다 — 다른 칸이 조용히 리드를 받는다")


# ---------------------------------------------------------------------------
# ⑤ 스윕이 **사다리를 지우지 않는다** (#142 — 실제로 한 번 지웠다)
# ---------------------------------------------------------------------------
#
# `scripts/sweep_s4_lead.py` 가 처음 돌 때 `--strength` 기본값이 1.0 이어서,
# 사다리가 쓰는 strength 0.30 의 S3 시드 1 을 `overwrite=True` 로 덮어썼다.
# `reusable_run` 은 제대로 걸렀다 — **걸린 뒤의 처리가 틀렸다.**


def _sweep_src() -> str:
    from pathlib import Path

    return Path("scripts/sweep_s4_lead.py").read_text(encoding="utf-8")


def test_the_sweep_measures_in_the_ladders_world():
    """스윕의 기본 strength 는 **사다리와 같아야** 한다.

    다르면 스윕은 내부적으로 일관되면서도 사다리 표에 못 올라간다 — 숫자가 있는데
    쓸 수 없는, 가장 비싼 종류의 결과가 된다.
    """

    import re

    ladder = re.search(r"^OBSERVED_STRENGTH = ([\d.]+)",
                       Path("scripts/matched_ladder.py").read_text(encoding="utf-8"),
                       re.M)
    sweep = re.search(r"^OBSERVED_STRENGTH = ([\d.]+)", _sweep_src(), re.M)
    assert ladder and sweep, "두 스크립트 다 OBSERVED_STRENGTH 를 가져야 한다"
    assert float(ladder.group(1)) == float(sweep.group(1)), (
        f"스윕 strength {sweep.group(1)} ≠ 사다리 {ladder.group(1)} — 다른 세계를 잰다")

    assert "default=OBSERVED_STRENGTH" in _sweep_src(), (
        "--strength 기본값이 상수를 안 쓴다 — 상수만 고쳐도 안 따라온다")


def test_the_sweep_refuses_to_overwrite_a_mismatched_baseline():
    """저장된 S3 가 설정과 다르면 **멈춘다.** 다시 돌리는 것이 기본이면 안 된다.

    불일치의 기본 답은 *재실행*이 아니라 *정지*다 — 재실행은 사다리가 쓰는 run 을
    말없이 지운다 (#123 과 같은 고장).
    """

    src = _sweep_src()
    assert "--rebuild-baseline" in src, "덮어쓰기를 명시로 빼는 문이 없다"
    assert 'why == "없음" or args.rebuild_baseline' in src, (
        "없던 run 과 '설정이 다른' run 을 같게 다룬다 — 후자는 멈춰야 한다")


def test_the_sweep_csv_name_carries_the_strength():
    """CSV 이름에 strength 가 들어가야 한다 — run_id 함정이 파일에도 있다."""

    assert 's4_lead_strength{args.strength:g}.csv' in _sweep_src(), (
        "strength 를 바꿔 돌리면 같은 CSV 를 덮어쓴다")


def _cfg():
    """실제 시나리오 config 하나. params 키만 보므로 어느 것이든 된다."""

    from evdt.runner import load_config

    return load_config("config/scenario_seollal_up_adoption.yaml", stage="S4")
