# T-15 큐 규칙 단일 모듈

`src/evdt/world/queue_rule.py` · 테스트 `tests/test_queue_rule.py` (26건)

## 1. 왜 DES 보다 먼저 만드는가

설계 규칙 1: **큐 규칙은 시뮬레이터와 예약 원장이 공유하는 단일 모듈이다.**

DES 를 먼저 만들면 큐 규칙이 SimPy 프로세스 안에 자연스럽게 녹아든다. 그다음 원장이
같은 규칙을 필요로 할 때 복사하게 된다. 그 순간 규칙이 두 곳에 생기고, 한쪽만 고치는
일이 반드시 벌어진다. 시뮬레이터가 계산한 대기시간과 원장이 약속한 대기시간이
달라지면 한계 외부비용 계산이 통째로 오염된다.

순서를 뒤집으면 복사할 것이 없다. 그래서 모듈이 먼저다.

## 2. 인터페이스

```python
from evdt.world.queue_rule import Arrival, Charger, assign

assignments = assign(arrivals, chargers)
# -> (Assignment(ev_id, charger_id, arrival_min, start_min, end_min, wait_min), ...)
```

| 타입 | 담는 것 |
|---|---|
| `Charger` | `charger_id`, `power_kw`, `available_from_min`(이미 물려 있는 차가 끝나는 시각) |
| `Arrival` | `ev_id`, `arrival_min`, 그리고 충전 요구 (`soc_from`, `soc_to`, `battery_kwh`, `vmax_kw`, `curve`, `charge_power_factor`) |
| `Assignment` | 배정된 충전기와 시작·종료·대기 시각(분) |

`Arrival` 이 **소요시간이 아니라 충전 요구**를 담는 이유: 점유시간이 충전기 출력에
따라 달라진다. 같은 차가 50 kW 와 200 kW 에서 다른 시간을 쓴다. 그래서 배정 전에는
소요시간을 확정할 수 없고, 배정이 끝난 뒤 `charge_time_min`(T-14)으로 계산한다.
점유시간도 한 곳에서만 계산돼야 두 호출자가 같은 답을 낸다.

보조 함수

| 함수 | 하는 일 |
|---|---|
| `arrival_order` | 규칙 1 (FIFO 정렬) |
| `choose_charger` | 규칙 2·3 (충전기 선택) |
| `service_minutes` | 이 차 × 이 충전기의 점유시간 |
| `chargers_after` | 배정 결과를 반영한 충전기 상태. 원장이 다음 Δt 로 넘길 때 |

## 3. 규칙 (설계문서 §7.2 규칙 4)

1. **도착 순서**: FIFO. 같은 시각이면 `ev_id` 사전순.
2. **배정**: 도착 시점에 비어 있는 충전기 중 **최고출력**.
3. **전부 사용 중이면**: **가장 빨리 비는** 충전기를 기다린다. 동률이면 고출력.
4. **대기시간** = 시작 시각 − 도착 시각.

동시 도착의 순서는 물리적으로 정해지지 않는다. 그래도 `ev_id` 사전순으로 **고정**
하는 이유는, 입력 순서에 따라 답이 달라지면 시뮬레이터와 원장이 같은 상태를 다른
순서로 담았을 때 다른 답을 내기 때문이다. 임의여도 결정적이어야 한다.

규칙 3 은 "가장 빨리 **비는**" 쪽이지 "가장 빨리 **끝나는**" 쪽이 아니다. 고출력
충전기가 조금 늦게 비어도 충전이 빨라 총 소요가 짧을 수 있지만, 그건 다른 규칙이다.
설계문서에 적힌 규칙을 그대로 구현했다. 바꾸려면 설계문서부터 고칠 것.

## 4. 호출자는 정확히 두 곳

```
1. 시뮬레이터   world/sim.py        (T-16, SimPy DES)
2. 예약 원장    engine/ledger.py
```

주석으로만 적어두면 3주 뒤에 누군가 편의상 한 줄 임포트한다. 그래서
`test_queue_rule_has_at_most_two_callers` 가 `src/evdt` 전체를 AST 로 훑어 세 번째
호출자가 생기면 실패한다. 지금은 호출자가 0곳이다 (T-16 이 첫 호출자가 된다).

같은 방식으로 `test_queue_rule_does_not_import` 이 `simpy` 와 `evdt.engine` 임포트를
막는다. SimPy 에 종속되면 원장이 이 모듈을 쓸 수 없고, `engine` 에 종속되면 설계
규칙 2 위반이다.

## 5. 이 모듈이 보장하는 것

| 보장 | 테스트 |
|---|---|
| 충전기 1대 · 3대 동시 도착 → 대기 0, t1, t1+t2 | `test_three_arrivals_one_charger_wait_is_cumulative` |
| 충전기 n대 · n대 도착 → 전원 대기 0 | `test_n_chargers_n_arrivals_all_wait_zero` |
| 입력 순서를 섞어도 같은 결과 (순열 24가지) | `test_fifo_result_is_independent_of_input_order` |
| 같은 입력 → 같은 출력, 입력 불변 | `test_same_input_gives_same_output_and_does_not_mutate` |
| 충전기 0대 → 예외 | `test_zero_chargers_raises_instead_of_waiting_forever` |
| **나눠서 투영해도 한 번에 투영한 것과 같은 답** | `test_incremental_projection_matches_single_shot` |

마지막 항목이 이 모듈을 만든 이유 그 자체다. 원장은 매 Δt 마다 상태를 갱신하며 다시
예측하고, 시뮬레이터는 한 번 쭉 돌린다. 둘이 같은 답을 내려면 규칙이 좌에서 우로
접히는 순수 함수여야 한다. `assign(전체)` 와 `assign(앞부분) → chargers_after →
assign(뒷부분)` 이 같다는 것을 테스트가 고정한다.

충전기 0대에 예외를 던지는 이유: 허용하면 대기시간이 무한이 되고, 그 무한이 KPI
평균까지 조용히 오염시킨다. 충전기가 없는 충전소는 **입력이 잘못된 것**이지 대기가
긴 것이 아니다.

## 6. 넣지 않은 것

- **주차 공간 제약.** 충전기는 비었는데 댈 자리가 없는 상황. T-16 의 옵션이다.
- **하드 예약.** 슬롯을 미리 확보해 순서를 바꾸는 메커니즘. 설계문서 §7.1 원칙 2에
  따라 예약은 **FIFO 큐의 예측**일 뿐이고 실제 순서는 실제 도착 순서로 정해진다.
- **충전기 고장·점검.** `available_from_min` 을 아주 큰 값으로 두면 표현은 되지만,
  "고장" 을 1급 개념으로 넣지는 않았다.
- **중도 이탈(balking·reneging).** 대기가 길면 포기하고 떠나는 행동. 충전소 선택
  단계(§7.2 규칙 2, T-18)에서 다룰 문제다.
- **이용 요금·혼잡 요금.** 스테이지 정책(`engine/`)의 몫이다. 큐 규칙은 물리만 안다.

## 7. 다음

- **T-16 SimPy DES** — 이 모듈의 첫 호출자. DES 는 규칙을 다시 구현하지 말고
  `assign` 을 부른다.
- **예약 원장** — 두 번째 호출자. `chargers_after` 로 Δt 마다 상태를 넘긴다.
- 완료 조건: 같은 입력에 대해 DES 실행 결과와 원장 투영 결과의 시작·종료 시각이
  일치. §5 의 마지막 테스트가 그 일치를 규칙 수준에서 미리 고정해 둔 것이다.
