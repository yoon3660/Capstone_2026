Issue #24 작업 정리 — CTM 교통류 파라미터 및 차로수 실측

Issue #24에서는 경부고속도로 CTM 구축에 필요한 차로수와 교통류 파라미터를 실제 데이터와 모델 관계식에 맞게 정리했다.
기존에는 차로수와 일부 교통류 파라미터가 가정값에 의존하고 있었기 때문에, 이번 작업에서는 측정 가능한 값은 실제 데이터에서 추출하고, 직접 관측하기 어려운 값은 config에서 관리하며, 계산 가능한 값은 기본도 관계식으로 파생하도록 구조를 정리했다.

1. 경부고속도로 차로수 실측

국토교통부 표준노드링크 SHP를 이용해 경부고속도로 본선의 실제 차로수를 추출했다.

본선 링크를 선택할 때는 LANES 값을 선택 기준으로 사용하지 않고, T-09a/#23에서 만든 경부고속도로 중심선과의 위치 관계 및 F/T node 연결성을 기준으로 본선 경로를 결정했다. 이후 선택된 본선 링크의 LANES 값을 실제 차로수로 사용했다.

생성된 차로수 프로파일은 다음 파일에 저장된다.

data/processed/lanes_gyeongbu.parquet

컬럼은 다음과 같다.

offset_km_start
offset_km_end
lanes
direction

최종 결과는 DOWN 155개 구간, UP 139개 구간으로 구성되며, 두 방향 모두 약 0~415.058 km 전체 구간을 연속적으로 커버한다. gap과 overlap은 없었고, 최종 차로수는 1~6 범위로 확인됐다.

일부 1~2차로 구간은 이상치 가능성을 확인하기 위해 원본 표준노드링크의 LANES 값까지 다시 확인했으며, 원본 데이터와 일치하는 경우 임의로 smoothing하지 않고 그대로 유지했다.

2. 차로 변경 지점을 corridor cell 분할 기준으로 정리

최종 차로수 프로파일에서 차로수가 바뀌는 모든 이정을 추출했다.

DOWN: 154개 변경점
UP:   138개 변경점
총:   292개 변경점

전체 변경점 목록은 Issue #24에 기록했다.

이 지점들은 이후 corridor cell을 생성할 때 강제 분할 anchor로 사용한다. 즉 하나의 CTM cell이 서로 다른 차로수 구간을 동시에 포함하지 않도록, 차로수가 바뀌는 위치에서는 반드시 cell 경계를 생성한다.

실제 cell 생성 시에는 Issue에 적은 Markdown 목록을 읽는 것이 아니라 lanes_gyeongbu.parquet의 구간 경계를 직접 사용한다.

3. CTM 파라미터를 config 중심 구조로 정리

교통류 파라미터가 Python 코드 안에 매직 넘버처럼 흩어지지 않도록 config/flow_params.yaml을 파라미터 관리의 기준으로 사용하도록 정리했다.

현재 기본 설정은 다음과 같다.

dt_min: 1.0

defaults:
  v_free_kmh: 100.0
  w_back_kmh: 18.0
  k_jam_veh_km_lane: 144.0
  lanes: 4
  q_max_anchor_veh_h_lane: 2200.0

sensitivity:
  k_jam_veh_km_lane: [132.0, 144.0, 169.0]

dt_min, w_back, k_jam, fallback 차로수, reference q_max, 민감도 후보와 같은 모델 설정값은 config에서 관리한다.

기존처럼 Python 내부의 ASSUMED = {...} 딕셔너리를 파라미터의 source of truth로 사용하지 않고, scripts/estimate_flow_params.py가 YAML의 값을 읽어 사용하도록 변경했다.

4. v_free를 VDS 실측값으로 교체

기존 fallback 값인 100 km/h를 그대로 사용하는 대신, VDS 속도 데이터를 이용해 방향별 자유류 속도를 추정했다.

새벽 3~5시 관측속도를 이용해 콘존별 85 percentile을 계산한 뒤, 방향별 콘존 추정값의 중앙값을 대표값으로 사용했다.

최종 결과는 다음과 같다.

방향

v_free

DOWN

96.4 km/h

UP

98.1 km/h

따라서 실제 CTM에서는 방향별 실측값을 사용하고, defaults.v_free_kmh = 100.0은 실측값을 사용할 수 없는 상황의 fallback으로만 남긴다.

5. w_back과 k_jam의 역할을 명확히 분리

w_back과 k_jam은 현재 VDS 데이터만으로 안정적으로 직접 관측하기 어려운 값이므로 config에서 가정값으로 관리한다.

현재 기준값은 다음과 같다.

w_back = 18.0 km/h
k_jam  = 144 veh/km/lane

k_jam = 144일 때 차량 1대당 정체 공간은 다음과 같다.

1000 / 144 ≈ 6.94 m

이는 현재 프로젝트에서 사용하는 물리 검산 범위인 6~8 m 안에 들어온다.

k_jam은 직접 관측값이 아니므로 이후 132 / 144 / 169 veh/km/lane 조건으로 민감도 분석을 수행할 수 있도록 config에 후보값을 함께 저장했다.

6. q_max를 독립 입력값이 아니라 triangular FD에서 파생

CTM 용량 q_max를 별도의 하드코딩 값으로 사용하지 않고, triangular fundamental diagram의 관계식으로 계산하도록 정리했다.

q_max_per_lane
    = v_free * w_back * k_jam
      / (v_free + w_back)

q_max
    = lanes * q_max_per_lane

현재 파라미터를 적용하면 다음 값이 나온다.

방향

q_max_per_lane

DOWN

2,184.2 veh/h/lane

UP

2,190.1 veh/h/lane

두 값 모두 Issue에서 사용한 권장 범위인 2,150~2,250 veh/h/lane 안에 들어온다.

기존의 2200 veh/h/lane은 실제 CTM 입력값으로 직접 사용하는 것이 아니라, 현재 파라미터 조합이 목표한 용량 수준과 크게 어긋나지 않는지 확인하는 reference anchor로만 사용한다.

7. VDS의 q_p95 / q_breakdown 역할 정리

콘존별 VDS 분석 결과는 다음 파일에 저장한다.

data/processed/flow_params_by_conzone.parquet

VDS에서 계산한 q_p95와 q_breakdown은 실제 교통 상태와 병목을 확인하기 위한 관측값으로 사용한다.

다만 이 값을 단순히 차로수로 나누어 q_max_per_lane로 직접 사용하지는 않는다. 콘존 내부에서 차로수가 변할 수 있고, breakdown flow 자체가 항상 동일한 의미의 도로 최대 용량을 의미하지 않기 때문이다.

따라서 VDS 값은 자유류 속도 추정, 병목 확인, 이후 CTM calibration/validation의 비교 자료로 사용한다.

8. cell 테이블에 lanes_source 추가

실제 차로수가 들어간 구간과 fallback 차로수가 사용된 구간을 구분할 수 있도록 cell 테이블에 lanes_source 컬럼을 추가했다.

lanes_source TEXT NOT NULL
    CHECK (lanes_source IN ('measured', 'assumed'))

의미는 다음과 같다.

measured
→ 표준노드링크에서 얻은 실제 차로수 사용

assumed
→ 실측값을 사용할 수 없어 config의 fallback lanes 사용

이를 통해 향후 DB에서도 어떤 cell이 실측값을 사용했고 어떤 cell이 가정값을 사용했는지 추적할 수 있다.

9. DB에서 triangular FD 관계 검증

cell 테이블에는 다음 관계가 유지되는지 확인하는 CHECK 조건이 있다.

q_max
≈ lanes * v_free * w_back * k_jam
  / (v_free + w_back)

허용 오차는 1%이며, 파라미터와 맞지 않는 q_max가 저장되지 않도록 했다.

lanes_source가 추가되면서 기존 tests/test_db.py의 CELL INSERT도 새 스키마에 맞게 수정했다. 기존 15개 값에서 lanes_source를 포함한 16개 값으로 변경했고, 테스트용 row에는 "measured"를 추가했다.

10. 테스트 보강 및 전체 회귀 테스트 완료

Issue #24 전용 테스트로 tests/test_flow_params.py를 추가했다.

여기서는 다음 내용을 검증한다.

q_per_lane()과 k_from_q()가 서로 역관계인지 확인

권장 파라미터에서 q_max가 2,150~2,250 veh/h/lane 범위인지 확인

1000 / k_jam이 6~8 m 범위인지 확인

권장 파라미터가 DB CHECK를 통과하는지 확인

잘못된 q_max가 DB에서 거부되는지 확인

v_free = 90 / 100 / 110 변화 시 q_max 변화가 기준 대비 5% 이내인지 확인

잘못된 lanes_source가 DB에서 거부되는지 확인

실행 결과:

tests/test_flow_params.py
→ 9 passed

tests/test_db.py
→ 12 passed

pytest -q
→ 110 passed

11. 최종 파라미터

현재 Issue #24 완료 시점의 기준값은 다음과 같다.

항목

값

출처/관리 방식

dt_min

1.0 min

config

DOWN v_free

96.4 km/h

VDS measured

UP v_free

98.1 km/h

VDS measured

fallback v_free

100.0 km/h

config

w_back

18.0 km/h

config assumed

k_jam

144 veh/km/lane

config assumed

k_jam sensitivity

132 / 144 / 169

config

fallback lanes

4

config

actual lanes

구간별 1~6

표준노드링크 measured

DOWN q_max/lane

2,184.2 veh/h/lane

derived

UP q_max/lane

2,190.1 veh/h/lane

derived

q_max reference anchor

2,200 veh/h/lane

config reference

12. 이번 작업에서 해결한 내용

차로수 실측

표준노드링크에서 경부고속도로 본선 차로수 추출

DOWN 155개 / UP 139개 구간 생성

차로수 1~6, gap 0, overlap 0 확인

차로 변경점 정리

DOWN 154개 / UP 138개, 총 292개 변경점 추출

Issue #24에 전체 목록 기록

corridor cell 분할 anchor로 사용하도록 기준 확정

파라미터 하드코딩 정리

config/flow_params.yaml을 source of truth로 사용

Python 내부 ASSUMED 기반 관리 제거

dt_min, w_back, k_jam, fallback lanes, q_max anchor, sensitivity를 config에서 관리

v_free 실측

VDS 데이터로 방향별 자유류 속도 계산

DOWN 96.4 km/h / UP 98.1 km/h 적용

q_max 계산 방식 정리

q_max 직접 입력 제거

triangular FD 관계식으로 파생

DOWN 2184.2 / UP 2190.1 veh/h/lane 확인

VDS 관측값 역할 분리

q_p95, q_breakdown은 직접 q_max로 사용하지 않음

병목 확인 및 CTM calibration/validation용으로 사용

DB 스키마 보강

lanes_source 추가

measured / assumed만 허용

triangular FD 관계를 DB CHECK로 검증

테스트 보강

tests/test_flow_params.py 추가

기존 tests/test_db.py를 새 schema에 맞게 수정

flow parameter 테스트 9 passed

DB 테스트 12 passed

전체 테스트 110 passed

후속 작업

k_jam = 132 / 144 / 169 veh/km/lane 민감도 분석을 별도 Issue에서 진행