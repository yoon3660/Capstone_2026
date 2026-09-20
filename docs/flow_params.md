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

---

## #24 후속 수정 (debug/lanes)

머지 후 리뷰에서 나온 문제를 정리한 기록이다.

### 1. 연결로(램프)가 본선 후보에 섞여 있었다

`audit_lane_mapping.py` 의 필터는 `ROAD_NAME` · `ROAD_RANK` · `ROAD_NO` · `ROAD_USE`
네 개뿐이라 `CONNECT`(연결로 구분)를 보지 않았다. 표준노드링크에서 램프도
ROAD_NAME='경부고속도로' 를 달고 있고 보통 1~2차로다. 그래서 3.7km 1차로,
24.7m 1차로, `3 → 4 → 3` 진동 같은 구간이 나왔다.

원본 LANES 값과 결과가 일치한다는 확인은 **매핑 단계**만 검증한다. 문제는 그 앞의
**링크 선택**이다. 잘못 고른 링크의 값을 정확히 옮겨도 결과는 틀린다.

고친 방법 (`evdt.io.lane_profile.select_mainline_candidates`)
- `CONNECT='0'`(본선)만 후보로 쓴다.
- 본선만으로 덮이지 않는 이정 구간에 걸치는 램프만 되살린다 (연결이 끊기지 않게).
- 되살린 링크에서 나온 차로수는 `lanes_source='assumed'` 로 내려 적는다.
- 되살린 링크 목록을 실행할 때 출력한다.

### 2. 실측 교통량으로 차로수를 반증한다

한 차로는 1시간에 q_max 대를 넘길 수 없다. 그러면 그 구간의 관측 최대 교통량 Q 에서
필요한 차로수가 나온다.

```
필요 차로수 = ceil(Q / q_max_per_lane)
```

VDS 정리본(`traffic_gyeongbu.parquet`)으로 확인한 결과, 콘존 115곳 중
**113곳의 관측 최대 교통량이 1차로 용량(2,196 veh/h)을 넘는다.** 53곳은 2차로
용량도 넘는다. 즉 본선에 1차로 구간은 있을 수 없다.

`build_lane_profile.py` 가 저장 직전에 이 검사를 돌리고, 모순이 있으면 멈춘다
(`check_lanes_against_observed`). 검증 범위도 `1~6` 에서 **`2~6`** 으로 좁혔다.

### 3. 셀 길이와 dt_min (CFL 조건)

차로수 변경점은 CTM 셀 경계가 된다. 24.7m 구간이 남으면 그 길이의 셀이 생기고,
CFL 조건 때문에 Δt 가 1초 미만이어야 한다.

```
v_free * dt <= 셀 길이
100 km/h, 0.5 km 셀 -> dt <= 0.30분 (18초)
```

- `config/flow_params.yaml`: `dt_min` 1.0 → **0.2분**, `defaults.min_cell_length_km: 0.5` 추가.
- `min_cell_length_km` 보다 짧은 구간은 **차로수가 적은 이웃**에 병합한다
  (`merge_short_segments`). 용량을 크게 잡는 쪽으로 틀리면 있어야 할 병목이 사라진다.
  병합된 구간은 `lanes_source='assumed'`.
- `estimate_flow_params.py` 가 config 를 쓸 때 CFL 조건을 검사하고, 어기면 멈춘다.
- `tests/test_lane_profile.py::test_config_dt_satisfies_cfl` 이 config 값을 직접 읽어 검사한다.

### 4. 스키마 마이그레이션

`schema.sql` 은 `CREATE TABLE IF NOT EXISTS` 라서 이미 DB 를 가진 사람에게는
`cell.lanes_source` 가 생기지 않는데, `init_db` 는 테이블 존재만 보고 성공이라고 했다.

- `io/db.py` 에 `MIGRATIONS` 를 두고 빠진 컬럼을 `ALTER TABLE` 로 채운다.
- 채운 뒤에도 컬럼이 없으면 `SchemaError` 로 멈춘다.
- `lanes_source` 에 `DEFAULT 'assumed'` 를 줬다. NOT NULL 인데 기본값이 없으면
  이 컬럼을 모르는 기존 INSERT 가 전부 깨진다 (머지 직후 `verify_setup` 이 그렇게
  실패했다). 기본값은 "모르는 값" 쪽이어야 한다.

### 5. 재현 경로

- `requirements.txt` 에 `pyshp` · `pyproj` 추가 (없어서 다른 사람은 실행 자체가 불가능했다).
- `data/raw/README.md` 에 표준노드링크 다운로드 위치와 파일 배치 경로를 적었다.

### 6. 판단 규칙을 테스트 가능한 모듈로

1,291줄 스크립트 안에 있던 판단 규칙을 `src/evdt/io/lane_profile.py` 로 옮겼다.
그래프 탐색(본선 링크 선택)은 표준노드링크 원본이 있어야 돌지만, 아래 규칙은
DataFrame 만 있으면 검사할 수 있다 — `tests/test_lane_profile.py` 29건.

| 함수 | 규칙 |
|---|---|
| `select_mainline_candidates` | 램프 제외, 끊긴 구간만 보충 |
| `check_lanes_against_observed` | 관측 교통량으로 차로수 반증 |
| `merge_short_segments` / `max_dt_min` | 최소 셀 길이와 CFL |
| `merge_adjacent_lane_segments` / `lane_change_points` | 프로파일 정리, 셀 앵커 |
| `repair_implausible_lanes` | 본선일 수 없는 차로수를 이웃값으로 보정 |
| `validate_lane_profile` | 스키마·차로수 범위(2~6)·gap/overlap |

## 재생성에서 드러난 것 (2026-09-20, 표준노드링크 2026-09-14 배포본)

위 필터를 적용해 실제로 다시 생성해보니 상행(UP)에서 멈췄다. 원인은 두 가지였고
둘 다 debug/lanes 쪽 문제였다.

### 7. `CONNECT='0'` 인데 `LANES=1` 인 링크가 있다

램프 필터는 `CONNECT` 를 본다. 그런데 **원본이 본선이라고 말하면서(`CONNECT='0'`)
차로수는 1이라고 적어둔 링크**가 있었다. 상행 78.202~81.930 km(구서IC 기점)에 5개가
연속으로 놓여 있다 (`3520811800`, `3520811803`, `3520811802`, `3520811801`,
`3520811600`). 편도 1차로 고속국도 본선은 존재하지 않으므로 이건 링크 선택이
아니라 **속성값**의 문제다.

하행에도 2개 있었다 (`1870197300` 24.7 m, `1961193201` 360.6 m). 둘 다
`MAX_SPD=40` 이라 애초에 본선이 아닐 가능성이 있다 — §10 참조.

문제는 대응 방식이었다. `validate_selected_links` 가 차로수 범위를 치명 오류로
처리해서, 링크 선택은 멀쩡한데(연결 컴포넌트 1개, 분기 0개, 빈 구간 0km) 속성
하나 때문에 파이프라인 전체가 멈췄다. **링크를 제대로 골랐는가**와 **원본 속성이
쓸 만한가**는 별개의 질문인데 하나로 묶여 있었다.

고친 방법
- 차로수 범위는 더 이상 링크 선택을 무효화하지 않는다. 해당 링크 목록을 출력만 한다.
  (연결성·분기 검사는 그대로 치명 오류다.)
- `repair_implausible_lanes` 가 연속된 문제 구간의 앞뒤에서 가장 가까운 정상
  구간을 찾아 **둘 중 차로수가 적은 쪽**으로 메우고 `lanes_source='assumed'` 로
  내려 적는다. 적게 잡는 쪽으로 틀리는 이유: 적게 잡은 오류는 실측 교통량이
  잡아낼 수 있지만, 많게 잡은 오류는 아무것도 잡아내지 못한다.
- 메운 총 길이가 방향당 `MAX_REPAIRED_LANE_KM`(10 km)를 넘으면 멈춘다. 그 정도면
  속성 하나가 튄 게 아니라 링크 선택이 틀렸다고 봐야 한다.
- 어디를 무엇으로 메웠는지 실행할 때마다 표로 출력한다.

원본 속성을 직접 확인하려면 `scripts/inspect_links.py` 로 audit 캐시를 들여다본다.

```
python scripts/inspect_links.py 3520811800 3520811801
python scripts/inspect_links.py --km 78.2 81.9 --direction UP
```

### 8. 교차검증이 이 구간을 아예 안 보고 있었다

더 나쁜 쪽은 이것이다. 경주IC~건천JC 콘존(76.716~87.920 km)의 관측 최대 교통량은
**4,244 veh/h** 로, 1차로는 물론이고 2차로로도 불가능한 값이다. 즉 §2 의
교차검증이 이 구간을 반증할 수 있었다.

그런데 `check_lanes_against_observed` 가 콘존의 **대표점 하나**(82.318 km)만 보고
포함 여부를 따졌다. 대표점이 3.7 km 짜리 차로 구간 밖이라 이 구간은 검증을 한 번도
거치지 않고 통과했다. 콘존은 IC~IC 라 보통 10 km 가 넘는데 차로 구간은 그보다
짧을 수 있다는 걸 놓쳤다.

> 위 콘존 이름과 km 값은 **구버전 이정축**(IC·휴게소 91점, 총연장 392.978 km)에서
> 확인한 것이다. 현재 중심선 이정축(415.058 km)에서는 같은 자리에 다른 콘존이
> 걸린다. 건너뛴다는 사실 자체는 이정축과 무관하고
> `test_observed_flow_uses_conzone_span_not_just_its_midpoint` 가 고정한다. §11 참조.

고친 방법: **구간 겹침**으로 맞춘다. 콘존 안에는 진출입이 없어 통과 교통량이
보존되므로, 콘존의 피크는 그 안의 모든 지점이 실제로 흘려보낸 양이다. 따라서
겹치기만 하면 그 콘존의 관측값을 적용해도 된다. `offset_km_start/end` 가 없는
입력은 기존 점 포함 방식으로 되돌아간다.

이 순서도 바꿨다. 예전에는 차로수 검증에서 멈춰서 **가장 강한 증거인 실측
교통량 교차검증까지 도달하지도 못했다.** 이제 메운 뒤에 검산한다.

### 9. DOWN 프로파일의 이정 기준이 나머지 데이터와 달랐다

`build_lane_profile` 이 링크의 `m`(구서IC 기점 누적거리)을 그대로 `offset_km` 으로
내보내고 있었다. 콘존·충전소의 DOWN `offset_km` 은 서울 쪽이 0 인데, 차로
프로파일만 DOWN 에서도 부산 쪽이 0 이었다. 이 상태로 §8 의 교차검증을 돌리면
코리도의 **반대쪽 끝**과 비교하게 된다.

`route.to_direction` 으로 방향별 프레임에 맞춰 내보내도록 고쳤다. 아직
`lanes_gyeongbu.parquet` 을 읽는 코드가 없어서 호환성 문제는 없다.

## 재생성 결과 (2026-09-21)

SHP 를 가진 팀원이 끝까지 돌렸고 전부 통과했다.

| | UP | DOWN |
|---|---|---|
| 본선 링크 | 1,015 | 1,025 |
| 연결 컴포넌트 / 분기 노드 / 빈 구간 | 1 / 0 / 0 km | 1 / 0 / 0 km |
| 범위 밖 LANES → 복구 | 5개 → 3.728 km | 2개 → 0.385 km |
| 차로수 변경점 | 33 | 30 |

최소 셀 길이 500 m 병합으로 300 → 95 구간, 실측 교통량 교차검증 통과
(차로당 q_max 2,197 대/h). §9 의 방향별 이정축 변환도 확인됐다 — 하행 링크
`1870197300`(m 276.546)이 하행 offset 138.512 로 나오고, 둘을 더하면 노선
총연장 415.058 km 가 된다.

### 10. 확인이 남은 것 (파이프라인은 통과했지만 눈으로 볼 것)

- **하행 `MAX_SPD=40` 링크 2개** (`1870197300` 24.7 m, `1961193201` 360.6 m).
  고속국도 본선에 제한속도 40 은 이상하다. 애초에 본선이 아닐 수 있다. 다만 둘 다
  최소 셀 길이(500 m) 병합에서 흡수되므로 결과에 미치는 영향은 없다.
  `MAX_SPD` 를 본선 판정의 보조 신호로 쓸지 검토할 것.
- **상행 241.252~256.992 km 가 편도 2차로** (15.7 km). 교차검증은 통과했다 —
  그 구간 관측 피크가 2차로 용량(4,394 대/h) 이하라는 뜻이다. 그래도 경부 본선에
  15.7 km 짜리 2차로 구간이 맞는지는 눈으로 확인하는 게 좋다. 맞다면 CTM 에서
  가장 강한 병목이 된다.

  ```
  python scripts/inspect_links.py --km 241 257 --direction UP
  ```

### 11. 이정축이 사람마다 달라질 수 있다

이번에 드러난 별개 문제다. 리뷰어 로컬의 `gyeongbu_route.json` 은 총연장
**392.978 km**(IC 54 + 휴게소 37 = 91점)였고, 팀원 쪽은 **415.058 km** 였다.
전자는 `build_route.py` 가 중심선을 쓰기 전의 산출물이다 (현재 IC 기반 코드는
주석 처리돼 있다). 22 km 차이는 폴리라인이 IC 사이를 직선으로 이었기 때문이다.

`data/processed/` 는 git 에 올라가지 않으므로 **각자 만든 이정축이 조용히
어긋날 수 있다.** 교통량·충전소·차로 프로파일이 전부 이 축을 공유하는데,
어긋나도 알려주는 장치가 없다. §9 와 같은 종류의 문제가 사람 사이에서 재발한다.

후속으로 넣을 것: `build_lane_profile.py` 가 노선 총연장과 교통량 정리본의 최대
offset 을 비교해, 다르면 멈추게 한다. 지금은 프레임이 어긋난 채로도 교차검증이
"통과" 를 찍는다.

### 남은 것

- 78.202~81.930 km 5개 링크의 원본 `LANES=1` 이 배포본의 오류인지 다른 의미인지는
  아직 확인하지 못했다. 현재는 이웃값(양쪽 중 적은 쪽)으로 메우고 `assumed` 로
  표시한 상태다.
- 이정축 어긋남 감지 (§11). `build_lane_profile.py` 가 노선 총연장과 교통량
  정리본의 최대 offset 을 대조해서 다르면 멈추게 한다.
- k_jam 132 / 144 / 169 민감도 분석은 여전히 후속 이슈다.

