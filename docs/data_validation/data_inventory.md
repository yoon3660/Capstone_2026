# #134 — 전체 데이터 인벤토리 및 데이터 계보

> 2026-10-14 · 대상: `yoon3660/Capstone_2026`, 작업 브랜치 `branch/134`의 사용자 **로컬 조회 결과**와 GitHub `main` 코드. 원본 대용량 파일은 Git에서 추적하지 않는 로컬 데이터이며, 2026-10-15 기준 #134 `branch/134`의 선행 문서(`9f412df`)와 검증 결과를 함께 반영한 정정 작업본이다. **원격 저장소에 자동 반영한 것은 아니다.**
>
> 이 표에서 **파일 존재·행 수·구조 일치**는 실측 정확도 PASS를 의미하지 않는다. 기본 시나리오와 #64 격리 실험의 입력을 혼합하지 않는다.

## 1. 보유 원본 및 수집 조건

| 구분 | `data/raw/` 경로 | 확인 수량 | 출처·범위·주의 |
|---|---|---:|---|
| 도로 IC·휴게소 보조 정보 | `ex_route_20260925_141537/` | 3개, 0.11MB | 과거 수집본; 현재 사용 여부는 개별 명시가 필요 |
| 도로 IC·휴게소 보조 정보 | `ex_route_20261007_140859/` | 3개, 0.11MB | `gyeongbu_route.json.meta.raw_dir`가 지목하는 수집본 |
| VDS 평상시 | `ex_vds_base202603_20260306_20260315_20260925_140322/` | 429개, 38.25MB | `2026-03-06~03-15`; manifest의 `volume/speed/volume_daily` |
| VDS 설 연휴 | `ex_vds_seollal2026_20260213_20260222_20260925_135419/` | 429개, 38.34MB | `2026-02-13~02-22`; 같은 3종 |
| 충전기 API 원본 | `highway_chargers_20260918_125202_all/` | JSON 3개, 1.56MB | 9월 API 스냅샷을 2월 직접 관측으로 소급하지 않음. **#64에서 과거 개소·운영·조달 자료를 대조하여 282 후보 포함·제외를 판정하고 BASE215 공급 선정 완료** |
| 환경부 제공 충전 이력 | `sp3_64/20260306_2026년2월_환경부_충전기_충전이력_상세내역.xlsx` | 1개, 42.64MB | 2026년 2월 충전 시작·종료 관측의 원본; 제공기관과 운영사 동일성 미확인 |
| TCS OD | `tcs_od/tcs_od_202602.csv`, `tcs_od/tcs_od_202603.csv` | 2개, 105.58MB | **#61 TCS Matrix/포털 조회 교차검증, 경부선 유효 영업소 42개 구축·coverage 검증 완료**. 자료의 내부 OD 관측범위와 #62의 외부·시간대 OD 추정 성능은 분리 |
| 도로중심선 CSV | `ETC_S0_07_04_345774.csv` | 1개 | `scripts/load_centerline.py` 원본 |
| 표준노드링크 SHP | 과거 `data/raw/nodelink/` | #134 조회 시 원본 SHP 로컬 경로 미확인 | **#24에서** F/T 노드 연결·geometry 및 표준노드링크 원본 `LANES`를 재대조해 차로수를 검증·확정함. #134 PC에서 원본 경로를 다시 찾지 못했다는 것은 선행 검증을 부정하지 않으며 재생성·버전 추적 범위만 남김 |

파일 개수·용량은 사용자 제공 PowerShell `Get-ChildItem` 출력에 근거. **XLSX 원본은 별도 조회로 561,710개 데이터행(헤더 포함 561,711행)·13열·시트 1개를 확인했다.** 파일 SHA-256과 개별 거래 운영사 모집단은 아직 검증되지 않았다.

## 2. 원본 → 가공 → 모델 소비 경로

| 영역 | 입력·변환 근거 | 처리 결과/DB | **실제 소비** 및 검증 역할 | 확인 상태 |
|---|---|---|---|---|
| 도로중심선·방향·휴게소 | 원본 CSV + `ex_route` → `scripts/load_centerline.py`, `scripts/build_route.py` | `centerline_gyeongbu.parquet`, `gyeongbu_route.json`; DB `corridor` | 도로 길이, `offset_km`, 충전소·CTM 셀 공간 기준 | 로컬 노선 메타, DB 길이 확인 |
| VDS 교통량·속도·구간 | 두 `ex_vds_*` + 노선/IC → `scripts/build_traffic.py` | `traffic_gyeongbu.parquet`, `speed_gyeongbu.parquet`, `conzone_gyeongbu.parquet` | EV 입력 프로파일, CTM 실측 비교, 차로수 교차검증 | 로컬 구조·결측 검산 완료 |
| 하루 진입량·통과율 | VDS → `scripts/build_demand_profile.py` | `volume_gyeongbu_{down,up}_seollal.csv`, `through_*` | 기본 시나리오 `volume_profile`; `through_profile`은 `entry_exit_profile` 설정 시 목적지 선정에서 대체됨 | 경로 확인, 값 검산 별도 |
| 중간 진입·진출 | VDS → `scripts/build_entry_exit_profile.py`, `src/evdt/io/entry_exit.py` | `entry_exit_gyeongbu_{down,up}_seollal.csv` | **기본 EV 진입·목적지 추정에 사용**, CTM 선택 시 램프 수요 | 로컬 재계산과 저장본 일치 |
| TCS OD | `tcs_od_202602/03.csv` → `scripts/build_tcs_od.py`, `scripts/build_tcs_od_gyeongbu.py` 등 | `tcs_od_gyeongbu.parquet` 등 중간표 | #99 등 실측 비교·대안 목적지 수요; **기본 `od_profile: null`** | 원본/최종 가공본 존재, 선행 보고 활용 |
| 차로수 | 표준노드링크/중심선 → `scripts/audit_lane_mapping.py`, `scripts/build_lane_profile.py` | `lane_mapping_audit.parquet`, `lanes_gyeongbu.parquet` | CTM 셀 차로수/병목 | **#24: 원본 링크 대조·차로 변경점 292개·교통류 파라미터 선정 완료**. 현재 원본 버전 경로·CTM 셀에서 17개 변경점 병합 영향은 별도 |
| 교통류 모수 | VDS·차로·`config/flow_params.yaml` → `scripts/estimate_flow_params.py` | `flow_params_by_conzone.parquet`, 설정 YAML | `seed_cells.py`는 **설정 YAML과 차로 프로파일**을 읽음; 단순히 parquet을 항상 직접 사용하는 것은 아님 | 코드 경로 확인, 과거 생성본과 개별 대조 미실시 |
| 충전기 공급 | 2026-09 API → `scripts/load_chargers.py` | DB `station`, `charger` | `runner.run_once` → `read_station_chargers` → `station_specs`/DES | 기본 DB 활성 282기 확인 |
| BASE215 격리 공급 | #64의 과거 설치·개소·운영·조달 근거 및 282 후보 분류 → `scripts/sp3_64_daily_base215_runner.py` | #64 격리 `isolated.db`, `model_unit_to_operator.csv` | **#64에서 ME95+비ME120=BASE215 공급 선정·20개 실행 적용 완료**. 기본 DB와 별도 | **PASS: #64 역사적 공급 판정 및 215 슬롯 내부 매핑**. 2월 개별 포트 정상가동 전수 관측과 사업자별 거래 범위는 별도 |
| 차량·충전곡선·온도 | `config/vehicles.yaml`, `config/temp_efficiency.yaml` → `scripts/seed_vehicles.py` | DB `vehicle_class`, `charge_curve`, `temp_efficiency` | `runner` 및 `world/charging.py`·UE/DES의 충전시간 | 마스터·단위 테스트 통과; **현재 기본 DB↔로컬 YAML 전 필드 비교 PASS(5·20·9행, 차이0)**. 과거 #64 격리 DB는 미검사 |
| 충전 실측 | #64 XLSX → `analyze_sp3_64_*`, `sp3_64_validate_20day_base215.py` | 관측 집계 CSV, `compare20_v1/validation_checks.json` | 충전 시작·점유시간·분포 오차의 **조건부** 기술통계 | JSON 필드·매핑 확인, 이벤트 원자료 20회 재집계 미실시 |
| 합성 EV | `volume`+`entry_exit`+EV 비율+차량·SoC 설정 → `src/evdt/runner.py::build_demand`, `io/synthetic_ev.py` | `synthetic_evs_...parquet`, 실행 로그 | UE·DES 차량 수·이동·충전 수요 | **#55 `holiday` SoC 가정 선정·20시드 비교 완료**, **#82 첫 충전 시작 SoC 36.4% 문헌대조 선행 통과 판정(참고 30~34%)**. 실제 진입 SoC 분포·EV 비중 5% 외적 관측은 별도 |
| CTM 셀 | 노선 JSON+`lanes`+`flow_params.yaml`+휴게소 DB → `scripts/seed_cells.py` | SQLite `cell` | CTM 사용시 기반 셀 | **#134에서 792셀(DOWN396/UP396) 기본 DB 적재·`validate` PASS**, 질량보존 0대. 평균속도 MAPE14.37% PASS, 정체 Recall0 FAIL은 별도 현실 검증 |

### 계보 개요

```text
중심선 CSV + IC/휴게소 ──> route/centerline ──> 휴게소 offset 및 CTM cell 공간 기준
VDS 원본 ─────────────────> traffic/speed/conzone ──> (a) volume/through
                                                   └─> (b) entry_exit ─┐
TCS 원본 ─────────────────> processed TCS OD ───────────────────────────┤ [기본 OD 미사용]
차종·온도 설정 ──────────> 차량/충전곡선 DB ────────────────────────────┤
충전기 API ──────────────> 기본 충전기 DB 282기 ───────────────────────┤
                                                                     └─> 기본 UE/DES
#64 공급 근거 + 가상 ID ─> 격리 BASE215 공급 ──> 별도 UE 20회 ──> 환경부 제공 관측 비교
```

## 3. 기본 설 연휴 시나리오 (정적 YAML, CLI 변형 가능)

| 설정 | DOWN / UP | 영향 |
|---|---|---|
| `volume_profile` | 각각 방향별 `volume_gyeongbu_*_seollal.csv` | 교통량 기반 진입 수요 |
| `entry_exit_profile` | 각각 방향별 `entry_exit_gyeongbu_*_seollal.csv` | 중간 진입 및 추정 목적지 |
| `od_profile` | **`null` 양방향** | 직접적인 TCS OD 목적지 샘플링 안 함 |
| `travel_time` | **`fixed` 양방향** | 현 기본 실행은 CTM 정체 기반 통행시간 아님 |
| `ev_share` | **0.05 양방향** | 근거 확정 전 잠정 EV 비중 (#67) |
| `departure_soc` | **`holiday` 양방향** | 명절 진입 SoC 행태 가정; 실측 측정값 아님 |
| `target_soc_cap` | **0.8 양방향** | 충전 목표 상한 |
| `habit_soc` | **0.0 양방향** | 이동에 필요한 만큼 충전하는 경향; 현실 점유시간 과소 위험 |
| `ev_count_method` | **DOWN/UP 모두 `fixed`** (`branch/134` 기본 YAML) | #53에서 Poisson 생성기를 구현·검증했지만 현재 기본값은 시간당 대수 fixed. Poisson 변동성을 기본 시나리오 결과에 소급 적용하지 않음 |
| `demand.layers` | **DOWN/UP 기본은 `[]`** | `adoption`은 EV10%+기회충전20%, `congested`는 EV15%+기회충전20%; 기본 대비 차이는 두 가정의 결합 효과. 순수 EV 보급률 효과가 아님 |

## 4. 이번 로컬 검사 결과의 데이터별 단위·범위

| 대상 | 확인된 내용 | 범위 해석 |
|---|---|---|
| 도로 JSON | `meta.source=centerline`, `raw_dir=ex_route_20261007_140859`; IC 54·휴게소 37 | 37은 **노선 메타의 휴게소 수**, DB 35는 **충전 대상 휴게소 수**; 반드시 같은 모집단 아님 |
| DB 도로 | DOWN/UP 각각 **415.058km** | 노선 총길이 일치. 개별 좌표 검증과 분리 |
| VDS 원본 manifest | 설 2/13~22, 평시 3/6~15; volume/speed/volume_daily | 각각 10일의 자료 |
| VDS 가공 `traffic` / `speed` | **각 63,360행**, DOWN/UP 각 31,680, 설/평시 각 31,680 | 기간×방향당 66 구간×10일×24시간=15,840행 |
| Conzone 위치 | **284행**, 기간×방향당 **71**개 중 내부 66·외부 5 | 기간당 132개 사용; 외부 10개는 의도적 제외 |
| VDS 키·좌표 | 중복 키 0, 양쪽 키 차집합 0, `offset_km_start/end` 상호 불일치 0, 24시간 키 누락 0 | **키/좌표 일관성 PASS**, 독립 관측 위치 정확성은 미검증 |
| VDS 관측 | 둘 다 유효 **54,523** / 둘 다 결측 **8,825** / 한쪽만 결측 **12** | 결측률 구분별 13.78~14.22%; 값의 완전성 CONDITIONAL |
| 차로수 | **95행**, UP49 / DOWN46, 약 0~415.058km | **#24에서 원본 `LANES`와의 대조·매핑 확정**. 현재 SHP 파일 접근 및 CTM 셀 병합 17곳 효과 확인은 별도 범위 |
| CTM 셀 | DOWN396/UP396=792, `dt=0.2분`, 휴게소 DOWN19/UP16 | 구조·CFL·휴게소 경계 확인 뒤 **기본 DB 792행 적재, DB validate PASS**. 실측 정체 재현 Recall0.0000 FAIL은 별도 |
| 진입/진출 DOWN | 기준일 **2/13**, 1,248행, 시간24/고유경계52, 중간 진입 266,160·진출 339,873 | **하루 추정치**, 설 10일 합계 아님 |
| 진입/진출 UP | 기준일 **2/17**, 1,200행, 시간24/고유경계50, 중간 진입 335,478·진출 302,470 | **하루 추정치** |
| 진입/진출 공백 | `gap_km>1` 고유 경계 양방향 **각 9곳**, 최대 DOWN 9.315km, UP 14.126km | 결측 구간 사이 진출입을 한 경계에 모아 추정할 수 있음 |
| 진입/진출 재현 | 현 VDS + `peak_date`/`entry_exit_profile`로 산출한 값과 저장 CSV의 행·열·수치 **양방향 모두 일치** | **파이프라인 재현성 PASS**, 차량별 실제 목적지 적합성은 별도 |
| DB 현황 | `corridor`2, `station`35, `charger`103그룹, `vehicle_class`5, `charge_curve`20, `temp_efficiency`9, `cell`**792** | `SUM(n_units)` 기본 DB 활성 **282기**(DOWN153/UP129). **초기 감사의 cell=0 이후 적재·재검증 완료**, 기본 이동시간 `fixed` 유지 |
| #64 BASE215 | DOWN/UP mapping CSV 각 215행·고유ID215, ME95+NONME120 | **#64 조사·공급 선정 완료 및 20개 실행 적용 PASS**, 격리 조건 DOWN112/UP103. 개별 포트의 매 시간 정상가동 실측은 별도 |
| #64 QA JSON | 20 실행(양방향 10일), 관측29·미관측6, 관측 290 station-day/6,960 station-hour | **JSON 기록 확인**. 20회 전체 이벤트·해시 재검산 아님 |
| DB 검증 | `python -m evdt.io.db validate` → `OK 마스터 데이터 정합성 이상 없음` | **CTM 792셀 적재 후 재실행 PASS**. 마스터 제약 검증과 CTM 정체 재현 FAIL 분리 |
| 충전 계산 테스트 | `python -m pytest tests/test_charging.py -q` → 28개 테스트 `[100%]` | 알고리즘 단위 테스트 통과. 현재 DB↔YAML 일치는 **별도 읽기 전용 5·20·9행 대조로 PASS**, 실측 충전 행태 적합성은 미판정 |
| #134 통합 읽기 전용 감사 | `python scripts/audit_issue134_readonly.py` 2026-10-14 사용자 로컬 실행 완료 | VDS 63,360×2 중복·키 차집합·좌표 불일치0, 대표일 진입진출 재계산 일치, DB 마스터 PASS·cell0, #64 BASE215·20개 QA 메타 재확인; 재시뮬레이션 아님 |
| 환경부 원본 충전 시간 필드 | 시작일 기준 설 10일 전국 원본 188,123행 중 ` 충전시간`과 종료−시작 정확일치98,837 / 60초초과차이39,327 | 경부선 매핑 전 **전국 표본**. 29개 휴게소 불일치율로 일반화하지 않음. 표시 충전시간의 의미와 충전량 단위는 제공기관 확인 필요 |

### #134 추가 실행 계보 검증 (2026-10-15)

#64 비피크 18개 독립 실행의 `volume.csv`·`through.csv`·`entry_exit.csv` 저장 SHA-256과 실제 파일을 대조해 **54/54 일치(불일치 0)**를 확인했다. 이는 파일 입력 무결성 PASS이며 기존 피크 재사용 2개, 과거 Git 커밋, 격리 DB 원본의 모든 해시 재대조까지 완료했다는 뜻은 아니다.

## 5. 남은 계보 확인 및 사용 제한

1. **#24 차로 매핑과 #61 TCS Matrix 구축·조회값 대조는 선행 검증 완료**다. #134 PC의 SHP/중간 파일 경로·해시 재확인은 별도 작업으로 남을 수 있으나, 과거 티켓 검증을 UNKNOWN으로 뒤집지 않는다.
2. VDS→중간 진입·진출의 생성 일치는 확인됐지만 결측·순유출입(net) 구조 때문에 **실제 IC/JCT 진입·진출**의 검증은 아니다.
3. `flow_params_by_conzone.parquet`와 최신 config/셀 파라미터의 모든 값 일치는 미검증. 2026-09 생성본과 2026-10 도로 좌표계의 개별 위치 비교도 별도 과제다.
4. 현재 DB의 282기와 #64 BASE215는 **서로 다른 실험 공급 환경**이며 섞어 비교하지 않는다.
5. #64의 10일 결과는 **날짜별 독립 하루 실행**으로 이전 날 대기·점유를 이월하지 않으며, 환경부 제공 기록의 운영사 모집단 동등성이 입증되지 않았다.
6. **현재 기본 DB↔YAML 차량/곡선/온도 정합성은 PASS**로 갱신했다. 다만 이는 과거 격리DB 검증, 차종별 실측 대표성, 2026년 2월 현장 조건까지 확정하는 결과가 아니다.
7. **원본 충전시간과 경과시간은 별도 필드**이며 일부 행에서 큰 차이가 관측된다. 충전시간/실제 장비 점유의 의미를 구분하지 않으면 DES 서비스시간 보정 근거가 부정확해질 수 있다. 상세값은 검증 보고서 §12를 따른다.
8. 사용 가능 범위와 선행 검증 근거: [`validation_report.md`](validation_report.md), [`dt_credibility.md`](dt_credibility.md), [`followup_issues.md`](followup_issues.md).
