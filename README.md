# 명절 고속도로 EV 충전 쏠림 완화 Digital Twin

경부고속도로 명절 피크의 충전 수요 쏠림을 완화하는 최적화 엔진과 디지털 트윈.
설계 결정 전문은 [`docs/DESIGN.md`](docs/DESIGN.md).

현재 상태: **스프린트 3 진행 중.** 무대(노선·교통류·수요)를 실측에 맞춰 세우고,
엔진 사다리의 두 칸(S0·S1)을 올렸다. S0 으로 **"정보만으로는 쏠림이 안 풀린다"** 를,
S1 으로 **"예약 원장은 평균이 아니라 최악을 자른다"** 를 확인했다.
다음은 참여율(#94·#95)과 S2(예측).

---

## 스코프 — 무엇을 하고 무엇을 안 하는가

**경부고속도로 한 축에서, 2026 설 연휴를 재현해 놓고, 충전소 "배정"만 바꿔 본다.**
충전기를 더 깔지 않고, 길을 바꾸지 않고, 값을 매기지 않는다.

| | **한다** | **안 한다** | 안 하는 이유 — *답할 수 있는 질문이 아니라서* |
|---|---|---|---|
| **공간** | 경부선 본선 서울↔부산, 상·하행 | 다른 고속도로 · 전국 네트워크 | 네트워크를 넣으면 **경로 선택과 배정 효과가 섞여** 분리되지 않는다 |
| | 휴게소 급속충전기 | 고속도로 **밖** IC 충전소 | 코리도 이탈은 **비용 120분으로 값만** 매긴다. 어디로 갔는지는 안 센다 |
| **시간** | 2026 설 날짜 창 + 평시 대조군 | 실시간 연동 · 운영 | **오프라인 재생**이다 |
| **차량** | 승용 EV (합성) | 화물 · 버스 | 쏠림의 주체가 아니다. 내연기관은 **교통량으로만** 들어온다 |
| **수단** | 충전소 **배정** | 충전기 **증설 설계** | 우리 주장이 *"부족한 게 아니라 나눠 쓰지 못한다"* 이므로 증설은 **반대쪽 가설** |
| | 참여율 파라미터 | **가격 · 인센티브 최적화** | 값↔행동 **탄력성 실측이 없다.** 가정 위에 가정이 된다 |
| | 도착 시각은 주어진 것 | **출발 시각 조정** | 출발을 바꾸면 **교통량 자체가 바뀌고** 대조군이 없다 |
| **결합** | 교통 → 충전 **단방향** | 충전 → 교통 역결합 | 양방향으로 묶으면 **어느 쪽이 원인인지 못 가린다** |

**경계 — 나오지만 목표가 아니다:** 충전기 증설 필요량은 배정으로 못 고치는 **부산물**
([`docs/charger_gaps.md`](docs/charger_gaps.md)) · 계산시간은 실시간이 범위 밖이어도 잰다.

➡ 전문과 Q&A 대응은 **[`docs/scope.md`](docs/scope.md)**. **세 곳(README·발표·이 문서)이
어긋나면 `docs/scope.md` 가 기준이다.**

---

## 어디까지 왔나

한 줄로: **#29~#30 에서 쏠림을 재현하고 재는 법을 만들었고, #60~#54 에서 그 재현이
실제 경부선과 맞는지 바로잡았고, #59 에서 그 위에 첫 엔진을 올렸다.**

### 스프린트 1 — 재현하고 재는 법

| 티켓 | 한 일 | 그때 문제였던 것 | 어떻게 끝났나 |
|---|---|---|---|
| **#29** UE 배분 | 각 EV 가 체류시간(대기+충전) 최소인 **계획**을 고르는 균형. 선택 단위가 휴게소가 아니라 계획인 이유는 장거리 차의 첫 정차가 두 번째 정차를 바꾸기 때문 | 완료조건의 "gap 단조감소" 를 **지킬 수 없었다** — 한 대만 옮겨도 뒷차가 밀리는 연쇄로 gap 이 오르내린다. 표준 MSA 는 5% 부근에서 진동 | 진입 순서 **Gauss-Seidel 최적반응**으로 교체. 조건은 없애지 않고 "왜 못 지키는지" 를 문서에 적고 더 강한 조건으로 대체. `gap_tol 0.8%` — 하행 평균 **9.8회**, 상행 **2.0회** 만에 수렴 |
| **#30** 러너·신뢰구간·히트맵 | `config → EV 생성 → UE → 충전 큐 → Parquet → KPI` 한 바퀴. 시드 20개 평균 + **95% 신뢰구간**(t분포), 시공간 히트맵 | 단일 시드는 우연일 수 있어 "A 가 B 보다 낫다" 를 주장할 수 없다 | 두 구간이 겹치면 차이를 주장하지 않는 원칙이 #59 까지 이어진다. 히트맵 세로축을 **실제 기점거리**로 그려 쏠림이 과장되지 않게 했다 |

### 스프린트 2 — 무대가 실제와 맞는가, 그리고 첫 엔진

| 티켓 | 한 일 | 그때 문제였던 것 | 어떻게 끝났나 |
|---|---|---|---|
| **#60** 노선 디버깅 | 중심선을 **415.058 km / 4,153점**으로 고정. 휴게소·콘존 위치 재계산, 충전기 적재의 옛 IC 방식 거리 계산 수정 | 노선이 틀리면 **휴게소 사이 거리**가 틀리고, "한 번 서면 되는가 두 번 서야 하는가" 가 통째로 바뀐다. 에러 없이 조용히 틀린다 | 고친 뒤 평균 대기 **1.8배**, 정차 **+632회**. 실제 도로가 22 km 길고 휴게소 간격이 넓어 두 번 서는 차가 늘었다. 검증 스크립트가 예상 노선 길이와 대조하도록 만들었다 |
| **#56** CTM 구현 | 코리도를 **792개 셀**로 쪼개 0.2분마다 밀도 전파. 유량 = `min(보낼 수 있는 양, 받을 수 있는 양)`. CFL 조건 위반이면 **돌기 전에** 멈춤 | 그전까지 모든 차가 **80 km/h 고정**이라 도착 시각이 틀리고 쏠림의 시간대가 어긋났다 | 검증: 실측 서울→부산 **4.3시간** vs CTM **4.4시간** — 2026 설은 원래 덜 막혔다. 통행시간을 `고정 속도`/`CTM` 스위치로 뺐다. 잡은 버그 둘: Daganzo 합류식을 혼잡 조건 없이 적용해 **차가 생겨난 것**(차량 보존 테스트가 잡음), 워밍업을 전날 같은 시각으로 잡아 0시 교통량이 −58%~+89% 로 튄 것 |
| **#54** 수요 확장 | 양방향 + 중간 진입·진출. 실측 OD 를 기다리지 않고 **콘존 교통량의 증감**으로 추정. 같은 표를 EV 생성기와 CTM 램프가 **같이** 쓴다 | `through_share`(누적 최솟값)는 한 번 내려가면 못 올라가서, **상행 코리도의 97% 에 목적지가 생기지 않았다.** 그리고 **CTM 램프는 전체 표를 쓰는데 EV 만 기점 콘존에서 뽑고 있었다** | 진입 EV **8,361 → 18,534**, 최대 대기 **1,997분(33시간) → 106분**. 33시간이 106분이 된 것은 모델을 손봐서가 아니라 **코리도 이탈**(줄이 길면 IC 로 빠져 시내 충전, 120분)이라는 현실의 선택지를 넣어서다. 이탈을 **이유별로 갈라** 엔진의 과녁과 증설의 몫을 나눴다 |
| **#59** S0 정책 · Δt 루프 | 시뮬레이터가 Δt 마다 `Policy.decide` 를 부르는 구조 + 첫 엔진 칸 S0(지금 화면의 대기 + 내 충전시간 최소) | UE 는 하루를 미리 다 풀어 놓고 배정했다. S0 는 **달리는 도중에** 고르므로 배정과 큐가 같이 굴러야 한다 | **양방향 시드 20개**: 평균 대기 하행 20.5→28.8분(+40.8%), 상행 19.1→22.5분(+17.7%). **정보를 주는 것만으로는 안 풀린다**를 숫자로 말할 수 있게 됐다 |

### #54·#59 에서 얻은 도구 셋

- **기준선 고정** — KPI 18개를 `reference/baseline_<방향>.csv` 에 못 박고 `scripts/baseline.py --check` 로 **차이만** 본다. 눈으로 비교하던 20분이 1분이 됐다. *"결과 파일이 아니라 계약"*
- **가정 레이어** — EV 비중·장거리 비중·기회 충전을 실측 위에 얹는 **유일한 통로**. 레이어 이름 한 줄이 실행 파라미터와 **모든 그림 부제**에 찍힌다. 현재 기준선은 `layers: []` — 제대로 세니 가정을 안 얹어도 쏠림이 나왔다
- **도착 뭉침 지수** — `5분당 도착 대수의 분산 ÷ 평균`. 몫으로 재는 몰림비가 거꾸로 말할 때 원인을 재는 지표

### 앞으로 (스프린트 3)

**무대 마무리**

| | 왜 |
|---|---|
| **#55** 출발 SoC 분포 | 지금은 진입 지점과 **무관하게** 뽑는다. 이탈 차의 74% 가 통행거리 40 km 미만이고 평균 출발 SoC 18% — 중간 IC 에서 거의 빈 채로 들어오는 차다. 쏠림 결과에 영향이 크다 |
| **#57·#58** CTM 보정·검증 | 목적함수 = 콘존×시간 속도 오차(RMSE) + 교통량 오차 |
| **#61~#63** 실측 OD | 지금의 진입·진출은 교통량 증감 **추정**이다. 일 단위로만 조회돼서 시간대별 추정 방법이 필요하다 |
| **#67** EV 비중 근거 | `ev_share = 0.05` 는 **잠정값**이다. 티핑 포인트(4→5%) 바로 위라는 건 근거가 아니다 |
| **#71** EV 생성기 기본값 | 푸아송 도착을 기본으로 돌릴지. 바꾸면 기준선 18개가 전부 움직이므로 다시 못 박아야 한다 |

**엔진 사다리** — 정보·조율 수준 순서다. 성능 순서가 아니다.

| 칸 | 무엇이 늘어나나 |
|---|---|
| **S0** (완료) | 지금 화면의 대기만 본다 |
| **S1** (완료) | 충전소와 **예약 원장**을 공유. 앞 배정이 뒤에 영향을 주도록 순차 결합. **최악을 자른다** — 최대 대기 577.7 → 169.0분, 못 기다려 나간 차 193.1 → 24.8대 (`docs/s1.md`) |
| **S2** | 휴게소마다 **도착 시점의 미래 큐**를 갖는다 |
| **S3** | 윈도우마다 **공동 동시 배정** (공간축 결합) |
| **S4** | **롤링 호라이즌** (시간축 결합). 재배정 + horizon 끝의 terminal penalty |

**경제 모델** — 혼잡 외부효과, 한계 외부비용(MILP 의 경제적 해석), 일반화 비용·통행시간가치(KDI 예타지침 원단위). 현재 엔진은 *이론적 최적*이 아니라 *이론에 근거한 휴리스틱*이므로 **사후 최적해로 성능을 검증**한다.

---

## 환경 설정

**Python 3.12 로 통일한다.** 팀원 전원 동일 버전.
아래 6단계를 순서대로. 각 단계마다 "완료 확인"이 맞으면 다음으로 넘어간다.

### 1. 클론

**왜** — 경로에 한글·공백이 있거나 OneDrive/iCloud 동기화 폴더에 두면
`.venv` 와 `evdt.db` 가 잠겨서 원인 찾기 어려운 오류가 난다.

```bash
# Windows:  C:\Users\<사용자>\dev  아래 권장 (바탕화면·문서 폴더는 OneDrive인 경우가 많다)
# macOS:    ~/dev  아래 권장
git clone <팀 리포 URL> evdt
cd evdt
```

**완료 확인** — 현재 폴더에 `pyproject.toml`, `README.md`, `src/` 가 나란히 보인다.

---

### 2. 가상환경

**왜** — 이 프로젝트의 패키지를 시스템 파이썬과 분리한다.
다른 과제와 버전이 충돌하지 않는다.

```powershell
# Windows PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
python3.12 -m venv .venv
source .venv/bin/activate
```

**완료 확인** — 프롬프트 앞에 `(.venv)` 가 붙고, 아래 두 줄이 맞는다.

```
python -V                                      →  Python 3.12.x
python -c "import sys; print(sys.executable)"  →  경로에 .venv 가 들어 있다
```

두 번째 줄이 중요하다. `(.venv)` 가 붙어 있어도 실제 파이썬은 시스템 쪽일 수 있다
(venv를 만든 뒤 폴더를 옮기면 venv 내부에 박힌 경로가 깨진다).
`.venv` 경로가 안 나오면 `.venv` 폴더를 지우고 이 단계를 다시 한다.

> **PowerShell에서 `where python` 은 쓰지 말 것.** `where` 가 `Where-Object` 의 별칭이라
> 아무것도 출력하지 않고 끝난다. 굳이 쓰려면 `where.exe python` 또는 `Get-Command python`.

> PowerShell에서 `(.venv)` 가 안 붙으면 실행 정책 문제다. 한 번만:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

---

### 3. 의존성 설치

**왜** — `pip install -e .` 가 핵심이다. 코드가 `src/evdt/` 안에 있어서(src 레이아웃)
이걸 빼면 `import evdt` 를 찾지 못한다. `-e`(editable)라서 코드를 고쳐도 재설치가 필요 없다.

```bash
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
pip install -e .
```

**완료 확인**

```
python -c "import evdt; print(evdt.__version__, evdt.PROJECT_ROOT)"
→  0.1.0 <프로젝트 폴더 경로>
```

---

### 4. 데이터베이스 생성

**왜** — 서버가 아니라 프로젝트 루트의 파일 하나(`evdt.db`)다. SQLite는 파이썬 내장이라
설치할 게 없다. `--seed-corridor` 는 경부선 상행·하행 corridor 2행을 넣는다 —
이게 없으면 시나리오 등록이 외래키 오류로 막힌다.

```bash
python scripts/init_db.py --seed-corridor
```

**완료 확인** — 프로젝트 루트에 `evdt.db` 가 생기고, 출력이 이렇게 나온다.

```
[OK] 스키마 적용: .../evdt/evdt.db
[OK] corridor 2개 적재
     cell                      0 rows
     corridor                  2 rows
     run                       0 rows
     ...
```

`corridor` 외에 전부 0행인 것이 정상이다. 충전소는 T-06, 차종은 T-10에서 채운다.

> **`evdt.db` 는 git에 올리지 않는다.** 바이너리라 충돌을 합칠 수 없다.
> 공유되는 것은 `src/evdt/sql/schema.sql` 과 적재 스크립트이고, 각자 같은 명령으로 같은 DB를 만든다.

---

### 5. 환경 셀프 체크

**왜** — Epic A의 완료 조건 14건을 자동으로 검사한다. 여기가 전부 `[OK]` 면
"내 PC에서만 안 되는" 상태가 아니라는 뜻이다. 임시 폴더에서 돌고 끝나면 지우므로
방금 만든 `evdt.db` 는 건드리지 않는다.

```bash
python scripts/verify_setup.py
```

**완료 확인**

```
전체 14건 통과. Sprint 1 Epic A 완료 조건을 만족한다.
```

`[FAIL]` 이 나오면 그 줄에 **어느 티켓의 어느 완료 조건**이 깨졌는지 적혀 있다.
그 줄만 들고 팀 채널에 물어보면 된다.

---

### 6. 테스트

**왜** — 앞으로 코드를 고칠 때마다 여기가 초록인지로 판단한다.
이 테스트들은 "도는지"가 아니라 **조용히 틀릴 수 있는 것**을 지킨다 —
외래키가 꺼진 커넥션, upsert가 자식 행을 지우는 사고, 교통류 기본도와 어긋난 셀,
그리고 한국어 Windows에서만 터지는 인코딩 문제.

```bash
python -m pytest -q
```

**완료 확인**

```
542 passed
```

---

### PyCharm 설정 (선택, 3가지)

| 할 일 | 경로 | 왜 |
|---|---|---|
| 인터프리터 지정 | 설정 → 프로젝트: evdt → Python 인터프리터 → `.venv` | 실행 버튼이 가상환경을 쓴다 |
| `src` 를 소스 루트로 | `src` 우클릭 → 디렉터리를 다음으로 표시 → **소스 루트** | 자동완성이 정확해진다 |
| 테스트 러너 = pytest | 설정 → 도구 → **Python 통합 도구** → 테스트 → 기본 테스트 러너 | 테스트 함수 옆에 ▶ 버튼이 생긴다 |

설정 창에서 항목을 못 찾으면 **왼쪽 위 검색창에 `pytest`** 처럼 쳐서 찾는 게 가장 빠르다.
`pyproject.toml` 에 pytest 설정이 이미 있어서, 러너를 안 바꿔도 터미널 실행은 정상 동작한다.

Database 도구 창은 PyCharm Pro 기능이다. 무료 티어라면 `python scripts/db_peek.py` 로
같은 일을 할 수 있다 (학생은 JetBrains 학생 라이선스로 Pro를 무료로 받을 수 있다).

---

## 자주 쓰는 명령

```bash
python scripts/init_db.py --seed-corridor   # DB 생성 (여러 번 실행해도 안전)
python scripts/db_peek.py                   # 테이블 목록과 행 수
python scripts/db_peek.py station           # 한 테이블 들여다보기
python scripts/db_peek.py --schema cell     # 컬럼 정의와 CHECK 제약
python scripts/smoke_run.py                 # 진짜 휴게소 3곳 + 가짜 수요로 파이프라인 한 바퀴
python scripts/smoke_clean.py               # smoke_run 흔적 삭제
python scripts/verify_setup.py              # 환경 셀프 체크 14건
python -m pytest -q                         # 테스트
python -m ruff check src tests scripts      # 린트
```

### 실험 돌리기 · 결과 보기 (스프린트 1~2)

```bash
# 한 번만 — 어느 단계든 --stage 로 고른다. 단계는 세계가 아니라 정책이 바뀌는 것이라
# scenario_id 를 바꾸지 않는다 (--soc, --demand-multiplier 는 세계를 바꾸므로 바꾼다)
python scripts/run_ue.py --stage S0 --seed 7

# 시드 20개 + 95% 신뢰구간 + 히트맵
python scripts/run_experiment.py --seeds 1-20 --stage S0

# UE vs S0 — KPI 표 + 나란히 놓은 히트맵
python scripts/compare_stages.py --seeds 1-20

# 휴게소별 대수·대기·도착 뭉침, 이탈 분해, 시간대별 대기 (+ 뭉침 막대 그림)
python scripts/report_s0.py --seeds 1-20 --plot docs/figures/s0_clumping_down.png

# 휴게소 한 곳의 대기 시계열과 5분당 도착 대수 (톱니가 보이는 그림)
python scripts/plot_station_wait.py <S0 run_id> --vs <UE run_id> --top 3

# 충전기 공백 — 엔진이 못 고치고 증설로 풀 부분
python scripts/report_charger_gaps.py

# 못 박아 둔 기준선과 차이만 본다 (--pin 으로 다시 못 박는다)
python scripts/baseline.py --check
```

상행은 어느 명령이든 `--config config/scenario_seollal_up.yaml` 을 붙인다.

### 실데이터 파이프라인 (T-05 ~ T-08)

```bash
# 공통: 경부선 노선 좌표계 (휴게소·VDS 구간이 같이 쓴다. .env 의 EVDT_EX_API_KEY 필요)
python scripts/build_route.py

# 충전 인프라 (T-05/T-06, .env 의 EVDT_MOE_API_KEY 필요)
python scripts/fetch_chargers.py --all
python scripts/load_chargers.py
python scripts/audit_charger_counts.py

# 구간 교통량·속도 (T-07/T-08, 인증키 불필요)
python scripts/fetch_traffic.py --start 2026-02-13 --end 2026-02-22 --label seollal2026
python scripts/fetch_traffic.py --start 2026-03-06 --end 2026-03-15 --label base202603
python scripts/build_traffic.py
python scripts/check_traffic.py --holiday seollal2026 --base base202603 \
    --outbound 2026-02-14 2026-02-16 --return 2026-02-17 2026-02-18
python scripts/estimate_flow_params.py
```

```bash
# 차로수 (T-24, data/raw/nodelink/MOCT_LINK.shp 필요 — data/raw/README.md 참고)
python scripts/audit_lane_mapping.py
python scripts/build_lane_profile.py
python scripts/seed_cells.py --write      # CTM 셀 (T-17). 다시 만들 때는 --replace
```

```bash
# 차종·온도·충전곡선 (T-10/T-11/T-14, 외부 API 불필요)
python scripts/seed_vehicles.py
```

---

## EV 데이터 합성 — 출발 SoC 는 어디서 오나 (T-12 · #53 · #55)

실측에 EV 통행은 없다. 교통량에 EV 비중을 곱해 **합성 EV** 를 만든다
(`src/evdt/io/synthetic_ev.py`, `scripts/generate_synthetic_evs.py`).

```bash
python scripts/generate_synthetic_evs.py      # 시간대별 EV 생성 + 검증 그림
```

차 한 대는 `(진입 시각, 진입 지점, 차종, 출발 SoC, 목적지)` 로 정해진다. 이 중
**출발 SoC 가 결과를 가장 크게 흔든다** — 이 값이 "충전이 필요한 차가 몇 대냐" 를 정하기
때문이다 (`ev_share` 와 함께. #67 과 같이 봐야 한다).

### 섞이기 쉬운 세 가지 SoC

| | 뜻 | 관측되나 |
|---|---|---|
| **진입 SoC** | 고속도로에 **올라올 때** 배터리 | ❌ **아무도 기록하지 않는다** |
| **충전 시작 SoC** | 휴게소에서 **꽂을 때** 배터리 | ✅ 충전기가 기록 |
| **목표 SoC** | 어디까지 **채우나** | ✅ (우리는 상한 0.8 고정, §2.3) |

> ⚠ **#55 이전에는 2번을 1번 자리에 넣고 있었다.** 설계문서 §2 의 Rupnik Beta(2,5)
> (평균 34%) 는 **휴게소 도착 SoC** 인데 그것을 진입 SoC 로 썼다. 충전소에서 보이는
> 차는 정의상 배터리가 낮은 차라, 그 분포를 전체 진입 차량에 쓰면 진입 SoC 가
> **체계적으로 너무 낮게** 나온다. 표본 선택 편향이다.
>
> 증상은 #59 에서 드러났다 — 중간 IC 에서 SoC 12% 로 진입해 40 km 도 못 가는 차가
> 생겼고, 그런 차가 이탈의 74% 였다.

### 그래서 어떻게 정했나 — 로그정규 + 바닥 40%

진입 SoC 는 **관측이 안 되므로 가정해야 한다.** 문헌이 하는 방식을 따른다.

| 결정 | 근거 |
|---|---|
| **분포는 로그정규** | 고속도로 EV 충전 부하 문헌의 표준. Bai et al. (2026) — 우리와 같은 설정(고속도로·CTM·혼잡) — 이 초기 SoC 를 로그정규로 둔다 |
| **바닥 33%** | 명절 장거리를 앞두고 그 미만으로 고속도로에 올라오지 않는다는 **행태 가정**. SoC 에 제약을 거는 것 자체는 표준 관행이다 (Huaman-Rivera et al. 2024 리뷰의 DoD 제약 선례) |
| **절삭 후 재정규화** | `max(soc, lo)` 로 깔면 바닥에 뾰족한 덩어리가 생겨 그 자체가 인공물이 된다 |
| **도달 가능성 조건** | 바닥만으로는 부족하다. 평사휴게소는 앞과 **52.5 km** 떨어져 있어, 260 km 에서 40% 로 들어와도 못 간다. 진입 지점에서 가장 가까운 하류 휴게소 + 안전버퍼까지 갈 수 있어야 한다 |

> **바닥은 고치는 장치가 아니라 경보 장치다.** 분포가 맞으면 바닥 미만이 애초에 거의
> 없어서 바닥이 할 일이 없다 (지금 1.6%). `n_soc_floored` 가 크게 나오면 **분포를 다시
> 봐야 한다는 신호**이지, 바닥이 잘 동작한다는 뜻이 아니다.
>
> 바닥 **바로 위** 구간도 같이 본다. 거기에 차가 몰리면 분포가 바닥에 눌린 것이다 —
> 지금 33~40% 구간이 4.6% 이고, 테스트가 15% 를 경계로 지킨다.

### 34% 는 입력이 아니라 **검증 대상**이다

구조를 뒤집었다.

```
[이전]  충전 시작 SoC 분포(34%)  ──직접 투입──>  진입 SoC            ❌ 범주 오류
[현재]  진입 SoC (로그정규+바닥)  ──시뮬레이션──>  충전 시작 SoC
                                                       ↓
                                             30~34% 가 나오는가?   ← 검증
```

검증 목표값의 근거가 둘 다 있고 서로 맞는다.

- **국내** — 김범일·안근원·신희철 (2022), 대한교통학회지 40(5): 잔량 **30%** 에서
  **85%** 까지 충전 (EV Magazine 2021 이용실태 설문)
- **EU** — Rupnik et al. (2025): Beta(2,5) on [0.1, 0.95], 평균 **34%**

이로써 검증 축이 교통량(MAPE 12.1%) 하나에서 **둘**이 된다.

### 아직 안 정해진 것

- **로그정규 (μ, σ)** 는 잠정값이다. Bai et al. (2026) 원문 파라미터로 바꾸거나,
  국내 **자동차 주행거리 실태조사**에서 유도하는 것이 목표다 (후자가 더 방어력이 높다)
- 한국교통연구원 (2024) 「수송부문 탄소중립을 위한 전기차 인프라 국가 전략 연구」
  **표 4-8 "전기승용차 충전 시작 시점의 SOC 분포 (OBD 데이터 분석)"** — 설문이 아니라
  실측 분포다. 입수하면 검증 목표를 이쪽으로 바꾼다

자세한 내용과 반대 근거는 `docs/departure_soc.md`.

---

## 폴더 구조

```
src/evdt/
  config.py        시나리오 YAML 로더 + 로딩 시점 검증          T-03
  interfaces.py    엔진 ↔ 트윈 계약 (Policy / EVState / WorldView)
  sql/schema.sql   SQLite 마스터 스키마 (10 테이블)             T-02
  io/              db · run_registry · writers · loaders        T-02, T-04
  world/           디지털 트윈 (CTM · 충전 큐 DES · Δt 루프)     T-14~T-17, #59
  engine/          배정 엔진 (UE 균형 · S0 정책 · 원장 · MILP)   T-18, Sprint 2~3
  viz/             스냅샷 → 렌더러                              T-20
config/            시나리오 YAML (실험 1개 = 파일 1장)
data/raw/          API 원본. 절대 수정하지 않는다. git 제외
data/processed/    전처리 산출물. git 제외
runs/<run_id>/     실험 산출물 Parquet + meta.json. git 제외
scripts/           DB 생성 · 확인 · 셀프 체크
tests/             pytest. GitHub Actions에서 자동 실행
docs/DESIGN.md     설계 결정 전문
docs/debug_lanes_log.md  차로 프로파일: 넘어간 것과 확인 순서
docs/T15_queue_rule.md   큐 규칙 단일 모듈 (호출자는 둘뿐)
docs/T16_charging_des.md 충전 큐 DES (SimPy 는 시간만 굴린다)
docs/T17_cells.md        CTM 셀 분할 (수정된 완료조건, 가짜 휴게소 사고)
docs/event_log.md        이벤트 로거 · 스냅샷 계약 · 휴게소×시간대 대기 SQL 한 줄
docs/UE_equilibrium.md   UE 반복 균형 · 쏠림이 어디서 오는가 · 지킬 수 없는 완료조건
docs/experiment.md       시드 반복 · 95% 신뢰구간 · offset_km 축 히트맵 (run_experiment.py)
docs/ctm.md              CTM 셀 전송 모형 · 통행시간 · cell_state · 시공간 속도 지도 읽는 법
docs/demand_layers.md    중간 진입·진출 · 기회 충전 · 티핑 포인트 · 실측과 가정 가르기
docs/dev_log.md          개발 로그 — 무엇을 잘못 알았고 어떻게 알아챘나 (길을 잃으면 여기부터)
docs/charger_gaps.md     충전기 공백 — 엔진이 못 고치고 증설로 풀 부분
docs/s0.md               S0 (사다리 첫 칸) · Δt 루프 · 도착 뭉침 지수 · UE 와의 경계
docs/s1.md               S1 (두 번째 칸) · 예약 원장 · 평균이 아니라 꼬리를 자른다
reference/               못 박아 둔 기준선 KPI. scripts/baseline.py --check 로 비교한다
docs/fake_data_audit.md  가짜 휴게소가 어디에 영향을 줬는지 전수 검토
```

**저장소 경계** — 재현 가능한 것(이벤트 로그 수십만 행)은 Parquet,
재현의 근거(어떤 설정·어떤 커밋에서 나왔는가)는 SQLite.

---

## 절대 깨면 안 되는 설계 규칙 4가지

테스트와 DB 제약으로 강제해 뒀다. 자세한 이유는 `docs/DESIGN.md` §4.1.

1. **큐 규칙은 단일 모듈.** 시뮬레이터와 예약 원장이 같은 함수를 호출한다 (T-15).
2. **`world/` 는 `engine/` 을 임포트하지 않는다.** → `tests/test_import_boundaries.py` 가 AST로 검사.
3. **모든 공간 엔티티에 `offset_km` 와 위경도를 동시에 저장.** → `station`/`cell` 테이블 NOT NULL.
4. **시뮬레이터와 뷰어 사이는 스냅샷 스트림으로만 연결** —
   `(t_min, entity_type, entity_id, lat, lon, state, value)`.

추가로 DB에 박아둔 함정 방지 하나:

> 삼각형 기본도에서 `q_max` 는 독립 파라미터가 아니다.
> `q_max = lanes × v_free × w_back × k_jam / (v_free + w_back)`.
> 어기면 **정체가 아예 생기지 않는다.** `cell` 테이블의 CHECK 제약이 거부한다.

설계문서 §10.1은 `Policy` 프로토콜을 `engine/policy.py` 에 두라고 되어 있지만,
그러면 `world/sim.py` 가 타입 힌트 때문에 engine을 임포트해야 해서 규칙 2가 깨진다.
그래서 계약만 중립 모듈 `src/evdt/interfaces.py` 로 옮겼다.

---

## 팀 작업 규칙

브랜치 이름은 티켓 번호로. `main` 에 직접 push 하지 않는다.

```bash
git switch -c t05-charger-api
# ... 작업 ...
python -m pytest -q                       # 통과 확인
git add -A && git commit -m "T-05: 환경부 충전소 API 원시 수집"
git push -u origin t05-charger-api
# GitHub에서 Pull Request → 리뷰 1명 → main 병합
```

**커밋하면 안 되는 것** — `data/raw/` 원본, `runs/` 실험 산출물, `*.db`, `.env`.
`.gitignore` 에 이미 들어 있지만, `git add -A` 전에 `git status` 를 한 번 보는 습관을 들일 것.
특히 `.env` 에는 API 키가 들어간다.

---

## 막혔을 때

| 증상 | 원인 | 해결 |
|---|---|---|
| pip에서 `UnicodeDecodeError: 'cp949' codec` | requirements 파일에 비ASCII 문자 | 해당 줄을 영어로. `tests/test_packaging.py` 가 재발을 막는다 |
| `ModuleNotFoundError: evdt` | `pip install -e .` 누락 또는 venv 꺼짐 | 프롬프트에 `(.venv)` 확인 후 재설치 |
| `where python` 이 아무것도 출력 안 함 | PowerShell에서 `where` 는 `Where-Object` 별칭 | `python -c "import sys; print(sys.executable)"` |
| `can't open file '...\scripts\xxx.py'` | 프로젝트 루트가 아닌 곳에서 실행 | `pwd` / `Get-Location` 으로 위치 확인 |
| `no such table: run` | DB 미생성 | `python scripts/init_db.py` |
| `corridor 가 DB 에 없다` | `--seed-corridor` 없이 생성 | `python scripts/seed_corridor.py` |
| `RunExistsError` | 같은 조건으로 두 번 실행 | 정상이다. 시드를 바꾸거나 `overwrite=True` |
| `database is locked` | DB 도구 창이 잡고 있거나 클라우드 동기화 폴더 | 연결 해제 후 재시도. 반복되면 경로를 옮긴다 |
| PowerShell에서 `(.venv)` 안 붙음 | 실행 정책 | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| FK 위반이 그냥 통과 | `sqlite3.connect` 직접 호출 | 반드시 `evdt.io.db.get_conn` 을 쓸 것 |

---

## 참고 문헌

1. Bai, C. et al. (2026). *Simulation of electric vehicle charging loads on highways considering fluid dynamics under traffic flow congestion.* Sustainable Energy Technologies and Assessments. — 방법론 앵커
2. Rupnik, B., Wang, Y., Kramberger, T. (2025). *Hybrid Model for Motorway EV Fast-Charging Demand Analysis Based on Traffic Volume.* Systems 13(4), 272. — UE 베이스라인 구현 청사진 (open access)
3. Daganzo, C. F. (1994). *The cell transmission model.* Transportation Research Part B.
4. Wardrop, J. G. (1952). *Some theoretical aspects of road traffic research.*
5. IEEE TITS (2017). *Distributed Scheduling and Cooperative Control for Charging of EVs at Highway Service Stations.* — 재현 대상
6. KDI, 예비타당성조사 수행 총괄지침 — 통행시간가치 원단위
