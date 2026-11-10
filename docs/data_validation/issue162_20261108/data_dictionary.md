# #162 생성 파일 사용법

주 모델 결과는 `passenger266/`이다. 상위 폴더의 같은 이름 파일은 등록282개 참고 설정이므로 합쳐서 사용하지 않는다.

| 파일 | 한 행 | 고유 키 | 주요 열·사용법 |
|---|---|---|---|
| `observed_chuseok_demand_history.csv` | 원본의 충전 세션 | `source_row` | 원본 시트의 헤더가1행, 데이터는2행부터. `station_id`는 원본 ME 충전소 ID, `charger_id`는 원본 장비 ID 문자열 |
| `observed_september_history.csv` | 9월 경부선 세션 | `source_row` | 기간 밖 이월·월중 장비 존재 확인. 추석 합계에 그대로 더하지 않음 |
| `observed_station_hourly_96h.csv` | 휴게소×시간 | `station_name,direction,hour_start` | `session_starts` 시작 건수, `energy_kwh` 해당 시각에 시작한 세션의 전체 에너지. `recorded_elapsed_busy_min`은 시작·종료 구간을 시간에 걸쳐 배분한 합계이며 실측 대기시간 아님 |
| `passenger266/generated_ev_demand_history.csv` | 합성 진입 EV | `date,direction,ev_id` | `entry_time_min`은 해당 input 날짜의 자정 기준 분. 진입·목적지·SoC·차종은 모델 생성값. `has_charge_plan`은 계획이 존재함을 뜻하며 실제 충전 이벤트 건수와 다름 |
| `passenger266/generated_charge_plan_demand_history.csv` | 충전 계획 차량 | `date,direction,ev_id` | `n_plans` 후보 계획 수. `soc0`, `battery_kwh`, `vmax_kw`, `consumption_kwh_km` 포함. 선택된 최종 충전 위치는 다음 파일 참조 |
| `passenger266/generated_chuseok_charging_history.csv` | 모델 충전 정차 | `run_id,ev_id,stop_seq` | 창 안 시작만 포함. `input_date`는 차량을 생성한 날, `start/end`는 달력 시각. 둘은 자정 이월 시 다를 수 있음 |
| `passenger266/generated_charging_history_all_events.csv` | 모델 전체 충전 정차 | 동일 | 창 밖 시작29건도 보존. 추석창 파일에 다시 합치지 않음 |

실측 `charger_id`는 CSV를 Excel로 열면 앞자리0이 사라질 수 있다. 프로그램에서는 `station_id,charger_id`를 문자열로 읽고 장비 ID는2자리로 정규화한다. 서로 다른 충전소의 `01`을 같은 장비로 합치면 안 된다.

모델 `model_station_id`는 내부 휴게소 키, `model_charger_id`는 원본 충전소ID와 장비ID를 조합한 키, `model_unit_id`는 시뮬레이터의 슬롯 접미사까지 포함한다. `station_id,charger_id` 열은 원본 API 식별자로 되돌린 값이다. 모델 ID를 실측 원본 ID처럼 보이게 덮어쓰지 않았다.

실측 `energy_kwh`는 원본의 충전량, `reported_charge_min`은 원본 충전시간 필드, `elapsed_min`은 종료−시작이다. `valid_elapsed=False`인0분2건도 시작 원장에는 남는다. `duplicate_exact`는 원본 이벤트 필드 조합 중복 표시다. `id_in_sep18`은 명부에 ID가 존재하는지일 뿐 실제 정상 가동 확인이 아니다.

모델 `wait_min`은 모델 도착부터 시작까지, `charge_min`은 시작부터 종료까지, `dwell_min`은 둘의 합이다. 모델 에너지·시간을 원본 관측 필드와 혼동하지 않는다. 모델과 실측에는 동일 차량 ID가 없어 세션별 MAE를 계산할 수 없다.

비교 파일의 `observed/model/error/abs_error`는 셀별 시작 건수와 차이다. `comparison_metrics.csv`의 WAPE는 절대오차합/관측합×100, TVD는 휴게소 비중 차이 절댓값합/2이다. `ALL_AT_OBSERVED_STATIONS`는 관측28곳의 전체 운영사 모델, `COMMON_ME_IDS_DIAGNOSTIC`은 공통92개 ID만의 비교다. 두 집합은 중첩되므로 더하면 안 된다.

추가 비교표: `comparison_peak_hours.csv`는 날짜·방향별 최대 시간대(동률 모두 보존)와 최소 원형 시간차, `comparison_concurrency.csv`는 기록상 동시 세션 최대값과 모델 최대값이다. 피크 시간차는 동률 집합 간 최소 차이이며 모델을 낙관적으로 보일 수 있어 원래 시간 목록을 함께 읽는다. 동시 세션은 모델 ALL↔실측ME, 독립 일별 실행 경계라는 한계가 있다.

관측되지 않은7곳의 N/A는 실측0이 아니다. 관측28곳에서 특정 시간에 기록이 없을 때만0으로 채운다. 관측 원장 자체가 ME 완전 전수임을 보장하지는 않는다.
