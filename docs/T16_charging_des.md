# T-16 휴게소 충전 큐 DES

`src/evdt/world/sim.py` · 테스트 `tests/test_sim.py` (27건)

```python
from evdt.world.sim import run_charging_des, station_specs, EVArrival

result = run_charging_des(stations, arrivals, snapshot_every_min=5.0)
# -> SimResult(charge_events, snapshots)   둘 다 writers.py 스키마 그대로
```

대기시간이 나오는 곳이다. 여기 숫자가 KPI 의 출발점이고, Sprint 3 의 한계
외부비용이 전부 여기서 파생된다.

## 1. SimPy 는 시간만 굴린다

`simpy.Resource` 를 쓰지 않는다. 쓰면 코드가 짧아지지만, **Resource 는 자기 나름의
FIFO 배정 규칙을 갖고 있다.** 그걸 쓰는 순간 큐 규칙이 두 곳에 생긴다.

우리 규칙은 "유휴 중 최고출력 / 전부 사용 중이면 최단 해제" 라서 Resource 의 규칙과
애초에 다르고, 예약 원장은 Resource 를 쓸 수 없으니 둘이 갈라진다 (설계 규칙 1,
T-15 를 만든 이유 그 자체다).

그래서 SimPy 에게는 `timeout` 만 시킨다.

| 누가 | 무엇을 |
|---|---|
| `queue_rule.assign` | 누가 · 어느 충전기를 · 언제 쓰는가 |
| SimPy | 그 시각까지 시간을 흘려보내는 것 |

`test_sim_does_not_use_simpy_resource` 가 AST 로 `simpy.` 아래 쓰는 이름을 훑어
`Environment` 외에는 못 쓰게 막는다. 주석으로 적어두면 3주 뒤에 누가 Resource 를
쓴다.

## 2. 같은 시각에 도착한 차들은 한 묶음으로

한 대씩 `assign` 을 부르면 SimPy 프로세스가 깨어나는 순서에 따라 동시 도착의 순서가
달라진다. 그러면 원장이 배치로 투영한 결과와 어긋난다.

그래서 같은 `t_arrive_min` 을 가진 차들을 모아 `assign` 을 **한 번** 부른다.
`assign` 은 동시 도착을 ev_id 사전순으로 고정하므로, 결과가 배치 투영과 같아진다
(T-15 `test_incremental_projection_matches_single_shot` 가 그 동치를 고정한다).

## 3. 충전기 대수는 DB 에서 온다

`charger` 한 행 = 같은 사양 `n_units` 기다 (`schema.sql` §4). 대수를 코드에 박으면
휴게소마다 다른 값이 하나로 뭉개지고 대기시간이 통째로 틀린다.

```
io/stations.py  read_station_chargers   DB 행을 그대로 읽는다
world/sim.py    station_specs           행 -> 시뮬레이터 입력
                expand_chargers         n_units 기로 펼친다
```

읽기와 변환을 나눈 이유: `io/` 는 `world/` 를 임포트하지 않는다
(`tests/test_import_boundaries.py`). 그래서 io 는 dict 만 돌려주고, `Charger` 로
바꾸는 일은 world 가 한다.

- 충전기 한 기의 ID 는 `<charger_id>#<번호>` 다. `#` 앞을 자르면 DB 행으로 돌아간다.
- `is_active = 0` 인 행은 제외한다. 없는 충전기로 대기를 계산하면 안 된다.
- 충전기가 한 기도 없는 휴게소는 후보에서 뺀다 (큐 규칙이 0대를 예외로 막는다).

`test_charger_count_comes_from_the_database` 가 임시 DB 에서 `n_units=4` 를 읽어
4대가 동시에 와도 아무도 안 기다리고, 5대째는 기다리는 것까지 확인한다.

## 4. 스냅샷 계약

컬럼은 `writers.py` 의 고정 스키마 그대로다 (설계 규칙 4).

```
(t_min, entity_type, entity_id, lat, lon, state, value)
```

**컬럼은 늘리지 않는다.** T-15 에서 정한 네 지표는 컬럼이 아니라 `state` 값으로
들어간다 — 지표가 늘어도 스키마가 그대로이므로 렌더러를 바꿔도 시뮬레이터를
고치지 않는다.

| `state` | `value` |
|---|---|
| `wait_min` | 지금 도착하면 기다릴 시간(분) — S0(UE) 가 보는 값 |
| `queue_len` | 도착했지만 아직 충전을 시작하지 못한 차 수 |
| `chargers_busy` | 충전 중인 충전기 수 |
| `chargers_total` | 총 충전기 수 |

목록의 원본은 `evdt/interfaces.py` 의 `SNAPSHOT_STATES` 다 (`sim.STATION_SNAPSHOT_STATES`
는 그걸 가리킨다). 로거(`io/event_log.py`)가 쓰기 전에 모든 스냅샷 행이 그 안에 있는지
검사한다 — `docs/event_log.md`.

`wait_min` 은 `queue_rule.wait_if_arriving_now` 가 계산한다. 정책이 보는 값과 실제
배정이 같은 규칙에서 나와야 하기 때문이다 (T-15 §6).

### 시각은 격자에 고정한다

스냅샷의 `t_min` 은 `env.now` 가 아니라 `tick × every_min` 이다. `env.now` 는
timeout 을 더해 나간 값이라 간격이 0.1 처럼 2진수로 안 떨어지면 오차가 누적된다.
하루치를 돌리면 스냅샷이 격자에서 밀려 시간대별 집계에서 경계 행이 옆 칸으로
넘어간다.

(값 자체의 부동소수 오차는 다른 얘기다. SoC 0.2→0.7 이 0.49999999999999994 라
충전시간이 29.999999999999996 분으로 나오는 것은 정상이고 반올림하지 않는다.)

## 5. 시나리오의 queue 설정을 실제로 강제한다

`config/scenario_seollal_down.yaml` 에는 이런 키가 있다.

```yaml
queue:
  discipline: FIFO
  charger_select: MAX_POWER_IDLE
```

그런데 큐 규칙은 `world/queue_rule.py` 에 박혀 있다. 둘이 어긋나면 **설정에 적힌
것과 다른 규칙으로 돈 결과**가 나오고, 그 결과는 재현도 해석도 안 된다. 예를 들어
`discipline: LIFO` 로 고쳐 놓고 FIFO 결과를 LIFO 라고 믿게 된다.

`check_queue_config()` 가 다르면 멈춘다. 규칙을 늘리려면 `queue_rule` 에 구현하고
목록을 늘린다.

## 6. 테스트가 보장하는 것

| 완료조건 | 테스트 |
|---|---|
| 도착률 < 서비스율 → 대기 발산 안 함 | `test_wait_does_not_blow_up_when_arrivals_are_slower_than_service` |
| 도착률 > 서비스율 → 큐 단조증가 | `test_queue_grows_without_bound_when_arrivals_outpace_service` |
| 시드 고정 재현성 | `test_same_seed_gives_identical_runs` |
| t_end > t_start > t_arrive | `test_event_times_are_ordered` |

### "발산하지 않는다" 를 무엇으로 볼 것인가

처음에는 도착을 20분마다 딱딱 끊어 넣었다. 돌려 보니 **대기가 전부 0** 이었다 —
충전기 2기에 30분짜리 차가 20분마다 오면 줄이 설 일이 없다. 아무것도 검사하지 못하는
테스트였다.

대기열이 생기려면 도착이 몰렸다 뜸했다 해야 한다. 그래서 지수 간격(이용률 0.75)으로
바꿨다. 그리고 판정 기준을 평균값이 아니라 **큐가 반복해서 비워지는가** 로 잡았다.
평균 기준은 분포와 시드에 흔들리지만, 안정된 큐는 계속 비워지고 발산하는 큐는 초반
이후 다시는 안 빈다. 시드 3개로 돌린다.

### t_start 는 t_arrive 와 같을 수 있다

완료조건은 `t_end > t_start > t_arrive` 지만, **빈 충전소에 도착하면 대기가 0이라
`t_start == t_arrive`** 다. 여기에 강부등호를 걸면 정상 동작이 실패로 잡힌다.
그래서 테스트는 `t_end > t_start >= t_arrive` 로 검사한다.

강부등호가 성립해야 하는 쪽은 `t_end > t_start` 이고, 이건 **충전할 것이 없는 차를
입력에서 막기 때문에** 항상 참이다 (`soc_target <= soc_in` 이면 예외). 그런 차가
충전소 큐에 있다는 것 자체가 배정 단계의 오류다.

## 7. smoke_run 에서 드러난 것

> **이후 바뀜 (debug/cells):** 아래 표는 가짜 휴게소 3곳(충전기 12기) 기준이다. 지금
> `smoke_run.py` 는 진짜 하행 휴게소 3곳(안성·천안호두·옥천, 28기)을 도로공사 코드로 골라
> 쓰고 N_EV 를 300 으로 잡는다. 결과는 평균 대기 7.4분, 최대 44.8분이다.
> 가짜 휴게소가 진짜 코리도에 섞여 있던 문제는 `docs/fake_data_audit.md`.


`scripts/smoke_run.py` 의 대기시간은 이제 지어낸 숫자가 아니라 DES 결과다.
바꾸고 처음 돌렸더니 **평균 대기 654분, 최대 1840분(30시간)** 이 나왔다.

시뮬레이터 문제가 아니라 **가짜 수요가 용량을 넘긴 것**이었다. 그 3곳의 하루 처리
가능 대수는 약 600대(충전기 12기 × 24시간, −5°C 에서 한 대 28분)인데, 600대를
넣으면서 60% 를 안성(4기)으로 몰고 오전 9시에 집중시켰다. 예전에는 숫자를 지어냈기
때문에 이 모순이 드러날 방법이 없었다.

150대로 낮추니 이런 그림이 나온다.

| 휴게소 | 충전기 | 대수 | 평균 대기 | 최대 대기 |
|---|---|---|---|---|
| 안성(가짜) | 4기 | 93대 | 112.7분 | 212.7분 |
| 천안(가짜) | 2기 | 30대 | 22.5분 | 67.9분 |
| 옥천(가짜) | 6기 | 26대 | **0.0분** | 0.0분 |

이게 이 프로젝트가 풀려는 문제 그 자체다. **한쪽은 두 시간씩 기다리는데 다른 쪽은
6기가 놀고 있다.** S1 이후의 개선은 전부 이 격차를 줄이는 것이다.

수요가 용량을 넘으면(마지막 충전이 시나리오 종료 시각 이후에 끝나면) 스크립트가
경고를 찍는다.

## 8. 넣지 않은 것

- **주차 공간 제약.** 충전기는 비었는데 댈 자리가 없는 상황. `station.n_parking` 은
  DB 에 있지만 쓰지 않는다.
- **중도 이탈(balking·reneging).** 대기가 길면 포기하고 떠나는 행동. 충전소 선택
  단계(T-18)의 문제다.
- **충전소 선택.** DES 는 배정된 대로 돌릴 뿐, 어느 휴게소로 갈지 정하지 않는다.
  그건 정책(T-18)이다. `smoke_run.py` 는 주사위로 정하고, 그렇다고 적어 뒀다.
- **재배정.** `was_reassigned` 는 항상 False 다. 재배정은 S3·S4 의 개념이다.
- **CTM 과의 연결.** 도착 시각은 입력으로 받는다. 주행시간에서 도착을 만드는 것은
  T-19 의 몫이다.

## 9. 다음

- **T-17 이벤트 로거** — 스냅샷 스키마는 §4 로 확정됐다. 이제 바꾸지 않는다.
- **T-18 UE Policy** — §4 의 `wait_min` 을 보고 충전소를 고른다. DES 는 그 결정을
  받아서 돌린다.
- **T-19 엔드투엔드 러너** — 실제 휴게소 19곳(하행)·충전기 153기에 T-07 교통량에서
  만든 도착을 얹어 시드 10회 반복.
