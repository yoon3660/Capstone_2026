"""VDS 실측으로 CTM 파라미터를 추정해 config/flow_params.yaml 을 쓴다 (T-08).

    python scripts/estimate_flow_params.py

입력: data/processed/speed_gyeongbu.parquet, traffic_gyeongbu.parquet (모든 기간)
출력: config/flow_params.yaml          방향별 대푯값 + 가정값(주석) + 출처
      data/processed/flow_params_by_conzone.parquet   콘존별 추정값

차로 수는 이 데이터에 없고 구간마다 다르다 (병목 용량이 서울~천안 약 5,000~5,600,
천안~대전 약 2,900~3,100 대/h/구간). 그래서 q_max 는 코리도 대푯값 하나를 두지 않고
콘존별 표로 남긴다. 차로당 q_max 와 그로부터 역산하는 w_back 은 T-09 에서
셀 차로 수가 정해진 뒤에 낸다.
"""

from __future__ import annotations

import sys
from datetime import datetime

import _bootstrap  # noqa: F401  (src 경로와 콘솔 인코딩을 먼저 준비한다)
import pandas as pd  # noqa: E402

from evdt.io import flow_params as fp  # noqa: E402
from evdt.paths import CONFIG_DIR, DATA_PROCESSED_DIR  # noqa: E402

#: 실측 전 가정값 (일반 문헌값). 팀이 따로 확정한 가정값 문서가 없어 이 값을 기준으로 삼는다.
ASSUMED = {
    "v_free_kmh": 100.0,          # 경부선 제한속도 100~110 km/h
    "q_max_veh_h_lane": 2200.0,   # 고속도로 기본구간 용량 (HCM 계열, 승용차 환산 전)
    "w_back_kmh": 20.0,           # 충격파 속도 문헌 범위 15~25 km/h
    "k_jam_veh_km_lane": 144.0,   # 차두간격 약 7m
}


def main() -> int:
    speed = pd.read_parquet(DATA_PROCESSED_DIR / "speed_gyeongbu.parquet")
    volume = pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet")
    periods = sorted(set(speed["period"]))

    by_conzone = fp.estimate_by_conzone(speed, volume)
    by_conzone.to_parquet(DATA_PROCESSED_DIR / "flow_params_by_conzone.parquet", index=False)
    summary = fp.summarize(by_conzone)

    lines = [
        "# ===========================================================================",
        "#  CTM 교통류 파라미터 (T-08)",
        "#",
        "#  scripts/estimate_flow_params.py 가 VDS 실측으로 쓴다. 손으로 고치지 말 것.",
        f"#  생성: {datetime.now().astimezone():%Y-%m-%d %H:%M}  /  기간: {', '.join(periods)}",
        "#",
        "#  각 값의 source:",
        "#    measured  VDS 실측 (포털 구간속도·구간교통량)",
        "#    derived   삼각형 기본도 q = v·w·k/(v+w) 에서 역산 (§9.5)",
        "#    assumed   가정값. 실측으로 대체되지 않은 값",
        "#  assumed_was: 실측 전 가정값. 얼마나 틀렸는지가 기록으로 남는다.",
        "# ===========================================================================",
        "",
        "# 콘존별 추정값: data/processed/flow_params_by_conzone.parquet",
        "#   v_free_kmh, q_p95_veh_h (용량 하한), q_breakdown_veh_h (병목 용량), 사례 수",
        "",
    ]

    for direction in ("DOWN", "UP"):
        s = summary[direction]
        v = s["v_free_kmh"]
        q95 = s["q_p95_veh_h"]
        qb = s["q_breakdown_veh_h"]

        # 참고용: 실측 q 가 용량이 아니라서 역산 w 가 비현실적으로 작게 나온다는 기록
        naive_w = round(
            fp.derived_w_back(v["median"], q95["median"] / 4, ASSUMED["k_jam_veh_km_lane"]), 1
        )

        lines += [
            f"{direction}:",
            "  v_free_kmh:",
            f"    value: {v['median']}",
            "    source: measured",
            f"    method: 새벽 {fp.FREE_FLOW_HOURS[0]}~{fp.FREE_FLOW_HOURS[-1]}시 관측속도의 "
            f"{int(fp.V_FREE_QUANTILE * 100)}퍼센타일, 콘존별 추정의 중앙값",
            f"    iqr: [{v['p25']}, {v['p75']}]",
            f"    n_conzones: {v['n_conzones']}",
            f"    assumed_was: {ASSUMED['v_free_kmh']}",
            "  q_max_veh_h_section:",
            "    value: null   # 차로 수가 구간마다 달라 대푯값 하나로 쓰면 안 된다. 콘존별 표를 쓸 것",
            "    source: measured (per conzone)",
            "    breakdown_flow:   # 정체 직전 1시간 교통량. 막힌 병목에서만 나온다 → 용량 추정",
            f"      median: {qb['median']}",
            f"      iqr: [{qb['p25']}, {qb['p75']}]",
            f"      n_conzones: {qb['n_conzones']}",
            f"    p{int(fp.Q_MAX_QUANTILE * 100)}_hourly_volume:   # 모든 콘존. 최댓값 대신 분위수. 수요라서 용량의 하한",
            f"      median: {q95['median']}",
            f"      iqr: [{q95['p25']}, {q95['p75']}]",
            f"      n_conzones: {q95['n_conzones']}",
            f"    assumed_was: {ASSUMED['q_max_veh_h_lane']}   # 대/h/차로",
            "  k_jam_veh_km_lane:",
            f"    value: {ASSUMED['k_jam_veh_km_lane']}",
            "    source: assumed",
            "    note: 점검지기로는 밀도가 관측되지 않아 이 데이터로도 안 나온다."
            f" 민감도 {list(fp.K_JAM_CANDIDATES)} (후속 이슈)",
            "  w_back_kmh:",
            f"    value: {ASSUMED['w_back_kmh']}",
            "    source: assumed",
            "    note: >-",
            f"      p95 교통량을 4차로로 나눠 역산하면 {naive_w} km/h 가 나온다. 문헌 범위(15~25)와",
            "      동떨어진 것은 p95 가 용량이 아니라 수요이기 때문이다. 셀 차로 수(T-09)가",
            "      정해지면 병목 구간 breakdown_flow 로 다시 역산한다: w = q·v / (k·v − q)",
            f"    assumed_was: {ASSUMED['w_back_kmh']}",
            "",
        ]

    lines += [
        "validation:",
        "  congestion_speed_kmh: 50.0   # 이 속도 미만인 시간을 '정체' 로 본다 (혼동행렬 기준)",
        "",
    ]

    path = CONFIG_DIR / "flow_params.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")

    print(path)
    for direction in ("DOWN", "UP"):
        s = summary[direction]
        print(
            f"{direction}: v_free {s['v_free_kmh']['median']} km/h "
            f"(IQR {s['v_free_kmh']['p25']}~{s['v_free_kmh']['p75']}), "
            f"병목 용량 중앙값 {s['q_breakdown_veh_h']['median']:,.0f} 대/h/구간 "
            f"({s['q_breakdown_veh_h']['n_conzones']}개 콘존), "
            f"p95 교통량 {s['q_p95_veh_h']['median']:,.0f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
