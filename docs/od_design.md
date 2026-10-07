# #62 코리도 시간대 OD 추정 — 설계·검증·재현

기준: 2026-10-07, develop/62. 대상: [GitHub #62](https://github.com/yoon3660/Capstone_2026/issues/62).

TCS 실측 일 합계를 보존한 일별·시간대 OD 추정, 좌표 보완, 날짜 분리 검증과 기존 방식 비교를 구현했다. **현재 결과는 가중 최소제곱 근사 시제품이다. 구간 일 교통량을 모두 정확히 맞추는 해는 현재 입력과 이동 규칙에서 존재하지 않으므로, 모든 완료조건 충족으로 판정하지 않는다.**

## 완료조건별 상태


| 완료조건 | 현재 판정 | 증거와 실제 한계 |
|---|---|---|
| 끝·영업소·JC 좌표 → 기점거리 | 완료 | 방향별 68개 노드. JC 19개 API 좌표 및 언양·옥산 2개 표준 링크 연결 노드 좌표를 투영. T62_completed_jc_coordinates.md 참조 |
| 중력 사전 + 일 교통량·TCS·비음수 제약 + 가장 가까운 해, 가중치 근거 | 코드 구현 완료 / 실제 입력 미충족 | 정확 제약 및 최근접 사전 해 구현·테스트. 실제 20일 양방향 40조건은 모두 불가능. 근사 결과를 정확 제약 결과로 바꾸지 않음 |
| 진입점 시간 비율 보정 + 통행시간 지연 | 구현·시제품 검증 완료 | 날짜 분리 학습, 분수 시간·자정 이월, 상수 90km/h 가정 |
| 지정 경로 parquet | 파일 생성·검사 완료 | 202602/202603 UP/DOWN 4개. 시제품 상태·SHA256 설정 파일, 34,440 TCS 일별 쌍 합계 보존 |
| 보정/검증 분리 + RMSE·MAPE 표 | 완료(조건부 검증) | 각 월 보정 2일·검증 8일. 검증 VDS 학습 제외, 검증 TCS 입력. 종합·날짜별 표와 재계산 대조 |
| 기존 방식 재현 오차·장거리 비율 비교 | 완료(비교 적용 명시) | 같은 관측 행·지연·날짜, 기존 저장소 함수의 보정일 기반 프로파일. 원본 스크립트의 검증일 VDS 직접 사용은 하지 않음 |
| 4지점의 알려진 OD를 구간 관측만으로 복원 | 완료(식별 가능 구조 한정) | 인접 3개 경로 및 겹치는 비인접 3개 경로의 미지 대수 복원. 6경로 전체는 유일 식별 불가능 사례도 검증 |

## 설계와 가중치


공유 중심선 길이 415.057836km에서 UP은 부산→서울 기점거리, DOWN은 공유 길이에서 UP 기점거리를 뺀 값이다. 끝점·TCS 영업소·JC·보충 IC를 별도 식별자로 보존한다. 가까운 TCS/JC를 자동 병합하지 않는다. JC 19개는 API 원본 좌표 투영, 언양·옥산 2개는 표준 링크 원본에서 본선과 연결 고속도로가 공유하는 노드 좌표를 투영한다. 최소 중심선 투영 거리를 가진 실제 연결 노드를 대표 위치로 선택하며 모든 램프의 중심이라는 의미는 아니다. 후보·원본 링크 식별자·좌표·CRS·해시는 아래 좌표 근거와 T62_validation_summary.json의 좌표 증거에 기록했다.

구간 관측은 콘존 중간점을 사용한다. 포털 집계의 대표 VDS와 정확한 단면이 미확정이므로 검지기 후보 좌표를 자동 채택하지 않는다. 구간 일 합계는 24시간 모두 관측된 경우만 사용하고 시간별 결측은 학습·평가에서 제외한다.

## 일별 OD

TCS 쌍별 실측 일 합계는 고정한다. 추가 OD는 끝점·영업소·JC 등의 허용 진출입을 잇는 전방 경로이며 영업소 간 추가 OD는 중복 수요 방지를 위해 제외한다. 끝점, 상서, 금강휴게소 정책은 od_movement_rules.py에 기록하며 나머지 이동 가능 여부는 가정이다.

고정 TCS 통과량 f와 경로 통과 행렬 A에 대해 추가 OD x를 추정한다. 중력 사전 q는 `(거리 + 10km)^(-1.5)`에 관측 잔여량의 중앙값 규모를 곱한다. 10km는 매우 가까운 노드 쌍에서 사전이 폭증하지 않도록 하는 완화이고 1.5는 시험 가정이다. 실제 통행거리 분포로 보정한 값이 아니다.

정확 모드에서는 `A x = y - f, x >= 0` 아래 `mean((x/q - 1)^2)`를 최소화한다. 사전의 절대 대수가 큰 경로만 목적함수를 지배하지 않도록 상대 이탈을 사용한다. 이는 q별 역제곱 가중 최소제곱이다. 실행 가능성을 먼저 검사하고 독립적인 잔차·비음수 검사를 수행한다. 불가능하면 최소 최대오차를 보고하고 저장 전에 중단한다. 현재 40조건 모두 불가능하므로 정확 모드 실제 결과는 없다.

근사 모드는 기존 시제품 재현을 위해 명시적으로 선택한다. 목적은 `mean(((A x + f - y)/max(y,100))^2) + 0.001*mean((x/q - 1)^2)`다. 100대 하한은 작은 관측치의 상대오차가 폭증하는 것을 제한한다. 0.001은 민감도 시험 설정이며 검증일 성능을 보고 고른 값이 아니다. 정확 제약 모드의 목적함수에 이 값은 영향을 주지 않는다.

## 시간대 배분

보정일 일별 OD를 진입점별 24시간 softmax 확률로 배분해 쌍별 일 합계를 보존한다. 사전은 가장 가까운 downstream 콘존의 보정일 시간별 평균을 정규화하며 불완전할 때 전체 보정일 평균을 사용한다. 관측점까지 거리/90km/h 지연을 인접 두 도착 시간대로 나눠 적용하고 자정 이월 및 마지막 날짜 이후 tail을 보존한다. 상수 속도는 가정이며 실측 속도가 아니다.

시간대 목적함수는 `mean((구간 시간오차/max(관측,100))^2) + 0.01*mean((24*(시간비율-사전비율))^2)`다. 24 배율은 균등 확률 1/24 정도의 편차를 1 단위로 표현한다. 0.01도 시험 설정이다. 일별 단계는 출발일·통과일을 구분하지 않는 근사이며 시간대 지연으로 그 일별 한계가 사라지는 것은 아니다.

## 학습·검증 및 식별성

2월 13–14일, 3월 6–7일을 각각 보정하고 같은 월의 후속 8일을 검증한다. 검증 VDS를 학습에 쓰지 않으며 검증 TCS는 입력으로 사용한다. 외부 OD는 보정일 평균에 검증 TCS 총량/보정 TCS 평균 총량을 곱한다. 따라서 TCS가 주어진 조건부 VDS 재현 검증이며 월간 이전 예측이 아니다.

전체 전방 6개 OD를 허용한 4지점은 3개 구간 교통량만으로 유일 식별할 수 없다. 복원 테스트는 구조적으로 알려진 3개 경로에 한정한다. 인접 경로와 겹치는 비인접 경로 모두에서 미지 OD 대수를 관측으로 복원하며, 부족한 관측으로 정답을 주장하지 않는 시험도 포함한다. 정확 모드에서 식별 불가능한 경우에는 사전에서 가장 가까운 해를 선택한다.

## 언양·옥산 JC 좌표 근거

API 원본에는 두 JC가 없어 ITS 표준 링크 MOCT_LINK에서 본선과 연결 고속도로가 공유하는 F_NODE/T_NODE를 추출했다. 연결 ID가 동일하고 좌표가 일관적인 후보 중 공유 중심선과의 투영 이격이 최소인 실제 연결 좌표를 선택했다. CRS는 기존 원본 처리와 같은 EPSG:5186이다. 분기점 전체의 중심이나 모든 램프 위치를 뜻하지 않는다.

| JC | 선택 노드 ID | 위도 | 경도 | UP 기점거리 km | 중심선 이격 km | 기존 경계 추정 km |
|---|---|---:|---:|---:|---:|---:|
| 언양 | 1960013608 | 35.569927442 | 129.130894557 | 39.910941599 | 0.003306195 | 48.959918 |
| 옥산 | 2710219801 | 36.704463439 | 127.347267481 | 313.922278858 | 0.004083544 | 318.182918 |

언양의 근거 링크는 1960027600 / 1960028400 / 1960037645, 옥산은 2710659901 / 2710683900 / 2710684100이다. 선택하지 않은 연결 후보도 검사표에 보존한다. 기존 추정과의 차이는 각각 약 9.049km, 4.261km다. 관측값을 이용해 위치를 맞춘 것이 아니라 원본 도로 연결과 좌표로 선택했다.

좌표계는 이 프로젝트의 기존 표준 노드·링크 읽기와 data/raw/README.md에 명시된 EPSG:5186을 사용해 WGS84로 변환했다. 원본 SHP/DBF, 공유 노선 및 입력 노드의 SHA256을 설정 파일에 기록했다. 국가교통정보센터 자료 경로: https://www.its.go.kr/nodelink/nodelinkRef

근거 자료: [ITS 표준 노드·링크](https://www.its.go.kr/nodelink/nodelinkRef). 선택 후보와 원본 SHA256은 T62_validation_summary.json의 od_jc_coordinate_evidence 및 coordinate_parameters에 보존한다.

## 검증 결과

각 월 보정 2일·검증 8일, 양방향 네 조건을 실제 보완 좌표로 재학습했다. 검증 TCS는 입력으로 사용하고 검증 VDS는 학습에서 제외한다. 34,440개 TCS 날짜·방향·쌍의 일 합계와 비음수를 검사했다.

| 기간 | 방향 | 새 RMSE 대/시간 | 새 MAPE % | 기존 RMSE 대/시간 |
|---|---|---:|---:|---:|
| 202602 | DOWN | 657.391 | 48.496 | 1,255.690 |
| 202602 | UP | 696.285 | 34.090 | 1,926.491 |
| 202603 | DOWN | 398.882 | 21.645 | 1,176.548 |
| 202603 | UP | 489.954 | 24.217 | 1,746.975 |

이 수치는 기존 근사 시제품의 검증이며 정확 제약 모드의 실데이터 성능 수치가 아니다. 새로운 모드를 추가했다고 기존 수치가 개선된 것으로 표현하지 않는다.

정리 후 전체 pytest 563개 통과 (제외한 경계 조사·상행 원인 진단 테스트 5개를 뺀 수치), src/tests/scripts ruff 및 git diff --check 통과. 4지점 복원은 알려진 후보 경로의 미지 대수를 구간 관측만으로 복원하는 시험이며, 전체 6경로의 유일 식별을 주장하지 않는다.

## 정확 일별 제약이 남는 이유

현재 자료의 20일 × 양방향 40조건 모두 정확한 비음수 해가 없다. 예를 들어 2026-02-14 DOWN의 신탄진–상서 / 상서–회덕 관측은 67,345 / 44,151대인데 고정 TCS 통과량의 차이는 3대다. 상서에서 추가 진출입을 허용하지 않는 현재 규칙으로는 이 차이를 맞출 수 없다. 최소 최대오차는 11,598.5대/일이다. 영락 부근과 일부 상행 조합에도 충돌이 있다.

이 결과는 현재 일별 중간점 모델 아래의 불가능성을 뜻한다. 센서 오류나 실제 램프 교통량의 원인을 확정하지 않는다. 관측 대응·날짜 경계·이동 규칙에는 추가 확인이 필요하다. 관측을 임의로 삭제하거나 추가 영업소간 OD를 넣어 충돌을 숨기지 않았다. 가중 최소제곱의 잔차 허용 결과를 완료로 인정할지 아직 확정되지 않았다.

## 입력·출력 및 재현

프로젝트 루트에서 실행한다. 입력 데이터는 Git에서 제외되므로 별도로 준비한다.

필수 입력: data/processed의 gyeongbu_route.json, od_nodes_gyeongbu_supplemented.parquet, conzone_gyeongbu.parquet, tcs_od_gyeongbu.parquet, traffic_gyeongbu.parquet, od_input_audit.csv. 좌표 완료 검사에는 od_jc_coordinate_evidence.csv도 필요하다. 초기 노드 생성에는 기존 IC JSON, JC 보완에는 MOCT_LINK의 SHP/SHX/DBF와 pyshp·pyproj 환경이 필요하다.

```powershell
python scripts/estimate_od_daily.py audit
python scripts/build_od_nodes.py coordinates --shp-file data/raw/nodelink/MOCT_LINK.shp --nodes-file data/processed/od_nodes_gyeongbu_supplemented.parquet --route-file data/processed/gyeongbu_route.json --output-dir data/processed/jc_coordinates
```

좌표 출력의 보완 노드 parquet를 supplemented 입력으로 사용하고 좌표 근거 CSV·설정 JSON을 processed에 둔다. 보충 JSON은 build_od_nodes.py의 --jc-coordinate-file 입력으로도 사용할 수 있다. 기존 결과와 다른 새 디렉터리에 근사 시제품을 생성한다.

```powershell
python scripts/run_od_validation.py run --processed-dir data/processed --results-dir data/processed/od_validation_t62 --daily-constraint-mode approximate --export-dir data/processed/od_export_t62
python scripts/run_od_validation.py feasibility --processed-dir data/processed --output-dir data/processed/od_feasibility_t62
python scripts/run_od_validation.py check --processed-dir data/processed/od_export_t62 --results-dir data/processed/od_validation_t62 --feasibility-dir data/processed/od_feasibility_t62 --report data/processed/t62_completion.json
```

완료 검사 --processed-dir에는 노드·콘존·TCS·traffic·audit·좌표 근거와 네 내보내기 파일을 함께 준비해야 한다. 위 예의 od_export_t62는 내보내기 파일만 자동 생성하므로, 검사 전에 동일 입력 파일들을 함께 복사해야 한다. 정확 제약을 검사하는 현재 완료 검사는 실패 상태로 종료하는 것이 예상 결과다. 명령행 기본 모드는 exact이며 현재 자료는 저장 전에 중단한다. approximate를 정확 제약 결과로 표시하지 않는다.

출력 이름: od_gyeongbu_<DOWN 또는 UP>_<202602 또는 202603>.parquet 및 각각의 .parameters.json. 설정에는 날짜 분할·가중치·가정·입력 및 결과 SHA256을 기록한다. 주요 열은 date, departure_hour, direction, start_node, end_node, start_offset_km, end_offset_km, distance_km, volume_veh, source, split이다. volume_veh는 실수 기대 대수다. measured_tcs_daily의 일 합계는 실측이나 시간 배분은 추정이며, 검증 외부 OD는 predicted_external_daily_from_training이다.

## PR에 포함하는 자료

실행·검증 코드, 관련 테스트, 이 문서 하나와 T62_validation_summary.json의 현재 검증 표를 포함한다. 원본 데이터·생성 parquet·중간 조사 문서·시제품 백업은 포함하지 않는다. 증거 CSV는 소규모 검증 결과이며 실행 입력 데이터와 구분한다.

## 팀원 사용 방법과 최소 업로드 구성

결과 parquet를 사용하는 팀원은 테스트나 검증 코드를 실행할 필요가 없다. 별도 전달한 결과 파일을 읽으면 된다. 같은 입력으로 결과를 재생성하려면 다음 세 기능 파일을 사용한다.

| 파일 | 역할 | 실행 구분 |
|---|---|---|
| scripts/build_od_nodes.py | 기본 노드, 보충 IC, 실제 JC 좌표 구성 | build / supplement / coordinates |
| scripts/estimate_od_daily.py | 입력 검사, 진출입·제약, 일별 및 시간대 추정 | audit / daily / hourly |
| scripts/run_od_validation.py | 두 기간·양방향 실행, 오차·기존 방식 비교, 제약 검사 | run / feasibility / check |

입력 검사는 일별 코드로, 관련 진출입·제약 함수도 같은 파일로 합쳤다. 시간대 코어는 시간대 추정 코드에, 검증 집계·기존 방식 비교·정확 실행 가능성 CLI·완료 검사는 검증 코드에 합쳤다. 후보 경로와 실행 가능성 함수는 일별 모델에 두며 검증 도구가 이를 가져온다.

테스트는 tests/test_od.py 한 파일로 모았고, 기존 tests/conftest.py의 scripts 경로 설정을 포함한다. 테스트는 팀원이 결과를 사용하는 데 필요하지 않지만 모델 변경 시 TCS 합계 보존·시간 지연·4지점 복원·좌표 연결을 확인하는 회귀 검사이므로 저장소에 포함한다.

업로드는 실행 코드 3개, 테스트 본문 1개와 기존 테스트 설정 수정 1개, 이 문서, T62_validation_summary.json의 총 7개 파일이다. 나머지 중간 조사 코드·문서·개별 테스트는 저장소 밖 작업 폴더에 보관했다. 데이터와 생성 parquet는 Git에서 제외한다.

정리 후 검증에서는 기존 결과의 집계·보존·비교를 다시 수행한다. 모델·관측·가중치는 이번 파일 통합에서 변경하지 않았다.

통합 후 확인: 전체 테스트 563개 통과, ruff 전체 검사 통과, 모든 실행 구분의 도움말 로딩 통과, 기존 결과 집계·TCS 합계 보존·기존 방식 비교 수치 동일.

## 파일 통합과 기존 코드 재사용

일별·시간대 추정은 estimate_od_daily.py 하나로 합쳤다. 파일명은 이미 스테이징된 기존 경로를 유지한 것이며, audit / daily / hourly 구분으로 실행한다. 같은 파일의 fit, allocate, learn_profiles, passage_operator를 검증 및 테스트에서 가져온다.

build_od_nodes.py는 도로 중심선을 새로 만드는 코드가 아니다. 기존 GyeongbuRoute.load/project/to_direction과 #61의 TCS 영업소 결과를 재사용해 #62에서 추가로 필요한 끝점·영업소·JC·IC의 방향별 OD 노드 표를 만든다. 기존 build_route.py는 공유 노선과 원본 IC를 생성하고 build_tcs_offices.py는 TCS 영업소만 전처리한다. 이들 기존 출력에 없는 JC/끝점 OD 식별자와 좌표 근거를 추가하므로 노드 구성 파일 하나를 유지한다. 기존 노선·TCS 출력 계약은 바꾸지 않는다.
