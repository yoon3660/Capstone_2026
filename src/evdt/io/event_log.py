"""이벤트 로거 — 시뮬레이션 결과를 검사해서 Parquet 로 남긴다 (설계문서 T-17).

    (charge_event 행, snapshot 행)  →  검사  →  runs/<run_id>/<table>.parquet

writers.py 는 **컬럼 이름과 타입**만 본다. 그것만으로는 못 잡는 것이 있다:

    - state 오타 ('wait_mins')      → 조용히 저장되고 렌더러에서 빈 칸으로만 보인다
    - 좌표 누락·뒤바뀜 (lat↔lon)     → 지도 밖에 찍히거나 NULL 로 빠진다
    - 시각 순서가 틀린 충전 이벤트    → 음수 대기가 평균을 끌어내린다

분석은 전부 이 로그에서 나온다. 여기서 틀린 행이 들어가면 히트맵(T-20)과 KPI 가
틀린 줄 모르고 틀린다. 그래서 **쓰기 전에 전부 검사하고, 하나라도 틀리면 한 줄도
쓰지 않는다.** 반쯤 써진 run 이 제일 위험하다.

world 를 임포트하지 않는다
    io 는 world 를 임포트할 수 없다 (tests/test_import_boundaries.py). 그래서
    SimResult 대신 행 목록을 받고, 스냅샷 계약은 evdt.interfaces 에서 읽는다.

실행 중에 흘려 쓰지 않고 끝나고 한 번에 쓰는 이유
    world/sim.py 는 IO 를 하지 않으므로 결과를 메모리에 모았다가 돌려준다.
    하루치 경부선 한 방향이 휴게소 ~20곳 × state 4개 × 288 틱 ≈ 2.3만 행,
    충전 이벤트 수만 행이다. 수 MB 라서 흘려 쓸 이유가 없고, 모아서 쓰면
    검사를 쓰기 전에 끝낼 수 있다. 시드 10회(T-19)는 run 마다 따로 쓴다.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evdt.interfaces import (
    SNAPSHOT_COLUMNS,
    SNAPSHOT_LAT_RANGE,
    SNAPSHOT_LON_RANGE,
    SNAPSHOT_STATES,
)
from evdt.io.writers import SCHEMAS, ParquetRunWriter

#: 시각 차이를 비교할 때의 허용 오차 (분). 부동소수 합 오차만 넘긴다.
TIME_TOL_MIN = 1e-9

#: 에러 메시지에 보여줄 최대 행 수. 수만 행이 틀리면 전부 찍어도 읽을 수 없다.
MAX_REPORTED = 5

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


class EventLogError(ValueError):
    """로그에 들어가면 안 되는 행이 있다."""


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def snapshot_row_problems(row: Mapping[str, Any]) -> list[str]:
    """스냅샷 한 행의 문제 목록. 비어 있으면 통과."""

    problems: list[str] = []

    if set(row) != set(SNAPSHOT_COLUMNS):
        return [f"컬럼이 계약과 다름: {sorted(row)} (계약: {list(SNAPSHOT_COLUMNS)})"]

    entity_type = row["entity_type"]
    states = SNAPSHOT_STATES.get(entity_type)

    if states is None:
        problems.append(f"entity_type={entity_type!r} 는 계약에 없음 (가능: {list(SNAPSHOT_STATES)})")
    elif row["state"] not in states:
        problems.append(f"{entity_type} 의 state={row['state']!r} 는 계약에 없음 (가능: {list(states)})")

    if not row["entity_id"]:
        problems.append("entity_id 가 비어 있음")

    if not _finite(row["t_min"]) or row["t_min"] < 0:
        problems.append(f"t_min={row['t_min']!r} 는 0 이상 유한값이어야 함")

    lat, lon = row["lat"], row["lon"]
    lo, hi = SNAPSHOT_LAT_RANGE

    if not _finite(lat) or not lo <= lat <= hi:
        problems.append(f"lat={lat!r} 가 [{lo}, {hi}] 밖 (lat/lon 이 뒤바뀌었는지 확인)")

    lo, hi = SNAPSHOT_LON_RANGE

    if not _finite(lon) or not lo <= lon <= hi:
        problems.append(f"lon={lon!r} 가 [{lo}, {hi}] 밖")

    if not _finite(row["value"]):
        problems.append(f"value={row['value']!r} 는 유한값이어야 함")

    return problems


def charge_event_problems(row: Mapping[str, Any]) -> list[str]:
    """충전 이벤트 한 행의 문제 목록. 비어 있으면 통과.

    도착 ≤ 시작 ≤ 종료, 대기 = 시작 − 도착, 체류 = 대기 + 충전, SoC 가 오르는지.
    이 관계가 깨지면 대기 평균이 틀린다.
    """

    expected = set(SCHEMAS["charge_event"].names) - {"run_id"}
    missing = sorted(expected - set(row))

    if missing:
        return [f"컬럼 누락: {missing}"]

    problems: list[str] = []

    for key in ("ev_id", "station_id", "charger_id"):
        if not row[key]:
            problems.append(f"{key} 가 비어 있음")

    times = ("t_arrive_min", "t_start_min", "t_end_min", "wait_min", "charge_min", "dwell_min")
    bad = [k for k in times if not _finite(row[k])]

    if bad:
        return [*problems, f"시각이 유한값이 아님: {bad}"]

    arrive, start, end = row["t_arrive_min"], row["t_start_min"], row["t_end_min"]

    if arrive < 0:
        problems.append(f"t_arrive_min={arrive} 가 음수")

    if not arrive <= start + TIME_TOL_MIN:
        problems.append(f"도착({arrive}) 전에 충전 시작({start})")

    if not start < end:
        problems.append(f"충전 시작({start}) 이 종료({end}) 보다 늦거나 같음")

    if abs(row["wait_min"] - (start - arrive)) > TIME_TOL_MIN:
        problems.append(f"wait_min={row['wait_min']} ≠ 시작−도착={start - arrive}")

    if abs(row["charge_min"] - (end - start)) > TIME_TOL_MIN:
        problems.append(f"charge_min={row['charge_min']} ≠ 종료−시작={end - start}")

    if abs(row["dwell_min"] - (row["wait_min"] + row["charge_min"])) > TIME_TOL_MIN:
        problems.append("dwell_min ≠ wait_min + charge_min")

    soc_in, soc_out = row["soc_in"], row["soc_out"]

    if not (_finite(soc_in) and _finite(soc_out) and 0.0 <= soc_in < soc_out <= 1.0):
        problems.append(f"SoC 가 0 ≤ soc_in < soc_out ≤ 1 이 아님: {soc_in} → {soc_out}")

    if not _finite(row["power_kw"]) or row["power_kw"] <= 0:
        problems.append(f"power_kw={row['power_kw']!r} 는 양수여야 함")

    return problems


def _check_all(
    table: str,
    rows: Sequence[Mapping[str, Any]],
    check,
    key: str,
) -> None:
    bad: list[str] = []
    n_bad = 0

    for i, row in enumerate(rows):
        problems = check(row)

        if problems:
            n_bad += 1

            if len(bad) < MAX_REPORTED:
                bad.append(f"  [{i}] {row.get(key)!r}: " + "; ".join(problems))

    if n_bad:
        more = f"\n  … 외 {n_bad - len(bad)}행" if n_bad > len(bad) else ""
        raise EventLogError(
            f"{table} {len(rows)}행 중 {n_bad}행이 계약을 어김. 한 줄도 쓰지 않았다.\n"
            + "\n".join(bad)
            + more
        )


def check_snapshots(rows: Sequence[Mapping[str, Any]]) -> None:
    _check_all("snapshot", rows, snapshot_row_problems, "entity_id")


def check_charge_events(rows: Sequence[Mapping[str, Any]]) -> None:
    _check_all("charge_event", rows, charge_event_problems, "ev_id")

    counts = Counter(r["ev_id"] for r in rows)
    dup = sorted(ev_id for ev_id, n in counts.items() if n > 1)

    # 지금은 차 한 대가 한 번 충전한다. 두 번 충전을 모델링하면 이 검사를 (ev_id, 순번) 으로 바꾼다.
    if dup:
        raise EventLogError(f"같은 ev_id 가 두 번 충전함: {dup[:MAX_REPORTED]}")


# ---------------------------------------------------------------------------
# 쓰기
# ---------------------------------------------------------------------------


def log_sim_result(
    writer: ParquetRunWriter,
    charge_events: Iterable[Mapping[str, Any]],
    snapshots: Iterable[Mapping[str, Any]],
    *,
    write_snapshots: bool = True,
) -> dict[str, int]:
    """DES 결과를 검사한 뒤 writer 에 넘긴다. 테이블별로 넘긴 행 수를 돌려준다.

        result = run_charging_des(stations, arrivals, ...)
        with RunContext.open(cfg, seed=7) as run:
            log_sim_result(run.writer, result.charge_events, result.snapshots,
                           write_snapshots=cfg.output.write_snapshots)

    두 테이블을 **모두 검사한 다음에** 쓴다. 스냅샷이 틀렸는데 충전 이벤트만
    써진 run 을 남기지 않기 위해서다. 예외가 나면 RunContext 가 run 을
    FAILED 로 기록한다.

    write_snapshots=False 여도 스냅샷은 검사한다. 끈 설정으로 버그를 숨기지 않는다.
    """

    events = [dict(r) for r in charge_events]
    snaps = [dict(r) for r in snapshots]

    check_charge_events(events)
    check_snapshots(snaps)

    writer.append_many("charge_event", events)

    if write_snapshots:
        writer.append_many("snapshot", snaps)

    return {"charge_event": len(events), "snapshot": len(snaps) if write_snapshots else 0}


# ---------------------------------------------------------------------------
# 읽기 — 저장해 둔 분석 SQL
# ---------------------------------------------------------------------------


def load_sql(name: str) -> str:
    """src/evdt/sql/<name>.sql 을 읽는다. 분석 SQL 을 코드 문자열로 흩뜨리지 않는다."""

    path = SQL_DIR / f"{name}.sql"

    if not path.is_file():
        known = sorted(p.stem for p in SQL_DIR.glob("*.sql"))
        raise FileNotFoundError(f"SQL 이 없다: {path.name} (있는 것: {known})")

    return path.read_text(encoding="utf-8")
