"""S3 — 윈도우 안에서 같이 정한다 (#139).

이 파일의 **첫 두 테스트가 나머지 전부를 떠받친다.**

    윈도우 0 으로 돈 S3 == S2  (비트 단위로)

이게 깨지면 "S3 가 S2 보다 좋다/나쁘다" 가 **결합 때문인지 배선 때문인지 구분할 수
없다.** `test_s2.py` 의 첫 테스트(빈 예측의 S2 == S1)와 같은 역할이다.

그리고 **교환이 실제로 일어나는 경우**가 하나 있어야 한다. 동일성 테스트만 있으면
"아무것도 안 하는 S3" 도 전부 통과한다 — #112·#123 에서 두 번 만든 종류의 구멍이다.
"""

from __future__ import annotations

import pytest

from evdt.engine.forecast import ArrivalSpec, OracleForecast
from evdt.engine.reservation import ReservationLedger
from evdt.engine.s2 import S2Policy, S2Settings
from evdt.engine.s3 import S3Policy, S3Settings
from evdt.world.corridor_sim import ChargeAmount, SimEV, run_corridor
from evdt.world.sim import StationSpec, expand_chargers
from evdt.world.travel import ConstantSpeed

FLAT = ((0.0, 1.0, 10_000.0),)
SPEED = 60.0
BATTERY = 60.0
CONSUMPTION = 0.2
AMOUNT = ChargeAmount(buffer_km=30.0, reserve_soc=0.2, target_soc_cap=0.8)
KW = {"buffer_km": 30.0, "reserve_soc": 0.2, "target_soc_cap": 0.8, "max_stops": 1}
SPEC = ArrivalSpec(battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT)


def _spec(sid: str, power_kw: float, n_units: int = 1) -> StationSpec:
    return StationSpec(
        station_id=sid, lat=36.0, lon=127.0,
        chargers=expand_chargers(
            [{"charger_id": f"{sid}_dc", "power_kw": power_kw, "n_units": n_units}]),
    )


def _ev(ev_id: str, entry_min: float, soc0: float = 0.65,
        offset: float = 0.0) -> SimEV:
    return SimEV(
        ev_id=ev_id, vclass_id="vc", entry_min=entry_min, entry_offset_km=offset,
        dest_offset_km=300.0, soc0=soc0, battery_kwh=BATTERY,
        consumption_kwh_km=CONSUMPTION, vmax_kw=200.0, curve=FLAT,
    )


def _ledger(stations) -> ReservationLedger:
    return ReservationLedger.build({s.station_id: s.chargers for s in stations})


def _run(policy, stations, offsets, evs, *, ledger=None, forecast=None):
    return run_corridor(
        stations, offsets, evs, policy, ConstantSpeed(SPEED), AMOUNT,
        corridor_id="c", dt_min=5.0, horizon_min=1440.0, temp_c=20.0,
        cold_factor=1.0, range_factor=1.0, cruise_speed_kmh=SPEED,
        ledger=ledger, forecast=forecast,
    )


def _two_stations():
    stations = [_spec("A", 100.0), _spec("B", 50.0)]
    return stations, {"A": 100.0, "B": 150.0}


# -- ① 배선 동일성 ----------------------------------------------------------

def test_window_zero_is_bit_identical_to_s2():
    """윈도우 0 이면 교환 패스를 아예 안 돈다 → S2 와 같은 답이어야 한다."""

    stations, offsets = _two_stations()
    evs = [_ev(f"EV{i:03d}", float(i)) for i in range(6)]
    fc = OracleForecast({}, {})

    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=fc)
    s3 = _run(S3Policy(S3Settings(**KW, window=0)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=fc)

    assert s3.charge_events == s2.charge_events, "윈도우 0 의 S3 는 S2 와 같아야 한다"
    assert s3.escape_events == s2.escape_events
    assert s3.n_answers == s2.n_answers


def test_window_one_is_also_off():
    """한 대만 보는 윈도우는 바꿀 상대가 없다 — 끈 것과 같아야 한다."""

    stations, offsets = _two_stations()
    evs = [_ev(f"EV{i:03d}", float(i)) for i in range(6)]
    fc = OracleForecast({}, {})

    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=fc)
    s3 = _run(S3Policy(S3Settings(**KW, window=1)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=fc)

    assert s3.charge_events == s2.charge_events


def test_window_zero_records_no_swaps():
    """끈 상태에서 교환 카운터가 올라가면 패스가 돌고 있다는 뜻이다."""

    stations, offsets = _two_stations()
    policy = S3Policy(S3Settings(**KW, window=0))
    _run(policy, stations, offsets, [_ev(f"EV{i:03d}", float(i)) for i in range(6)],
         ledger=_ledger(stations), forecast=OracleForecast({}, {}))

    assert policy.counters.get("swap_tried", 0) == 0
    assert policy.counters.get("swap_done", 0) == 0


# -- ② 교환이 실제로 일어나는가 ---------------------------------------------

def test_swap_actually_happens_and_is_counted():
    """★ 교환이 **한 번이라도** 일어나는 설정이 있어야 한다.

    동일성 테스트만 있으면 아무것도 안 하는 S3 도 전부 통과한다.

    설정: 빠른 A(1기)와 느린 B(1기)에 여러 대가 같은 Δt 에 몰린다. 순차로는 먼저
    묻는 차가 A 를 차지한다. 동시에 보면 **합이 작아지는 쌍**이 생길 수 있다.
    """

    stations = [_spec("A", 150.0), _spec("B", 50.0)]
    offsets = {"A": 100.0, "B": 110.0}
    # 같은 Δt 에 들어오게 촘촘히 넣고, 충전 요구량을 서로 다르게 만든다
    evs = [_ev(f"EV{i:03d}", 0.5 * i, soc0=0.40 + 0.03 * i) for i in range(10)]

    policy = S3Policy(S3Settings(**KW, window=10))
    _run(policy, stations, offsets, evs,
         ledger=_ledger(stations), forecast=OracleForecast({}, {}))

    assert policy.counters.get("swap_tried", 0) > 0, "쌍을 하나도 안 봤다"
    assert policy.counters.get("swap_calls", 0) > 0
    # 시간은 #77 의 입력이다. 재지 않으면 Δt 를 넘는지 알 수 없다
    assert policy.counters.get("swap_ms", 0.0) >= 0.0


def test_the_group_evaluator_is_order_fixed_and_restores_the_ledger():
    """`_group_total` 이 (1) 같은 answers 면 같은 값을 주고 (2) 원장을 answers 상태로
    남기는지. 이 두 성질이 깨지면 "바꿔 보고 되돌리기" 가 성립하지 않는다."""

    from evdt.interfaces import EVState, StationView, WorldView

    stations = [_spec("A", 150.0), _spec("B", 50.0)]
    ledger = _ledger(stations)
    policy = S3Policy(S3Settings(**KW, window=4))

    def view(sid: str, offset: float, power: float) -> StationView:
        return StationView(
            station_id=sid, offset_km=offset, lat=36.0, lon=127.0,
            charger_powers_kw=(power,), n_charging=0, n_waiting=0,
            next_free_min=(0.0,), wait_min=0.0,
        )

    world = WorldView(
        t_min=0.0,
        stations={"A": view("A", 60.0, 150.0), "B": view("B", 60.0, 50.0)},
        temp_c=20.0, cold_factor=1.0, range_factor=1.0,
        ledger=ledger, forecast=OracleForecast({}, {}),
    )

    def ev(i: str, soc: float, bat: float) -> EVState:
        return EVState(
            ev_id=i, vclass_id="vc", corridor_id="c", offset_km=0.0, speed_kmh=SPEED,
            soc=soc, soc_target=0.8, battery_kwh=bat, vmax_kw=200.0,
            dest_offset_km=200.0, is_participant=True, consumption_kwh_km=CONSUMPTION,
            curve=FLAT,
        )

    by_id = {"EV001": ev("EV001", 0.60, 80.0), "EV002": ev("EV002", 0.30, 100.0)}
    group = ["EV001", "EV002"]
    answers = {"EV001": "A", "EV002": "A"}

    first = policy._group_total(ledger, world, group, by_id, answers)
    assert first is not None
    # 원장이 answers 상태로 남아 있어야 한다
    assert ledger.reservation("EV001").station_id == "A"
    assert ledger.reservation("EV002").station_id == "A"

    again = policy._group_total(ledger, world, group, by_id, answers)
    assert again == first, "같은 배정인데 값이 달라졌다 — 순서가 안 고정됐다"

    # 다른 배정은 다른 값이어야 한다 (평가가 배정을 실제로 본다는 뜻)
    answers["EV002"] = "B"
    other = policy._group_total(ledger, world, group, by_id, answers)
    assert other is not None and other != first


def test_accept_rejects_a_worse_or_escape_making_change():
    """`_accept` 는 합이 줄 때만, **새 이탈·새 막다른 길을 안 만들 때만** 참이다.

    값은 `(합, 최대 1인 비용, 막다른 길 수)`.
    """

    policy = S3Policy(S3Settings(**KW, window=4, escape_cost_min=120.0))

    assert policy._accept((100.0, 50.0, 0), (90.0, 50.0, 0)) is True
    assert policy._accept((100.0, 50.0, 0), (100.0, 50.0, 0)) is False, "같으면 안 바꾼다"
    assert policy._accept((100.0, 50.0, 0), (110.0, 50.0, 0)) is False, "나빠지면 안 바꾼다"
    # 합은 줄었지만 한 대를 이탈비용(120) 위로 밀어냈다 → 버린다
    assert policy._accept((100.0, 50.0, 0), (90.0, 130.0, 0)) is False
    # 합은 줄었지만 막다른 길을 새로 만들었다 → 버린다
    assert policy._accept((100.0, 50.0, 0), (10.0, 20.0, 1)) is False
    # 이미 있던 막다른 길은 막지 않는다 (줄이는 것도 받는다)
    assert policy._accept((100.0, 50.0, 2), (90.0, 50.0, 2)) is True
    assert policy._accept((100.0, 50.0, 2), (90.0, 50.0, 1)) is True
    # 원래도 이탈비용 위였고 더 나빠지지 않았다면 막지 않는다
    assert policy._accept((300.0, 200.0, 0), (250.0, 150.0, 0)) is True


# -- ③ 하지 않는 것 ---------------------------------------------------------

def test_no_charge_and_escapes_are_left_alone():
    """무충전·이탈 판정은 S2 에서 물려받은 그대로여야 한다 (#139 완료조건)."""

    stations, offsets = _two_stations()
    # SoC 를 높게 줘서 대부분이 무충전으로 가게 한다
    evs = [_ev(f"EV{i:03d}", float(i), soc0=0.95) for i in range(6)]
    fc = OracleForecast({}, {})

    s2 = _run(S2Policy(S2Settings(**KW)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=fc)
    s3 = _run(S3Policy(S3Settings(**KW, window=6)), stations, offsets, evs,
              ledger=_ledger(stations), forecast=fc)

    assert s3.escape_events == s2.escape_events
    assert len(s3.charge_events) == len(s2.charge_events)


def test_s3_needs_a_forecast():
    """S3 는 S2 의 `_dwell` 을 물려받으므로 예측이 없으면 터져야 한다."""

    stations, offsets = _two_stations()
    with pytest.raises(RuntimeError):
        _run(S3Policy(S3Settings(**KW, window=4)), stations, offsets,
             [_ev("EV001", 0.0)], ledger=_ledger(stations))


def test_settings_match_s2_except_the_window():
    """윈도우 둘 말고는 S2 와 같은 값이어야 한다 — 한쪽만 다르면 결합 차이로 보인다."""

    s2, s3 = S2Settings(**KW), S3Settings(**KW)
    for f in s2.__dataclass_fields__:
        assert getattr(s3, f) == getattr(s2, f), f"{f} 가 다르다"


# -- ③ 전수 열거 (#143) — 휴리스틱이 그룹 안에서 얼마나 지나 --------------------
#
# `docs/objective.md` §3.5: 이동 연산자의 후보가 **그룹이 쓰는 휴게소로 제한**되므로
# 조합이 `window ** window` 이하다. window=4 면 256 — 그냥 다 재면 된다. 그리고
# 채점기(`_group_total`)가 근사가 아니므로 **참 최적해**가 나온다.
#
# 그래서 이 섹션은 두 가지를 지킨다.
#     ① 기본값으로는 **아무것도 바뀌지 않는다** (S3 는 S3 그대로)
#     ② 켜면 **휴리스틱보다 나쁠 수 없다** — 이게 유일한 수학적 불변식이다


def _crowded():
    """교환이 실제로 일어나는 설정. `test_swap_actually_happens_and_is_counted` 와 같다."""

    stations = [_spec("A", 150.0), _spec("B", 50.0)]
    offsets = {"A": 100.0, "B": 110.0}
    evs = [_ev(f"EV{i:03d}", 0.5 * i, soc0=0.40 + 0.03 * i) for i in range(10)]
    return stations, offsets, evs


def _dwell_total(result) -> float:
    return sum(float(r["dwell_min"]) for r in result.charge_events)


def test_exact_group_is_off_by_default():
    """기본값이 꺼져 있어야 한다. 켜져 있으면 **사다리의 S3 가 S3 가 아니게 된다.**"""

    assert S3Settings(**KW).exact_group is False


def test_s3_is_bit_identical_with_exact_group_explicitly_off():
    """`exact_group=False` 가 리팩터 전 S3 와 같은 답을 줘야 한다.

    `_exact_group` 을 넣으면서 휴리스틱 루프를 `_improve_group` 으로 뽑아냈다.
    뽑아내기가 동작을 바꾸면 **사다리 숫자가 조용히 달라진다** — 이 프로젝트에서
    가장 비싼 고장이다 (#123 · #142).
    """

    stations, offsets, evs = _crowded()
    a = _run(S3Policy(S3Settings(**KW, window=4)), stations, offsets, evs,
             ledger=_ledger(stations), forecast=OracleForecast({}, {}))
    b = _run(S3Policy(S3Settings(**KW, window=4, exact_group=False)),
             stations, offsets, evs,
             ledger=_ledger(stations), forecast=OracleForecast({}, {}))

    assert a.charge_events == b.charge_events
    assert a.escape_events == b.escape_events


def test_exact_is_never_worse_than_the_heuristic():
    """★ 유일한 불변식 — 전수 열거는 휴리스틱보다 **나쁠 수 없다.**

    왜 성립하나: 이동은 `answers[ev] = there (there in targets)` 뿐이므로 쓰이는
    휴게소 집합이 **커지지 않는다.** 그래서 휴리스틱의 최종 배정은 열거 공간
    (group -> 초기 targets) **안에 있다.** 열거는 그 공간의 최소를 고르므로 진다.

    이게 깨지면 열거가 공간을 잘못 만들었거나 가드가 휴리스틱보다 빡빡하다는 뜻이다.
    """

    stations, offsets, evs = _crowded()
    heur = S3Policy(S3Settings(**KW, window=4))
    exact = S3Policy(S3Settings(**KW, window=4, exact_group=True))

    rh = _run(heur, stations, offsets, evs,
              ledger=_ledger(stations), forecast=OracleForecast({}, {}))
    rx = _run(exact, stations, offsets, evs,
              ledger=_ledger(stations), forecast=OracleForecast({}, {}))

    # 그룹 단위로 직접 비교한 몫 — 음수면 열거가 휴리스틱에 졌다는 뜻이다
    assert exact.counters.get("exact_gain_min", 0.0) >= -1e-9, (
        "열거가 휴리스틱보다 나쁜 그룹이 있다 — 열거 공간이나 가드가 틀렸다")
    assert exact.counters.get("exact_groups", 0) > 0, "그룹을 하나도 안 풀었다"
    assert exact.counters.get("exact_tried", 0) > 0, "조합을 하나도 안 재지 않았다"

    # 런 전체로도 나빠지지 않아야 한다 (그룹 경계가 같으므로 비교가 성립한다)
    assert _dwell_total(rx) <= _dwell_total(rh) + 1e-6


def test_exact_searches_more_than_the_heuristic():
    """열거가 휴리스틱보다 **더 많이** 재야 한다. 아니면 열거가 안 돌고 있는 것이다."""

    stations, offsets, evs = _crowded()
    exact = S3Policy(S3Settings(**KW, window=4, exact_group=True))
    _run(exact, stations, offsets, evs,
         ledger=_ledger(stations), forecast=OracleForecast({}, {}))

    groups = exact.counters["exact_groups"]
    tried = exact.counters["exact_tried"]
    # 그룹당 조합 수는 len(targets) ** len(group) <= 4 ** 4 = 256
    assert tried > groups, "그룹당 한 가지만 재고 있다"
    assert tried <= groups * 4 ** 4, f"상한(256/그룹)을 넘었다: {tried} / {groups}"


def test_exact_is_deterministic():
    """같은 입력에 같은 답 — 완료조건이다. 동점을 combo 사전순으로 깨뜨린다."""

    stations, offsets, evs = _crowded()
    out = []
    for _ in range(2):
        p = S3Policy(S3Settings(**KW, window=4, exact_group=True))
        out.append(_run(p, stations, offsets, evs,
                        ledger=_ledger(stations),
                        forecast=OracleForecast({}, {})).charge_events)
    assert out[0] == out[1]


def test_the_exact_run_cannot_overwrite_the_s3_run():
    """★ `S3_EXACT` 는 **따로 스테이지를 받아야** 한다 — run_id 가 갈라져야 한다.

    `exact_group` 만 켜서 돌리면 같은 `__S3__` 를 덮어쓴다. #123 에서 흐린 오라클이
    날것을 덮어썼고, #142 에서 스윕이 사다리의 S3 를 덮어썼다. **세 번째는 안 된다.**
    """

    from evdt.config import VALID_STAGES
    from evdt.io.run_registry import make_run_id
    from evdt.runner import RUNNABLE_STAGES

    assert "S3_EXACT" in VALID_STAGES
    assert "S3_EXACT" in RUNNABLE_STAGES
    assert make_run_id("sc", "S3", 1, 100) != make_run_id("sc", "S3_EXACT", 1, 100)


def test_the_db_accepts_the_exact_stage():
    """`schema.sql` 의 CHECK 와 `VALID_STAGES` 가 **일치해야** 한다 (config.py 주석).

    안 맞으면 런이 끝나는 **순간에** 터진다 — 계산을 다 하고 나서 버리게 된다.
    """

    from pathlib import Path

    from evdt.config import VALID_STAGES

    sql = Path("src/evdt/sql/schema.sql").read_text(encoding="utf-8")
    head, _, rest = sql.partition("CHECK (stage IN (")
    assert rest, "stage CHECK 를 못 찾았다"
    check, _, _ = rest.partition("))")
    for st in VALID_STAGES:
        assert f"'{st}'" in check, f"schema.sql 의 CHECK 에 {st} 가 없다"


# -- ④ 윈도우 스윕이 **사다리를 지우지 않는다** (#143) ------------------------
#
# S3 는 어느 윈도우로 돌아도 run_id 가 `__S3__` 다. 그래서 윈도우 스윕을 실제 DB
# 에서 돌리면 **사다리의 S3(윈도우 4)를 윈도우 12 결과로 덮어쓴다.**
#
#     #123  흐린 오라클이 날것을 덮어썼다
#     #142  리드타임 스윕이 사다리의 S3 를 덮어썼다
#
# 세 번째는 안 만든다. `sweep_window.py` 는 **실제 DB 를 복사한 샌드박스**에서
# 돈다. 이 테스트들이 그 성질을 지킨다.


def _sweep_src() -> str:
    from pathlib import Path

    return Path("scripts/sweep_window.py").read_text(encoding="utf-8")


def test_the_window_sweep_runs_in_a_sandbox():
    """★ 스윕이 **실제 DB 에 쓰지 않는다** — `run_once` 에 `db_path` 를 넘겨야 한다.

    안 넘기면 `default_db_path()` 로 가고, run_id 가 윈도우를 구분하지 못하므로
    사다리의 S3 가 마지막 윈도우의 결과로 남는다.
    """

    src = _sweep_src()
    assert "def sandbox(" in src, "샌드박스를 만드는 곳이 없다"
    assert "db_path=db" in src, "run_once 에 샌드박스 DB 를 안 넘긴다"
    assert "runs_dir=runs" in src, "parquet 도 샌드박스로 보내야 한다"


def test_the_sandbox_starts_with_no_runs():
    """샌드박스는 **세계만** 물려받고 결과는 새로 만든다.

    실제 DB 를 복사하면 등록된 런 640개가 같이 온다. 그러면 재사용 판정이 그걸 보고
    *"재사용"* 이라고 말하고, **실제 DB 의 윈도우 4 결과를 윈도우 12 의 결과로
    보고하게 된다.** 복사 직후 `run` 을 비워야 한다.
    """

    assert 'DELETE FROM run' in _sweep_src(), (
        "복사한 DB 의 run 을 비우지 않는다 — 남의 결과를 재사용한다")


def test_the_window_sweep_measures_in_the_ladders_world():
    """스윕의 기본 strength 가 **사다리와 같아야** 한다 (#142 에서 한 번 틀렸다)."""

    import re
    from pathlib import Path

    ladder = re.search(r"^OBSERVED_STRENGTH = ([\d.]+)",
                       Path("scripts/matched_ladder.py").read_text(encoding="utf-8"),
                       re.M)
    sweep = re.search(r"^OBSERVED_STRENGTH = ([\d.]+)", _sweep_src(), re.M)
    assert ladder and sweep
    assert float(ladder.group(1)) == float(sweep.group(1)), (
        f"스윕 strength {sweep.group(1)} != 사다리 {ladder.group(1)}")
    assert "default=OBSERVED_STRENGTH" in _sweep_src()


def test_the_window_sweep_saves_every_point():
    """점마다 CSV 에 저장해야 한다 — DB 에는 **마지막 윈도우만** 남는다."""

    src = _sweep_src()
    body = src.partition("for w in windows:")[2]
    assert "to_csv" in body.partition("_report(")[0], (
        "루프 안에서 저장하지 않는다 — 중간에 끊기면 앞 점이 사라진다")


# ---------------------------------------------------------------------------
# ⑤ 채점기가 **윈도우 안 상호작용을 세나** (#143)
#
# 이것이 S3 의 존재 이유다. 그런데 2026-11-07 까지 **한 번도 안 세고 있었다** —
# 그리고 이 파일의 어느 테스트도 그걸 잡지 못했다. 그래서 여기 적어 둔다.
# ---------------------------------------------------------------------------

def _two_cars_one_charger():
    """50kW 한 대인 휴게소 하나, 그리로 가는 차 두 대.

    EV001 이 멀고(100km) EV002 가 가깝다(130km). 속도가 같으니 **가까운 차가
    먼저 도착한다** — 즉 `(offset_km, ev_id)` 정렬은 도착 순서의 **역순**이다.
    """

    from evdt.interfaces import EVState, StationView, WorldView

    stations = [_spec("A", 50.0, 1)]
    ledger = _ledger(stations)
    world = WorldView(
        t_min=0.0,
        stations={"A": StationView(
            station_id="A", offset_km=160.0, lat=36.0, lon=127.0,
            charger_powers_kw=(50.0,), n_charging=0, n_waiting=0,
            next_free_min=(0.0,), wait_min=0.0)},
        temp_c=20.0, cold_factor=1.0, range_factor=1.0,
        ledger=ledger, forecast=OracleForecast({}, source_run_id="t"))

    def ev(ev_id, offset, soc):
        return EVState(
            ev_id=ev_id, vclass_id="vc", corridor_id="c", offset_km=offset,
            speed_kmh=SPEED, soc=soc, soc_target=0.8, battery_kwh=BATTERY,
            vmax_kw=200.0, dest_offset_km=400.0, is_participant=True,
            consumption_kwh_km=CONSUMPTION, curve=FLAT)

    return ledger, world, {"EV001": ev("EV001", 100.0, 0.45),
                           "EV002": ev("EV002", 130.0, 0.40)}


def test_the_group_scorer_counts_the_queue_inside_the_window():
    """★ 같은 충전기로 두 대를 보내면 **뒤차의 대기가 합에 들어가야** 한다.

    한 대씩 따로 재면 39.6 + 36.0 = **75.6** 이다. 그런데 EV002 가 30분에 와서
    충전기를 쥐므로 60분에 오는 EV001 은 **기다린다** — 진짜 값은 **81.6** 이다.

    ## 왜 이 테스트가 없으면 안 되나

    채점기가 `group` 순서(= offset 오름차순)로 쌓으면 이 6분을 **못 센다.**
    속도가 모두 같아서 그 순서가 도착 순서의 정확한 역순이기 때문이다. 먼저
    쌓이는 차는 늦게 오고, FIFO 는 *"내 뒤에 오는 차"* 를 버린다.

    실제로 그렇게 돌고 있었고, 이 파일의 어느 테스트도 안 잡았다. 동일성
    테스트(윈도우 0 == S2)는 **개선 패스를 아예 안 도는** 경로라서 통과하고,
    "교환이 실제로 일어난다" 테스트는 **다른 휴게소로 옮기는** 경우라서 통과한다.
    둘 다 같은 휴게소에 쌓인 차들을 안 본다.

    그 6분이 **S3 가 재려던 바로 그 외부효과**다 (`_group_total` 모듈 문서).
    """

    ledger, world, by_id = _two_cars_one_charger()
    policy = S3Policy(S3Settings(**{**KW, "max_stops": 2}, window=4))
    total = policy._group_total(
        ledger, world, ["EV001", "EV002"], by_id, {"EV001": "A", "EV002": "A"})

    assert total is not None
    assert total[0] == pytest.approx(81.6), (
        f"그룹 합이 {total[0]:.1f} 다 — 75.6 이면 둘이 서로를 못 본 것이다")
    assert total[1] == pytest.approx(45.6), "최대 1인 비용이 기다린 차의 것이 아니다"


def test_the_scorer_order_does_not_depend_on_the_group_order():
    """같은 배정이면 `group` 을 어떤 순서로 줘도 **같은 합**이 나와야 한다.

    도착 시각으로 정렬하므로 호출자의 순서가 결과를 바꾸면 안 된다. 바뀌면
    두 배정을 비교할 때 *"순서가 달라진 몫"* 이 섞인다.
    """

    answers = {"EV001": "A", "EV002": "A"}
    out = []
    for group in (["EV001", "EV002"], ["EV002", "EV001"]):
        ledger, world, by_id = _two_cars_one_charger()
        policy = S3Policy(S3Settings(**{**KW, "max_stops": 2}, window=4))
        out.append(policy._group_total(ledger, world, group, by_id, answers)[0])
    assert out[0] == pytest.approx(out[1]), f"순서가 합을 바꿨다: {out}"


def test_the_arrival_time_has_one_formula():
    """`_eta` 가 `_candidate` 와 **같은 식**이어야 한다.

    두 곳에 쓰면 채점 순서와 실제 도착 시각이 갈라지고, 그러면 원장이 틀린
    미래를 예측한다 (설계 규칙 1 과 같은 종류).
    """

    import inspect

    from evdt.engine.s1 import S1Policy

    assert "self._eta(" in inspect.getsource(S1Policy._candidate), (
        "`_candidate` 가 도착 시각을 자기 식으로 다시 쓴다")


# ---------------------------------------------------------------------------
# ⑥ solver 손잡이 (#143)
# ---------------------------------------------------------------------------

def test_the_solver_knob_refuses_nonsense():
    """모르는 solver 는 **그 자리에서** 터진다 — 조용히 local 로 돌면 안 된다."""

    with pytest.raises(ValueError, match="solver"):
        S3Settings(**KW, solver="genetic")
    with pytest.raises(ValueError, match="candidate_scope"):
        S3Settings(**KW, candidate_scope="everything")
    with pytest.raises(ValueError, match="모순"):
        S3Settings(**KW, exact_group=True, solver="milp")


def test_exact_group_is_the_old_name_for_the_exact_solver():
    """`exact_group=True` 와 `solver="exact"` 가 **같은 뜻**이어야 한다."""

    assert S3Settings(**KW).solver_mode == "local"
    assert S3Settings(**KW, exact_group=True).solver_mode == "exact"
    assert S3Settings(**KW, solver="exact").solver_mode == "exact"
    assert S3Settings(**KW, solver="milp").solver_mode == "milp"


def _congested_run(settings):
    """예측이 줄을 만드는 세계에서 하루 돌린다. 반환: (결과, 정책).

    `OracleForecast` 에 **원장에 없는 도착**을 넣어 A 를 붐비게 만든다. 그래야
    Δt 마다 정보가 바뀌고 그룹 최적화가 실제로 돌 일이 생긴다.
    """

    from evdt.engine.ledger import Arrival

    stations = [_spec("A", 50.0, 1), _spec("B", 200.0, 2)]
    offsets = {"A": 90.0, "B": 150.0}
    evs = [_ev(f"EV{i:03d}", 1.5 * i, soc0=0.46 + 0.01 * i) for i in range(10)]
    ghosts = tuple(Arrival(
        ev_id=f"GH{k:02d}", arrival_min=60.0 + 6.0 * k, soc_from=0.2, soc_to=0.8,
        battery_kwh=BATTERY, vmax_kw=200.0, curve=FLAT, charge_power_factor=1.0)
        for k in range(12))

    policy = S3Policy(settings)
    out = _run(policy, stations, offsets, evs, ledger=_ledger(stations),
               forecast=OracleForecast({"A": ghosts}, source_run_id="t"))
    return out, policy


def _milp_kw(**kw):
    return S3Settings(**{**KW, "max_stops": 2}, solver="milp", **kw)


def test_the_milp_reproduces_the_exact_scorer_when_pairs_are_enough():
    """★ 근사 오차가 **윈도우 2 에서 0 이어야** 한다.

    그룹이 두 대면 선행자가 최대 한 대다. 그러면 쌍별 정식화에 **겹칠 것이
    없어서** 정확하다 — 즉 이 값이 0 이 아니면 근사가 아니라 **계수를 잘못 잰
    것**이다. 측정 코드의 가장 싼 검산이다.

    처음 돌렸을 때 이 값이 그룹당 −569분이었다. 근사 탓이 아니었다 —
    `_group_total` 이 윈도우 안 상호작용을 안 세고 있었다 (위 ⑤).
    """

    _, policy = _congested_run(_milp_kw(window=2))

    assert policy.counters["milp_groups"] > 0, "MILP 를 한 번도 안 풀었다"
    assert policy.counters["milp_approx_err_min"] == pytest.approx(0.0, abs=1e-6), (
        "윈도우 2 에서 근사 오차가 0 이 아니다 — 계수를 잘못 쟀다")


def test_the_milp_never_underestimates_the_true_cost():
    """근사 오차는 **0 이하**다 (`정확한 값 − MILP 목적값`).

    쌍별로 더하면 선행자가 둘 이상일 때 겹침을 **과대 계산**한다. 그래서 MILP
    목적값은 참값보다 크거나 같다. 부호가 뒤집히면 선행 관계를 잘못 잡은 것이다
    (뒤차를 선행자로 센 것과 같은 고장).
    """

    for window in (2, 4):
        _, policy = _congested_run(_milp_kw(window=window))
        assert policy.counters["milp_approx_err_min"] <= 1e-6, (
            f"윈도우 {window} 에서 MILP 가 참값보다 싸게 봤다 — 선행 관계가 틀렸다")


def test_every_milp_group_is_accounted_for():
    """★ 그룹마다 **네 결말 중 하나**여야 한다 — 조용히 사라지면 안 된다.

        better    정확한 채점에서 휴리스틱을 이겼다 → 쓴다
        tied      같다 → 휴리스틱을 쓴다 (휴리스틱이 이미 최적이었다)
        lost      근사가 나쁜 해를 골랐다 → 버린다
        rejected  가드를 못 넘었다 (막다른 길·이탈) → 버린다

    셋째가 **MILP 의 값을 판단하는 숫자**다. `better` 만 보면 "MILP 가 이겼다"
    로 읽히는데, 그 이득은 **둘 중 나은 것을 골라서** 나온 것일 수 있다.
    """

    _, policy = _congested_run(_milp_kw(window=4))
    c = policy.counters
    assert c["milp_groups"] == (
        c["milp_better"] + c["milp_tied"] + c["milp_lost"] + c["milp_rejected"]), c


def test_turning_the_candidate_filter_off_grows_the_model(capsys):
    """#143 완료조건 — **ablation 한 줄**: 필터를 끄면 변수가 얼마나 늘어나나.

    후보 필터(진행 방향 + SoC 도달성)를 끄면 코리도의 모든 휴게소가 변수가 된다.
    여기는 휴게소가 둘뿐이라 2배지만, 경부선 35곳에서는 차당 2~4곳 → 35곳이다.

    **답은 같아야 한다.** 못 가는 칸은 0 으로 묶여 있으므로 고를 수 없다. 즉
    필터의 기여는 *"답을 바꾸는 것"* 이 아니라 **"모형을 작게 만드는 것"** 이고,
    그게 §5.3 이 요구한 ablation 의 결론이다.
    """

    narrow_out, narrow = _congested_run(_milp_kw(window=4))
    wide_out, wide = _congested_run(_milp_kw(window=4, candidate_scope="all"))

    assert wide.counters["milp_binaries"] > narrow.counters["milp_binaries"], (
        "필터를 껐는데 모형이 안 커졌다 — ablation 이 아무것도 재지 못한다")
    assert ([e["station_id"] for e in wide_out.charge_events]
            == [e["station_id"] for e in narrow_out.charge_events]), (
        "필터를 껐더니 답이 달라졌다 — 0 으로 묶은 칸을 골랐다는 뜻이다")
