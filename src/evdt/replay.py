"""재현 기간 — 어느 연휴를 무대로 쓸 것인가 (#54).

## 왜 갈아끼울 수 있어야 하나

우리가 만드는 것은 **엔진**이다. 재현(UE)은 쏠림을 만들어 내고 엔진들을 같은 조건에서
비교하는 **무대**다. 무대가 하나로 못박혀 있으면, 더 나은 무대가 생겼을 때 코드를
고쳐야 한다.

실제로 그럴 일이 생겼다. 2026 설(2/13) 실측은 구간 평균속도 89.4 km/h 로 **거의 안
막혔다** (평시 90.1 km/h 와 차이가 없다). 서울→부산이 실측 속도로 4.3시간이다.
반면 2026 추석은 10시간이 걸렸다고 보도됐다 — 41.5 km/h, 설의 절반 이하다.
정체를 재현하고 그 위에서 엔진을 비교하기에는 추석이 훨씬 나은 무대다.

추석 자료는 아직 포털에 안 올라왔을 수 있다. 그래서 **지금은 설로 돌리되, 자료가
올라오면 config 한 줄로 갈아끼울 수 있게** 해 둔다.

## 갈아끼우는 법

```bash
python scripts/fetch_traffic.py --start 2026-09-23 --end 2026-10-02 --label chuseok2026
python scripts/build_traffic.py
python scripts/build_replay.py --period chuseok2026 --direction DOWN
python scripts/build_replay.py --period chuseok2026 --direction UP
```

그 다음 시나리오 config 에서

```yaml
demand:
  period: chuseok2026
  volume_profile:     data/processed/volume_gyeongbu_down_chuseok.csv
  through_profile:    data/processed/through_gyeongbu_down_chuseok.csv
  entry_exit_profile: data/processed/entry_exit_gyeongbu_down_chuseok.csv
```

`period` 는 교통량 정리본(`traffic_gyeongbu.parquet`)의 `period` 열과 같아야 한다.
프로파일 파일 이름은 규칙이 있을 뿐 강제는 아니다 — config 가 가리키는 것이 진실이다.

## 무엇이 재현 기간에 딸려 오나

| | 지금 | 갈아끼울 때 같이 봐야 할 것 |
|---|---|---|
| 교통량·속도 | `period` 로 고른다 | `fetch_traffic.py` 로 먼저 받아야 한다 |
| 진입·목적지 | 프로파일 3개 | `build_replay.py` 가 한 번에 만든다 |
| 기온 | `environment.temp_c` | 그 연휴의 실측 기온으로 바꿔야 한다 |
| 충전기 현황 | 수집본 | 그 시점 현황 (#52 · #64) |
| EV 비중 | `demand.ev_share` | 그 시점 값 (근거는 3차 스프린트) |

**기온·충전기·EV 비중은 자동으로 안 따라온다.** 그래서 `PERIODS` 에 알려진 기간의
정보를 적어 두고, 러너가 무엇을 쓰는지 로그와 run params 에 남긴다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplayPeriod:
    """재현 기간 하나. `traffic_gyeongbu.parquet` 의 period 열과 이름이 같다."""

    period: str
    label: str
    note: str = ""

    def profile_tag(self) -> str:
        """프로파일 파일 이름에 쓰는 짧은 이름 (seollal2026 → seollal)."""

        for suffix in ("2026", "2027", "2025"):
            if self.period.endswith(suffix):
                return self.period[: -len(suffix)]

        return self.period


#: 알려진 재현 기간. 없는 period 도 쓸 수 있다 — 여기 있으면 로그에 설명이 붙는다.
PERIODS: dict[str, ReplayPeriod] = {
    "seollal2026": ReplayPeriod(
        "seollal2026", "2026 설 연휴",
        "실측 구간 평균속도 89.4 km/h · 서울→부산 4.3시간. 평시(90.1)와 차이가 거의 없다 — "
        "정체 재현의 무대로는 약하다",
    ),
    "chuseok2026": ReplayPeriod(
        "chuseok2026", "2026 추석 연휴",
        "서울→부산 약 10시간(41.5 km/h) 보도. 정체 재현과 엔진 비교에 훨씬 나은 무대. "
        "자료가 포털에 올라오면 fetch_traffic.py → build_replay.py 로 갈아끼운다",
    ),
    "base202603": ReplayPeriod("base202603", "평시 2026-03", "대조군"),
}


def describe(period: str) -> str:
    """로그와 run params 에 남길 한 줄."""

    known = PERIODS.get(period)

    return f"{known.label} ({period})" if known else f"기간 {period} (등록되지 않은 기간)"
