"""Parquet 이벤트 로그 writer (T-04).

왜 스키마를 코드에 박아두는가
    pandas.to_parquet 에 맡기면 어떤 실행에서는 컬럼이 float64, 다른 실행에서는
    전부 NULL 이라 object 로 추론된다. 나중에 여러 run 을 한꺼번에 읽을 때
    타입 충돌로 깨진다. 그래서 pyarrow 스키마를 여기서 명시적으로 고정한다.

⚠ snapshot 스키마는 설계 규칙 4다 (설계문서 §4.1).
    (t_min, entity_type, entity_id, lat, lon, state, value)
    시뮬레이터와 뷰어 사이의 유일한 계약이다. 렌더러를 바꿔도 시뮬레이터는
    고치지 않는다. 컬럼을 추가하고 싶으면 value 를 쓰거나 새 entity_type 을 만든다.
    허용 entity_type·state 는 evdt.interfaces.SNAPSHOT_STATES, 값 검사는
    evdt.io.event_log 가 한다 (이 writer 는 컬럼 이름과 타입만 본다).

사용법
    with ParquetRunWriter(run_dir, run_id) as w:
        w.append("charge_event", {...})
        w.append_many("snapshot", rows)
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

_STR = pa.string()
_F64 = pa.float64()
_I64 = pa.int64()
_I32 = pa.int32()
_BOOL = pa.bool_()


def _schema(*fields: tuple[str, pa.DataType]) -> pa.Schema:
    # run_id 를 모든 테이블에 넣는다. 여러 run 의 parquet 를 한 번에 읽을 때
    # 어느 실행에서 온 행인지 파일 경로를 파싱하지 않고 알 수 있다.
    return pa.schema([pa.field("run_id", _STR, nullable=False), *(pa.field(n, t) for n, t in fields)])


#: 이벤트 로그 테이블 스키마 (설계문서 §9.3)
SCHEMAS: dict[str, pa.Schema] = {
    # 차량 한 대의 충전 정차 한 번 = 한 행 (한 차가 여러 번 서면 stop_seq 로 구분)
    "charge_event": _schema(
        ("ev_id", _STR),
        ("stop_seq", _I32),          # 이 차의 몇 번째 충전 정차인가 (1부터). 장거리는 2회 이상
        ("vclass_id", _STR),
        ("station_id", _STR),
        ("charger_id", _STR),
        ("power_kw", _F64),          # 실제로 배정된 충전기 출력
        ("t_arrive_min", _F64),
        ("t_start_min", _F64),
        ("t_end_min", _F64),
        ("wait_min", _F64),          # t_start - t_arrive
        ("charge_min", _F64),        # t_end - t_start
        ("dwell_min", _F64),         # 체류시간 = wait + charge  (사적 비용의 기본 단위)
        ("soc_in", _F64),
        ("soc_out", _F64),
        ("soc_target", _F64),
        ("energy_kwh", _F64),
        ("cold_factor", _F64),       # 저온 감쇠 계수
        ("was_reassigned", _BOOL),   # 최초 배정과 다른 곳에 갔는가
    ),
    # 엔진이 내린 배정 결정 한 건 = 한 행 (UE 는 "운전자의 선택"으로 기록)
    "assignment": _schema(
        ("t_min", _F64),
        ("ev_id", _STR),
        ("station_id", _STR),
        ("stage", _STR),
        ("window_id", _I64),
        ("private_cost_min", _F64),  # 사적 비용 PC (§6.1)
        ("mec_min", _F64),           # 한계 외부비용 MEC
        ("social_cost_min", _F64),   # SC = PC + MEC
        ("n_candidates", _I32),
        ("is_participant", _BOOL),
    ),
    # 코리도를 벗어나 시내에서 충전한 차 (#54). 평균 대기만 보면 이 차들이 빠져서
    # 좋아 보이므로, **어디서 왜 나갔는지**를 남겨야 착시를 막을 수 있다.
    "escape_event": _schema(
        ("ev_id", _STR),
        ("vclass_id", _STR),
        ("entry_time_min", _F64),
        ("entry_offset_km", _F64),
        ("dest_offset_km", _F64),
        ("escape_cost_min", _F64),
        # 왜 나갔나. 둘을 반드시 갈라서 본다 (#54)
        #   'no_plan'  사거리 안에 휴게소가 없다 — **엔진이 못 고친다** (증설·SoC 문제)
        #   'balked'   갈 수는 있는데 줄이 이탈 비용보다 길다 — **엔진이 고칠 수 있다**
        ("reason", _STR),
        # 코리도 안에 남았다면 갔을 곳 (no_plan 이면 빈 문자열)
        ("best_station_id", _STR),
    ),
    # CTM 셀 상태 시계열 (Sprint 2부터 채워진다)
    "cell_state": _schema(
        ("t_min", _F64),
        ("cell_id", _STR),
        ("density_veh_km", _F64),
        ("flow_veh_h", _F64),
        ("speed_kmh", _F64),
    ),
    # 재계획 윈도우별 솔버 로그 (§8.1 엔진 품질 지표가 여기서 나온다)
    "solver_log": _schema(
        ("window_id", _I64),
        ("t_min", _F64),
        ("n_vehicles", _I32),
        ("n_stations", _I32),
        ("n_binaries", _I64),
        ("status", _STR),            # 'Optimal' | 'Not Solved' | ...
        ("objective", _F64),
        ("dual_bound", _F64),
        ("mip_gap", _F64),
        ("solve_time_s", _F64),
        ("time_limit_hit", _BOOL),
        ("fallback_used", _BOOL),
        # 반복 솔버(UE 균형)의 반복 번호와 상대 gap. UE 는 이 두 칸만 채운다.
        # mip_gap 과 섞지 않는 이유: 뜻이 다르다 (최적성 하한 대비 vs 균형 조건 위반량).
        ("iteration", _I32),
        ("rel_gap", _F64),
    ),
    # ⚠ 설계 규칙 4 — 시뮬레이터와 렌더러 사이의 계약. 함부로 바꾸지 말 것.
    "snapshot": _schema(
        ("t_min", _F64),
        ("entity_type", _STR),       # 'station' | 'cell' | 'vehicle'
        ("entity_id", _STR),
        ("lat", _F64),
        ("lon", _F64),
        ("state", _STR),             # 허용 값은 evdt.interfaces.SNAPSHOT_STATES
        ("value", _F64),
    ),
}

TABLES: tuple[str, ...] = tuple(SCHEMAS)


class ParquetRunWriter:
    """한 run 의 모든 이벤트 로그를 runs/<run_id>/<table>.parquet 로 쓴다.

    - 행을 버퍼에 모았다가 batch_size 마다 flush 한다 (행마다 쓰면 느리다).
    - 한 번도 쓰지 않은 테이블은 파일을 만들지 않는다.
    - with 문을 쓰면 종료 시 자동으로 flush + close 된다.
    """

    def __init__(
        self,
        run_dir: str | Path,
        run_id: str,
        *,
        batch_size: int = 20_000,
        compression: str = "zstd",
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.batch_size = batch_size
        self.compression = compression
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self._buffers: dict[str, list[dict[str, Any]]] = {t: [] for t in TABLES}
        self._writers: dict[str, pq.ParquetWriter] = {}
        self._counts: dict[str, int] = dict.fromkeys(TABLES, 0)
        self._closed = False

    # -- 쓰기 ---------------------------------------------------------------
    def append(self, table: str, row: dict[str, Any]) -> None:
        """한 행 추가. 스키마에 없는 컬럼은 즉시 에러."""
        self._check_table(table)
        allowed = set(SCHEMAS[table].names)
        unknown = [k for k in row if k not in allowed]
        if unknown:
            raise KeyError(
                f"{table} 스키마에 없는 컬럼: {unknown}\n  사용 가능: {sorted(allowed - {'run_id'})}"
            )
        buf = self._buffers[table]
        buf.append({"run_id": self.run_id, **row})
        if len(buf) >= self.batch_size:
            self.flush(table)

    def append_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.append(table, r)

    def snapshot(
        self,
        t_min: float,
        entity_type: str,
        entity_id: str,
        lat: float,
        lon: float,
        state: str,
        value: float,
    ) -> None:
        """설계 규칙 4의 스냅샷 한 줄. 인자 순서가 곧 스키마다."""
        self.append(
            "snapshot",
            {
                "t_min": float(t_min),
                "entity_type": entity_type,
                "entity_id": entity_id,
                "lat": float(lat),
                "lon": float(lon),
                "state": state,
                "value": float(value),
            },
        )

    # -- flush / close ------------------------------------------------------
    def flush(self, table: str | None = None) -> None:
        for name in (TABLES if table is None else (table,)):
            self._check_table(name)
            rows = self._buffers[name]
            if not rows:
                continue
            schema = SCHEMAS[name]
            # 누락된 컬럼은 None 으로 채운다 (스키마가 타입을 고정하므로 안전)
            filled = [{col: r.get(col) for col in schema.names} for r in rows]
            batch = pa.Table.from_pylist(filled, schema=schema)
            writer = self._writers.get(name)
            if writer is None:
                writer = pq.ParquetWriter(
                    self.run_dir / f"{name}.parquet", schema, compression=self.compression
                )
                self._writers[name] = writer
            writer.write_table(batch)
            self._counts[name] += len(rows)
            rows.clear()

    def close(self) -> dict[str, int]:
        """flush 후 파일을 닫는다. 테이블별 기록 행 수를 반환."""
        if self._closed:
            return dict(self._counts)
        self.flush()
        for w in self._writers.values():
            w.close()
        self._writers.clear()
        self._closed = True
        return dict(self._counts)

    @property
    def counts(self) -> dict[str, int]:
        return {t: self._counts[t] + len(self._buffers[t]) for t in TABLES}

    def _check_table(self, table: str) -> None:
        if table not in SCHEMAS:
            raise KeyError(f"알 수 없는 테이블: {table!r}  (가능: {list(TABLES)})")

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> ParquetRunWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
