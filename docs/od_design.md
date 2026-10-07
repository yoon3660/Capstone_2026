# #62 OD 추정: 최종 선택·설계·검증·재현

기준: 2026-10-08, develop/62. 대상: [GitHub #62](https://github.com/yoon3660/Capstone_2026/issues/62).

## 최종 결론과 선택 이유

현재 채택한 개발 모델은 **실측 TCS 일별 OD 고정 + 중력 사전의 외부 OD 근사 추정 + 평일·주말·연휴별 시간 패턴 및 하루 규모 추정 + 통행 지연**이다. 월별 앞 4일을 보정하고 뒤 6일을 검증하며, 통행 지연은 상수 90km/h를 유지한다. 정확한 구간 일 교통량 제약을 충족하는 모델이나 실제 OD의 유일한 정답이 아니라 **한계를 기록한 조건부 추정 시제품**이다.

이 구성을 선택한 이유는 다음과 같다.

1. TCS 쌍별 일 합계와 비음수는 지키면서, 구간 교통량을 이용해 코리도 밖에서 진입·진출하는 통행을 추가할 필요가 있었다. 기존 한쪽 끝 출발 방식은 중간 진출입을 충분히 표현하지 못한다.
2. 시간 패턴의 날짜 유형 구분과 보정일 확대는 같은 날짜로 재평가한 원래 2일 모델보다 네 조건의 전체 MAPE를 낮췄다. 두 변화의 효과를 분리하기 위해 4일 공통 패턴 대조군도 실행했다.
3. 모든 보정일의 평균 규모를 쓰던 방식을 날짜 유형별 TCS 대비 외부 OD 비율로 바꾸자, 직전 4일 유형별 패턴 모델 대비 네 조건 모두 전체 MAPE와 RMSE가 낮아졌다. 검증일 VDS를 예측에 사용하지 않는다.
4. 추가 후보는 작은 개선과 다른 조건의 악화가 함께 나타나 반영하지 않았다. 유리한 날짜에만 다른 모델을 적용하거나 검증 관측값으로 결과를 직접 맞추지 않았다.

이는 현재까지 확인한 방법 중 선택한 구성이지, 모든 가능한 모형 중 최선임을 증명한 결과가 아니다. 검증 날짜는 개발 과정에서 이미 살펴본 자료이므로 새로운 독립 검증이라고 주장하지 않는다. 전체 MAPE가 낮아졌어도 일부 날짜·시간대 및 RMSE는 악화됐다.

**최종 실행 설정과 코드 기본값은 다르다.** 코드 기본값은 정확 제약·2일 보정·공통 패턴·공통 규모이며 그대로 유지했다. 아래 재현 명령의 approximate / 4 / calendar / calendar 옵션으로 최종 개발 모델을 실행한다. 기존에 공유한 20261007 결과 ZIP도 2일 공통 모델이며, 새 개선 결과로 바뀐 것이 아니다.

## 최종 성능과 남은 한계

모든 모델을 같은 뒤 6일의 가용 구간×시간 관측으로 비교했다. RMSE 단위는 대/시간, MAPE 단위는 %다. 2월 검증은 17~22일, 3월은 10~15일이다.

| 기간 | 방향 | 원래 2일 전체 MAPE | 직전 4일 유형별 패턴 MAPE | 최종 전체 MAPE | 최종 RMSE | 최종 새벽 0~5시 MAPE |
|---|---|---:|---:|---:|---:|---:|
| 202602 | DOWN | 56.391 | 49.327 | 47.972 | 750.788 | 92.890 |
| 202602 | UP | 36.030 | 33.978 | 31.841 | 613.916 | 38.819 |
| 202603 | DOWN | 18.429 | 15.057 | 13.957 | 372.953 | 19.263 |
| 202603 | UP | 21.490 | 17.526 | 15.459 | 444.022 | 18.510 |

2월 DOWN의 새벽 MAPE는 같은 표본에서 원래 2일 모델 137.183% → 4일 공통 모델 104.935% → 최종 모델 92.890%다. 큰 새벽 상대 오차가 이번 수정으로 새로 생긴 것은 아니다. 다만 원래 대비 새벽 RMSE는 797.591 → 827.947, 전체 RMSE는 675.242 → 750.788로 증가했다. MAPE 감소만으로 모든 지표가 개선됐다고 말하지 않는다.

2월 DOWN의 완전 24시간 구간·날짜 336개만 분석하면 하루 통과량 평균 절대 오차는 직전 모델 23.619% → 최종 21.089%다. 그러나 0~5시 MAPE는 90.911% → 91.069%다. 이 값은 위 표의 가용 새벽 시간 전체와 표본이 다르다. 19·20일 하루 편차는 줄었지만 21·22일은 커졌다. 여기서 하루 총량은 차량의 중복 없는 통행 건수가 아니라 구간을 통과한 대수다.

현재 입력·일별 중간점 모델·이동 규칙에서 20일×양방향 40조건 모두 정확한 비음수 해가 없다. 2026-02-14 DOWN의 신탄진–상서 / 상서–회덕 관측은 67,345 / 44,151대지만 고정 TCS 차이는 3대이며 상서의 추가 진출입을 허용하지 않는다. 이 조합의 최소 최대오차는 11,598.5대/일이다. 관측 의미·단면 대응·날짜 경계·이동 규칙 중 무엇이 실제 원인인지는 확정하지 못했다. 이를 센서 오류나 실제 램프 통행으로 단정하지 않는다.

**#62 완료조건을 전부 충족했다고 판정하지 않는다.** 좌표·추정 코드·시간 지연·출력·날짜 분리 지표·기존 방식 비교·식별 가능한 4지점 복원 검사는 구현했다. TCS 34,440개 날짜·방향·쌍 합계 보존과 비음수도 검사했다. 그러나 정확 일별 교통량 제약과 2월 DOWN 새벽 정확도는 미해결이다. 근사 허용이 완료조건에 부합하는지는 팀의 기준 확인이 필요하다.

## 추가 개선안을 제외한 이유

| 시험한 방법 | 실제 결과와 결정 |
|---|---|
| 단거리·장거리별 시간 패턴 | 초기 실행은 수렴 실패. 한도를 늘려 수렴시킨 2월 DOWN도 전체 47.972 → 48.242%, 새벽 92.890 → 94.442%로 악화돼 제외 |
| 통행시간 가정 70·90·110km/h | 2월 DOWN 보정일의 동일 5,130개 관측에서 선택된 110을 네 조건에 고정 적용. 세 조건의 MAPE는 줄었지만 2월 UP은 악화. 기본값 90 유지. 실제 속도를 측정한 결과가 아님 |
| 외부 OD의 새벽 비중 별도 학습 | 보정일만으로 조정했고 하루 OD 합계 보존. 효과가 거의 없고 2월 DOWN이 악화돼 제외 |
| 유형별 패턴의 공통 사전 페널티 0.01 → 0.1 | 2월 전체 MAPE는 개선됐지만 2월 DOWN 새벽과 3월 전체 MAPE가 악화돼 제외 |
| 지역별 TCS 기반 외부 규모 | 40km 지수 커널·전체/지역 비율 50:50 혼합. 개선 폭이 작고 2월 DOWN이 악화돼 제외 |
| 50km 이하 TCS 기반 하루 규모 | 네 조건 중 세 조건의 MAPE 악화로 제외 |

관측 하루 합계를 모델에 직접 적용한 오차 분해는 진단에만 사용했다. 이는 실행 가능한 OD 해나 새로운 성능 결과가 아니다. 구간별 독립 조정은 TCS·네트워크 제약을 보장하지 않는다. 시험 코드·자료는 별도 작업 폴더에 보관하며 최종 저장소 파일에 넣지 않는다.

추가 개선 여지는 남아 있다. 현재 보정 자료의 설 연휴는 16일 하루뿐이라 귀성·귀경 단계별 차이를 배우기 어렵다. 추가 자료와 새 검증 날짜, 관측 의미 및 이동 규칙 확인이 필요하며 자료를 늘리면 반드시 큰 폭으로 좋아진다고 보장하지 않는다.

## 설계와 가중치


공유 중심선 길이 415.057836km에서 UP은 부산→서울 기점거리, DOWN은 공유 길이에서 UP 기점거리를 뺀 값이다. 끝점·TCS 영업소·JC·보충 IC를 별도 식별자로 보존한다. 가까운 TCS/JC를 자동 병합하지 않는다. JC 19개는 API 원본 좌표 투영, 언양·옥산 2개는 표준 링크 원본에서 본선과 연결 고속도로가 공유하는 노드 좌표를 투영한다. 최소 중심선 투영 거리를 가진 실제 연결 노드를 대표 위치로 선택하며 모든 램프의 중심이라는 의미는 아니다. 후보·원본 링크 식별자·좌표·CRS·해시는 아래 좌표 근거와 별도 검증 근거 ZIP의 T62_validation_summary.json 좌표 증거에 기록했다.

구간 관측은 콘존 중간점을 사용한다. 포털 집계의 대표 VDS와 정확한 단면이 미확정이므로 검지기 후보 좌표를 자동 채택하지 않는다. 구간 일 합계는 24시간 모두 관측된 경우만 사용하고 시간별 결측은 학습·평가에서 제외한다.

## 일별 OD

TCS 쌍별 실측 일 합계는 고정한다. 추가 OD는 끝점·영업소·JC 등의 허용 진출입을 잇는 전방 경로이며 영업소 간 추가 OD는 중복 수요 방지를 위해 제외한다. 끝점, 상서, 금강휴게소 정책은 estimate_od_daily.py의 apply_rules에 기록하며 나머지 이동 가능 여부는 가정이다.

고정 TCS 통과량 f와 경로 통과 행렬 A에 대해 추가 OD x를 추정한다. 중력 사전 q는 `(거리 + 10km)^(-1.5)`에 관측 잔여량의 중앙값 규모를 곱한다. 10km는 매우 가까운 노드 쌍에서 사전이 폭증하지 않도록 하는 완화이고 1.5는 시험 가정이다. 실제 통행거리 분포로 보정한 값이 아니다.

정확 모드에서는 `A x = y - f, x >= 0` 아래 `mean((x/q - 1)^2)`를 최소화한다. 사전의 절대 대수가 큰 경로만 목적함수를 지배하지 않도록 상대 이탈을 사용한다. 이는 q별 역제곱 가중 최소제곱이다. 실행 가능성을 먼저 검사하고 독립적인 잔차·비음수 검사를 수행한다. 불가능하면 최소 최대오차를 보고하고 저장 전에 중단한다. 현재 40조건 모두 불가능하므로 정확 모드 실제 결과는 없다.

근사 모드는 기존 시제품 재현을 위해 명시적으로 선택한다. 목적은 `mean(((A x + f - y)/max(y,100))^2) + 0.001*mean((x/q - 1)^2)`다. 100대 하한은 작은 관측치의 상대오차가 폭증하는 것을 제한한다. 0.001은 민감도 시험 설정이며 검증일 성능을 보고 고른 값이 아니다. 정확 제약 모드의 목적함수에 이 값은 영향을 주지 않는다.

## 시간대 배분

보정일 일별 OD를 진입점별 24시간 softmax 확률로 배분해 쌍별 일 합계를 보존한다. 사전은 가장 가까운 downstream 콘존의 보정일 시간별 평균을 정규화하며 불완전할 때 전체 보정일 평균을 사용한다. 관측점까지 거리/90km/h 지연을 인접 두 도착 시간대로 나눠 적용하고 자정 이월 및 마지막 날짜 이후 tail을 보존한다. 상수 속도는 가정이며 실측 속도가 아니다.

시간대 목적함수는 `mean((구간 시간오차/max(관측,100))^2) + 0.01*mean((24*(시간비율-사전비율))^2)`다. 24 배율은 균등 확률 1/24 정도의 편차를 1 단위로 표현한다. 0.01도 시험 설정이다. 일별 단계는 출발일·통과일을 구분하지 않는 근사이며 시간대 지연으로 그 일별 한계가 사라지는 것은 아니다.

## 학습·검증 및 식별성

최종 개발 모델은 2월 13–16일 및 3월 6–9일을 각각 보정하고 뒤 6일을 검증한다. 검증 VDS를 학습에 쓰지 않으며 검증 TCS는 입력으로 사용한다. 외부 OD는 같은 날짜 유형의 보정일 외부 OD 합계에 검증일 TCS 총량/해당 보정일 TCS 총량을 곱한다. 보정 자료에 없는 유형은 전체 보정일로 처리한다. 따라서 TCS가 주어진 조건부 VDS 재현 검증이며 월간 이전 예측이 아니다.

전체 전방 6개 OD를 허용한 4지점은 3개 구간 교통량만으로 유일 식별할 수 없다. 복원 테스트는 구조적으로 알려진 3개 경로에 한정한다. 인접 경로와 겹치는 비인접 경로 모두에서 미지 OD 대수를 관측으로 복원하며, 부족한 관측으로 정답을 주장하지 않는 시험도 포함한다. 정확 모드에서 식별 불가능한 경우에는 사전에서 가장 가까운 해를 선택한다.

## 언양·옥산 JC 좌표 근거

API 원본에는 두 JC가 없어 ITS 표준 링크 MOCT_LINK에서 본선과 연결 고속도로가 공유하는 F_NODE/T_NODE를 추출했다. 연결 ID가 동일하고 좌표가 일관적인 후보 중 공유 중심선과의 투영 이격이 최소인 실제 연결 좌표를 선택했다. CRS는 기존 원본 처리와 같은 EPSG:5186이다. 분기점 전체의 중심이나 모든 램프 위치를 뜻하지 않는다.

| JC | 선택 노드 ID | 위도 | 경도 | UP 기점거리 km | 중심선 이격 km | 기존 경계 추정 km |
|---|---|---:|---:|---:|---:|---:|
| 언양 | 1960013608 | 35.569927442 | 129.130894557 | 39.910941599 | 0.003306195 | 48.959918 |
| 옥산 | 2710219801 | 36.704463439 | 127.347267481 | 313.922278858 | 0.004083544 | 318.182918 |

언양의 근거 링크는 1960027600 / 1960028400 / 1960037645, 옥산은 2710659901 / 2710683900 / 2710684100이다. 선택하지 않은 연결 후보도 검사표에 보존한다. 기존 추정과의 차이는 각각 약 9.049km, 4.261km다. 관측값을 이용해 위치를 맞춘 것이 아니라 원본 도로 연결과 좌표로 선택했다.

좌표계는 이 프로젝트의 기존 표준 노드·링크 읽기와 data/raw/README.md에 명시된 EPSG:5186을 사용해 WGS84로 변환했다. 원본 SHP/DBF, 공유 노선 및 입력 노드의 SHA256을 설정 파일에 기록했다. 국가교통정보센터 자료 경로: https://www.its.go.kr/nodelink/nodelinkRef

근거 자료: [ITS 표준 노드·링크](https://www.its.go.kr/nodelink/nodelinkRef). 선택 후보와 원본 SHA256은 별도 검증 근거 ZIP의 T62_validation_summary.json의 od_jc_coordinate_evidence 및 coordinate_parameters에 보존한다.

## 입력·출력 및 재현

프로젝트 루트에서 실행한다. 입력 데이터는 Git에서 제외되므로 별도로 준비한다.

필수 입력: data/processed의 gyeongbu_route.json, od_nodes_gyeongbu_supplemented.parquet, conzone_gyeongbu.parquet, tcs_od_gyeongbu.parquet, traffic_gyeongbu.parquet, od_input_audit.csv. 좌표 완료 검사에는 od_jc_coordinate_evidence.csv도 필요하다. 초기 노드 생성에는 기존 IC JSON, JC 보완에는 MOCT_LINK의 SHP/SHX/DBF와 pyshp·pyproj 환경이 필요하다.

```powershell
python scripts/estimate_od_daily.py audit
python scripts/build_od_nodes.py coordinates --shp-file data/raw/nodelink/MOCT_LINK.shp --nodes-file data/processed/od_nodes_gyeongbu_supplemented.parquet --route-file data/processed/gyeongbu_route.json --output-dir data/processed/jc_coordinates
```

좌표 출력의 보완 노드 parquet를 supplemented 입력으로 사용하고 좌표 근거 CSV·설정 JSON을 processed에 둔다. 보충 JSON은 build_od_nodes.py의 --jc-coordinate-file 입력으로도 사용할 수 있다. 기존 결과와 다른 새 디렉터리에 근사 시제품을 생성한다.

```powershell
python scripts/run_od_validation.py run --processed-dir data/processed --results-dir data/processed/od_validation_t62 --daily-constraint-mode approximate --train-day-count 4 --profile-mode calendar --external-scale-mode calendar --speed-kmh 90 --export-dir data/processed/od_export_t62
python scripts/run_od_validation.py feasibility --processed-dir data/processed --output-dir data/processed/od_feasibility_t62
python scripts/run_od_validation.py check --processed-dir data/processed/od_export_t62 --results-dir data/processed/od_validation_t62 --feasibility-dir data/processed/od_feasibility_t62 --report data/processed/t62_completion.json
```

완료 검사 --processed-dir에는 노드·콘존·TCS·traffic·audit·좌표 근거와 네 내보내기 파일을 함께 준비해야 한다. 위 예의 od_export_t62는 내보내기 파일만 자동 생성하므로, 검사 전에 동일 입력 파일들을 함께 복사해야 한다. 정확 제약을 검사하는 현재 완료 검사는 실패 상태로 종료하는 것이 예상 결과다. 명령행 기본 모드는 exact이며 현재 자료는 저장 전에 중단한다. approximate를 정확 제약 결과로 표시하지 않는다.

출력 이름: od_gyeongbu_<DOWN 또는 UP>_<202602 또는 202603>.parquet 및 각각의 .parameters.json. 설정에는 날짜 분할·가중치·가정·입력 및 결과 SHA256을 기록한다. 주요 열은 date, departure_hour, direction, start_node, end_node, start_offset_km, end_offset_km, distance_km, volume_veh, source, split이다. volume_veh는 실수 기대 대수다. measured_tcs_daily의 일 합계는 실측이나 시간 배분은 추정이며, 검증 외부 OD는 predicted_external_daily_from_training이다.

## 저장소 파일 통합과 검증

| 파일 | 통합한 역할 |
|---|---|
| scripts/build_od_nodes.py | 기본 노드·보충 IC·JC 좌표 근거 생성: build / supplement / coordinates |
| scripts/estimate_od_daily.py | 입력 검사·이동 규칙·일별 제약·일별 및 시간대 추정: audit / daily / hourly |
| scripts/run_od_validation.py | 기간·방향 실행·오차 집계·기존 방식 비교·정확 실행 가능성·완료 검사: run / feasibility / check |
| tests/test_od.py | 관련 OD 회귀 검사를 한 파일로 통합 |
| docs/od_design.md | 최종 결론·설계·제약·재현·자료 제공 안내를 한 문서로 통합 |

커밋 대상은 위 5개다. 테스트 경로 설정은 test_od.py 내부로 옮겨 기존 conftest.py 변경을 원복했다. 최종 결론·핵심 수치·좌표·가중치·재현 안내는 이 문서에 모았다. 전체 상세 수치 JSON은 별도 T62_validation_evidence_20261008.zip에 보존하며 커밋에 포함하지 않는다. 데이터 파일도 Git에서 제외한다. 이미 커밋된 JSON의 제거와 conftest.py 원복은 다음 정리 커밋에 함께 반영해야 최종 PR 파일 구성이 5개가 된다.

build_od_nodes.py는 기존 GyeongbuRoute.load/project/to_direction 및 #61 TCS 영업소 결과를 재사용해 OD 노드 표를 만든다. 기존 build_route.py·build_tcs_offices.py·공유 노선 자체는 변경하지 않았다. 입력 생성, 추정, 검증은 서로 다른 실행 역할이므로 이미 통합된 세 파일을 유지한다. 검증 코드는 추정 함수를 가져와 중복 구현을 피한다. 테스트는 팀원의 parquet 사용에는 필요하지 않지만 이후 모델 수정의 회귀 검사이므로 유지한다.

최근 코드 검증은 전체 테스트 572개·ruff 통과다. 이번 파일 축소는 테스트의 경로 설정 위치와 검증 근거 보관 위치만 바꿨으며 추정 코드·가중치·모델 결과는 변경하지 않았다. 전체 테스트·코드 검사, 별도 근거 JSON 파싱, 최종 5개 파일 복사본 해시와 git diff --check를 확인했다.

## 데이터 배포와 준비

코드 커밋과 데이터 첨부를 분리한다. 아래 링크는 기존 PR 댓글에 공유한 자료다. 결과 ZIP은 기존 2일 보정·공통 패턴 모델이며 최종 4일 개선 모델과 구분한다.

| 목적 | 첨부 ZIP | 포함 내용 | 첨부 링크 |
|---|---|---|---|
| OD 결과 사용 | T62_results_20261007.zip | 202602/202603 UP/DOWN parquet 4개와 설정 JSON 4개 | [T62_results_20261007.zip](https://github.com/user-attachments/files/33161978/T62_results_20261007.zip) |
| 추정 재실행 | T62_inputs_20261007.zip | 동일 노선·영업소·노드·콘존·TCS·traffic·audit·좌표 근거·IC 스냅샷 | [T62_inputs_20261007.zip](https://github.com/user-attachments/files/33162354/T62_inputs_20261007.zip) |

각 ZIP에 T62_MANIFEST.json(파일 경로·크기·SHA256)과 T62_README.txt를 포함한다. 프로젝트 루트에 풀면 data/processed 및 data/raw 경로에 배치된다. 기존 데이터가 있는 팀원은 먼저 해시를 비교하고, 서로 다른 노선 기준 자료를 섞어 덮어쓰지 않는다. 배포 입력은 노선과 교통량의 동일 스냅샷을 함께 제공한다.

결과만 사용하는 경우 results ZIP만 받는다. 추정 재실행은 inputs ZIP을 받고 다음 명령을 실행한다. 기존 결과를 덮어쓰지 않는 새 출력 폴더를 사용한다.

```powershell
# 기존에 공유한 2일 공통 모델 ZIP의 재현
python scripts/run_od_validation.py run --processed-dir data/processed --results-dir data/processed/od_validation_shared --daily-constraint-mode approximate --train-day-count 2 --profile-mode shared --external-scale-mode shared --speed-kmh 90 --export-dir data/processed/od_export_shared
```

ZIP에 포함한 processed 파일: gyeongbu_route.json, centerline_gyeongbu.parquet, tcs_offices_gyeongbu.parquet, od_nodes_gyeongbu.parquet, od_nodes_gyeongbu_supplemented.parquet, conzone_gyeongbu.parquet, tcs_od_gyeongbu.parquet, traffic_gyeongbu.parquet, od_input_audit.csv, od_jc_coordinate_evidence.csv, od_jc_coordinate_parameters.json, ic_junction_coordinates.json. 원본 IC는 data/raw/ex_route_20260919_152634의 ic_gyeongbu.json·ic_all.json을 함께 제공한다.

### 원본부터 재현할 때

| 카테고리 | 원본·출처 | 기간·저장 위치 및 기존 처리 |
|---|---|---|
| TCS 실측 OD | [도로공사 전체 영업소간 교통량 매트릭스](https://data.ex.co.kr/portal/fdwn/view?num=39&requestfrom=dataset&type=TCS) | 월 단위 202602·202603 CSV. data/raw/tcs_od/tcs_od_202602.csv 및 tcs_od_202603.csv. #61 build_tcs_od.py 이후 영업소·경부 OD 전처리 사용 |
| 구간 교통량·속도 | [도로공사 공공데이터 포털](https://data.ex.co.kr/) | 기존 fetch_traffic.py로 2026-02-13~22 / 2026-03-06~15 수집 후 build_traffic.py 실행. 두 ex_vds 기간 폴더를 사용. 별도의 전국 VDS 지점 월 ZIP은 이번 추정 입력 자체가 아닌 원인 조사용 |
| IC·영업소 | [도로공사 OpenAPI 안내](https://data.ex.co.kr/guidedown/openoasis_guide.pdf) | 기존 build_route.py·build_tcs_offices.py로 수집. API 키는 각자 .env에 준비. 동일 입력 재현에는 inputs ZIP의 IC 스냅샷 사용 |
| 도로 중심선 | 기존 프로젝트의 도로 중심선 CSV | 기존 load_centerline.py와 build_route.py 사용. 원본 ETC_S0_07_04_345774.csv의 정확한 개별 다운로드 페이지와 배포본은 이번 작업에서 확인하지 못했으므로 임의 링크를 적지 않음. 기존 팀 원본 또는 ZIP의 centerline·route 스냅샷 사용 |
| 누락 JC 연결 좌표 | [ITS 전국 표준 노드·링크](https://www.its.go.kr/nodelink/nodelinkRef) | MOCT_LINK.shp / .shx / .dbf를 data/raw/nodelink에 배치 후 build_od_nodes.py coordinates 실행. 새 배포본은 기존 결과와 좌표가 달라질 수 있어 manifest의 원본 해시와 비교 |

큰 전국 원본은 ZIP에 포함하지 않는다. 입력 ZIP으로 #62 추정을 다시 실행하는 데 원본 다운로드는 필요하지 않다. 전국 원본에서 전처리까지 다시 만드는 경우에는 #61 및 기존 노선·교통량 파이프라인도 필요하며, 위 표만으로 원본 배포본의 동일성을 보장하지 않는다.
