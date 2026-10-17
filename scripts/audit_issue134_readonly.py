"""#134 검증 보고서의 `LOCAL_NEW` 주장을 다시 계산해 문서값과 대조한다.

    python scripts/audit_issue134_readonly.py
    python scripts/audit_issue134_readonly.py --verbose

## 왜 이 스크립트가 있는가

`docs/data_validation/` 의 보고서는 근거를 `LOCAL_NEW`(이번에 직접 확인) /
`PRIOR_REPORT`(기존 티켓 인용) / `CODE_CONFIG`(설정 확인) 로 나눠 적는다.
그 구분은 정확하다. 그런데 **정작 `LOCAL_NEW` 수치를 산출한 스크립트가
저장소에 없었다.** 사람의 PowerShell 세션 출력을 문서에 옮겨 적은 상태였다.

그러면 두 가지가 안 된다.

1. **다른 사람이 같은 수치를 얻는지 확인할 수 없다.** 실제로 이 검증을 다른
   머신에서 다시 돌려보니 수집본 디렉터리 이름이 전혀 달랐다 (§ 아래 주의).
2. **데이터가 바뀌어도 아무도 모른다.** 가공 파이프라인을 손대면 63,360 이나
   54,523 이 조용히 달라지고, 문서만 옛 숫자를 들고 남는다.

그래서 이 스크립트는 **문서에 적힌 값을 기대값으로 박아 두고** 다시 계산한다.
어긋나면 실패한다. 즉 이건 감사 도구가 아니라 **문서에 대한 회귀 테스트**다.

## 읽기 전용이다

SQLite 는 `mode=ro` 로 열고, parquet·CSV·YAML·JSON 은 읽기만 한다.
DB·설정·가공본을 **쓰지 않는다.** 시뮬레이션도 돌리지 않는다.

## 이 스크립트가 **덮지 못하는** 것

보고서의 `LOCAL_NEW` 중 아래는 저장소 데이터만으로 재현할 수 없다.

| 보고서 | 왜 못 하나 |
|---|---|
| §11·§12 원본 세션 통계 | `data/raw/sp3_64/*.xlsx` (42MB) 가 Git 추적 대상이 아니다 |
| §14 입력 SHA 54/54 | `run_summary.json` 이 과거 실행 산출물이고 저장소에 없다 |
| §13 CTM 민감도 | CTM 런과 전용 스크립트가 필요하다 (`followup_issues.md` F-01) |

그 세 개는 **가진 사람이 스크립트를 커밋해야** 재현 가능해진다. 여기서는
해당 항목을 `미수행` 으로 출력만 하고 통과/실패로 세지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3

import pandas as pd
import yaml
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.vehicles import read_temp_efficiency, read_vehicle_classes, temp_table  # noqa: E402
from evdt.paths import CONFIG_DIR, DATA_PROCESSED_DIR, default_db_path  # noqa: E402
from evdt.world.charging import temp_factors  # noqa: E402

#: 문서에 적힌 값. 바꾸려면 **문서와 같이** 바꿀 것.
#: 출처: docs/data_validation/validation_report.md §3.1·§3.2,
#:       docs/data_validation/data_inventory.md §4
EXPECT = {
    # §3.1 VDS
    "vds_rows_each": 63_360,
    "vds_rows_per_period_dir": 15_840,
    "vds_both_observed": 54_523,
    "vds_both_missing": 8_825,
    "vds_one_missing": 12,
    # 기간×방향별 (양측관측, 양측결측, 한쪽만)
    "vds_breakdown": {
        ("base202603", "DOWN"): (13_646, 2_192, 2),
        ("base202603", "UP"): (13_634, 2_200, 6),
        ("seollal2026", "DOWN"): (13_656, 2_183, 1),
        ("seollal2026", "UP"): (13_587, 2_250, 3),
    },
    # Conzone
    "conzone_rows": 284,
    "conzone_internal_per_period_dir": 66,
    "conzone_external_per_period_dir": 5,
    # §3.2 진입·진출 (방향: 행, 시간, 고유경계, 진입합, 진출합, gap>1 경계, 최대gap)
    "entry_exit": {
        "down": (1_248, 24, 52, 266_160, 339_873, 9, 9.315),
        "up": (1_200, 24, 50, 335_478, 302_470, 9, 14.126),
    },
    # 차로수
    "lanes_rows": 95,
    "lanes_up": 49,
    "lanes_down": 46,
    # DB
    "corridor_rows": 2,
    "corridor_length_km": 415.058,
    "station_rows": 35,
    "charger_group_rows": 103,
    "charger_n_units": 282,
    "charger_n_units_down": 153,
    "charger_n_units_up": 129,
    "vehicle_class_rows": 5,
    "charge_curve_rows": 20,
    "temp_efficiency_rows": 9,
    "cell_rows": 792,
    "cell_rows_per_direction": 396,
    # 노선 메타
    "n_interchanges": 54,
    "n_rest_areas": 37,
    # §8.3 온도 보간 (-5 °C)
    "temp_c": -5.0,
    "range_factor": 0.827761,
    "charge_power_factor": 0.610149,
    # 기본 시나리오 설정 (양방향 동일)
    "scenario": {
        "demand.ev_share": 0.05,
        "demand.ev_count_method": "fixed",
        "demand.od_profile": None,
        "demand.travel_time": "fixed",
        "vehicles.departure_soc": "holiday",
        "vehicles.target_soc_cap": 0.8,
        "vehicles.habit_soc": 0.0,
    },
}

#: 저장소 데이터만으로는 재현할 수 없는 항목 (위 docstring 참조)
NOT_REPRODUCIBLE = [
    ("§11·§12 원본 세션 통계", "data/raw/sp3_64/*.xlsx 가 Git 추적 대상이 아니다"),
    ("§14 입력 SHA 54/54", "run_summary.json 이 과거 실행 산출물이라 저장소에 없다"),
    ("§13 CTM 민감도", "CTM 런과 전용 스크립트가 필요하다 (F-01)"),
]

FLOAT_TOL = 1e-3


class Audit:
    """검사 결과를 모아 두고 마지막에 한 번에 보고한다."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, bool]] = []

    def check(self, name: str, got: object, want: object) -> None:
        if isinstance(want, float) and isinstance(got, (int, float)):
            ok = abs(float(got) - want) <= FLOAT_TOL
        else:
            ok = got == want
        self.rows.append((name, _fmt(got), _fmt(want), ok))

    @property
    def failed(self) -> list[tuple[str, str, str, bool]]:
        return [r for r in self.rows if not r[3]]

    def report(self, *, verbose: bool) -> int:
        width = max(len(r[0]) for r in self.rows)
        print()
        for name, got, want, ok in self.rows:
            if ok and not verbose:
                continue
            mark = "  " if ok else "✗ "
            tail = "" if ok else f"   ← 문서값 {want}"
            print(f"  {mark}{name:<{width}}  {got}{tail}")

        print()
        print(f"  검사 {len(self.rows)}개 · 일치 {len(self.rows) - len(self.failed)}개 "
              f"· 불일치 {len(self.failed)}개")

        print()
        print("  저장소 데이터만으로 재현 불가 (통과/실패로 세지 않는다):")
        for item, why in NOT_REPRODUCIBLE:
            print(f"    - {item} — {why}")

        print()
        if self.failed:
            print("  [불일치] 데이터와 문서가 어긋났다. **둘 중 하나가 틀렸다.**")
            print("  가공 파이프라인을 고쳤다면 docs/data_validation/ 의 수치도 같이 고칠 것.")
            return 1
        print("  [일치] 보고서의 LOCAL_NEW 수치가 현재 데이터에서 그대로 재현된다.")
        return 0


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def audit_vds(a: Audit) -> None:
    """§3.1 — 교통량·속도의 키 정합과 결측."""
    t = pd.read_parquet(DATA_PROCESSED_DIR / "traffic_gyeongbu.parquet")
    s = pd.read_parquet(DATA_PROCESSED_DIR / "speed_gyeongbu.parquet")
    key = ["period", "date", "hour", "direction", "conzone_id"]

    a.check("VDS traffic 행", len(t), EXPECT["vds_rows_each"])
    a.check("VDS speed 행", len(s), EXPECT["vds_rows_each"])
    a.check("VDS traffic 중복 키", int(t.duplicated(key).sum()), 0)
    a.check("VDS speed 중복 키", int(s.duplicated(key).sum()), 0)

    # 상호 키 차집합 — 한쪽에만 있는 키가 있으면 격자가 깨진 것이다
    outer = t[key].merge(s[key], on=key, how="outer", indicator=True)
    a.check("VDS 키 차집합", int((outer["_merge"] != "both").sum()), 0)

    # 공간 좌표 상호 불일치
    pos = ["offset_km_start", "offset_km_end"]
    m = t[key + pos].merge(s[key + pos], on=key, suffixes=("_t", "_s"))
    mismatch = sum(
        int((m[f"{c}_t"] - m[f"{c}_s"]).abs().gt(FLOAT_TOL).sum()) for c in pos
    )
    a.check("VDS 좌표 불일치", mismatch, 0)

    # 결측: 값이 NaN 인지로 판정한다 (missing_code 전수 재판정은 아니다 — §3.1 주의)
    j = t.merge(s[key + ["speed_kmh"]], on=key, how="inner")
    vna, sna = j["volume_veh"].isna(), j["speed_kmh"].isna()
    a.check("VDS 양측 관측", int((~vna & ~sna).sum()), EXPECT["vds_both_observed"])
    a.check("VDS 양측 결측", int((vna & sna).sum()), EXPECT["vds_both_missing"])
    a.check("VDS 한쪽만 결측", int((vna ^ sna).sum()), EXPECT["vds_one_missing"])

    for (period, direction), want in EXPECT["vds_breakdown"].items():
        g = j[(j["period"] == period) & (j["direction"] == direction)]
        gv, gs = g["volume_veh"].isna(), g["speed_kmh"].isna()
        label = f"VDS {period} {direction}"
        a.check(f"{label} 행", len(g), EXPECT["vds_rows_per_period_dir"])
        a.check(f"{label} 양측관측", int((~gv & ~gs).sum()), want[0])
        a.check(f"{label} 양측결측", int((gv & gs).sum()), want[1])
        a.check(f"{label} 한쪽만", int((gv ^ gs).sum()), want[2])


def audit_conzone_lanes(a: Audit) -> None:
    """Conzone 과 차로수 — CTM 셀과 실측 격자의 공간 기준."""
    c = pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet")
    a.check("Conzone 행", len(c), EXPECT["conzone_rows"])
    for (period, direction), n in c[c["in_corridor"]].groupby(["period", "direction"]).size().items():
        a.check(f"Conzone 내부 {period} {direction}", int(n),
                EXPECT["conzone_internal_per_period_dir"])
    for (period, direction), n in c[~c["in_corridor"]].groupby(["period", "direction"]).size().items():
        a.check(f"Conzone 외부 {period} {direction}", int(n),
                EXPECT["conzone_external_per_period_dir"])

    lanes = pd.read_parquet(DATA_PROCESSED_DIR / "lanes_gyeongbu.parquet")
    counts = lanes["direction"].value_counts()
    a.check("차로수 행", len(lanes), EXPECT["lanes_rows"])
    a.check("차로수 UP", int(counts.get("UP", 0)), EXPECT["lanes_up"])
    a.check("차로수 DOWN", int(counts.get("DOWN", 0)), EXPECT["lanes_down"])
    a.check("차로수 끝 offset", float(lanes["offset_km_end"].max()),
            EXPECT["corridor_length_km"])


def audit_entry_exit(a: Audit) -> None:
    """§3.2 — 대표일 진입·진출 프로파일.

    진입/진출 합은 **대표일 하루의 중간 경계 추정치**이며 10일 합계가 아니다.
    두 값이 서로 다른 것은 보존 오류가 아니다 (§3.2 주의).
    """
    for direction, want in EXPECT["entry_exit"].items():
        rows, hours, bounds, entry, exit_, gaps, maxgap = want
        e = pd.read_csv(
            DATA_PROCESSED_DIR / f"entry_exit_gyeongbu_{direction}_seollal.csv"
        )
        tag = f"진입진출 {direction.upper()}"
        a.check(f"{tag} 행", len(e), rows)
        a.check(f"{tag} 시간", int(e["hour"].nunique()), hours)
        a.check(f"{tag} 고유경계", int(e["offset_km"].nunique()), bounds)
        a.check(f"{tag} 진입 합", int(e["entry_veh"].sum()), entry)
        a.check(f"{tag} 진출 합", int(e["exit_veh"].sum()), exit_)
        a.check(f"{tag} 음수", int((e[["entry_veh", "exit_veh"]] < 0).sum().sum()), 0)
        wide = e[e["gap_km"] > 1.0]
        a.check(f"{tag} gap>1 경계", int(wide["offset_km"].nunique()), gaps)
        a.check(f"{tag} 최대 gap", float(e["gap_km"].max()), maxgap)


def audit_db(a: Audit) -> None:
    """기본 DB 현황.

    `cell` 은 **792** 다. 보고서 §4·§5 에 남은 `cell=0` 은 셀 적재 **이전**
    시점의 기록이다 (§4 머리말).
    """
    path = default_db_path()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        def n(table: str) -> int:
            return int(con.execute(f"select count(*) from {table}").fetchone()[0])

        a.check("DB corridor 행", n("corridor"), EXPECT["corridor_rows"])
        for corridor_id, length in con.execute("select corridor_id, length_km from corridor"):
            a.check(f"DB 길이 {corridor_id}", float(length), EXPECT["corridor_length_km"])

        a.check("DB station 행", n("station"), EXPECT["station_rows"])
        a.check("DB charger 그룹 행", n("charger"), EXPECT["charger_group_rows"])
        a.check("DB vehicle_class 행", n("vehicle_class"), EXPECT["vehicle_class_rows"])
        a.check("DB charge_curve 행", n("charge_curve"), EXPECT["charge_curve_rows"])
        a.check("DB temp_efficiency 행", n("temp_efficiency"),
                EXPECT["temp_efficiency_rows"])
        a.check("DB cell 행", n("cell"), EXPECT["cell_rows"])

        for corridor_id, cnt in con.execute(
            "select corridor_id, count(*) from cell group by corridor_id"
        ):
            a.check(f"DB cell {corridor_id}", int(cnt),
                    EXPECT["cell_rows_per_direction"])

        # 충전기는 그룹 행이고 대수는 n_units 의 합이다 — 103 과 282 를 혼동하지 않는다
        inactive = int(con.execute(
            "select count(*) from charger where is_active != 1"
        ).fetchone()[0])
        a.check("DB charger 비활성", inactive, 0)
        a.check("DB charger n_units 합", int(con.execute(
            "select sum(n_units) from charger where is_active = 1"
        ).fetchone()[0]), EXPECT["charger_n_units"])

        by_dir = dict(con.execute(
            "select s.corridor_id, sum(c.n_units) from charger c "
            "join station s on c.station_id = s.station_id "
            "where c.is_active = 1 group by s.corridor_id"
        ))
        a.check("DB n_units DOWN", int(by_dir.get("gyeongbu_down", 0)),
                EXPECT["charger_n_units_down"])
        a.check("DB n_units UP", int(by_dir.get("gyeongbu_up", 0)),
                EXPECT["charger_n_units_up"])
    finally:
        con.close()


def audit_route(a: Audit) -> None:
    """노선 메타.

    `n_rest_areas` 37 은 **노선 메타의 휴게소 수**이고 DB `station` 35 는
    **충전 대상 휴게소 수**다. 같은 모집단이 아니다 (인벤토리 §4).
    """
    route = json.loads(
        (DATA_PROCESSED_DIR / "gyeongbu_route.json").read_text(encoding="utf-8")
    )
    meta = route.get("meta", {})
    a.check("노선 IC 수", int(meta["n_interchanges"]), EXPECT["n_interchanges"])
    a.check("노선 휴게소 수", int(meta["n_rest_areas"]), EXPECT["n_rest_areas"])
    a.check("노선 총연장", float(route["length_km"]), EXPECT["corridor_length_km"])

    # 수집본 이름은 머신마다 다를 수 있다 — 값을 고정하지 않고 보여만 준다
    print(f"  (참고) 노선 raw_dir = {meta.get('raw_dir')!r}")


def audit_config(a: Audit) -> None:
    """§8.2·§8.3 — 설정과 온도 보간."""
    for direction in ("down", "up"):
        cfg = yaml.safe_load(
            (CONFIG_DIR / f"scenario_seollal_{direction}.yaml").read_text(encoding="utf-8")
        )
        for dotted, want in EXPECT["scenario"].items():
            section, field = dotted.split(".")
            a.check(f"설정 {direction} {dotted}", cfg[section].get(field), want)
        a.check(f"설정 {direction} demand.layers", cfg["demand"].get("layers"), [])

    classes, curves = read_vehicle_classes()
    a.check("차종 수", len(classes), EXPECT["vehicle_class_rows"])
    a.check("충전곡선 구간", len(curves), EXPECT["charge_curve_rows"])
    a.check("차종 share 합", round(sum(c["share"] for c in classes), 9), 1.0)

    rows = read_temp_efficiency()
    a.check("온도 지점", len(rows), EXPECT["temp_efficiency_rows"])
    rf, cf = temp_factors(EXPECT["temp_c"], temp_table(rows))
    a.check("온도 range_factor(-5°C)", round(rf, 6), EXPECT["range_factor"])
    a.check("온도 charge_power(-5°C)", round(cf, 6), EXPECT["charge_power_factor"])


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--verbose", action="store_true",
                    help="일치한 검사까지 모두 출력한다")
    args = ap.parse_args()

    print("#134 LOCAL_NEW 재계산 — 읽기 전용")
    print(f"  DB: {default_db_path()}")

    a = Audit()
    audit_vds(a)
    audit_conzone_lanes(a)
    audit_entry_exit(a)
    audit_db(a)
    audit_route(a)
    audit_config(a)
    return a.report(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
