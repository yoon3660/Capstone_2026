# 명절 고속도로 EV 충전 쏠림 완화 Digital Twin

경부고속도로 명절 피크의 충전 수요 쏠림을 완화하는 최적화 엔진과 디지털 트윈.
설계 결정 전문은 [`docs/DESIGN.md`](docs/DESIGN.md).

현재 상태: **Sprint 1 / Epic A (T-01 ~ T-04) 완료.** 다음은 Epic B(데이터 수집).

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
49 passed in 1.0s
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
python scripts/smoke_run.py                 # 가짜 데이터로 파이프라인 한 바퀴 (전용 코리도 smoke_down)
python scripts/smoke_clean.py               # smoke_run 흔적 삭제
python scripts/verify_setup.py              # 환경 셀프 체크 14건
python -m pytest -q                         # 테스트
python -m ruff check src tests scripts      # 린트
```

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

## 폴더 구조

```
src/evdt/
  config.py        시나리오 YAML 로더 + 로딩 시점 검증          T-03
  interfaces.py    엔진 ↔ 트윈 계약 (Policy / EVState / WorldView)
  sql/schema.sql   SQLite 마스터 스키마 (10 테이블)             T-02
  io/              db · run_registry · writers · loaders        T-02, T-04
  world/           디지털 트윈 (CTM + SimPy 충전 큐)            T-14~T-17
  engine/          배정 엔진 (정책 · 원장 · MILP)               T-18, Sprint 3
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
