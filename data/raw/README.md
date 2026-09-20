# data/raw — 원시 데이터 (절대 수정하지 않는다)

API 응답을 **받은 그대로** 둔다. 컬럼 이름 하나도 고치지 않는다.
전처리 결과는 `data/processed/` 로 간다.

왜 이렇게 하는가: 두 달 뒤 "이 충전기 기수 어디서 나온 숫자냐"는 질문을 반드시 받는다.
그때 원본이 남아 있어야 답할 수 있다. 원본을 고치면 되돌릴 수 없다.

## 파일 이름 규칙

```
<출처>_<대상>_<수집일 YYYYMMDD>.<확장자>

moe_chargers_20260918.json          환경부 무공해차 통합누리집 충전소 (T-05)
ex_volume_gyeongbu_20260918.csv     도로공사 교통량 (T-07)
ex_vds_speed_gyeongbu_20260918.csv  도로공사 VDS 구간 속도 (T-08)
```

수집일을 파일명에 넣는 이유: 충전 인프라는 계속 늘어난다. 어느 시점 스냅샷인지가
결과 해석에 영향을 준다.

## 표준노드링크 (차로수, T-24)

경부선 본선 차로수는 국토교통부 **표준노드링크** SHP 에서 뽑는다.

1. ITS 국가교통정보센터 자료실에서 "전국표준노드링크" 를 받는다.
   <https://www.its.go.kr/nodelink/nodelinkRef>  (공공데이터포털에도 같은 자료가 있다)
2. 압축을 풀어 `MOCT_LINK.shp` / `.shx` / `.dbf` 세 파일을 아래 경로에 둔다.

```
data/raw/nodelink/
    MOCT_LINK.shp
    MOCT_LINK.shx
    MOCT_LINK.dbf
```

3. 실행 순서

```bash
python scripts/build_route.py           # -> data/processed/gyeongbu_route.json (이정축)
python scripts/audit_lane_mapping.py    # SHP -> data/processed/lane_mapping_audit.parquet
python scripts/build_lane_profile.py    # -> data/processed/lanes_gyeongbu.parquet
```

`build_lane_profile.py` 의 마지막 단계는 실측 교통량으로 차로수를 검산한다
(`traffic_gyeongbu.parquet`). 이 파일이 없으면 검산을 건너뛰고 그대로 저장하는데,
**차로수를 반증할 수 있는 유일한 장치라서 건너뛰면 안 된다.** 먼저 T-07 정리본을
만든다 (인증키 불필요, 각 수집 몇 분).

```bash
python scripts/fetch_traffic.py --start 2026-02-13 --end 2026-02-22 --label seollal2026
python scripts/fetch_traffic.py --start 2026-03-06 --end 2026-03-15 --label base202603
python scripts/build_traffic.py         # -> data/processed/traffic_gyeongbu.parquet
```

정리본을 남의 것으로 복사해 쓰지 않는다. 교통량의 `offset_km` 은 각자의
`gyeongbu_route.json` 기준이라, 차로 프로파일과 이정축이 어긋나면 검산이 엉뚱한
구간끼리 비교한다.

좌표계는 EPSG:5186(Korea 2000 중부원점)이고 스크립트가 WGS84 로 변환한다.
읽기는 `pyshp` + `pyproj` 로 한다 (geopandas 는 GDAL 의존성 때문에 쓰지 않는다).

`build_lane_profile.py` 가 "본선일 수 없는 차로수" 를 보고하면, 그 링크의 원본
속성을 캐시에서 그대로 확인할 수 있다 (SHP 매핑을 다시 돌리지 않는다).

```bash
python scripts/inspect_links.py 3520811800 3520811801
python scripts/inspect_links.py --km 78.2 81.9 --direction UP
```
배포본이 갱신되면 링크 좌표와 차로수가 바뀔 수 있으니, 받은 날짜를 같은 폴더의
`.md` 노트에 적어둔다.

## 파일마다 같이 남길 것

같은 이름의 `.md` 노트에 이걸 적는다.

- 요청한 API 엔드포인트와 파라미터 (키는 제외)
- 응답 필드의 의미 — 특히 애매한 것 (`chgerType`, `stat`, `busiId` 등)
- 이상한 점 (중복 행, 결측, 좌표가 바다 위에 찍힌 행 등)

## git

이 폴더는 `.gitignore` 로 제외된다 (용량 + API 키로 받은 데이터).
팀원끼리는 공유 드라이브로 주고받고, 리포에는 `scripts/fetch_*.py` 만 올려서
누구나 다시 받을 수 있게 한다.

## API 키

`.env.example` 을 `.env` 로 복사해서 채운다. `.env` 는 커밋되지 않는다.
