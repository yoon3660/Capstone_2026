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

def _driving_world():
    """A 로 가고 있는 차 한 대. **B 가 더 좋아서 옮길 이유가 있다.**

    지금 100분, 100km 지점. A 는 160km(50kW 1대) 라서 160분에 닿고, B 는
    200km(200kW 2대) 다. 그래서 *"옮기면 이득, 그런데 값을 치러야 한다"* 를
    한 대로 잴 수 있다.

    `decide()` 를 직접 부르는 이유: 재배정은 **정보가 바뀔 때** 일어나는데
    장난감 세계에는 바뀔 정보가 없어서 시뮬레이션으로는 이 분기를 못 밟는다.
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

    ledger.book("EV001", "A", Arrival(
        ev_id="EV001", arrival_min=160.0, soc_from=0.35, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, charge_power_factor=1.0))

    return ev, world, ledger


def test_a_car_inside_the_hard_lock_is_pinned_not_vetoed():
    """★ 도착이 임박한 차는 **계획에서 고정된다** — 사후에 되돌리지 않는다.

    #142 가 사후 거부로 구현했을 때 **재배정 0회인데 S3 보다 431h 나빴다.**
    계획이 "이 차가 비켜 준다" 를 전제로 남을 옮기고, 그 차만 되돌렸기 때문이다.

    그래서 두 가지를 같이 본다.

        답이 σ₀ 인가              — 안 옮겼나
        **원장이 그대로인가**     — 계산에서 사라지지 않았나

    둘째가 핵심이다. 고정된 차를 원장에서도 빼면 남은 차들이 *"거기가 한산하다"*
    고 잘못 보고, 그 위에 세운 계획이 또 틀린 전제를 갖는다.
    """

    ev, world, ledger = _driving_world()
    before = ledger.reservation("EV001")

    # 60분 남았는데 120분을 요구한다 → 고정
    pinned = S4Policy(S4Settings(**KW, window=4, hard_lock_min=120.0))
    assert pinned.decide([ev], world)["EV001"] == "A", "고정한 차가 옮겨졌다"
    assert pinned.counters["locked"] == 1.0
    assert pinned.counters["reassignable"] == 0.0
    assert ledger.reservation("EV001") == before, "고정한 차의 예약이 바뀌었다"


def test_the_reassignment_cost_decides_whether_a_car_moves():
    """★ $r_i$ 가 **목적함수 안에** 있다 — 비싸지면 안 옮긴다.

    같은 세계·같은 차인데 `reassign_base` 만 바꾼다. 옮기는 이득은 고정이므로
    **값이 이득보다 커지는 순간 안 옮겨야** 한다. 이것이 사후 거부와 다른 점이다:
    거부는 "옮긴 뒤 되돌리기" 이고, 비용은 "옮길지 말지를 계획에서 정하기" 다.
    """

    def where(reassign_base: float) -> str:
        ev, world, _ = _driving_world()
        policy = S4Policy(S4Settings(
            **KW, window=4, hard_lock_min=0.1, reassign_base=reassign_base))
        return policy.decide([ev], world)["EV001"]

    assert where(0.0) == "B", "공짜인데 안 옮겼다 — 옮길 이유가 있는 세계가 아니다"
    assert where(5.0) == "B", "5분은 이득보다 작다"
    assert where(1.0e6) == "A", "값이 이득보다 큰데 옮겼다 — r_i 가 안 걸렸다"


def test_the_greedy_pass_also_pays_the_reassignment_cost():
    """$r_i$ 가 **윈도우에 혼자 있는 차에게도** 걸려야 한다.

    `S3Policy.decide` 는 2대 미만이면 그룹 최적화를 아예 건너뛴다. 그래서 r_i 를
    그룹 채점기에만 넣으면 **혼자인 차는 공짜로 옮겨 다닌다** — 목적함수에 항이
    있는데 절반의 차에게만 걸리는 상태다. 실제로 처음 구현이 그랬다.

    위 테스트가 차 **한 대**로 돌기 때문에 이미 이 경로를 밟는다. 여기서는
    *"그리디가 보는 자리에 훅이 있다"* 를 코드로 고정한다 — 지워지면 위 테스트가
    왜 깨지는지 찾는 데 오래 걸린다.
    """

    import inspect

    from evdt.engine.s1 import S1Policy

    assert "_extra_cost" in inspect.getsource(S1Policy._one), (
        "그리디 argmin 에서 `_extra_cost` 가 사라졌다 — 혼자인 차가 공짜로 옮겨진다")


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
    s4 = _run(S4Policy(S4Settings(**KW, window=10, hard_lock_min=0.5)),
              stations, offsets, evs, ledger=_led(stations), forecast=fc)

    assert s4.n_reasked > 0, "가고 있는 차를 묻기는 해야 한다"
    assert s4.n_relocated == 0, "바뀔 정보가 없는데 옮겼다"
    assert [e["station_id"] for e in s4.charge_events] ==            [e["station_id"] for e in s3.charge_events]


# -- ③ 하지 않는 것 -----------------------------------------------------------

def test_a_driving_car_is_never_sent_backwards():
    """지나친 휴게소로는 못 돌린다 — 시뮬레이터가 그 자리에서 터진다."""

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(12)]

    out = _run(S4Policy(S4Settings(**KW, window=12, hard_lock_min=0.5)),
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


def test_locking_everything_reproduces_s3():
    """★ `hard_lock_min = inf` 면 **S3 와 같은 답**이 나와야 한다.

    `docs/objective.md` §2b 가 요구한 검산이다 — *"$r_i = M$ 을 모든 가던 차에
    주면 그 차들은 못 움직이고, 그러면 S3 와 같은 답이 나와야 한다."*

    **사후 거부 구현은 이 검산을 통과하지 못했다.** 리드타임을 100,000분으로 줘서
    재배정을 0회로 만들었는데도 S3 보다 431h 나빴다 (`engine/s4.py` 모듈 문서).
    계획을 세운 뒤에 되돌렸기 때문이다. 지금은 **계획에서 고정**하므로 통과한다.

    ## 이 세계에서는 "안 움직인다" 쪽도 같다

    장난감 세계에는 Δt 마다 바뀔 정보가 없어서 고정을 풀어도 재배정이 0 이다
    (`test_relocation_needs_changing_information`). 그래서 이 테스트는
    **배선 검산**이다 — 고정 경로가 실제로 돌았는지를 `locked` 로 확인한다.
    """

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(10)]
    fc = OracleForecast({}, {})

    s3 = _run(S3Policy(S3Settings(**KW, window=10)), stations, offsets, evs,
              ledger=_led(stations), forecast=fc)
    locked = S4Policy(S4Settings(**KW, window=10, hard_lock_min=float("inf")))
    s4 = _run(locked, stations, offsets, evs, ledger=_led(stations), forecast=fc)

    assert locked.counters["locked"] > 0, "고정 경로를 한 번도 안 밟았다"
    assert locked.counters["reassignable"] == 0.0, "전부 고정인데 옮길 수 있는 차가 있다"
    assert s4.n_relocated == 0
    assert ([e["station_id"] for e in s4.charge_events]
            == [e["station_id"] for e in s3.charge_events]), "S3 를 재현하지 못했다"
    assert (sum(e["wait_min"] for e in s4.charge_events)
            == sum(e["wait_min"] for e in s3.charge_events))


def test_an_en_route_car_is_never_allowed_to_bail_out():
    """★ 가던 차가 이탈을 답하면 **원래 예약으로 되돌린다.**

    시뮬레이터는 가던 차의 이탈 답을 무시한다 (의도된 설계 — "중간 포기" 는
    시간축 결합이 아니다). 그런데 `S1Policy._one` 은 이탈을 고를 때 **원장에서
    예약을 뺀다.** 둘이 겹치면

        정책: 예약을 뺐다        세계: 차는 원래 휴게소로 계속 간다
        ⇒ 뒤 차들은 그 자리가 비었다고 보는데 실제로는 그 차가 거기 선다

    이탈 비용을 1분으로 두면 어느 휴게소든 그 위라서 반드시 이탈을 고른다.
    """

    ev, world, ledger = _driving_world()
    before = ledger.reservation("EV001")

    policy = S4Policy(S4Settings(
        **KW, window=4, hard_lock_min=0.1, escape_cost_min=1.0))
    answer = policy.decide([ev], world)["EV001"]

    assert answer == "A", f"가던 차가 이탈 답을 냈다: {answer!r}"
    assert policy.counters["enroute_opt_out_blocked"] == 1.0
    assert ledger.reservation("EV001") == before, (
        "예약이 사라졌다 — 예약 없는 차가 도착한다")


def test_the_objective_values_live_in_one_place():
    """목적함수의 값이 **`Objective` 하나에서** 나와야 한다 (§7 의 민감도 손잡이).

    흩어 두면 민감도를 돌릴 때 무엇을 움직였는지 알 수 없다.
    """

    from evdt.engine.objective import Objective

    settings = S4Settings(**KW, hard_lock_min=20.0, reassign_base=3.0,
                          wait_weight=1.25)
    assert settings.objective == Objective(
        time_value=1.0, wait_weight=1.25, reassign_base=3.0, hard_lock_min=20.0)


def test_s4_settings_refuse_a_terminal_penalty():
    """terminal penalty 는 #144 다 — 넣은 척하면 안 된다."""

    with pytest.raises(NotImplementedError, match="#144"):
        S4Settings(**KW, terminal_weight=0.5)


# -- ⑤ 러너 통로 (#143) ------------------------------------------------------

def test_engine_opts_are_written_down_as_one_sorted_line():
    """엔진 설정이 `params_json` 에 남아야 한다 — 손잡이가 늘어도 통로는 하나다."""

    from evdt.runner import run_params

    cfg = _cfg()
    spec = run_params(cfg, engine_opts={"solver": "milp", "reassign_base": 5.0})
    assert spec["engine_opts"] == "reassign_base=5.0,solver=milp", spec["engine_opts"]
    assert run_params(cfg)["engine_opts"] is None
    assert "engine_opts" in run_params(cfg)


def test_an_unknown_engine_opt_stops_instead_of_being_ignored():
    """철자가 틀리면 **그 자리에서** 멈춘다.

    조용히 무시하면 `params_json` 에는 적히고 효과는 없다 — 나중에 *"손잡이를
    돌렸는데 결과가 같다"* 를 설명할 수 없다. 이 프로젝트에 다섯 번 있었다.
    """

    import pytest as _pytest

    from evdt.engine.s4 import S4Settings
    from evdt.runner import engine_settings

    assert engine_settings({"solver": "milp", "window": "6"}, S4Settings) == {
        "solver": "milp", "window": 6}
    with _pytest.raises(ValueError, match="없는 엔진 설정"):
        engine_settings({"solvr": "milp"}, S4Settings)


def test_a_run_scored_by_the_blind_scorer_is_not_reused():
    """★ 옛 S3·S4 run 은 **재사용하지 않는다** (#143).

    `group_scorer` 가 없는 run 은 윈도우 안 상호작용을 한 번도 안 세는 채점기로
    돌았다. 다른 칸들은 "기록에 없으면 안 버린다" 가 맞지만 (나중에 추가된 키라서),
    여기서는 **옛 run 이 틀린 값**이다 — 조용히 쓰면 틀린 기준선 위에 사다리를
    쌓는다 (#55 와 같은 고장).
    """

    from evdt.runner import load_config, run_params, stale_reason

    cfg = _cfg()
    fresh = run_params(cfg)
    assert stale_reason(fresh, cfg) is None, "방금 만든 기록이 안 맞는다"

    old = {k: v for k, v in fresh.items() if k != "group_scorer"}
    why = stale_reason(old, cfg)
    assert why is not None and "group_scorer" in why, f"옛 채점기를 못 잡았다: {why}"

    # S0~S2 는 그룹 채점기를 안 쓰므로 **버리지 않는다**
    s2 = load_config("config/scenario_seollal_up_adoption.yaml", stage="S2")
    assert stale_reason({k: v for k, v in run_params(s2).items()
                         if k != "group_scorer"}, s2) is None


def test_a_car_about_to_arrive_is_still_treated_as_en_route():
    """★ **정책의 ETA 가 이미 지나 보여도** 가던 차는 가던 차다.

    ## 이것을 놓쳐서 고정이 새어 나갔다

    처음에는 *"예약의 ETA 가 미래면 가던 차"* 로 분류했다. 실제 시나리오에서
    **`hard_lock_min = inf` 인데 재배정이 524회** 나왔다 — 전부 고정이면 0 이어야
    한다. 원인은 두 ETA 가 다르다는 것이다.

        시뮬레이터:  eta = car.t_min + 통행시간    ← 충전이 끝난 **그 시각**
        정책:        eta = world.t_min + 통행시간  ← **묻는 시각**

    `car.t_min` 은 Δt 버킷 안에서 `t` 보다 최대 Δt 뒤일 수 있다. 그래서 **도착까지
    Δt 미만 남은 차**는 정책 눈에 이미 도착한 것으로 보이고, "새로 묻는 차" 로
    분류돼 **$r_i$ 없이 공짜로 옮겨진다.** 하필 **절대 옮겨서는 안 되는 차들**이다.

    그래서 시각이 아니라 **위치**로 본다. 여기서는 예약 ETA 를 **과거**(t − 20분)로
    두고도 고정되는지 확인한다 — 휴게소 A 는 여전히 차 앞에 있다.
    """

    from evdt.engine.ledger import Arrival

    ev, world, ledger = _driving_world()
    # 예약 ETA 를 **과거**로 바꿔 끼운다 (지금 100분, ETA 80분). 휴게소 A 는
    # 160km 로 차(100km) 앞에 그대로 있다
    ledger.release("EV001")
    ledger.book("EV001", "A", Arrival(
        ev_id="EV001", arrival_min=80.0, soc_from=0.35, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, charge_power_factor=1.0))

    locked = S4Policy(S4Settings(**KW, window=4, hard_lock_min=float("inf")))
    assert locked.decide([ev], world)["EV001"] == "A", "ETA 가 지났다고 옮겨 버렸다"
    assert locked.counters["locked"] == 1.0, (
        "ETA 로 분류하고 있다 — 도착 임박한 차가 고정에서 새어 나간다")


def test_a_car_standing_at_its_reserved_station_is_not_en_route():
    """반대쪽 — **서 있는 자리**가 예약인 차는 가던 차가 아니다.

    시뮬레이터가 `mark()` 를 안 불러서 한 번 선 차에게도 묵은 예약이 남는다. 그걸
    "가던 길" 로 오해하면 **이미 지나친 휴게소로 되돌리는 답**이 나오고, 그러면
    시뮬레이터가 *"앞에 있지 않은 휴게소를 골랐다"* 로 멈춘다.
    """

    from evdt.engine.ledger import Arrival

    ev, world, ledger = _driving_world()
    ledger.release("EV001")
    # 차는 100km 에 있고, 예약은 **거기 있는** 휴게소다 (A 를 100km 로 본다)
    import dataclasses

    from evdt.interfaces import WorldView
    at_here = {"A": dataclasses.replace(world.stations["A"], offset_km=100.0),
               "B": world.stations["B"]}
    here = WorldView(t_min=100.0, stations=at_here, temp_c=20.0, cold_factor=1.0,
                     range_factor=1.0, ledger=ledger, forecast=world.forecast)
    ledger.book("EV001", "A", Arrival(
        ev_id="EV001", arrival_min=100.0, soc_from=0.35, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, charge_power_factor=1.0))

    policy = S4Policy(S4Settings(**KW, window=4, hard_lock_min=float("inf")))
    policy.decide([ev], here)
    assert policy.counters["locked"] == 0.0, (
        "서 있는 자리를 '가던 길' 로 봤다 — 뒤쪽 휴게소로 되돌리는 답이 나온다")
