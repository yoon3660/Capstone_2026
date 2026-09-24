"""CTM 을 하루 돌리고 기록을 남긴다 (#56).

`ctm.step` 은 한 스텝만 안다. 하루를 돌리면서

    - `cell_state` 행 (5분 집계)
    - 스냅샷 행 (`entity_type="cell"`)
    - 통행시간용 속도 격자 (`travel.CellSpeedField`)

를 같이 만드는 것이 여기 일이다.

왜 5분으로 집계하나
    dt 는 0.2분이라 하루 7,200 스텝이다. 셀 396개면 스텝마다 남기면 285만 행이
    된다. 5분 간격이면 11만 행으로 줄고, 대기 히트맵(5분 칸)과 가로축이 맞는다.
    집계는 **평균**이다 — 5분 중 한 스텝만 막힌 것과 내내 막힌 것이 달라야 한다.
    한 칸의 마지막 값만 쓰면 둘이 같아 보인다.

속도 격자와 cell_state 는 같은 집계를 쓴다
    통행시간이 보는 속도와 기록에 남는 속도가 다르면, 나중에 "이 차가 왜 이때
    도착했나" 를 기록으로 따라갈 수 없다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from evdt.world.ctm import CellArrays, StepResult, check_cfl, density_veh_km_lane, speed_kmh, step
from evdt.world.travel import CellSpeedField

#: 기록·속도 격자의 집계 간격 (분). 대기 히트맵과 같은 칸이다.
RECORD_EVERY_MIN = 5.0


@dataclass
class _Window:
    """집계 한 칸. 합을 들고 있다가 끝날 때 평균으로 바꾼다."""

    steps: int = 0
    speed: np.ndarray | None = None
    density: np.ndarray | None = None
    flow: np.ndarray | None = None
    ev: np.ndarray | None = None

    def add(self, speed, density, flow, ev) -> None:
        if self.speed is None:
            self.speed, self.density, self.flow, self.ev = (
                speed.copy(), density.copy(), flow.copy(), ev.copy()
            )
        else:
            self.speed += speed
            self.density += density
            self.flow += flow
            self.ev += ev
        self.steps += 1

    def mean(self) -> tuple[np.ndarray, ...]:
        n = max(self.steps, 1)
        return self.speed / n, self.density / n, self.flow / n, self.ev / n


@dataclass(frozen=True)
class CTMRun:
    """하루치 결과."""

    cell_state_rows: tuple[dict, ...]
    snapshot_rows: tuple[dict, ...]
    speed_field: CellSpeedField
    entered_veh: float
    left_veh: float
    remaining_veh: float

    @property
    def conservation_error_veh(self) -> float:
        """들어온 차 − 나간 차 − 남은 차. 0 이어야 한다."""

        return self.entered_veh - self.left_veh - self.remaining_veh


def run_day(
    cells: CellArrays,
    cell_rows: Sequence[dict],
    dt_min: float,
    *,
    inflow_veh_per_step,
    horizon_min: float = 24 * 60.0,
    ramp_demand_veh: np.ndarray | None = None,
    exit_ratio: np.ndarray | None = None,
    ramp_demand_per_step=None,
    exit_ratio_per_step=None,
    ev_count_per_cell=None,
    record_every_min: float = RECORD_EVERY_MIN,
) -> CTMRun:
    """하루를 돌린다.

    cell_rows
        `io.cells.read_cells` 가 준 행. cell_id 와 좌표를 여기서 꺼내 쓴다.
    inflow_veh_per_step
        `f(t_min) -> 이번 스텝에 코리도 시작점으로 들어오려는 대수`.
    ramp_demand_veh / exit_ratio
        시각에 무관한 램프. 시간대별로 바꾸려면 `*_per_step` 에 `f(t_min) -> 배열` 을
        준다 — #54 의 중간 진입·진출 표가 이쪽으로 들어온다. 둘 다 주면 per_step 이 이긴다.
    ev_count_per_cell
        `f(t_min) -> 셀마다 그 시각의 합성 EV 수`. 없으면 0 으로 둔다 (배경 교통만
        도는 경우). EV 이동은 UE 가 통행시간으로 계산하므로, 여기서는 **보여주기용**
        숫자다 — CTM 의 물리에는 들어가지 않는다.
    """

    check_cfl(cells, dt_min)

    if len(cell_rows) != len(cells):
        raise ValueError(f"cell_rows 와 셀 수가 다르다: {len(cell_rows)} vs {len(cells)}")

    n = np.zeros(len(cells))
    zeros = np.zeros(len(cells))
    steps = int(round(horizon_min / dt_min))
    per_window = max(int(round(record_every_min / dt_min)), 1)

    window = _Window()
    windows_done = 0
    cell_state: list[dict] = []
    snapshots: list[dict] = []
    speed_rows: list[np.ndarray] = []
    entered = left = 0.0
    result: StepResult | None = None

    for index in range(steps):
        t_min = index * dt_min

        result = step(
            n, cells, dt_min,
            inflow_veh=float(inflow_veh_per_step(t_min)),
            ramp_demand_veh=(ramp_demand_veh if ramp_demand_per_step is None
                             else ramp_demand_per_step(t_min)),
            exit_ratio=(exit_ratio if exit_ratio_per_step is None
                        else exit_ratio_per_step(t_min)),
        )
        n = result.n_veh
        entered += result.boundary_flow[0] + result.ramp_in_veh.sum()
        left += result.boundary_flow[-1] + result.ramp_out_veh.sum()

        outflow = result.boundary_flow[1:]
        window.add(
            speed_kmh(n, outflow, cells, dt_min),
            density_veh_km_lane(n, cells) * cells.lanes,      # 셀 전체 밀도 (대/km)
            outflow / (dt_min / 60.0),                        # 대/h
            zeros if ev_count_per_cell is None else np.asarray(ev_count_per_cell(t_min), dtype=float),
        )

        if window.steps == per_window:
            # 칸이 끝난 시각은 스텝을 더해서 내지 않는다. 0.2분을 25번 더하면
            # 5.000000000000001 이 되고, 그 값이 그대로 기록에 남아 나중에
            # 시간대별로 묶을 때 칸이 어긋난다.
            windows_done += 1
            _flush(window, windows_done * record_every_min,
                   cell_rows, cell_state, snapshots, speed_rows)
            window = _Window()

    if window.steps:
        _flush(window, horizon_min, cell_rows, cell_state, snapshots, speed_rows)

    edges = np.array(
        [float(cell_rows[0]["offset_km_start"])] + [float(r["offset_km_end"]) for r in cell_rows]
    )

    return CTMRun(
        cell_state_rows=tuple(cell_state),
        snapshot_rows=tuple(snapshots),
        speed_field=CellSpeedField(edges, np.vstack(speed_rows), record_every_min),
        entered_veh=entered,
        left_veh=left,
        remaining_veh=float(n.sum()),
    )


def _flush(window, t_min, cell_rows, cell_state, snapshots, speed_rows) -> None:
    speed, density, flow, ev = window.mean()
    speed_rows.append(speed)

    for i, row in enumerate(cell_rows):
        cell_state.append({
            "t_min": float(t_min),
            "cell_id": row["cell_id"],
            "density_veh_km": float(density[i]),
            "flow_veh_h": float(flow[i]),
            "speed_kmh": float(speed[i]),
        })

        # 셀은 선분이라 점 하나로 줄여야 한다. 시작점을 쓴다 (끝점은 이웃과 겹친다)
        base = {
            "t_min": float(t_min),
            "entity_type": "cell",
            "entity_id": row["cell_id"],
            "lat": float(row["lat_start"]),
            "lon": float(row["lon_start"]),
        }
        for state, value in (
            ("speed_kmh", speed[i]),
            ("density_veh_km", density[i]),
            ("flow_veh_h", flow[i]),
            ("ev_count", ev[i]),
        ):
            snapshots.append({**base, "state": state, "value": float(value)})
