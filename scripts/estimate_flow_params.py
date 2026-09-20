"""VDS 실측과 CTM 설정값으로 교통류 파라미터를 정리한다 (T-08 / #24).

    python scripts/estimate_flow_params.py

입력:
    data/processed/speed_gyeongbu.parquet
    data/processed/traffic_gyeongbu.parquet
    config/flow_params.yaml

출력:
    config/flow_params.yaml
        CTM 설정값 + 방향별 VDS 실측값 + 파생값

    data/processed/flow_params_by_conzone.parquet
        콘존별 VDS 추정값

#24 이후 파라미터 역할:
- v_free: VDS 실측
- lanes: 표준노드링크 실측 (lanes_gyeongbu.parquet)
- w_back: config 기준값
- k_jam: config 기준값
- q_max: triangular FD 식으로 파생

VDS의 q_p95 / q_breakdown 값은 실제 교통 상태를 검증하기 위한 관측값이며,
그 자체를 차로당 q_max 로 사용하지 않는다.
"""

from __future__ import annotations

import sys
from datetime import datetime

import _bootstrap  # noqa: F401
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from evdt.io import flow_params as fp  # noqa: E402
from evdt.io.lane_profile import max_dt_min  # noqa: E402
from evdt.paths import CONFIG_DIR, DATA_PROCESSED_DIR  # noqa: E402


def main() -> int:
    config_path = CONFIG_DIR / "flow_params.yaml"

    config = yaml.safe_load(
        config_path.read_text(encoding="utf-8")
    )

    defaults = config["defaults"]
    sensitivity = config["sensitivity"]

    dt_min = float(config["dt_min"])

    default_v_free = float(defaults["v_free_kmh"])
    w_back = float(defaults["w_back_kmh"])
    k_jam = float(defaults["k_jam_veh_km_lane"])
    default_lanes = int(defaults["lanes"])
    q_anchor = float(
        defaults["q_max_anchor_veh_h_lane"]
    )
    min_cell_km = float(defaults["min_cell_length_km"])

    # CFL: v_free * dt <= 셀 길이. 어기면 한 스텝에 셀을 건너뛴다.
    dt_limit_min = max_dt_min(default_v_free, min_cell_km)

    if dt_min > dt_limit_min + 1e-9:
        raise SystemExit(
            f"dt_min={dt_min}분 은 CFL 조건을 어깁니다. "
            f"v_free {default_v_free} km/h, 셀 {min_cell_km} km 면 "
            f"dt <= {dt_limit_min:.2f}분 이어야 합니다."
        )

    k_jam_candidates = [
        float(x)
        for x in sensitivity["k_jam_veh_km_lane"]
    ]

    speed = pd.read_parquet(
        DATA_PROCESSED_DIR / "speed_gyeongbu.parquet"
    )
    volume = pd.read_parquet(
        DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet"
    )

    periods = sorted(set(speed["period"]))

    by_conzone = fp.estimate_by_conzone(
        speed,
        volume,
    )

    by_conzone.to_parquet(
        DATA_PROCESSED_DIR
        / "flow_params_by_conzone.parquet",
        index=False,
    )

    summary = fp.summarize(by_conzone)

    lines = [
        "# ===========================================================================",
        "#  CTM 교통류 파라미터 (T-08 / #24)",
        "#",
        "#  defaults / sensitivity는 모델 설정값.",
        "#  DOWN / UP의 measured 값은 scripts/estimate_flow_params.py가 갱신한다.",
        f"#  생성: {datetime.now().astimezone():%Y-%m-%d %H:%M}"
        f"  /  기간: {', '.join(periods)}",
        "#",
        "#  source:",
        "#    measured  VDS 또는 표준노드링크 실측",
        "#    derived   triangular FD 관계식으로 계산",
        "#    assumed   직접 관측하기 어려워 config 기준값 사용",
        "#",
        "#  triangular FD:",
        "#    q_max_per_lane = v_free * w_back * k_jam / (v_free + w_back)",
        "# ===========================================================================",
        "",
        "# CFL 조건: 한 스텝에 차가 셀 하나를 넘어가면 CTM 이 성립하지 않는다.",
        "#   v_free * dt <= min_cell_length_km",
        f"#   {default_v_free:g} km/h, {min_cell_km:g} km 셀 -> dt <= {dt_limit_min:.2f}분",
        f"dt_min: {dt_min}",
        "",
        "defaults:",
        f"  v_free_kmh: {default_v_free}",
        f"  w_back_kmh: {w_back}",
        f"  k_jam_veh_km_lane: {k_jam}",
        f"  lanes: {default_lanes}",
        f"  q_max_anchor_veh_h_lane: {q_anchor}",
        "  # 차로수 변경점이 셀 경계가 된다. 이보다 짧은 구간은 이웃에 병합한다.",
        f"  min_cell_length_km: {min_cell_km}",
        "",
        "sensitivity:",
        f"  k_jam_veh_km_lane: {k_jam_candidates}",
        "",
        "# 콘존별 VDS 분석:",
        "#   data/processed/flow_params_by_conzone.parquet",
        "#",
        "# 차로수:",
        "#   data/processed/lanes_gyeongbu.parquet",
        "#   값이 있으면 measured, 없으면 defaults.lanes 사용",
        "",
    ]

    for direction in ("DOWN", "UP"):
        s = summary[direction]

        v = s["v_free_kmh"]
        q95 = s["q_p95_veh_h"]
        qb = s["q_breakdown_veh_h"]

        v_free = float(v["median"])

        q_model = fp.q_per_lane(
            v_free_kmh=v_free,
            w_back_kmh=w_back,
            k_jam_veh_km_lane=k_jam,
        )

        spacing_m = 1000.0 / k_jam

        lines += [
            f"{direction}:",
            "",
            "  v_free_kmh:",
            f"    value: {v_free}",
            "    source: measured",
            f"    method: 새벽 {fp.FREE_FLOW_HOURS[0]}~"
            f"{fp.FREE_FLOW_HOURS[-1]}시 관측속도의 "
            f"{int(fp.V_FREE_QUANTILE * 100)}퍼센타일, "
            "콘존별 추정값의 중앙값",
            f"    iqr: [{v['p25']}, {v['p75']}]",
            f"    n_conzones: {v['n_conzones']}",
            f"    assumed_was: {default_v_free}",
            "",
            "  w_back_kmh:",
            f"    value: {w_back}",
            "    source: assumed",
            "    note: >-",
            "      정체파 후진 전파속도는 VDS 교통량만으로 안정적으로 직접",
            "      추정하지 않고 defaults의 기준값을 사용한다.",
            "",
            "  k_jam_veh_km_lane:",
            f"    value: {k_jam}",
            "    source: assumed",
            f"    implied_vehicle_spacing_m: {spacing_m:.2f}",
            f"    sensitivity: {k_jam_candidates}",
            "    note: >-",
            "      정체밀도는 VDS만으로 직접 관측하기 어렵기 때문에",
            "      민감도 분석 대상으로 남긴다.",
            "",
            "  q_max_veh_h_lane:",
            f"    value: {q_model:.1f}",
            "    source: derived",
            "    formula: v_free * w_back * k_jam / (v_free + w_back)",
            f"    reference_anchor: {q_anchor}",
            "    note: >-",
            "      q_max는 독립 입력값이 아니라 v_free, w_back, k_jam으로",
            "      계산한다. reference_anchor는 파라미터 조합의",
            "      물리적 타당성을 검산하는 기준값이다.",
            "",
            "  observed_vds:",
            "    breakdown_flow_veh_h_section:",
            f"      median: {qb['median']}",
            f"      iqr: [{qb['p25']}, {qb['p75']}]",
            f"      n_conzones: {qb['n_conzones']}",
            "    p95_hourly_volume_veh_h_section:",
            f"      median: {q95['median']}",
            f"      iqr: [{q95['p25']}, {q95['p75']}]",
            f"      n_conzones: {q95['n_conzones']}",
            "    note: >-",
            "      위 값은 콘존 전체의 관측 교통량이다.",
            "      차로당 CTM q_max로 직접 사용하지 않는다.",
            "",
        ]

    congestion_speed = float(
        config["validation"]["congestion_speed_kmh"]
    )

    lines += [
        "validation:",
        f"  congestion_speed_kmh: {congestion_speed}",
        "",
    ]

    config_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    print(config_path)

    for direction in ("DOWN", "UP"):
        s = summary[direction]

        v_free = float(
            s["v_free_kmh"]["median"]
        )

        q_model = fp.q_per_lane(
            v_free_kmh=v_free,
            w_back_kmh=w_back,
            k_jam_veh_km_lane=k_jam,
        )

        print(
            f"{direction}: "
            f"v_free {v_free} km/h, "
            f"q_max {q_model:,.1f} veh/h/lane, "
            f"병목 flow 중앙값 "
            f"{s['q_breakdown_veh_h']['median']:,.0f} veh/h/구간 "
            f"({s['q_breakdown_veh_h']['n_conzones']}개 콘존)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())