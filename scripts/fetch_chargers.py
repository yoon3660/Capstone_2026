import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError


API_URL = "https://apis.data.go.kr/B552584/EvCharger/getChargerInfo"


def load_api_key():
    """프로젝트 루트의 .env에서 인증키를 읽는다."""
    env_path = Path(".env")

    if not env_path.exists():
        raise RuntimeError(".env 파일이 없습니다.")

    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()

        if line.startswith("EV_CHARGER_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")

            if key:
                return key

    raise RuntimeError(".env에 EV_CHARGER_API_KEY를 설정해주세요.")


def fetch_page(api_key, page, rows):
    """API에서 한 페이지의 원본 데이터와 JSON을 반환한다."""
    params = {
        "serviceKey": api_key,
        "pageNo": page,
        "numOfRows": rows,
        "kindDetail": "C001",
        "dataType": "JSON",
    }

    url = API_URL + "?" + urlencode(params)

    try:
        with urlopen(url, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"API HTTP 오류: {exc.code}") from None
    except URLError:
        raise RuntimeError("API 연결 실패: 인터넷 연결을 확인하세요.") from None

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError(
            "JSON 응답이 아닙니다. API 승인 상태와 dataType 설정을 확인하세요."
        ) from None

    if not isinstance(data, dict) or str(data.get("resultCode")) != "00":
        raise RuntimeError(
            f"API 오류: {data.get('resultMsg', '알 수 없는 응답')}"
        )

    return raw, data


def get_items(data):
    """문서에 나온 items.item 구조에서 충전기 목록을 꺼낸다."""
    items = data.get("items") or {}

    if not isinstance(items, dict):
        raise RuntimeError("예상과 다른 items 응답 구조입니다.")

    chargers = items.get("item") or []

    if isinstance(chargers, dict):
        chargers = [chargers]

    if not isinstance(chargers, list):
        raise RuntimeError("예상과 다른 item 응답 구조입니다.")

    return chargers


def main():
    all_pages = "--all" in sys.argv

    api_key = load_api_key()
    rows = 1000 if all_pages else 10

    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    mode = "all" if all_pages else "sample"

    output_dir = Path("data/raw") / f"highway_chargers_{timestamp}_{mode}"
    output_dir.mkdir(parents=True, exist_ok=False)

    page = 1
    collected = 0
    total_count = None
    station_ids = set()
    charger_ids = set()

    while True:
        raw, data = fetch_page(api_key, page, rows)
        chargers = get_items(data)

        # 서버가 보내준 JSON 원본을 수정 없이 저장한다.
        output_path = output_dir / f"page_{page:04d}.json"
        output_path.write_bytes(raw)

        if total_count is None:
            total_count = int(data["totalCount"])
            print(f"API 전체 충전기 수: {total_count}")

        # C001 필터가 실제 적용됐는지 확인한다.
        invalid = [
            item for item in chargers
            if item.get("kindDetail") != "C001"
        ]

        if invalid:
            raise RuntimeError(
                "C001 이외 데이터가 반환됐습니다. "
                "서버 필터 적용 여부를 확인해야 합니다."
            )

        for item in chargers:
            station_ids.add(item.get("statId"))
            charger_ids.add(
                (item.get("statId"), item.get("chgerId"))
            )

        collected += len(chargers)

        print(
            f"페이지 {page}: {len(chargers)}건 수집 "
            f"(누적 {collected}/{total_count})"
        )

        if not all_pages:
            print("\n[샘플 충전소]")
            for item in chargers[:5]:
                print(
                    item.get("statNm"),
                    item.get("statId"),
                    item.get("chgerId"),
                    item.get("output"),
                    item.get("kindDetail"),
                )
            break

        if collected >= total_count:
            break

        if not chargers:
            raise RuntimeError(
                "전체 수집이 끝나기 전에 빈 페이지가 반환됐습니다."
            )

        page += 1

    print("\n저장 폴더:", output_dir)
    print("수집된 충전기 행:", collected)
    print("고유 충전소 수:", len(station_ids))
    print("고유 충전기 수:", len(charger_ids))

    if all_pages and collected != total_count:
        raise RuntimeError(
            "API 전체 건수와 실제 수집 건수가 다릅니다."
        )

    print("수집 완료!")


if __name__ == "__main__":
    main()