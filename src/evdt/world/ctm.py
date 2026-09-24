"""CTM — 셀 전송 모형 (이슈 #56).

셀(T-17, `cell_split.py`) 위에서 dt 마다 차량을 앞으로 보낸다. 셀 하나의 상태는
"그 안에 몇 대 있는가" `n` 하나뿐이고, 셀 사이 유량은

    y = min(앞 셀이 보낼 수 있는 양, 뒤 셀이 받을 수 있는 양)

이다. 이 한 줄이 CTM 의 전부다. 정체는 "받을 수 있는 양" 이 작아져서 상류로
번지는 것으로 저절로 나온다.

삼각형 기본도 (설계문서 §9.5 — 반드시 읽을 것)
    q_max 는 독립 파라미터가 **아니다**.

        q_max = v_free × w_back × k_jam / (v_free + w_back)

    이걸 어기면 정체가 아예 생기지 않는다. `cell` 테이블에 CHECK 가 있고,
    여기서도 CellArrays 를 만들 때 다시 검사한다. 두 군데서 막는 이유는, 셀이
    DB 를 거치지 않고 만들어지는 경로(테스트·합성 셀)가 있기 때문이다.

CFL 조건
    한 스텝에 차가 셀 하나를 건너뛰면 모형이 성립하지 않는다.

        max(v_free, w_back) × dt ≤ 셀 길이

    어기면 **시작 전에** 멈춘다 (`check_cfl`). 돌다가 틀린 값을 내는 것보다
    안 도는 편이 낫다.

단위
    거리 km · 속도 km/h · 시간 **분** (프로젝트 전체가 분이다) · 대수 veh.
    dt_min 을 시간으로 바꿔 쓰는 곳은 `_fractions` 한 곳뿐이다.

층 규칙
    순수 함수와 numpy 만 쓴다. DB·파일·config 를 읽지 않고 engine 을 임포트하지
    않는다 (설계 규칙 2). 셀을 읽어 오는 것은 부르는 쪽 일이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: 삼각형 기본도 일관성 검사의 상대 허용오차. schema.sql 의 CHECK 와 같은 값이다.
FD_TOLERANCE = 0.01

EPS = 1e-9


class CFLViolation(ValueError):
    """셀이 CFL 조건보다 짧다. 이 상태로 돌리면 정체파가 불가능한 속도로 퍼진다."""


class FundamentalDiagramError(ValueError):
    """q_max 가 v_free·w_back·k_jam 과 맞지 않는다 (설계문서 §9.5)."""


@dataclass(frozen=True)
class CellArrays:
    """한 방향의 셀을 컬럼별 배열로 들고 있는 것. seq 순서대로 정렬되어 있다.

    `cell` 테이블의 한 방향을 그대로 옮긴 것이다. 셀마다 dict 를 만들면 하루
    7,200 스텝 × 400 셀에서 파이썬 객체 오버헤드가 그대로 비용이 된다.
    """

    length_km: np.ndarray
    lanes: np.ndarray
    v_free_kmh: np.ndarray
    w_back_kmh: np.ndarray
    k_jam_veh_km_lane: np.ndarray
    q_max_veh_h: np.ndarray

    def __post_init__(self) -> None:
        arrays = {
            "length_km": self.length_km,
            "lanes": self.lanes,
            "v_free_kmh": self.v_free_kmh,
            "w_back_kmh": self.w_back_kmh,
            "k_jam_veh_km_lane": self.k_jam_veh_km_lane,
            "q_max_veh_h": self.q_max_veh_h,
        }

        sizes = {name: np.asarray(a).shape for name, a in arrays.items()}

        if len({s for s in sizes.values()}) != 1:
            raise ValueError(f"셀 배열 길이가 서로 다르다: {sizes}")

        for name, array in arrays.items():
            values = np.asarray(array, dtype=float)
            if values.ndim != 1:
                raise ValueError(f"{name} 은 1차원이어야 한다 (받은 모양 {values.shape})")
            if not np.all(np.isfinite(values)) or np.any(values <= 0):
                raise ValueError(f"{name} 에 0 이하이거나 유한하지 않은 값이 있다")

        expected = (
            self.lanes
            * self.v_free_kmh
            * self.w_back_kmh
            * self.k_jam_veh_km_lane
            / (self.v_free_kmh + self.w_back_kmh)
        )
        off = np.abs(self.q_max_veh_h - expected) > FD_TOLERANCE * self.q_max_veh_h

        if np.any(off):
            i = int(np.flatnonzero(off)[0])
            raise FundamentalDiagramError(
                f"셀 {i}: q_max {self.q_max_veh_h[i]:.1f} ≠ 삼각형 기본도 {expected[i]:.1f} 대/h"
                " (설계문서 §9.5 — q_max 는 v_free·w_back·k_jam 에서 따라 나오는 값이다)"
            )

    def __len__(self) -> int:
        return int(self.length_km.size)

    @property
    def jam_veh(self) -> np.ndarray:
        """셀이 꽉 찼을 때의 대수 N = k_jam × 차로수 × 길이."""

        return self.k_jam_veh_km_lane * self.lanes * self.length_km


def check_cfl(cells: CellArrays, dt_min: float) -> None:
    """CFL 조건을 어긴 셀이 하나라도 있으면 멈춘다. 돌리기 **전에** 부른다."""

    if not np.isfinite(dt_min) or dt_min <= 0:
        raise ValueError(f"dt_min 은 0보다 커야 한다: {dt_min}")

    floor_km = np.maximum(cells.v_free_kmh, cells.w_back_kmh) * dt_min / 60.0
    short = cells.length_km < floor_km - EPS

    if np.any(short):
        i = int(np.flatnonzero(short)[0])
        raise CFLViolation(
            f"셀 {i}: 길이 {cells.length_km[i]:.3f} km < CFL 하한 {floor_km[i]:.3f} km"
            f" (max(v_free {cells.v_free_kmh[i]:.0f}, w_back {cells.w_back_kmh[i]:.0f}) × dt {dt_min} 분)."
            f" 어긴 셀 {int(short.sum())}개. dt 를 줄이거나 셀을 길게 나눌 것"
        )


def _fractions(cells: CellArrays, dt_min: float) -> tuple[np.ndarray, np.ndarray]:
    """한 스텝에 차가 셀의 몇 할을 지나는가 (자유류 / 후퇴파). CFL 이면 둘 다 ≤ 1."""

    dt_h = dt_min / 60.0

    return (
        cells.v_free_kmh * dt_h / cells.length_km,
        cells.w_back_kmh * dt_h / cells.length_km,
    )


def sending(n_veh: np.ndarray, cells: CellArrays, dt_min: float) -> np.ndarray:
    """셀이 이번 스텝에 **보내고 싶은** 양 (수요). min(자유류로 흘러나올 양, 용량)."""

    free_fraction, _ = _fractions(cells, dt_min)

    return np.minimum(free_fraction * n_veh, cells.q_max_veh_h * dt_min / 60.0)


def receiving(n_veh: np.ndarray, cells: CellArrays, dt_min: float) -> np.ndarray:
    """셀이 이번 스텝에 **받을 수 있는** 양 (공급). min(용량, 빈자리가 비워지는 속도)."""

    _, back_fraction = _fractions(cells, dt_min)

    return np.minimum(
        cells.q_max_veh_h * dt_min / 60.0,
        back_fraction * np.maximum(cells.jam_veh - n_veh, 0.0),
    )


def _merge(mainline: np.ndarray, ramp: np.ndarray, supply: np.ndarray,
           priority: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """본선과 진입 램프가 같은 빈자리를 두고 만났을 때의 배분 (Daganzo 1995).

    둘의 수요 합이 공급 안에 들어가면 각자 수요만큼 지나간다. 넘칠 때만 나눈다.

        y_본선 = median(수요, 공급 − 램프수요, 우선순위 × 공급)

    세 값의 **가운데**를 고르면, 우선순위 몫을 보장하면서도 상대가 덜 쓰면 그만큼
    더 가져가는 배분이 한 줄로 나온다.

    ⚠ 이 median 은 **막혔을 때만** 맞다. 안 막혔는데 그대로 쓰면 수요보다 많이
    흘려보낸다 (예: 수요 13, 공급 33, 우선순위 0.5 → median 16.7 > 13). 그러면
    앞 셀이 내보낸 것보다 뒤 셀이 더 받아서 차가 저절로 생긴다.
    """

    congested = mainline + ramp > supply

    main = np.where(
        congested,
        np.median(np.stack([mainline, supply - ramp, priority * supply]), axis=0),
        mainline,
    )
    ramp_in = np.where(
        congested,
        np.median(np.stack([ramp, supply - mainline, (1.0 - priority) * supply]), axis=0),
        ramp,
    )

    return np.maximum(main, 0.0), np.maximum(ramp_in, 0.0)


@dataclass(frozen=True)
class StepResult:
    """한 스텝의 결과. 대수는 모두 "이번 스텝 동안 몇 대" 다 (대/h 가 아니다)."""

    n_veh: np.ndarray        #: 스텝이 끝난 뒤 셀마다 남은 대수
    boundary_flow: np.ndarray  #: 길이 N+1. [0] 은 유입, [N] 은 코리도 밖으로 나간 양
    ramp_in_veh: np.ndarray  #: 셀마다 진입 램프로 실제로 들어간 양 (수요보다 적을 수 있다)
    ramp_out_veh: np.ndarray  #: 셀마다 진출 램프로 빠져나간 양


def step(
    n_veh: np.ndarray,
    cells: CellArrays,
    dt_min: float,
    *,
    inflow_veh: float = 0.0,
    ramp_demand_veh: np.ndarray | None = None,
    exit_ratio: np.ndarray | None = None,
    merge_priority: np.ndarray | float = 0.5,
    downstream_supply_veh: float = np.inf,
) -> StepResult:
    """CTM 한 스텝.

    inflow_veh
        코리도 시작점으로 들어오려는 대수. 첫 셀이 막혀 있으면 덜 들어간다
        (돌려주는 `boundary_flow[0]` 이 실제로 들어간 양이다).
    ramp_demand_veh
        셀마다 그 셀의 **상류 끝** 진입 램프로 들어오려는 대수.
    exit_ratio
        셀마다 그 셀의 **하류 끝** 진출 램프로 빠지는 비율 (0~1).
        본선이 막히면 진출 차량도 같이 막힌다 (FIFO). 한 줄로 선 차들 사이에서
        "나는 나갈 거니까" 로 앞질러 갈 수 없기 때문이다. 이 가정 때문에 램프
        근처 정체가 실제보다 조금 세게 나올 수 있다 — 보정(#57)에서 볼 것.
    downstream_supply_veh
        코리도 끝 바깥이 받아줄 수 있는 양. 기본은 무한(자유 유출)이다.
    """

    n = np.asarray(n_veh, dtype=float)
    size = len(cells)

    if n.shape != (size,):
        raise ValueError(f"n_veh 의 길이가 셀 수와 다르다: {n.shape} vs {size}")

    ramp = (
        np.zeros(size)
        if ramp_demand_veh is None
        else np.asarray(ramp_demand_veh, dtype=float)
    )
    beta = (
        np.zeros(size)
        if exit_ratio is None
        else np.asarray(exit_ratio, dtype=float)
    )
    priority = np.broadcast_to(np.asarray(merge_priority, dtype=float), (size,))

    if np.any(beta < 0.0) or np.any(beta > 1.0):
        raise ValueError("exit_ratio 는 0 과 1 사이여야 한다")
    if np.any(ramp < 0.0):
        raise ValueError("ramp_demand_veh 에 음수가 있다")
    if np.any(priority < 0.0) or np.any(priority > 1.0):
        raise ValueError("merge_priority 는 0 과 1 사이여야 한다")

    send = sending(n, cells, dt_min)
    recv = receiving(n, cells, dt_min)

    # 본선 수요: 보내고 싶은 양 중 진출 램프로 빠지지 않는 몫
    mainline_demand = send * (1.0 - beta)

    # 경계 k (셀 k → 셀 k+1). 상류 끝과 하류 끝은 따로 둔다.
    boundary_flow = np.zeros(size + 1)
    ramp_in = np.zeros(size)

    # 상류 끝: 유입 수요와 첫 셀의 진입 램프가 첫 셀의 빈자리를 나눠 쓴다
    head_main, head_ramp = _merge(
        np.array([float(inflow_veh)]),
        ramp[:1],
        recv[:1],
        priority[:1],
    )
    boundary_flow[0] = head_main[0]
    ramp_in[0] = head_ramp[0]

    if size > 1:
        main, ramp_got = _merge(mainline_demand[:-1], ramp[1:], recv[1:], priority[1:])
        boundary_flow[1:size] = main
        ramp_in[1:] = ramp_got

    # 하류 끝: 코리도 밖으로
    boundary_flow[size] = min(mainline_demand[-1], float(downstream_supply_veh))

    # 본선이 허용된 만큼만 지나가면, 같은 줄에 섰던 진출 차량도 그 비율로 빠진다 (FIFO)
    mainline_out = boundary_flow[1:]
    passable = np.divide(
        mainline_out,
        1.0 - beta,
        out=np.full(size, np.inf),
        where=(1.0 - beta) > EPS,
    )
    # total_out 은 본선과 진출 램프를 **합친** 양이다. 아래에서 한 번만 뺀다.
    total_out = np.minimum(send, passable)
    ramp_out = total_out * beta

    updated = n + boundary_flow[:-1] + ramp_in - total_out

    return StepResult(
        n_veh=updated,
        boundary_flow=boundary_flow,
        ramp_in_veh=ramp_in,
        ramp_out_veh=ramp_out,
    )


def density_veh_km_lane(n_veh: np.ndarray, cells: CellArrays) -> np.ndarray:
    """셀 밀도 (대/km/차로). 기본도와 같은 단위라 정체 판정에 바로 쓴다."""

    return np.asarray(n_veh, dtype=float) / (cells.length_km * cells.lanes)


def speed_kmh(n_veh: np.ndarray, outflow_veh: np.ndarray, cells: CellArrays,
              dt_min: float) -> np.ndarray:
    """셀 평균 속도 = 유량 ÷ 밀도. 빈 셀은 자유속도로 본다.

    밀도로 나누므로 셀에 차가 거의 없으면 값이 튄다. 그래서 빈 셀은 나누지 않고
    자유속도를 쓴다 — 차가 없는 구간은 정의상 막히지 않은 것이다.
    """

    n = np.asarray(n_veh, dtype=float)
    flow_veh_h = np.asarray(outflow_veh, dtype=float) / (dt_min / 60.0)
    density_veh_km = n / cells.length_km

    return np.where(
        density_veh_km > EPS,
        np.divide(flow_veh_h, density_veh_km, out=np.zeros_like(n), where=density_veh_km > EPS),
        cells.v_free_kmh,
    )
