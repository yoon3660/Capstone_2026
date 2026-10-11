"""미래 도착 예측 (설계문서 §8.2 · #118) — **S2 가 S1 에 더하는 단 하나의 정보.**

    S0   지금 화면의 대기
    S1   + **이미 배정된** 차들이 만드는 내 도착 시각의 줄
    S2   + **아직 배정되지 않은** 차들이 만들 줄        ← 여기

S1 의 원장에는 **이미 결정한 차만** 있다. 내가 40분 뒤 도착할 휴게소에, 지금 아직
상류에 있어서 질문도 받지 않은 차 30대가 나보다 먼저 도착할 수 있다. S1 은 그 30대를
못 본다. **S2 는 그걸 본다.**

## ⚠ 이 파일의 오라클은 "상한" 이 아니라 "상한 추정치" 다

설계문서 §8.2 는 *"완전 예측 롤링은 거의 공짜다 — 시뮬레이터가 이미 정답을 알고
있다"* 고 적었다. **고속도로 코리도에서는 반만 맞다.**

시뮬레이터가 공짜로 아는 것은 **외생적인 것**뿐이다 — 각 차의 진입 시각·진입 SoC·
목적지·속도. 그래서 "그 차가 몇 시에 어디쯤 있을까" 는 안다.

**어느 휴게소에 설지는 모른다.** 그건 결정 변수이고, 지금 고르려는 바로 그 값이다.

    "미래 도착" 을 알려면 미래의 결정을 알아야 하고
    미래의 결정을 하려면 "미래 도착" 을 알아야 한다

그래서 이 오라클은 **직전 런의 실현된 도착**을 미래로 쓴다 (`from_run`). 그 런이 S1
이면 오라클이 보는 미래는 **S1 의 미래**다. S2 가 차를 다른 데로 보내면 실제 미래는
그와 달라진다.

**결론: 이 수치는 "완전 예측의 가치" 의 하한도 상한도 아닌 추정치다.** 발표에서
"이론 상한" 이라고 말하면 안 된다. 근거가 필요하면 아래 고정점을 쓴다.

## 고정점으로 좁힌다 — 순환을 측정으로 바꾸는 방법

순환은 **반복으로** 줄일 수 있다. UE 의 최적반응과 같은 모양이다.

    1회차   오라클 ← S1 의 도착        → S2(1) 실행
    2회차   오라클 ← S2(1) 의 도착      → S2(2) 실행
    ...
    n회차   도착이 더 안 바뀌면 **자기 예측과 일치하는 해**다

수렴하면 그 해는 **"모두가 미래를 정확히 알고, 그 앎이 만든 미래가 다시 정확한"**
상태다 — 이게 §8.2 가 말한 완전 예측 롤링이다. 수렴하지 않으면(진동) 그 사실 자체가
결과다: *"완전 예측은 이 문제에서 잘 정의되지 않는다."*

`describe_shift()` 가 회차 간 도착 변화를 재서 수렴을 판정할 재료를 준다.

## 예측은 원장에 쓰지 않는다

`ReservationLedger.dwell_if_i_go(..., future=)` 로 **계산에만** 들어간다. 원장에
쓰면 다른 차가 **아직 아무도 약속하지 않은 예약**을 확정으로 본다 — #83 이 두 번째
정차를 원장에서 뺀 것과 같은 이유다.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol, runtime_checkable

# ⚠ `queue_rule` 을 직접 임포트하지 않는다 — **타입만 쓰더라도** 그렇다. 큐 계산이
# 필요한 엔진 코드는 원장을 거친다 (s1.py 와 같은 이유, 호출자가 늘면 규칙이 갈라진다).
# 예측은 큐를 **계산하지 않는다**: 도착을 모아서 돌려주기만 하고, 그것들이 만드는
# 줄은 `StationLedger` 가 센다
# (tests/test_queue_rule.py::test_queue_rule_has_at_most_two_callers).
from evdt.engine.ledger import Arrival

__all__ = ["Forecast", "OracleForecast", "OracleSource", "ArrivalSpec",
           "describe_shift", "specs_from_trips"]


@runtime_checkable
class Forecast(Protocol):
    """정책이 예측에 물을 수 있는 것. **딱 하나다.**

    넓히기 전에 생각할 것: 정책이 예측에서 더 많은 걸 꺼낼 수 있으면 S2 와 S3 의
    경계가 흐려진다. S2 는 *"내가 도착할 때 줄이 얼마일까"* 만 묻는다. *"그래서 우리가
    같이 어디로 갈까"* 는 S3 다.
    """

    def arrivals_between(self, station_id: str, *,
                         after_min: float, before_min: float) -> tuple[Arrival, ...]:
        """(after_min, before_min) 사이에 그 휴게소에 올 것으로 보는 도착들."""
        ...


@dataclass(frozen=True)
class OracleSource:
    """오라클의 **재료** — 어느 런의 도착을 미래로 쓸 것인가 (#118).

    `rows` 는 `charge_event` 모양의 행들이다 (ev_id · station_id · t_arrive_min ·
    soc_in · soc_out · cold_factor). 파케이에서 읽어도 되고, 고정점 반복 중이면
    직전 회차의 결과를 메모리에서 그대로 넘겨도 된다.

    **차량 물리는 들어 있지 않다** — 그건 `run_once` 가 이번 런의 차량 집합에서
    채운다. 과거 런의 `charge_min` 을 재사용하면 그때 잡은 충전기 출력이 박혀
    들어오기 때문이다 (`ArrivalSpec` 참고).
    """

    rows: Sequence[Mapping]
    #: 어느 런에서 왔나. **run 기록에 박힌다** — 이게 없으면 "어떤 미래를 본
    #: S2 인가" 를 나중에 알 수 없고, #101 처럼 다른 세계의 결과를 재사용하게 된다
    source_run_id: str
    #: 고정점 회차 (1 = S1 의 미래)
    round_no: int = 1


@dataclass(frozen=True, slots=True)
class ArrivalSpec:
    """차 한 대의 **충전 물리** — 예측이 도착을 `Arrival` 로 되살리는 데 필요한 값.

    과거 런의 `charge_event` 에는 이게 없다 (vclass_id 만 있다). 그런데 **같은
    시나리오·같은 시드면 같은 차들**이라, 이번 런의 차량 집합에서 그대로 가져올 수
    있다. 그래서 예측은 "누가 언제 어디로" 만 과거에서 읽고 **물리는 현재에서** 읽는다.

    이게 중요한 이유: 과거 런의 `charge_min` 을 그대로 쓰면 **그 차가 그때 잡은
    충전기 출력**이 박혀 들어온다. 100kW 에서 잰 시간을 350kW 자리에 놓으면 줄이
    없는데도 길다고 말한다.
    """

    battery_kwh: float
    vmax_kw: float
    curve: tuple
    charge_power_factor: float = 1.0


@dataclass(frozen=True)
class OracleForecast:
    """직전 런에서 **실제로 일어난** 도착을 미래로 돌려준다.

    휴게소별로 도착 시각 순 정렬해 두고 이분탐색한다. 불변이라 여러 Δt 에서 같은
    객체를 다시 쓸 수 있다.
    """

    #: 휴게소 → 도착 시각 순 Arrival
    by_station: Mapping[str, tuple[Arrival, ...]]
    #: 어느 런의 미래인가. **이 값이 순환의 출처다** — 결과에 반드시 기록한다
    source_run_id: str
    #: 고정점 반복 회차 (1 = S1 의 미래를 썼다)
    round_no: int = 1

    # -- 만들기 ---------------------------------------------------------------
    @classmethod
    def from_rows(cls, rows: Iterable[Mapping], specs: Mapping[str, ArrivalSpec],
                  *, source_run_id: str, round_no: int = 1) -> OracleForecast:
        """`charge_event` 행들 + 이번 런의 차량 물리 → 예측.

        `specs` 에 없는 ev_id 는 **버린다.** 그 차는 이번 런에 없다 — 모집단이 다른
        런을 오라클로 쓴 것이고, 조용히 섞으면 있지도 않은 차가 줄을 만든다.
        몇 대를 버렸는지는 `missing` 으로 센다.
        """

        buckets: dict[str, list[Arrival]] = {}
        missing = 0

        for r in rows:
            ev_id = str(r["ev_id"])
            spec = specs.get(ev_id)
            if spec is None:
                missing += 1
                continue
            buckets.setdefault(str(r["station_id"]), []).append(Arrival(
                ev_id=ev_id,
                arrival_min=float(r["t_arrive_min"]),
                soc_from=float(r["soc_in"]),
                soc_to=float(r["soc_out"]),
                battery_kwh=spec.battery_kwh,
                vmax_kw=spec.vmax_kw,
                curve=spec.curve,
                # 저온 계수는 **과거 런의 값**을 쓴다. 기온은 세계의 상태라 같은
                # 시나리오면 같지만, 다르면 그건 모집단이 다르다는 신호다
                charge_power_factor=float(r.get("cold_factor", spec.charge_power_factor)),
            ))

        if missing:
            raise ValueError(
                f"오라클로 쓴 런에 이번 런에 없는 차가 {missing:,}대 있다 "
                f"({source_run_id}). 같은 (시나리오·stage·참여율·시드) 의 런이어야 한다 — "
                "모집단이 다르면 예측이 없는 차로 줄을 만든다"
            )

        return cls(
            by_station={sid: tuple(sorted(a, key=lambda x: (x.arrival_min, x.ev_id)))
                        for sid, a in buckets.items()},
            source_run_id=str(source_run_id),
            round_no=int(round_no),
        )

    # -- 묻기 -----------------------------------------------------------------
    def arrivals_between(self, station_id: str, *,
                         after_min: float, before_min: float) -> tuple[Arrival, ...]:
        """`after_min` **초과**, `before_min` **미만**.

        `after_min` 초과인 이유: 이미 지난 도착은 **원장과 실제 큐에 이미 있다.**
        예측에서 또 주면 지난 차를 두 번 센다.
        """

        arrs = self.by_station.get(station_id)
        if not arrs:
            return ()
        times = self._times[station_id]
        lo = bisect_right(times, float(after_min))
        hi = bisect_left(times, float(before_min), lo)
        return arrs[lo:hi]

    @cached_property
    def _times(self) -> Mapping[str, list[float]]:
        """도착 시각만 뽑아 둔다. Δt 마다 휴게소마다 다시 만들면 이게 제일 비싸다."""

        return {sid: [a.arrival_min for a in arrs] for sid, arrs in self.by_station.items()}

    # -- 보고 -----------------------------------------------------------------
    @property
    def n_arrivals(self) -> int:
        return sum(len(v) for v in self.by_station.values())

    def describe(self) -> str:
        return (f"오라클 {self.round_no}회차 · 출처 {self.source_run_id} · "
                f"도착 {self.n_arrivals:,}건 · 휴게소 {len(self.by_station)}곳")


def describe_shift(before: OracleForecast, after: OracleForecast) -> dict[str, float]:
    """두 회차의 도착이 얼마나 달라졌나 — **고정점 수렴 판정의 재료** (#118).

    셋을 센다.

        moved_share     휴게소가 바뀐 차의 비율. **0 이면 배정이 고정점이다**
        n_only_before   앞 회차에만 있던 차 (충전을 안 하게 됐거나 이탈)
        mean_abs_shift  같은 휴게소에 남은 차들의 도착 시각 변화 (분, 절대값 평균)

    `moved_share` 가 회차마다 줄면 수렴, 두 값 사이를 오가면 **진동**이다 —
    #55·#78·#112 에서 본 점질량 2주기와 같은 모양이고, 그때는 수렴을 보고하지 않는다.
    """

    def where(f: OracleForecast) -> dict[str, tuple[str, float]]:
        return {a.ev_id: (sid, a.arrival_min)
                for sid, arrs in f.by_station.items() for a in arrs}

    b, a = where(before), where(after)
    shared = b.keys() & a.keys()
    moved = sum(1 for e in shared if b[e][0] != a[e][0])
    stayed = [abs(a[e][1] - b[e][1]) for e in shared if b[e][0] == a[e][0]]

    return {
        "n_before": float(len(b)),
        "n_after": float(len(a)),
        "n_shared": float(len(shared)),
        "n_moved": float(moved),
        "moved_share": moved / len(shared) if shared else 0.0,
        "n_only_before": float(len(b.keys() - a.keys())),
        "n_only_after": float(len(a.keys() - b.keys())),
        "mean_abs_shift_min": sum(stayed) / len(stayed) if stayed else 0.0,
    }


def specs_from_trips(trips: Sequence) -> dict[str, ArrivalSpec]:
    """이번 런의 `TripDemand` 들에서 충전 물리를 뽑는다.

    `runner` 가 부른다 — 거기만 과거 런(파케이)과 이번 런(차량 집합)을 **둘 다** 본다.
    엔진은 예측을 받아 쓰기만 하고 어디서 왔는지 모른다 (설계 규칙 2).
    """

    return {t.ev_id: ArrivalSpec(battery_kwh=t.battery_kwh, vmax_kw=t.vmax_kw,
                                 curve=t.curve, charge_power_factor=t.cold_factor)
            for t in trips}
