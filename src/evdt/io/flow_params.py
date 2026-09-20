"""VDS 실측으로 CTM 교통류 파라미터를 추정한다 (T-08).

v_free (자유속도)
    교통량이 가장 적은 새벽(FREE_FLOW_HOURS) 관측속도의 85퍼센타일.
    평균이 아니라 85퍼센타일인 이유: 새벽에도 느린 화물차가 섞여 평균을
    끌어내린다. 교통공학에서 자유속도는 관행적으로 상위 분위수로 잡는다.

q_max (용량) — 두 가지를 같이 낸다
    q_p95_veh_h
        관측 시간교통량의 95퍼센타일. 최댓값을 쓰지 않는다 — 검지기 이상치
        한 시간이 용량을 결정해 버린다.
        ⚠ "관측된 최대 수요" 라서 용량의 하한일 뿐이다. 2026 설·3월 자료에서
        중앙값이 구간당 약 3,300대/h (4차로로 나누면 830대/h/차로)로, 용량의
        절반도 안 된다. 대부분 구간이 한 번도 막히지 않았기 때문이다.
    q_breakdown_veh_h
        정체 직전 1시간(이번 시간 소통, 다음 시간 정체)의 교통량 중앙값.
        실제로 막힌 병목 구간에서만 나오고, 그 구간 용량의 추정값이다.
        서울~천안 약 5,000~5,600, 천안~대전 약 2,900~3,100 대/h/구간 으로
        차로 수에 따라 두 배 가까이 다르다.
    ⚠ 포털 교통량은 전 차로 합계다. 차로 수가 구간마다 달라 코리도 대푯값
      하나로 쓰면 안 되고, 차로당 값은 차로 수(T-09)를 알아야 나온다.

k_jam (혼잡밀도)
    이 데이터로는 나오지 않는다. 밀도는 점검지기로 직접 관측되지 않는다.
    끝까지 가정값이며 132 / 144 / 169 대/km/차로 로 민감도 분석한다.

w_back (충격파 속도)
    삼각형 기본도에서 q_max = v·w·k / (v + w) 이므로, v_free·q_max 실측과
    k_jam 가정이 정해지면 w_back 이 따라 나온다 (§9.5, 독립 파라미터가 아니다).
    ⚠ 차로당 q_max 가 있어야 역산할 수 있어서 셀 차로 수(T-09) 이후로 미룬다.
      p95 교통량을 넣으면 6km/h 처럼 비현실적인 값이 나온다 (수요 ≠ 용량).
        w = q·v / (k·v − q)
"""

from __future__ import annotations

import pandas as pd

FREE_FLOW_HOURS = (3, 4, 5)
V_FREE_QUANTILE = 0.85
Q_MAX_QUANTILE = 0.95

#: 새벽 표본이 이보다 적은 콘존은 v_free 를 내지 않는다
MIN_FREE_FLOW_SAMPLES = 12

K_JAM_CANDIDATES = (132.0, 144.0, 169.0)

#: 정체 직전 흐름을 내려면 이만큼의 정체 시작 사례가 있어야 한다
MIN_BREAKDOWN_EVENTS = 2


def estimate_by_conzone(
    speed: pd.DataFrame,
    volume: pd.DataFrame,
    congestion_kmh: float = 50.0,
) -> pd.DataFrame:
    """콘존별 v_free, q_p95, q_breakdown 추정. 입력은 build_traffic 정리본."""

    key = ["direction", "conzone_id", "conzone_name", "offset_km"]

    night = speed[speed["hour"].isin(FREE_FLOW_HOURS)].dropna(subset=["speed_kmh"])
    v = night.groupby(key)["speed_kmh"].agg(
        v_free_kmh=lambda s: s.quantile(V_FREE_QUANTILE),
        n_free_flow="size",
    )
    v.loc[v["n_free_flow"] < MIN_FREE_FLOW_SAMPLES, "v_free_kmh"] = float("nan")

    q = (
        volume.dropna(subset=["volume_veh"])
        .groupby(key)["volume_veh"]
        .agg(
            q_p95_veh_h=lambda s: s.quantile(Q_MAX_QUANTILE),
            q_observed_max_veh_h="max",
            n_hours="size",
        )
    )

    b = breakdown_flow(speed, volume, congestion_kmh)

    return (
        v.join(q, how="outer").join(b, how="left").reset_index()
        .sort_values(["direction", "offset_km"])
    )


def breakdown_flow(
    speed: pd.DataFrame,
    volume: pd.DataFrame,
    congestion_kmh: float,
) -> pd.DataFrame:
    """콘존별 정체 직전 1시간 교통량의 중앙값과 사례 수."""

    key = ["direction", "conzone_id", "conzone_name", "offset_km"]
    at = ["period", *key, "date", "hour"]

    d = (
        volume[at + ["volume_veh"]]
        .merge(speed[at + ["speed_kmh"]], on=at)
        .sort_values(["period", "conzone_id", "date", "hour"])
    )
    # 다음 시간이 실제로 바로 다음 시각이어야 한다 (결측으로 건너뛴 시간은 제외)
    d["jam"] = d["speed_kmh"] < congestion_kmh
    group = d.groupby(["period", "conzone_id", "date"])
    d["next_jam"] = group["jam"].shift(-1)
    d["next_hour"] = group["hour"].shift(-1)

    pre = d[
        d["speed_kmh"].notna()
        & d["volume_veh"].notna()
        & ~d["jam"]
        & (d["next_jam"] == True)  # noqa: E712  (NaN 을 거르기 위해 == 비교)
        & (d["next_hour"] == d["hour"] + 1)
    ]

    b = pre.groupby(key)["volume_veh"].agg(q_breakdown_veh_h="median", n_breakdown="size")
    b.loc[b["n_breakdown"] < MIN_BREAKDOWN_EVENTS, "q_breakdown_veh_h"] = float("nan")
    return b


def q_per_lane(
    v_free_kmh: float,
    w_back_kmh: float,
    k_jam_veh_km_lane: float,
) -> float:
    """삼각형 기본도에서 차로당 q_max를 계산한다."""
    if v_free_kmh <= 0:
        raise ValueError("v_free_kmh는 0보다 커야 합니다.")
    if w_back_kmh <= 0:
        raise ValueError("w_back_kmh는 0보다 커야 합니다.")
    if k_jam_veh_km_lane <= 0:
        raise ValueError("k_jam_veh_km_lane는 0보다 커야 합니다.")

    return (
        v_free_kmh
        * w_back_kmh
        * k_jam_veh_km_lane
        / (v_free_kmh + w_back_kmh)
    )


def k_from_q(
    v_free_kmh: float,
    w_back_kmh: float,
    q_max_veh_h_lane: float,
) -> float:
    """삼각형 기본도에서 q_max로 k_jam을 역산한다."""
    if v_free_kmh <= 0:
        raise ValueError("v_free_kmh는 0보다 커야 합니다.")
    if w_back_kmh <= 0:
        raise ValueError("w_back_kmh는 0보다 커야 합니다.")
    if q_max_veh_h_lane <= 0:
        raise ValueError("q_max_veh_h_lane는 0보다 커야 합니다.")

    return (
        q_max_veh_h_lane
        * (v_free_kmh + w_back_kmh)
        / (v_free_kmh * w_back_kmh)
    )

def derived_w_back(v_free_kmh: float, q_max_veh_h_lane: float, k_jam: float) -> float:
    """삼각형 기본도에서 v, q, k 로 w 를 역산한다. 성립하지 않으면 예외."""

    denominator = k_jam * v_free_kmh - q_max_veh_h_lane

    if denominator <= 0:
        raise ValueError(
            f"q_max({q_max_veh_h_lane:.0f}) 가 k_jam·v_free({k_jam * v_free_kmh:.0f}) 이상이라 "
            "삼각형 기본도가 성립하지 않습니다"
        )

    return q_max_veh_h_lane * v_free_kmh / denominator


def summarize(by_conzone: pd.DataFrame) -> dict:
    """방향별 대푯값 (콘존 추정값의 중앙값과 사분위 범위)."""

    out = {}

    for direction, g in by_conzone.groupby("direction"):
        entry = {}
        for column in ("v_free_kmh", "q_p95_veh_h", "q_breakdown_veh_h"):
            s = g[column].dropna()
            entry[column] = {
                "median": round(float(s.median()), 1),
                "p25": round(float(s.quantile(0.25)), 1),
                "p75": round(float(s.quantile(0.75)), 1),
                "n_conzones": int(s.size),
            }
        out[direction] = entry

    return out
