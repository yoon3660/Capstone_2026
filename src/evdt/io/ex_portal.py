"""고속도로 공공데이터 포털(data.ex.co.kr) 구간 교통량·속도 수집 (T-07 / T-08).

포털의 "데이터조회 > 교통 > 구간교통량(VDS)" 과 "구간속도" 화면이 부르는
JSON 엔드포인트를 그대로 쓴다. 화면의 CSV 버튼은 이 JSON 을 브라우저에서 표로
바꿔 내려받는 것뿐이라, 서버가 준 JSON 이 가장 원본에 가깝다.

    교통량  POST /portal/traffic/getTrafficVds   → result.excelData
    속도    POST /portal/speed/getSpeedVDS       → excelData
    콘존    POST /portal/traffic/getTrafficConzone (lineNo, direction)

인증키가 필요 없다. 한 번에 최대 30일(종료일 포함 31일 이내)까지만 조회된다.

값의 단위
    교통량: 콘존(VDS 구간) 단면의 1시간 통과 대수, 전 차로 합계 [대/h]
    속도:   콘존의 1시간 평균 속도 [km/h]

노선 식별자 (세 표기가 같은 경부선을 가리킨다)
    도로중심선 파일        ROAD_NO = "10"
    도로공사 OpenAPI       routeNo = "0010"   (locationinfoRest / Ic 등)
    포털 구간 교통량·속도  lineNo  = "0010",  conzoneId = "0010CZ{S|E}nnn"

방향 코드 (포털 콘존)
    S = 서울 → 부산 = 하행(DOWN).  콘존 목록이 한남IC 에서 시작해 구서IC 로 끝난다.
    E = 부산 → 서울 = 상행(UP).
    getTrafficDirection 은 startName=부산, endName=서울 을 주고, 화면은 S 를
    "서울→부산" 으로 표시한다. 이름만 보면 뒤집혀 보이니 주의.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PORTAL_URL = "https://data.ex.co.kr"
GYEONGBU_LINE_NO = "0010"

#: 포털 방향 코드 → 우리 방향
PORTAL_DIRECTION = {"S": "DOWN", "E": "UP"}

#: 포털이 한 번에 돌려주는 최대 일수 (화면 안내: "최대 30일", 실제 31일까지 허용)
MAX_WINDOW_DAYS = 30

#: dataset → (엔드포인트, xAxis)
#: volume_daily 는 포털이 따로 집계한 일 합계다. 시간대 합계와 대조하는 데 쓴다.
DATASETS = {
    "volume": ("/portal/traffic/getTrafficVds", "time"),
    "speed": ("/portal/speed/getSpeedVDS", "time"),
    "volume_daily": ("/portal/traffic/getTrafficVds", "day"),
}

#: 포털의 결측 코드. 실제 값이 아니므로 결측(None)으로 바꾸고 코드는 따로 남긴다.
#:   교통량 -1 : 콘존·일자 단위로 24시간 통째로 나온다 (검지기 없음·장기 고장)
#:   교통량 -2 : 시간 단위로 드문드문 나온다 (일시 장애)
#:   속도    0 : 교통량이 결측인 시간과 거의 1:1 로 겹친다 (2026 설: 4,437 vs 4,433)
#: 2026-02 설날 수집본 기준 교통량의 14% 가 결측이다.
MISSING_CODES = {
    "volume": {-1.0, -2.0},
    "speed": {0.0},
}

#: 값 범위 검사. 결측 코드가 아닌데 넘으면 파싱 단계에서 멈춘다.
#: 교통량: 경부선 최대 단면(편도 5차로) 용량이 약 11,000 대/h 이므로 넉넉히 두 배.
MAX_VOLUME_VEH_H = 20_000
MAX_SPEED_KMH = 200.0


class PortalError(RuntimeError):
    """포털 응답이 예상과 다를 때."""


@dataclass(frozen=True)
class Conzone:
    conzone_id: str
    name: str          # "안성IC-오산IC"
    direction: str     # "UP" | "DOWN"
    seq: int           # 진행 방향 순서 0, 1, 2, ...

    @property
    def endpoints(self) -> tuple[str, str]:
        start, end = (part.strip() for part in self.name.split("-", 1))
        return start, end


def _post(path: str, params: dict[str, str], *, retries: int = 3) -> object:
    body = urlencode(params).encode()
    request = Request(
        PORTAL_URL + path,
        data=body,
        headers={
            # 기본 urllib UA 는 포털 방화벽이 막는 경우가 있다.
            "User-Agent": "Mozilla/5.0 (evdt capstone data collection)",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )

    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8-sig"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))

    raise PortalError(f"포털 요청 실패: {path} {params} ({last_error})")


def fetch_conzones(direction: str) -> tuple[list[Conzone], object]:
    """경부선 한 방향의 콘존 목록. (파싱 결과, 원본 응답) 을 돌려준다."""

    portal_code = {v: k for k, v in PORTAL_DIRECTION.items()}[direction]
    raw = _post(
        "/portal/traffic/getTrafficConzone",
        {"lineNo": GYEONGBU_LINE_NO, "direction": portal_code},
    )
    items = raw.get("result") if isinstance(raw, dict) else None

    if not isinstance(items, list) or not items:
        raise PortalError(f"콘존 목록이 비어 있습니다: {direction}")

    conzones = []

    for seq, item in enumerate(items):
        conzone_id = item.get("value", "")

        if not conzone_id.startswith(f"{GYEONGBU_LINE_NO}CZ{portal_code}"):
            raise PortalError(f"방향 코드가 다른 콘존이 섞였습니다: {conzone_id}")

        conzones.append(
            Conzone(
                conzone_id=conzone_id,
                name=item.get("text", ""),
                direction=direction,
                seq=seq,
            )
        )

    return conzones, raw


def date_windows(start: date, end: date) -> list[tuple[date, date]]:
    """[start, end] 를 포털 조회 한도 안의 구간으로 나눈다."""

    if end < start:
        raise ValueError(f"종료일이 시작일보다 앞입니다: {start} ~ {end}")

    windows = []
    cursor = start

    while cursor <= end:
        window_end = min(cursor + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)

    return windows


def fetch_window(
    dataset: str,
    conzone_id: str,
    start: date,
    end: date,
) -> object:
    """콘존 하나의 값을 한 조회 창만큼 받아 원본 응답 그대로 돌려준다."""

    if dataset not in DATASETS:
        raise ValueError(f"알 수 없는 dataset: {dataset}")

    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise ValueError(f"포털 조회 한도({MAX_WINDOW_DAYS}일)를 넘습니다: {start} ~ {end}")

    path, x_axis = DATASETS[dataset]

    return _post(
        path,
        {
            "searchDay": end.strftime("%Y%m%d"),
            "searchDayFrom": start.strftime("%Y%m%d"),
            "conzoneId": conzone_id,
            "xAxis": x_axis,
            "compTgt": "day",
            "compTgt2": "",
        },
    )


def _excel_rows(dataset: str, raw: object) -> list[dict]:
    if dataset in ("volume", "volume_daily"):
        rows = raw.get("result", {}).get("excelData") if isinstance(raw, dict) else None
    else:
        rows = raw.get("excelData") if isinstance(raw, dict) else None

    if rows is None:
        raise PortalError(f"{dataset} 응답에 excelData 가 없습니다")

    return rows


def parse_hourly(dataset: str, raw: object) -> list[dict]:
    """원본 응답을 (date, hour, value) 행으로 펼친다.

    excelData 는 시간(preTime 00~23) 한 행에 날짜 열(day_0, day_1, ...)과
    값 열(day_Cnt0, day_Cnt1, ...)이 짝지어 들어 있다.
    값이 비어 있거나 결측 코드(MISSING_CODES)면 value=None 으로 남기고
    missing_code 에 원래 코드를 적는다. 행을 버리지 않는다.
    """

    if dataset not in ("volume", "speed"):
        raise ValueError(f"시간대별 dataset 이 아닙니다: {dataset}")

    upper = MAX_VOLUME_VEH_H if dataset == "volume" else MAX_SPEED_KMH
    result = []

    for row in _excel_rows(dataset, raw):
        hour = int(row["preTime"])

        if not 0 <= hour <= 23:
            raise PortalError(f"시간 값이 범위를 벗어났습니다: {row['preTime']}")

        i = 0
        while f"day_{i}" in row:
            day = row.get(f"day_{i}")

            if day:
                text = row.get(f"day_Cnt{i}")
                value = None if text in (None, "") else float(text)
                missing_code = None

                if value in MISSING_CODES[dataset]:
                    missing_code, value = value, None

                lower_ok = value is None or (
                    value >= 0 if dataset == "volume" else value > 0
                )

                if value is not None and (not lower_ok or value > upper):
                    # 결측 코드가 아닌 음수, 단면 용량의 두 배를 넘는 교통량,
                    # 0 이하·200 초과 속도는 멈춘다. 교통량 0 은 남긴다.
                    raise PortalError(
                        f"{dataset} 값이 비상식적입니다: {day} {hour:02d}시 = {value}"
                    )

                result.append(
                    {"date": day, "hour": hour, "value": value, "missing_code": missing_code}
                )

            i += 1

    return result


def parse_daily(raw: object) -> list[dict]:
    """volume_daily 원본을 (date, value) 행으로. 결측은 None.

    포털은 결측 코드(-1, -2)가 섞인 날도 그대로 더해서 준다 (하루 전체 -1 이면 -24).
    음수 일 합계는 결측으로 본다. 시간대 합계와의 대조는 24시간이 다 있는 날만 한다.
    """

    result = []

    for row in _excel_rows("volume_daily", raw):
        value = row.get("trafficCnt")
        value = None if value in (None, "") else float(value)

        if value is not None and value < 0:
            value = None

        if value is not None and value > MAX_VOLUME_VEH_H * 24:
            raise PortalError(f"일 교통량이 비상식적입니다: {row.get('preDay')} = {value}")

        result.append({"date": row["preDay"], "value": value})

    return result
