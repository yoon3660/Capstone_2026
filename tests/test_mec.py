"""MEC — 결정 기준만 사회적 비용으로 (**기준 축**, #138).

이 파일이 지켜야 하는 네 가지.

    ① **식**      손으로 계산한 값과 같나 (설계문서 §6.1)
    ② **동일성**  `mec_weight = 0` 이면 S1 과 **비트 단위로 같다**
    ③ **효과**   기준을 바꾸면 **실제로 다른 곳을 고르는** 경우가 있어야 한다
    ④ **격리**   이탈 판정과 그룹 채점기에 MEC 가 **새어 들어가지 않는다**

③이 없으면 "아무것도 안 하는 MEC" 도 전부 통과한다 — #139 에서 교환만 넣었더니
성공이 0 이었던 것과 같은 구멍이다.
"""

from __future__ import annotations

import pytest

from evdt.engine.forecast import OracleForecast
from evdt.engine.ledger import Arrival, Charger, StationLedger
from evdt.engine.mec import MecPolicy, MecSettings
from evdt.engine.reservation import ReservationLedger
from evdt.engine.s1 import S1Policy, S1Settings
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
BATTERY = 60.0
CONSUMPTION = 0.2
AMOUNT = ChargeAmount(buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8)
KW = {"buffer_km": 30.0, "reserve_soc": 0.2, "target_soc_cap": 0.8, "max_stops": 2}

#: 100kW · 60kWh · flat 곡선 → 충전시간(분) = (soc_to − soc_from) × 36
def charge_min(lo: float, hi: float) -> float:
    return (hi - lo) * BATTERY / 100.0 * 60.0


def arr(ev_id: str, t: float, lo: float = 0.3, hi: float = 0.8) -> Arrival:
    return Arrival(ev_id=ev_id, arrival_min=t, soc_from=lo, soc_to=hi,
                   battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT,
                   charge_power_factor=1.0)


# ---------------------------------------------------------------------------
# ① 식 — 손으로 계산한 값과 같나
# ---------------------------------------------------------------------------

def test_cutting_in_front_of_one_car_pushes_it_by_my_service_time():
    """★ 손으로 계산되는 가장 작은 경우.

    100kW 한 대. J 가 10분에 와서 혼자면 10~28 (충전 18분). 내가 0분에 끼면
    나는 0~18 을 쓰고 J 는 충전기가 비는 18분부터 시작해 18~36 이 된다.

        MEC = 36 − 28 = **8.0분**
    """

    led = StationLedger((Charger("c1", 100.0, 0.0),))
    led.commit(arr("J", 10.0))

    assert charge_min(0.3, 0.8) == 18.0
    total, per = led.externality_parts(arr("I", 0.0), exclude="I")
    assert total == pytest.approx(8.0)
    assert per == {"J": pytest.approx(8.0)}


def test_arriving_last_costs_nobody_anything():
    """뒤에 아무도 없으면 **0 이다.** §6.1 예제에서 휴게소 B 가 이 경우다."""

    led = StationLedger((Charger("c1", 100.0, 0.0),))
    led.commit(arr("J", 0.0))

    total, per = led.externality_parts(arr("I", 100.0), exclude="I")
    assert total == 0.0 and per == {}


def test_cars_ahead_of_me_contribute_exactly_zero():
    """내 **앞** 차는 합에 0 을 기여한다 — §6.1 식이 "다른 차량 j" 라고만 적은 이유.

    `assign` 은 도착순으로 처리하고 FIFO 라서, 나보다 먼저 온 차의 충전기 선택과
    종료 시각은 내가 있든 없든 같다. 그래서 **전원에 대해 합을 내도 된다.**
    """

    led = StationLedger((Charger("c1", 100.0, 0.0),))
    led.commit(arr("A", 0.0))        # 0~18. 내 앞이다
    led.commit(arr("Z", 50.0))       # 혼자면 50~68. 내가 20~38 을 써도 영향 없다

    total, per = led.externality_parts(arr("I", 20.0), exclude="I")
    assert per["Z"] == 0.0, "충전기가 내 뒤에 비는데 Z 를 밀었다고 셌다"
    assert total == 0.0
    assert "A" not in per or per["A"] == 0.0


def test_the_externality_is_not_spread_evenly():
    """★ 외부비용이 **고르게 안 퍼진다** (§6.1).

    *"뒤차 3대 중 1대가 떠안고 나머지는 적다."* 100kW 2기에 뒤차 3대를 두면
    한 대가 대부분을 떠안는다.

    **"앞차 수 × 평균 충전시간" 같은 근사로는 이 분포가 안 나온다** — 그 근사는
    모두에게 같은 값을 주기 때문이다. 이것이 MEC 를 큐 규칙으로 계산해야 하는
    이유이고, 이 테스트가 그걸 고정한다.
    """

    led = StationLedger((Charger("c1", 100.0, 85.0), Charger("c2", 100.0, 86.5)))
    for ev_id, t in (("f1", 86.0), ("f2", 92.0), ("f3", 95.0)):
        led.commit(arr(ev_id, t))

    total, per = led.externality_parts(arr("I", 60.0), exclude="I")
    loads = sorted(per.values(), reverse=True)

    assert total > 0.0
    assert len(loads) == 3
    assert loads[0] > 5.0 * loads[1], (
        f"분포가 고르다 — 근사로 대체할 수 있다는 뜻이 된다: {per}")
    assert total == pytest.approx(sum(loads))


def test_the_end_time_catches_a_slower_charger_not_just_the_wait():
    """★ **종료 시각**을 쓰는 이유 — 대기만 보면 못 잡는 몫이 있다 (§6.1).

    200kW 한 대와 50kW 한 대. J 는 내가 없으면 **200kW** 를 쓰고(큐 규칙 2 —
    비어 있는 것 중 최고출력), 내가 그 자리를 쥐고 있으면 **50kW 로 밀려난다.**

        내가 없을 때   J 는 fast: 10 → 19     (충전 9분, 대기 0)
        내가 있을 때   J 는 slow: 10 → 46     (충전 36분, **대기 0**)
        MEC = 46 − 19 = **27.0분**

    **대기시간은 양쪽 다 0 이다.** 대기만 보면 이 27분이 통째로 사라진다 —
    §6.1 이 종료 시각을 쓰라고 적은 이유가 이것이다.
    """

    from evdt.world.queue_rule import assign

    fleet = (Charger("fast", 200.0, 0.0), Charger("slow", 50.0, 0.0))
    # 나는 0 분에 와서 fast 를 14.4분 쥔다 (swing 0.8 × 60kWh ÷ 200kW)
    me = arr("I", 0.0, lo=0.0, hi=0.8)
    j_car = arr("J", 10.0)                       # swing 0.5

    (alone,) = assign([j_car], fleet)
    assert alone.charger_id == "fast"
    assert alone.wait_min == 0.0
    assert alone.end_min == pytest.approx(19.0)

    led = StationLedger(fleet)
    led.commit(j_car)
    total, per = led.externality_parts(me, exclude="I")

    assert per["J"] == pytest.approx(27.0), "충전기가 느려진 몫을 못 셌다"
    assert total == pytest.approx(27.0)

    # 대기는 **양쪽 다 0** 이다 — 그래서 대기만으로는 0 이 나온다
    (pushed,) = [a for a in assign([me, j_car], fleet) if a.ev_id == "J"]
    assert pushed.charger_id == "slow" and pushed.wait_min == 0.0


def test_the_policy_never_computes_the_queue_itself():
    """큐 규칙 호출자는 **여전히 둘**이다 — 정책이 직접 쌓으면 안 된다.

    `tests/test_queue_rule.py` 가 호출자 수를 센다. 여기서는 MEC 가 그 통로를
    **원장을 거쳐** 쓰는지만 본다.
    """

    import inspect

    from evdt.engine import mec

    src = inspect.getsource(mec)
    assert "ledger.externality(" in src, "원장을 거치지 않는다"
    # 문서에는 `queue_rule` 을 **언급**해도 된다. 금지는 **임포트와 호출**이다
    assert "import" not in src.split("class MecSettings")[0].split(
        "from evdt.world")[0] or "from evdt.world.queue_rule" not in src, (
        "정책이 큐 규칙을 직접 임포트한다")
    assert "assign(" not in src, "정책이 큐 규칙을 직접 부른다"


# ---------------------------------------------------------------------------
# ② 동일성 — 가중치 0 이면 S1 이다
# ---------------------------------------------------------------------------

def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]))


def _ev(ev_id: str, entry_min: float, soc0: float = 0.5) -> SimEV:
    return SimEV(ev_id=ev_id, vclass_id="vc", entry_min=entry_min,
                 entry_offset_km=0.0, dest_offset_km=300.0, soc0=soc0,
                 battery_kwh=BATTERY, consumption_kwh_km=CONSUMPTION,
                 vmax_kw=200.0, curve=FLAT)


def _world():
    """앞쪽에 느린 곳, 뒤쪽에 빠른 곳 — 사적 기준과 사회적 기준이 갈릴 여지."""
    return [_spec("A", 50.0, 1), _spec("B", 200.0, 2)], {"A": 90.0, "B": 150.0}


def _run(policy, stations, offsets, evs):
    return run_corridor(
        stations, offsets, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
        ledger=ReservationLedger.build(
            {s.station_id: s.chargers for s in stations}),
        forecast=None)


def test_zero_weight_is_bit_identical_to_s1():
    """★ `mec_weight = 0` 이면 **S1 과 비트 단위로 같다.**

    이게 깨지면 "MEC 가 S1 보다 좋다/나쁘다" 가 **기준 때문인지 배선 때문인지
    구분할 수 없다.** `test_s2.py` 의 첫 테스트(빈 예측의 S2 == S1)와 같은 역할이다.
    """

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(10)]

    s1 = _run(S1Policy(S1Settings(**KW)), stations, offsets, evs)
    off = _run(MecPolicy(MecSettings(**KW, mec_weight=0.0)), stations, offsets, evs)

    assert ([e["station_id"] for e in off.charge_events]
            == [e["station_id"] for e in s1.charge_events])
    assert (sum(e["wait_min"] for e in off.charge_events)
            == sum(e["wait_min"] for e in s1.charge_events))


def test_zero_weight_does_not_even_query_the_ledger():
    """가중치 0 이면 **질의도 하지 않는다** — 비싼 질의를 공짜로 돌리지 않는다."""

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(6)]

    policy = MecPolicy(MecSettings(**KW, mec_weight=0.0))
    _run(policy, stations, offsets, evs)
    assert policy.counters.get("mec_calls", 0.0) == 0.0


def test_mec_uses_no_forecast():
    """MEC 는 **S1 의 정보**를 쓴다 — 예측을 섞으면 축이 둘 바뀐다."""

    stations, offsets = _world()
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(6)]

    policy = MecPolicy(MecSettings(**KW))
    out = _run(policy, stations, offsets, evs)      # forecast=None 으로 돈다
    assert out.n_answers > 0

    import inspect
    # 호출 **코드**에 future 가 없어야 한다 (문서에서 언급하는 것은 괜찮다)
    code = [ln for ln in inspect.getsource(MecPolicy._mec).splitlines()
            if "ledger.externality(" in ln]
    assert code and all("future" not in ln for ln in code), (
        f"MEC 가 예측을 끼워 넣는다 — 정보 축이 같이 움직인다: {code}")


# ---------------------------------------------------------------------------
# ③ 효과 — 기준을 바꾸면 다른 곳을 고르나
# ---------------------------------------------------------------------------

def _two_station_world():
    """★ 설계문서 §6.1 의 검증 예제를 그대로 세운다.

        휴게소 A·B 각각 100kW 2기.
        A 는 **곧 충전 시작 2대 + 뒤에 확정 3대**,
        B 는 **2대 충전 중이나 뒤가 비어 있음**.
        신규 차량 i 는 A 에 60분 도착 SoC 30%, B 에 78분 도착 SoC 24%.

    i 를 offset 0 에 두고 A 를 60km, B 를 78km 에 두면 60km/h 에서 도착이 60분과
    78분이 되고, 0.2kWh/km · 60kWh 에서 도착 SoC 가 **30% 와 24%** 가 된다 —
    문서의 숫자가 그대로 나온다.

    A 의 두 충전기는 **85.0 분과 86.5 분에** 빈다 (먼저 온 2대가 쥐고 있다).
    B 의 첫 충전기는 **106.24 분에** 빈다.

    ⚠ 여기서 §6.1 의 사적 비용(43.0 · 48.4)을 그대로 기대하면 안 된다 — 목적지를
    한 번에 닿게 뒀으므로 충전량이 **목표 SoC 상한이 아니라 "닿을 만큼"** 이다.
    문서의 숫자는 `test_the_design_docs_private_costs_are_reproduced` 가 **원장
    수준에서** 확인한다. 이 세계는 **정책이 다른 곳을 고르는지**를 보는 자리다.
    """

    from evdt.interfaces import EVState, StationView, WorldView

    stations = [_spec("A", 100.0, 2), _spec("B", 100.0, 2)]
    ledger = ReservationLedger.build({s.station_id: s.chargers for s in stations})

    # A — 곧 충전 시작 2대. 충전기를 85.0 · 86.5 까지 쥔다 (28.8분 = swing 0.8)
    for ev_id, t in (("p1", 56.2), ("p2", 57.7)):
        ledger.book(ev_id, "A", arr(ev_id, t, lo=0.0, hi=0.8))
    # A — 뒤에 확정 3대. **내가 끼면 이들이 밀린다**
    for ev_id, t in (("f1", 86.0), ("f2", 92.0), ("f3", 95.0)):
        ledger.book(ev_id, "A", arr(ev_id, t))
    # B — 2대 충전 중이고 **뒤가 비어 있다.** 둘 다 i(78분)보다 먼저 왔다
    for ev_id, t in (("g1", 77.44), ("g2", 77.5)):
        ledger.book(ev_id, "B", arr(ev_id, t, lo=0.0, hi=0.8))

    def view(sid, offset):
        return StationView(station_id=sid, offset_km=offset, lat=36.0, lon=127.0,
                           charger_powers_kw=(100.0, 100.0), n_charging=0,
                           n_waiting=0, next_free_min=(0.0, 0.0), wait_min=0.0)

    world = WorldView(
        t_min=0.0, stations={"A": view("A", 60.0), "B": view("B", 78.0)},
        temp_c=20.0, cold_factor=1.0, range_factor=1.0,
        ledger=ledger, forecast=OracleForecast({}, source_run_id="t"))

    ev = EVState(ev_id="I", vclass_id="vc", corridor_id="c", offset_km=0.0,
                 speed_kmh=SPEED, soc=0.5, soc_target=0.8, battery_kwh=BATTERY,
                 vmax_kw=200.0, dest_offset_km=200.0, is_participant=True,
                 consumption_kwh_km=CONSUMPTION, curve=FLAT)
    return ev, world, ledger


def _design_doc_a() -> StationLedger:
    """§6.1 의 휴게소 A — 100kW 2기, **곧 충전 시작 2대 + 뒤에 확정 3대.**

    "곧 충전 시작 2대" 는 충전기가 **85.0 분과 86.5 분에 빈다**로 표현한다
    (`Charger.available_from_min` — 이미 물려 있는 차가 끝나는 시각).
    """

    led = StationLedger((Charger("a1", 100.0, 85.0), Charger("a2", 100.0, 86.5)))
    for ev_id, t in (("f1", 86.0), ("f2", 92.0), ("f3", 95.0)):
        led.commit(arr(ev_id, t))
    return led


def _design_doc_b() -> StationLedger:
    """§6.1 의 휴게소 B — 100kW 2기, **2대 충전 중이나 뒤가 비어 있음.**

    첫 충전기가 106.24 분에 빈다. 뒤에 확정된 차가 **하나도 없다** — 그래서
    내가 끼어도 밀 상대가 없고, MEC 가 0 이 된다.
    """

    return StationLedger((Charger("b1", 100.0, 106.24),
                          Charger("b2", 100.0, 110.0)))


def test_the_design_docs_private_costs_are_reproduced():
    """★ 설계문서 §6.1 의 **사적 비용**이 그대로 나온다 — A 43.0 · B 48.4.

    §6.1 의 사적 비용은 *"차량 자신의 체류시간 (대기 + 충전)"* 이다. 그래서
    원장에게 **직접** 묻는다 — 정책의 `_candidate` 비용은 목적지에 못 닿을 때
    **두 번째 정차**까지 더하므로 (`_next_stop_cost`) §6.1 의 PC 와 다른 양이다.

        A: 대기 25.00 + 충전 18.00  = **43.00**   (SoC 30% → 80%)
        B: 대기 28.24 + 충전 20.16  = **48.40**   (SoC 24% → 80%)

    충전시간은 100kW · 60kWh 에서 (0.8 − SoC) × 36 분이다.
    """

    me_a = arr("I", 60.0, lo=0.30, hi=0.80)
    dwell_a, wait_a = _design_doc_a().evaluate_parts(me_a, exclude="I")
    assert charge_min(0.30, 0.80) == pytest.approx(18.0)
    assert wait_a == pytest.approx(25.0)
    assert dwell_a == pytest.approx(43.0), f"A 의 사적 비용: {dwell_a}"

    me_b = arr("I", 78.0, lo=0.24, hi=0.80)
    dwell_b, wait_b = _design_doc_b().evaluate_parts(me_b, exclude="I")
    assert charge_min(0.24, 0.80) == pytest.approx(20.16)
    assert wait_b == pytest.approx(28.24)
    assert dwell_b == pytest.approx(48.4), f"B 의 사적 비용: {dwell_b}"

    # 문서의 결론 — **사적 기준은 A** (43.0 < 48.4)
    assert dwell_a < dwell_b


def test_the_design_docs_social_conclusion_is_reproduced():
    """★★ §6.1 의 **결론** — 사적 기준은 A, **사회적 기준은 B.**

        후보   사적    MEC    사회적
        A      43.00   > 0    > 43
        B      48.40   0.0    48.40

    A 는 뒤에 3대가 확정돼 있어 내가 끼면 밀고, B 는 뒤가 비어 있어 밀 상대가
    없다. 그래서 **기준을 바꾸면 선택이 뒤집힌다.**

    ## ⚠ MEC 값은 문서(22.0)와 다르다 — 뒤차들의 충전량이 §6.1 에 없다

    문서는 **신규 차량의** SoC 만 준다 (30% · 24%). A 뒤 확정 3대의 충전 요구는
    적혀 있지 않고 **그 값이 MEC 를 정한다.** 문서의 22.0분(19 + 1.5 + 1.5)을 만든
    `demo.py` 는 저장소에 없다. 그래서 여기서 고정하는 것은 **구조와 결론**이다 —
    A > 0, B = 0, 선택이 뒤집힌다, 분포가 고르지 않다.
    """

    me_a = arr("I", 60.0, lo=0.30, hi=0.80)
    me_b = arr("I", 78.0, lo=0.24, hi=0.80)

    led_a, led_b = _design_doc_a(), _design_doc_b()
    pc_a = led_a.evaluate(me_a, exclude="I")
    pc_b = led_b.evaluate(me_b, exclude="I")
    mec_a, per_a = led_a.externality_parts(me_a, exclude="I")
    mec_b, per_b = led_b.externality_parts(me_b, exclude="I")

    assert mec_b == 0.0, f"B 는 뒤가 비어 있는데 외부비용이 났다: {per_b}"
    assert mec_a > 0.0, f"A 는 뒤에 3대가 있는데 외부비용이 0 이다: {per_a}"

    # ★ 기준이 선택을 뒤집는다
    assert pc_a < pc_b, "사적 기준이 A 가 아니다"
    assert pc_a + mec_a > pc_b + mec_b, "사회적 기준이 B 가 아니다"

    # 분포가 고르지 않다 — 밀린 차들 중 한 대가 대부분을 떠안는다
    pushed = sorted((v for v in per_a.values() if v > 0.0), reverse=True)
    assert len(pushed) >= 2 and pushed[0] > 2.0 * pushed[1], f"분포가 고르다: {per_a}"


def test_the_private_and_the_social_choice_differ():
    """★★ 설계문서 §6.1 의 핵심 — **사적 기준과 사회적 기준이 다른 곳을 고른다.**

    같은 세계·같은 차인데 S1 과 MEC 가 다른 답을 낸다. 이것이 이 칸의 존재
    이유다. 하나라도 같은 답이면 *"기준을 바꿨는데 아무것도 안 바뀐다"* 이고,
    그러면 이 티켓은 **효과가 없다는 결과**를 보고해야 한다.

    §6.1 은 이 구조에서 사적 기준이 A, 사회적 기준이 B 라고 적었다. 여기서도
    그래야 한다 — A 는 뒤에 3대가 확정돼 있고 B 는 뒤가 비어 있다.
    """

    ev, world, _ = _two_station_world()
    private = S1Policy(S1Settings(**KW))._one(ev, world)

    ev2, world2, _ = _two_station_world()
    social_policy = MecPolicy(MecSettings(**KW))
    social = social_policy._one(ev2, world2)

    assert private == "A", f"사적 기준이 A 가 아니다 — 세계 설정을 봐야 한다: {private}"
    assert social == "B", f"사회적 기준이 B 가 아니다: {social}"
    assert social_policy.counters["mec_calls"] > 0


def test_the_internalised_externality_is_counted_separately():
    """고른 자리의 외부비용과 **후보마다 재 본 값**을 섞지 않는다.

    차 한 대가 코리도의 여러 곳을 재 보고 한 곳을 고른다. 둘을 섞으면 수십 배
    과대보고가 된다 — "MEC 가 하루 몇 분을 내재화했나" 가 핵심 수치여서 중요하다.
    """

    ev, world, _ = _two_station_world()
    policy = MecPolicy(MecSettings(**KW))
    policy._one(ev, world)

    probed = policy.counters["mec_probed_min"]
    chosen = policy.counters.get("mec_chosen_min", 0.0)
    assert probed > 0.0
    assert chosen < probed, "고른 값이 후보 합과 같다 — 둘을 섞었다"
    # B 를 골랐고 B 는 뒤가 비어 있으므로 내재화된 양은 0 이다
    assert chosen == pytest.approx(0.0)


def test_a_bigger_weight_pushes_harder():
    """가중치가 커지면 **덜 미는 쪽**으로 간다 — 단조여야 한다."""

    def choose(weight: float) -> str:
        ev, world, _ = _two_station_world()
        return MecPolicy(MecSettings(**KW, mec_weight=weight))._one(ev, world)

    assert choose(0.0) == "A", "가중치 0 인데 사적 선택이 아니다"
    assert choose(1.0) == "B", "가중치 1 인데 사회적 선택이 아니다"
    # 외부비용을 **부분만** 내재화하면 아직 A 가 싸다 (43.0 + w·MEC < 48.4)
    assert choose(0.2) == "A"


def test_a_negative_weight_is_refused():
    """음수 가중치는 *"남을 밀면 상이다"* 가 된다 — 다른 모형이다."""

    with pytest.raises(ValueError, match="음수"):
        MecSettings(**KW, mec_weight=-0.5)


# ---------------------------------------------------------------------------
# ④ 격리 — MEC 가 새어 들어가지 않는다
# ---------------------------------------------------------------------------

def test_the_escape_decision_stays_private():
    """★ 이탈 판정은 **사적 비용**으로 한다.

    이탈은 *"나한테 너무 비싸다"* 이지 *"남에게 떠넘긴다"* 가 아니다. 사회적
    비용으로 재면 **남의 몫 때문에 내가 여행을 포기**하는 셈이고, 그러면 이탈
    수가 기준 변경 때문에 흔들려서 **"이탈을 맞춘 비교" 가 성립하지 않는다**
    (`docs/compare_rule.md`).

    이탈 비용을 사적 비용과 사회적 비용 **사이**에 두고 확인한다 — 사회적 비용으로
    판단하면 이탈이, 사적 비용으로 판단하면 휴게소가 나온다.
    """

    ev, world, ledger = _two_station_world()

    probe = MecPolicy(MecSettings(**KW))
    arrival = probe._candidate(ev, world, ledger, world.stations["A"], 1.0,
                               opportunity=False)
    assert arrival is not None
    private_a = arrival[0]
    mec_a = ledger.externality("A", arrival[1])
    assert mec_a > 0.0, "이 세계에서는 A 의 외부비용이 양수여야 한다"

    between = private_a + mec_a / 2.0
    ev2, world2, _ = _two_station_world()
    answer = MecPolicy(MecSettings(**KW, escape_cost_min=between))._one(ev2, world2)

    assert answer in ("A", "B"), (
        f"사회적 비용으로 이탈을 판단했다 — 남의 몫 때문에 포기했다: {answer!r}")


def test_the_group_scorer_never_sees_mec():
    """`S3Policy._group_total` 의 이탈 가드는 **개인 체류**를 본다 (#143 § worst).

    MEC 가 `_candidate` 안에 들어가면 그 가드가 오염된다. 그래서 `_mec` 는
    `_one` 의 argmin 에만 있어야 한다.
    """

    import inspect

    from evdt.engine.s1 import S1Policy as S1

    assert "_mec(" not in inspect.getsource(S1._candidate), (
        "`_candidate` 가 MEC 를 더한다 — 그룹 채점기의 이탈 가드가 오염된다")
    assert "_mec(" in inspect.getsource(S1._one)
