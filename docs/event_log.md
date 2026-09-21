# 이벤트 로거와 Parquet 출력 (설계문서 T-17 · 이슈 #41)

> 번호 주의: 팀 이슈 #17 은 CTM 셀 분할(`docs/T17_cells.md`)이다. 이 문서는
> **설계문서 T-17** 이고 브랜치는 `develop/41` 이다.

`src/evdt/io/event_log.py` · 저장 쿼리 `src/evdt/sql/station_hourly_*.sql` ·
테스트 `tests/test_event_log.py` (37건)

```python
result = run_charging_des(stations, arrivals, snapshot_every_min=5.0)

with RunContext.open(cfg, seed=7) as run:
    log_sim_result(run.writer, result.charge_events, result.snapshots,
                   write_snapshots=cfg.output.write_snapshots)
```

## 1. 완료 기준 — SQL 한 줄

휴게소별 · 시간대별 평균 대기:

```sql
SELECT run_id, station_id, CAST(FLOOR(t_arrive_min / 60) AS INTEGER) AS hour, COUNT(*) AS n_ev, AVG(wait_min) AS mean_wait_min FROM charge_event GROUP BY ALL ORDER BY ALL
```

```python
from evdt.io.event_log import load_sql
from evdt.io.loaders import duck_connect

con = duck_connect(run_ids=[run_id])          # runs/<run_id>/*.parquet 를 뷰로 올린다
con.sql(load_sql("station_hourly_wait")).df()
```

`test_station_hourly_wait_matches_hand_calculation` 이 손으로 계산한 값과 비교한다
(충전기 1기, 도착 10·20·59.9·60.0 분 → 0시 평균 10.03분, 1시 40분).

쿼리는 코드 문자열이 아니라 `src/evdt/sql/` 에 파일로 둔다. 스크립트마다 집계를
다시 쓰면 두 벌이 갈라진다 — 실제로 갈라져 있었다 (아래 §4).

### 대기가 두 종류다

| 쿼리 | 원천 | 뜻 | 차가 없는 시간대 |
|---|---|---|---|
| `station_hourly_wait` | `charge_event.wait_min` | 그 시간에 **도착한 차가 실제로 기다린** 시간 | 행 없음 (관측 없음) |
| `station_hourly_shown_wait` | `snapshot` `state='wait_min'` | 그 시각에 도착했다면 기다렸을 시간 = **S0(UE)가 보는 값** | 0 으로 행 있음 |

T-20 히트맵은 앞의 것이 기본이다. 둘의 차이가 "표시된 대기가 얼마나 빗나갔는가" 이고,
S0 쏠림을 설명할 때 쓴다.

여러 run 을 한꺼번에 올려도 `run_id` 로 갈린다 (`test_query_keeps_runs_apart`).
시드 10회(T-19)를 쿼리 한 번으로 본다.

## 2. 스냅샷 스트림 계약 확정

컬럼·entity_type·state·좌표 범위를 `evdt/interfaces.py` 한 곳에 둔다.

```
SNAPSHOT_COLUMNS   (t_min, entity_type, entity_id, lat, lon, state, value)
SNAPSHOT_STATES    {"station": (wait_min, queue_len, chargers_busy, chargers_total)}
SNAPSHOT_LAT_RANGE (33, 39)   SNAPSHOT_LON_RANGE (124, 132)   ← schema.sql CHECK 와 같다
```

왜 interfaces 인가: 시뮬레이터(world)가 쓰고, 로거(io)가 검사하고, 렌더러(viz)가 읽는다.
그런데 io·viz 는 world 를 임포트할 수 없다 (`test_import_boundaries.py`). 전에는 목록이
`world/sim.py` 에 있어서 로거도 렌더러도 볼 수 없었다. 이제 `sim.STATION_SNAPSHOT_STATES`
는 interfaces 의 목록 그 자체다 (`is` 로 테스트).

- `t_min` 은 시뮬레이션 기준일 00:00 부터의 분이다 (시나리오 `time.start_min` 과 같은 축).
- 위경도가 스냅샷 행마다 들어 있어서 렌더러는 master DB 없이 지도에 찍는다.
- `cell`·`vehicle` 은 아직 state 가 없다 → **지금 쓰면 거부된다.** Sprint 2 에서 CTM·차량
  스냅샷을 붙일 때 `SNAPSHOT_STATES` 에 추가한다. 컬럼은 늘리지 않는다 (설계 규칙 4).

## 3. 로거가 막는 것

`writers.py` 는 컬럼 이름과 타입만 본다. 그걸로는 못 잡는 것:

| 틀린 행 | 검사 없이 저장되면 |
|---|---|
| state 오타 `wait_mins` | 조용히 저장되고 렌더러에서 빈 칸으로만 보인다 |
| lat↔lon 뒤바뀜, NULL, NaN | 지도 밖에 찍히거나 집계에서 빠진다 |
| 도착 전에 충전 시작, `wait ≠ 시작−도착` | 음수 대기가 평균을 끌어내린다 |
| `dwell ≠ wait + charge`, SoC 가 안 오름, 출력 없음 | 사적 비용(PC)이 틀린다 |
| 같은 ev_id 두 번 | 대수가 부풀어 평균이 흔들린다 |

**두 테이블을 모두 검사한 다음에 쓴다. 하나라도 틀리면 한 줄도 쓰지 않는다.**
충전 이벤트만 써지고 스냅샷이 빠진 run 이 제일 위험하다 — 겉보기엔 멀쩡하다.
예외는 `RunContext` 가 받아서 run 을 `FAILED` 로 남긴다.

`write_snapshots: false` 여도 스냅샷은 검사한다. 끈 설정으로 버그를 숨기지 않는다.
에러 메시지는 틀린 행 5개까지만 보여주고 나머지는 개수로 적는다.

## 4. 고친 것 — 시간대 칸이 반올림되고 있었다

`scripts/smoke_run.py` 의 예전 쿼리:

```sql
CAST(e.t_arrive_min / 60 AS INTEGER) AS 시각
```

DuckDB 의 `CAST(double AS INTEGER)` 는 **버림이 아니라 반올림**이다. 9:45 도착이
10시 칸에 들어갔다. 스모크 결과 표에서 "안성 9시 36.3분" 이던 칸이 실제로는
8시 30분 ~ 9시 30분이 섞인 값이었다. 저장 쿼리는 `FLOOR` 를 쓰고,
`test_hour_bucket_floors_instead_of_rounding` 이 이 함정을 고정한다.
smoke_run 도 이제 저장 쿼리를 감싸서 휴게소 이름만 붙인다 (결과: 안성 **8시** 36.3분).

## 5. 실행 중에 흘려 쓰지 않는 이유

`world/sim.py` 는 IO 를 하지 않으므로 결과를 모았다가 한 번에 돌려준다. 이걸 그대로 둔다.

- 하루치 경부선 한 방향: 휴게소 ~20곳 × state 4개 × 288 틱 ≈ 2.3만 행 + 충전 이벤트
  수만 행. 수 MB 다. (스모크: 3곳 299대 → charge_event 27 KB, snapshot 5 KB)
- 모아서 쓰면 **쓰기 전에** 검사를 끝낼 수 있다 (§3 의 "한 줄도 안 쓴다" 가 가능해진다).
- 시드 10회는 run 마다 따로 쓰므로 메모리가 누적되지 않는다.

차량 스냅샷(Sprint 2, 수천 대 × 틱)을 붙이면 이 가정이 깨질 수 있다. 그때 sim 에
sink 를 주입하는 방식으로 바꾸되, 검사는 `snapshot_row_problems` 를 그대로 쓴다.

## 6. 테스트가 보장하는 것

| 무엇 | 테스트 |
|---|---|
| SQL 한 줄 = 손 계산 | `test_station_hourly_wait_matches_hand_calculation` |
| 시간대는 버림 | `test_hour_bucket_floors_instead_of_rounding` |
| run 이 섞이지 않음 | `test_query_keeps_runs_apart` |
| 표시 대기 쿼리 = 스냅샷 평균 | `test_shown_wait_query_reads_the_snapshot_stream` |
| 스냅샷에 좌표 | `test_snapshot_parquet_carries_coordinates` |
| 저장 쿼리는 한 줄 | `test_saved_query_is_one_line` |
| 계약은 한 곳 | `test_writer_snapshot_schema_follows_the_interfaces_contract`, `test_sim_station_states_are_the_interfaces_contract` |
| DES 출력은 검사 통과 | `test_des_output_passes_the_logger_checks` |
| 틀린 행은 이유와 함께 거부 | `test_bad_snapshot_rows_are_named` (9), `test_bad_charge_events_are_named` (9), 중복 ev_id |
| 틀리면 아무것도 안 씀 | `test_nothing_is_written_when_any_snapshot_is_bad` |
| 꺼도 검사함 | `test_snapshots_are_checked_even_when_not_written` |
