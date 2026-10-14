# #134 — 전체 데이터 인벤토리 및 데이터 계보

> 2026-10-14 · 대상: `yoon3660/Capstone_2026`, 작업 브랜치 `branch/134`의 사용자 **로컬 조회 결과**와 GitHub `main` 코드. 원본 대용량 파일은 Git에서 추적하지 않는 로컬 데이터이며, 이 문서는 GitHub에 아직 커밋되지 않았다.
>
> 이 표에서 **파일 존재·행 수·구조 일치**는 실측 정확도 PASS를 의미하지 않는다. 기본 시나리오와 #64 격리 실험의 입력을 혼합하지 않는다.

## 1. 보유 원본 및 수집 조건

| 구분 | `data/raw/` 경로 | 확인 수량 | 출처·범위·주의 |
|---|---|---:|---|
| 도로 IC·휴게소 보조 정보 | `ex_route_20260925_141537/` | 3개, 0.11MB | 과거 수집본; 현재 사용 여부는 개별 명시가 필요 |
| 도로 IC·휴게소 보조 정보 | `ex_route_20261007_140859/` | 3개, 0.11MB | `gyeongbu_route.json.meta.raw_dir`가 지목하는 수집본 |
| VDS 평상시 | `ex_vds_base202603_20260306_20260315_20260925_140322/` | 429개, 38.25MB | `2026-03-06~03-15`; manifest의 `volume/speed/volume_daily` |
| VDS 설 연휴 | `ex_vds_seollal2026_20260213_20260222_20260925_135419/` | 429개, 38.34MB | `2026-02-13~02-22`; 같은 3종 |
| 충전기 API 원본 | `highway_chargers_20260918_125202_all/` | JSON 3개, 1.56MB | **2026년 9월 수집 정보**. 2026년 2월 실제 운영 수와 동일하다고 가정 금지 |
| 환경부 제공 충전 이력 | `sp3_64/20260306_2026년2월_환경부_충전기_충전이력_상세내역.xlsx` | 1개, 42.64MB | 2026년 2월 충전 시작·종료 관측의 원본; 제공기관과 운영사 동일성 미확인 |
| TCS OD | `tcs_od/tcs_od_202602.csv`, `tcs_od/tcs_od_202603.csv` | 2개, 105.58MB | 2026년 2·3월 OD 원본; 내부/외부 통행 구분 필요 |
| 도로중심선 CSV | `ETC_S0_07_04_345774.csv` | 1개 | `scripts/load_centerline.py` 원본 |
| 표준노드링크 SHP | 과거 `data/raw/nodelink/` | **현재 조회 목록에 없음** | `lane_mapping_audit.parquet`는 있으나 현재 로컬에서 링크 원본 재생성 불명 |

파일 개수·용량은 사용자 제공 PowerShell `Get-ChildItem` 출력에 근거. XLSX 행 수·해시·원본의 모집단은 이번 검사에서 다시 대조하지 않았다.

## 2. 원본 → 가공 → 모델 소비 경로

| 영역 | 입력·변환 근거 | 처리 결과/DB | **실제 소비** 및 검증 역할 | 확인 상태 |
|---|---|---|---|---|
| 도로중심선·방향·휴게소 | 원본 CSV + `ex_route` → `scripts/load_centerline.py`, `scripts/build_route.py` | `centerline_gyeongbu.parquet`, `gyeongbu_route.json`; DB `corridor` | 도로 길이, `offset_km`, 충전소·CTM 셀 공간 기준 | 로컬 노선 메타, DB 길이 확인 |
| VDS 교통량·속도·구간 | 두 `ex_vds_*` + 노선/IC → `scripts/build_traffic.py` | `traffic_gyeongbu.parquet`, `speed_gyeongbu.parquet`, `conzone_gyeongbu.parquet` | EV 입력 프로파일, CTM 실측 비교, 차로수 교차검증 | 로컬 구조·결측 검산 완료 |
| 하루 진입량·통과율 | VDS → `scripts/build_demand_profile.py` | `volume_gyeongbu_{down,up}_seollal.csv`, `through_*` | 기본 시나리오 `volume_profile`; `through_profile`은 `entry_exit_profile` 설정 시 목적지 선정에서 대체됨 | 경로 확인, 값 검산 별도 |
| 중간 진입·진출 | VDS → `scripts/build_entry_exit_profile.py`, `src/evdt/io/entry_exit.py` | `entry_exit_gyeongbu_{down,up}_seollal.csv` | **기본 EV 진입·목적지 추정에 사용**, CTM 선택 시 램프 수요 | 로컬 재계산과 저장본 일치 |
| TCS OD | `tcs_od_202602/03.csv` → `scripts/build_tcs_od.py`, `scripts/build_tcs_od_gyeongbu.py` 등 | `tcs_od_gyeongbu.parquet` 등 중간표 | #99 등 실측 비교·대안 목적지 수요; **기본 `od_profile: null`** | 원본/최종 가공본 존재, 선행 보고 활용 |
| 차로수 | 표준노드링크/중심선 → `scripts/audit_lane_mapping.py`, `scripts/build_lane_profile.py` | `lane_mapping_audit.parquet`, `lanes_gyeongbu.parquet` | CTM 셀 차로수/병목 용량 | 가공본 존재, 원본 SHP 부재; dry-run 통과 |
| 교통류 모수 | VDS·차로·`config/flow_params.yaml` → `scripts/estimate_flow_params.py` | `flow_params_by_conzone.parquet`, 설정 YAML | `seed_cells.py`는 **설정 YAML과 차로 프로파일**을 읽음; 단순히 parquet을 항상 직접 사용하는 것은 아님 | 코드 경로 확인, 과거 생성본과 개별 대조 미실시 |
| 충전기 공급 | 2026-09 API → `scripts/load_chargers.py` | DB `station`, `charger` | `runner.run_once` → `read_station_chargers` → `station_specs`/DES | 기본 DB 활성 282기 확인 |
| BASE215 격리 공급 | #64 시설 근거표·매핑 → `scripts/sp3_64_daily_base215_runner.py` | #64 전용 `isolated.db`, `model_unit_to_operator.csv` | 20회 별도 UE·충전 관측 대조. **기본 DB 변경 없음** | 215 고유 슬롯·운영사 매핑 내부 일치 확인 |
| 차량·충전곡선·온도 | `config/vehicles.yaml`, `config/temp_efficiency.yaml` → `scripts/seed_vehicles.py` | DB `vehicle_class`, `charge_curve`, `temp_efficiency` | `runner` 및 `world/charging.py`·UE/DES의 충전시간 | 마스터 검증 및 테스트 통과; YAML↔현재 DB 값 1:1 별도 미대조 |
| 충전 실측 | #64 XLSX → `analyze_sp3_64_*`, `sp3_64_validate_20day_base215.py` | 관측 집계 CSV, `compare20_v1/validation_checks.json` | 충전 시작·점유시간·분포 오차의 **조건부** 기술통계 | JSON 필드·매핑 확인, 이벤트 원자료 20회 재집계 미실시 |
| 합성 EV | `volume`+`entry_exit`+EV 비율+차량·SoC 설정 → `src/evdt/runner.py::build_demand`, `io/synthetic_ev.py` | `synthetic_evs_...parquet`, 실행 로그 | UE·DES 차량 수·이동·충전 필요량 | 동작 경로 확인, 외적 수요 적합성은 미판정 |
| CTM 셀 | 노선 JSON+`lanes`+`flow_params.yaml`+휴게소 DB → `scripts/seed_cells.py` | SQLite `cell` | `demand.travel_time: ctm`일 때 사용 | 792셀 dry-run 및 기본 DB 적재 완료, DB validate PASS; 실제 정체 재현 FAIL |

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
| `ev_count_method` | DOWN `fixed` 확인 | `fixed`면 기대 EV 수 반올림, Poisson 표본이 아님; UP은 이번 검사에서 별도 확인하지 않음 |

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
| 차로수 | **95행**, UP49 / DOWN46, 양쪽 구간 범위 약 0~415.058km | SHP 재생성 경로 미확인 |
| CTM 셀 | DOWN396/UP396=792, `dt=0.2분`, 휴게소 DOWN19/UP16 | 구조·길이·CFL·휴게소 경계 확인 후 기본 DB 792셀 적재 및 validate PASS. 정체 재현성은 별도 FAIL |
| 진입/진출 DOWN | 기준일 **2/13**, 1,248행, 시간24/고유경계52, 중간 진입 266,160·진출 339,873 | **하루 추정치**, 설 10일 합계 아님 |
| 진입/진출 UP | 기준일 **2/17**, 1,200행, 시간24/고유경계50, 중간 진입 335,478·진출 302,470 | **하루 추정치** |
| 진입/진출 공백 | `gap_km>1` 고유 경계 양방향 **각 9곳**, 최대 DOWN 9.315km, UP 14.126km | 결측 구간 사이 진출입을 한 경계에 모아 추정할 수 있음 |
| 진입/진출 재현 | 현 VDS + `peak_date`/`entry_exit_profile`로 산출한 값과 저장 CSV의 행·열·수치 **양방향 모두 일치** | **파이프라인 재현성 PASS**, 차량별 실제 목적지 적합성은 별도 |
| DB 현황 | `corridor`2, `station`35, `charger`103그룹, `vehicle_class`5, `charge_curve`20, `temp_efficiency`9, `cell`792 | 활성 충전기 `SUM(n_units)` 282기(DOWN153, UP129). CTM 셀 적재는 기본 `travel_time=fixed` 설정을 변경하지 않음 |
| #64 BASE215 | DOWN/UP mapping CSV 각 215행/고유ID215, ME95 + NONME120, 전체 매핑 동일 | 실험 공급 DOWN112/UP103; 2월 물리 신원 확정 아님 |
| #64 QA JSON | 20 실행(양방향 10일), 관측29·미관측6, 관측 290 station-day/6,960 station-hour | **JSON 기록 확인**. 20회 전체 이벤트·해시 재검산 아님 |
| DB 검증 | `python -m evdt.io.db validate` → `OK 마스터 데이터 정합성 이상 없음` | CTM 셀 792행 적재 후 재실행 PASS. 내부 마스터 데이터 정합성 검사이며 실측 병목·충전 행태 적합성 검증은 별도 |
| 충전 계산 테스트 | `python -m pytest tests/test_charging.py -q` → 28개 테스트 진행 표시, `[100%]` | 알고리즘 단위 테스트 통과. YAML 테스트이지 현재 DB-설정값 동기화 검사는 아님 |

## 5. 남은 계보 확인 및 사용 제한

1. 현재 로컬에서 표준노드링크 SHP 원본과 TCS OD 모든 중간 산출물의 보유·해시를 확인하지 못했다. 가공 차로·OD 파일이 있다는 이유로 완전 재생성 가능하다고 말하지 않는다.
2. VDS→중간 진입·진출의 생성 일치는 확인됐지만 결측·순유출입(net) 구조 때문에 **실제 IC/JCT 진입·진출**의 검증은 아니다.
3. `flow_params_by_conzone.parquet`와 최신 config/셀 파라미터의 모든 값 일치는 미검증. 2026-09 생성본과 2026-10 도로 좌표계의 개별 위치 비교도 별도 과제다.
4. 현재 DB의 282기와 #64 BASE215는 **서로 다른 실험 공급 환경**이며 섞어 비교하지 않는다.
5. #64의 10일 결과는 **날짜별 독립 하루 실행**으로 이전 날 대기·점유를 이월하지 않으며, 환경부 제공 기록의 운영사 모집단 동등성이 입증되지 않았다.
6. 사용 가능 범위와 선행 검증 근거: [`validation_report.md`](validation_report.md), [`dt_credibility.md`](dt_credibility.md), [`followup_issues.md`](followup_issues.md).
