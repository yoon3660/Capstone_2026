# #134 — 데이터 신뢰성 검증 보고서 및 검증표

> 2026-10-15 갱신. 기존 검증(`PRIOR_REPORT`), 이번 로컬 확인(`LOCAL_NEW`), 코드·설정 조회(`CODE_CONFIG`)를 구분한다. 기존 #58·#64의 선행 결과는 별도로 보존하고, #134의 추가 CTM 실행 결과는 12절에 기록한다. 모든 데이터 PASS를 주장하는 보고서가 아니다.

## 1. 판정 기준

- **PASS** — *명시한 검사*에 대해 기준 충족. 해당 현실 현상 전체가 적합하다는 뜻은 아님.
- **CONDITIONAL** — 특정 조건·범위에서 사용 가능하나 결측, 보간, 추정, 모집단 제한 또는 시나리오 가정이 있음.
- **FAIL** — 확인된 모순 또는 기존에 사전 정의한 검증 임계값 미달.
- **UNKNOWN** — 관측, 동등 모집단, 원본 계보 또는 타당성 근거가 부족하여 검증 불가.

**PASS 기준의 단위를 혼동하지 않는다.** VDS 키 PASS ≠ 관측값 무결성 PASS, 충전 알고리즘 테스트 PASS ≠ 대기시간 실측 PASS, 셀 생성 PASS ≠ 정체 재현 PASS.

## 2. 전체 데이터 검증표

| 데이터/모델 요소 | 사용 위치 | 이번/기존 검증 방법·결과 | 판정 범위 | 근거 구분 | 후속 |
|---|---|---|---|---|---|
| 경부선 노선 길이 | 공간/충전소/CTM | 노선 JSON 선언 `centerline`; DB DOWN·UP 415.058km 일치 | **PASS: 총길이**. 위치별 좌표는 별도 | LOCAL_NEW | F-07 |
| Conzone·VDS 키 | 교통 입력·수요 | 기간×방향마다 66 내부·5 외부; 교통량/속도 각 63,360행, 중복0, 상호 키 차집합0, 공간좌표 상호 불일치0, 일별 24행 | **PASS: 구조·상호 좌표** | LOCAL_NEW | — |
| VDS 관측값 | EV 진입·CTM 비교 | 둘 다 유효 54,523, 둘 다 결측 8,825, 한쪽만 12; 방향·기간별 결측률 약 13.78~14.22% | **CONDITIONAL: 관측 완전성** | LOCAL_NEW + PRIOR_REPORT #7/#8 | F-05 |
| VDS 원본 일별 검산 | 교통 집계 | 기존 `docs/T07_T08_traffic_speed.md`의 시간합=일합 검사(결측 없는 날) | **PASS: 과거 전처리 검사의 보고 범위**. 이번에 재수집·재실행하지 않음 | PRIOR_REPORT | — |
| 진입·진출 CSV 생성 | 기본 EV 수요·CTM 램프 | DOWN 2/13 1,248행, UP 2/17 1,200행; 현재 VDS로 `peak_date`+`entry_exit_profile` 재계산, 전 수치 일치, 중복0·음수0 | **PASS: 현재 코드/가공본 재현성** | LOCAL_NEW | — |
| 진입·진출 공간 정확도 | 차량 진입·목적지 | 1km 초과 관측 공백 방향별 각 9경계; 최대 DOWN9.315/UP14.126km. 실제 IC 양방향 총량이 아닌 순차분(net) | **CONDITIONAL: 추정 위치** | LOCAL_NEW + CODE_CONFIG | F-03 |
| 차로수·교통류 기본도 | CTM 병목 용량 | 차로 95구간, 노선 415.058km; 기존 차로 프로파일 실측교통량 교차검증; 현재 raw/nodelink 원본 미확인 | **CONDITIONAL: 원본/구간별 공간 신뢰** | LOCAL_NEW + PRIOR_REPORT #24 | F-07 |
| CTM 셀 구조 | CTM 활성화 시 | `seed_cells.py` 구조 검증 후 `--write`로 기본 DB에 792셀 적재(DOWN 396/UP 396); 이후 DB validate PASS | **PASS: 셀 구조·적재·내부 검증**. 실제 정체 재현과 별개 | LOCAL_NEW | F-01/F-07 |
| TCS 내부 OD | 목적지 대안·실측 대조 | 기존 TCS/구간량 교차검증 및 일부 특수영업소 처리; 기본 YAML은 `od_profile: null` | **CONDITIONAL: 관측 가능한 내부 통행** | PRIOR_REPORT #61 + CODE_CONFIG | F-03 |
| 위험률 기반 목적지 | 기본 EV 충전 수요 | 기존 평균 내부 통행거리 52.6km vs TCS 내부 37.2km; 구간량 배율 약 0.98 | **CONDITIONAL: 구간량 vs 통행거리 상충** | PRIOR_REPORT #99/#111 | F-03 |
| TCS OD 목적지 대안 | 선택적 OD | 기존 하행 거리 MAPE 53.0→8.1%; 반면 구간량 배율 0.67(약 33% 부족) | **CONDITIONAL: 내부 거리 개선 한정** | PRIOR_REPORT #99/#111 | F-03 |
| 시간대/외부 OD | 미래 도착 분포 | 기존 설 검증일 하행 MAPE 약 48%, 외부 통행 부분은 추정 비중이 큼 | **CONDITIONAL: 설 외삽 한계** | PRIOR_REPORT #62 | F-03 |
| EV 비중 5% | 차량 개수·충전부하 | 현 YAML 0.05, 주석에 #67 근거 미확정 명시. 실측 2026 설 도로상의 EV 구성비 미확인 | **UNKNOWN: 현실 비율** | CODE_CONFIG | F-04 |
| 출발 SoC holiday | 충전 필요 대수 | 기존 `docs/departure_soc.md`: 평균 약 66.5%, 33% 바닥은 행태 **가정**, 측정 아님 | **UNKNOWN: 실제 진입 SoC 분포** | PRIOR_REPORT #55 | F-04 |
| 차량 5종·곡선20·온도9 | 충전 시간·전비 | `python -m evdt.io.db validate` 오류 없음; `tests/test_charging.py` 28 tests `[100%]` | **PASS: DB 내부 제약·코드 단위 테스트**. 실측 세션 적합성 별도 | LOCAL_NEW + CODE_CONFIG | F-06 |
| 차량 구성비·온도계수의 현실 대표성 | 충전/도착 부하 | `vehicles.yaml` 5종 대표, 등록 비중 추정; `temp_efficiency.yaml` 문헌·인증 근거를 수치화, 차종별 배터리 예열 구분 없음 | **CONDITIONAL/UNKNOWN: 실제 분포·저온 현장 조건** | CODE_CONFIG | F-06 |
| 충전 목표 SoC | 점유시간/큐 길이 | 기본·#64 `habit_soc=0`, cap0.8. **별도 #112 가정 실험** habit0.85/cap0.8에서 충전시간9.5→23.7분, ≥7기 이용률12.4→31.6% (관측 비교33.5%) | **CONDITIONAL: 충전 목표의 강한 영향 확인**. 별도 실험 결과를 #64와 혼합 금지 | PRIOR_REPORT #112 + CODE_CONFIG | F-04/F-06 |
| 기본 충전기 DB | 기본 UE/DES | `charger=103`은 그룹 행, 활성 `SUM(n_units)=282` (DOWN153·UP129), station35. 전부 active | **PASS: DB 집계**; 2월 가동 정확도는 UNKNOWN | LOCAL_NEW | F-02 |
| #64 BASE215 공급 | #64 격리 UE | DOWN/UP 매핑 215개 고유ID·행별 완전 일치; ME95/NONME120, 별도 격리 DOWN112/UP103 | **PASS: 매핑 내부 일관성**, **UNKNOWN: 2월 개별 장비 정체·가동** | LOCAL_NEW + CODE_CONFIG | F-02 |
| #64 집계 QA | 과거 외부 충전이력 대조 | `validation_checks.json`: 총20회, 각 방향10일, 관측29/미관측6, 290 station-day/6,960 observed station-hour, 총 관측 시작11,405, 모의 ME8,413 | **PASS: QA 메타 존재·값 확인**. 원본 전체 이벤트 재계산 안 함 | LOCAL_NEW | F-02 |
| 충전 시작·점유의 현실 재현 | DES/UE 신뢰성 | 기존 #64 보고: 총량 편향−26.23%, 휴게소×일 WAPE78.49%, 실측 점유5,141.3h vs 모의 ME1,779.6h | **UNKNOWN: 모집단 동등성 미입증**, 수치는 조건부 기술통계 | PRIOR_REPORT #64 | F-02/F-06 |
| CTM 평균 속도 | 실측 속도 비교 | 기존 #58: MAPE 14.4%; 이번 #134 하행 2026-02-13 재검증: MAPE 14.37%, RMSE 14.39km/h, Bias +4.92km/h | **PASS: 해당 날짜·방향·표본의 속도 MAPE만** | PRIOR_REPORT #58 + LOCAL_NEW | F-01 |
| **CTM 정체 재현** | 실측 정체 비교 | 기존 #58 Recall 0.00; 이번 #134 재검증도 실측 정체 60칸 중 적중 0칸, 누락 60칸, Recall 0.00 (기준 ≥0.50) | **FAIL: 정체 재현** | PRIOR_REPORT #58 + LOCAL_NEW | **F-01** |
| 기본 CTM/OD 활성화 여부 | 기본 실행 경로 확인 | 기본 DB `cell` 792행 적재 완료. 다만 DOWN/UP YAML은 `travel_time=fixed`, `od_profile=null` | **PASS: 실행 경로 구분**. 셀 적재가 CTM 기본 활성화를 의미하지 않음 | CODE_CONFIG + LOCAL_NEW | F-01/F-03 |

## 3. 새로 확인한 중요 수치와 제한

### 3.1 VDS 로컬 관측/결측

| 기간·방향 | 전체 | 교통량·속도 모두 관측 | 둘 다 결측 | 한쪽만 결측 | 교통량/속도 결측률 |
|---|---:|---:|---:|---:|---|
| 평상시 DOWN | 15,840 | 13,646 | 2,192 | 2 | 13.84% / 13.85% |
| 평상시 UP | 15,840 | 13,634 | 2,200 | 6 | 13.89% / 13.93% |
| 설 DOWN | 15,840 | 13,656 | 2,183 | 1 | 13.78% / 13.79% |
| 설 UP | 15,840 | 13,587 | 2,250 | 3 | 14.20% / 14.22% |
| **합계** | **63,360** | **54,523** | **8,825** | **12** | — |

이 숫자는 값의 `NaN` 관측 여부 기준이며 `missing_code`의 전체 코드를 독립 재판정한 결과는 아니다. Conzone×시간 구조 24행이 곧 유효 관측 24개를 뜻하지 않는다.

### 3.2 진입·진출 대표 하루 프로파일

| 항목 | DOWN | UP |
|---|---:|---:|
| 최대 진입일 선택 | 2026-02-13 | 2026-02-17 |
| 시간대×경계 | 24×52 = 1,248 | 24×50 = 1,200 |
| 저장/메모리 재생성 수치 일치 | True | True |
| 1km 초과 **고유 공백 경계** | 9 | 9 |
| 최대 공백 | 9.315km | 14.126km |

진입 266,160/335,478 및 진출 339,873/302,470대는 **각 대표일 중간 경계에서 추정한 값**이며, 코리도 기점 진입량이 포함된 총 진입·총 진출이나 10일 합계가 아니다. 서로 다르다고 보존 오류는 아니다.

### 3.3 충전기 공급·실험 모집단

- 기본 `evdt.db`: 총 282기(모두 active), DOWN153/UP129. `charger` 103행은 충전기 그룹 행이며 대수는 `n_units`의 합.
- #64 격리 BASE215: 215개의 가상 충전 서비스 슬롯, DOWN112/UP103, ME95/NONME120. 스크립트가 별도 `isolated.db`와 런타임 입력 어댑터 사용. #64 모델 공급을 실제 2월 가동 대수로 승인한 게 아님.
- `validation_checks.json` 원문 결론 `DIAGNOSTIC_ONLY_NOT_EXTERNAL_VALIDATION_PASS`.
- 관측 29개소·미관측 6개소. 미관측을 0건으로 채우지 않음. 환경부 **제공** 이력이 ME **운영** 거래만을 포함한다고 검증되지 않음.
- 20개 모델 날짜는 각각 24시간 **독립 실행**이어서 전날 충전 점유·큐가 다음 날 이월되지 않음.
- 기존 WAPE 78.49%는 이번 로컬에서 다시 계산한 게 아니라 #64 종료 보고서 인용값.

## 4. 코드 검증과 현실 적합성 분리

1. **초기 검사 당시** `python -m evdt.io.db validate`는 셀이 0행인 상태의 마스터 데이터를 검사했다. **이후** `seed_cells.py --write`로 792셀을 적재하고 동일 검증 명령을 재실행해 PASS를 확인했다. 이는 구조·내부 제약 검증이며 실제 정체 재현의 PASS가 아니다.
2. `python scripts/seed_cells.py`로 처음에는 792개 셀의 구조를 dry-run 검증했다. 이후 `python scripts/seed_cells.py --write`를 실행해 기본 DB에 DOWN 396셀·UP 396셀을 적재했다. 차로 변경점 17개의 병합과 실측 병목 정확도는 별도 후속 검토 대상이다.
3. `python -m pytest tests/test_charging.py -q`는 사용자 로그의 **28개 성공 표시와 `[100%]`**를 확인했다. 350kW 기준 차종별 10→80% 공개 시간 ±15% 테스트 등 **코드/설정** 검증이며, DB 값과 현재 YAML의 1:1 일치 및 #64 관측 개별 세션의 예측오차 검사는 아니다.
4. 기존 `docs/ctm_validation.md`의 **정체 재현 FAIL**은 셀 생성 테스트 PASS와 양립 가능하다.

## 5. 검증 명령 및 근거 구분

### 이번 #134에서 실제 실행한 명령 (사용자 로그 확인)

```powershell
# 원본/가공 데이터 목록 조회
Get-ChildItem data\raw -Directory
Get-ChildItem data\processed -File

# 기본 SQLite 읽기 전용 통계 (당시 SQL 조회)
# corridor 2 / station 35 / charger groups 103 / activated n_units 282 / cell 0

# 셀 생성 검증만, DB 쓰기 없음
python scripts/seed_cells.py

# 마스터 DB 검사
python -m evdt.io.db validate

# 차종·충전 알고리즘의 기존 테스트
python -m pytest tests/test_charging.py -q

# 본 패키지에 포함된 재현용 읽기 전용 감사 스크립트 — 제공본이며 아직 사용자 PC에서 실행되지 않음
python scripts/audit_issue134_readonly.py
```

VDS 키/결측과 `entry_exit`의 저장본 재계산은 **이전 채팅 내 임시 Python 명령으로 실제 확인**됐다. 마지막 `audit_issue134_readonly.py`는 해당 구조 검사 및 DB/QA 확인을 **한 명령으로 재현할 수 있도록 이번에 새로 작성한 코드**이며 아직 사용자 환경의 재실행 결과는 없다. 그 실행을 완료했다고 표시하지 않는다.

### 선행 결과 문서 — 이번에 수치 재산출하지 않음

- [VDS 수집·결측 `docs/T07_T08_traffic_speed.md`](https://github.com/yoon3660/Capstone_2026/blob/main/docs/T07_T08_traffic_speed.md)
- [도로중심선 `docs/T09a_centerline.md`](https://github.com/yoon3660/Capstone_2026/blob/main/docs/T09a_centerline.md)
- [차로·교통류 #24](https://github.com/yoon3660/Capstone_2026/blob/main/docs/flow_params.md)
- [TCS OD 출처 대조 #61](https://github.com/yoon3660/Capstone_2026/blob/main/docs/T61_tcs_od_validation.md)
- [OD 거리·통행량 #99](https://github.com/yoon3660/Capstone_2026/blob/main/docs/od_validation.md)
- [OD 구간량 균형 #111](https://github.com/yoon3660/Capstone_2026/blob/main/docs/od_balance.md)
- [시간대 OD #62](https://github.com/yoon3660/Capstone_2026/blob/main/docs/od_hourly_review.md)
- [CTM 정체 재현 #58](https://github.com/yoon3660/Capstone_2026/blob/main/docs/ctm_validation.md)
- [출발 SoC #55](https://github.com/yoon3660/Capstone_2026/blob/main/docs/departure_soc.md)
- [충전량·목표 SoC #112](https://github.com/yoon3660/Capstone_2026/blob/main/docs/charge_amount.md)
- [충전 공급·실측 대조 #64](https://github.com/yoon3660/Capstone_2026/blob/main/docs/sp3_64/ISSUE_64_CLOSEOUT_20261013.md)

## 6. 판정 및 개선방향

- **구조적으로 검증된 부분:** VDS 키·좌표·범위, 대표일 진입·진출 파이프라인 재현성, 도로 길이, CTM dry-run 구조, 충전기 슬롯 매핑, 마스터 기본 제약, 충전 알고리즘 단위 테스트.
- **사용조건을 명시해야 하는 부분:** VDS 관측 결측, 진입·진출 순량 해석, TCS 내부 OD, 차로 원본 부재, #64 공급 및 운영사 모집단, 충전시간/점유시간 외적 대조.
- **실측 재현 FAIL:** 기존 CTM 정체 재현율 0.00(기준 미달).
- **현실 적합성 UNKNOWN:** EV 5%, 실제 진입 SoC, 2월 실제 충전기 가동 기수·동등 사업자 모집단, 개별 충전소 실측 대기시간, S2 최적화의 현실 개선 효과.
- **수정·대규모 재실험은 #134 밖:** 개선 후보 및 완료 기준은 [`followup_issues.md`](followup_issues.md)에 분리했다.

## 12. #134 CTM 직접 재검증 및 민감도 실험 (2026-10-14)

### 12.1 검증 조건과 결과

- 근거 구분: `LOCAL_NEW` (사용자가 실행한 PowerShell/Python 출력).
- 기존 #58의 보고값은 `PRIOR_REPORT`로 별도 보존한다.
- 방향 및 검증일: 경부선 하행(DOWN), 2026-02-13.
- CTM: DOWN 396셀, `dt=0.2분`. 기본 DB 전체에는 UP/DOWN 합계 792셀 적재.
- 실행 방식: 기본 YAML의 `travel_time=fixed` 값을 CTM으로 변경하지 않고, 별도로 `run_day` 및 `build_travel_field`를 호출하여 실험.
- 실측: VDS Conzone×시간 속도. 비교 가능 표본 1,368/1,584칸.
- 정체 정의: 속도 60km/h 미만.

| 지표 | #134 기준 실행 | 판정 |
|---|---:|---|
| 속도 MAPE | 14.37% | PASS: 기준 ≤15% |
| 속도 RMSE | 14.39km/h | 참고 |
| 속도 Bias | +4.92km/h | 참고 |
| 실측 정체 | 60칸 | 관측 |
| 정체 적중 | 0칸 | FAIL |
| 정체 누락 | 60칸 | FAIL |
| 정체 Recall | 0.0000 | FAIL: 기준 ≥0.50 |
| 정체 오경보 | 0칸 | 정체 미예측과 함께 해석 |
| 질량 보존 오차 | +0.000000대 | PASS: 내부 보존 |

속도 MAPE 통과는 정체 발생 위치·시간·지속시간의 재현 성공을 의미하지 않는다.
검증 범위는 해당 날짜의 하행 비교 가능 표본이며, 전체 설 연휴·상행·미래 예측의 정확도를 입증하지 않는다.

### 12.2 추정 진출량 민감도 실험

VDS 인접 구간 유량 차이로 계산된 `exit_share` 및 `exit_veh`를
메모리에서만 변경하여 CTM을 재실행했다.
원본 CSV·DB·YAML은 이 민감도 실험으로 수정하지 않았다.

| 실험 | 변경 경계 | 제거한 추정 진출 합계 | MAPE | 정체 Recall | 적중/60 | 오경보 |
|---|---|---:|---:|---:|---:|---:|
| 기준 | 없음 | 0대 | 14.37% | 0.0000 | 0 | 0 |
| A | 82.636km | 52,223대 | 14.17% | 0.1000 | 6 | 29 |
| B | 135.801km | 26,068대 | 14.57% | 0.0000 | 0 | 0 |
| C | 두 경계 | 78,291대 | 13.33% | 0.2833 | 17 | 56 |

모든 실험의 질량 보존 오차는 +0.000000대였다.
제거량은 하루 시간대별 VDS 유량 차이에서 계산된 **추정 진출량**의 합계이며,
실측 진출 차량의 확인된 수량이 아니다.

두 경계를 동시에 제거하면 정체 재현율이 상승하므로,
진출입 추정이 모델의 정체 형성에 영향을 미친다는 민감도 근거를 확보했다.
그러나 가상 진출 제거가 실제 도로 조건에 부합한다는 근거는 없고,
C 실험도 정체 43칸을 놓치며 오경보 56칸을 발생시킨다.

### 12.3 한계 및 후속 처리

- 82.636km의 큰 진출 추정은 실제 IC·JC 이동, 차량 축적, VDS 측정 조건을 분리하여 확인하지 못했다.
- 135~140km의 일부 3차로 설정은 `lanes_source=assumed`이므로 실제 병목 용량의 근거가 추가로 필요하다.
- 향후 정체 및 휴게소 도착 시각의 독립 예측 정확도는 UNKNOWN이다.
- 진출입 추정·차로수·병목 용량 원인 분석 및 모델 보정은 F-01 후속 이슈 범위로 분리한다.
- 기본 시나리오는 계속 `travel_time=fixed`이며, CTM 셀 적재가 기본 CTM 활성화를 의미하지 않는다.

이 절은 이전 문서의 '당시 DB cell=0', 'CTM 재실행하지 않음' 기록보다
나중에 수행한 검증의 현재 상태를 나타낸다. 이전 시점의 로그는 이력으로 보존한다.

## 13. #64 과거 실행 입력 무결성 확인 (2026-10-15)

근거: 사용자 로컬 읽기 전용 Python 검사 결과 (`LOCAL_NEW`).

### 확인된 실행 계보

- `validation_checks.json`: 전체 20개 실행, DOWN 10개·UP 10개.
- 관측 휴게소 29개, 미관측 6개.
- `10_run_input_provenance.csv`: 20행, 방향·날짜 중복 0건.
- DOWN/UP `setup.json`: 원본 격리 DB, 가상 충전기 매핑, 기준 설정의 SHA-256 기록 존재.
- 확인한 날짜별 `run_summary.json`에 실행 ID와 입력 프로파일 해시 존재.
- 비교 결과 자체의 판정은 `DIAGNOSTIC_ONLY_NOT_EXTERNAL_VALIDATION_PASS`를 유지한다.

### 날짜별 입력 파일 SHA-256 대조

| 검증 항목 | 결과 |
|---|---|
| 대상 | 기존 피크일 재사용 2개 제외, 날짜별 실행 18개 |
| 각 실행의 입력 | `volume.csv`, `through.csv`, `entry_exit.csv` |
| 비교 방법 | `run_summary.json`의 저장된 SHA-256과 현재 입력 파일의 SHA-256 직접 대조 |
| 전체 검사 | 18 × 3 = 54개 |
| 해시 일치 | **54/54** |
| 불일치 및 누락 | **0건** |
| 판정 | **PASS: 확인한 날짜별 입력 파일 무결성** |

### 검증 범위와 한계

- 해시 일치는 현재 파일과 기록된 입력의 바이트 단위 일치를 의미한다.
- 기존 피크일 재사용 2개는 이번 54개 검사 대상이 아니다.
- 양방향 `setup.json`의 원본 DB·설정·매핑 해시는 존재를 확인했으나, 이번 검사에서 모두 현재 원본과 직접 재대조한 것은 아니다.
- 과거 실행 당시의 Git 커밋은 이 검사로 확인되지 않는다.
- 충전시설의 2026년 2월 실제 가동 상태와 운영사 모집단 동등성은 여전히 UNKNOWN이다.
- 기존 20개 UE 시뮬레이션을 재실행하지 않았고 모델·DB·입력 파일을 수정하지 않았다.

**결론:** #64의 18개 날짜별 실행 입력 파일에 대해 저장된 해시와 현재 파일의 일치가 확인됐다. 실행 추적성 근거는 보강됐지만, 현실 재현성 PASS로 해석하지 않는다.
