#!/usr/bin/env python3
"""SP3 #64: BASE215 peak-day UE replay on other Feb 2026 dates.

Uses original, already-verified, one-day BASE215 operator labs as immutable references.
Does not alter public DB, original lab DB, original configs, or src/evdt.
Run from the project root. Start with --check; then --direction DOWN --date 2026-02-14.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date
from pathlib import Path

SOURCE_LABS = {
    "DOWN": "data/processed/sp3_64_operator_lab_down_s0007",
    "UP": "data/processed/sp3_64_operator_lab_up_s0007_retry1",
}
PEAK_DATES = {"DOWN": "2026-02-13", "UP": "2026-02-17"}
ROOT_OUTPUT = Path("data/processed/sp3_64_validation/ue_daily_base215")
INPUT_DIR = Path("data/processed/sp3_64_validation/daily_inputs")
EXPECTED = {"ME": 95, "NONME": 120}
DIRECTION_CAPACITY = {"DOWN": 112, "UP": 103}
EVENT_FIELDS = [
    "station_id",
    "charger_id",
    "operator",
    "operator_bucket",
    "t_start_min",
    "t_end_min",
    "wait_min",
    "energy_kwh",
]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1 << 20), b""):
            h.update(part)
    return h.hexdigest()


def read_rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, rows: list[dict], fields: list[str]) -> None:
    if path.exists():
        raise FileExistsError(f"기존 출력 덮어쓰기 거부: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def validate_date(direction: str, when: str) -> None:
    d = date.fromisoformat(when)
    if not date(2026, 2, 13) <= d <= date(2026, 2, 22):
        raise ValueError(f"설 검증 범위 밖 날짜: {when}")
    if when == PEAK_DATES[direction]:
        raise ValueError(f"이미 실험한 피크일 재실행 금지: {direction} {when}")


def all_targets() -> list[tuple[str, str]]:
    return [
        (direction, f"2026-02-{day:02d}")
        for direction in ("DOWN", "UP")
        for day in range(13, 23)
        if f"2026-02-{day:02d}" != PEAK_DATES[direction]
    ]


def source_config(lab: Path, direction: str) -> Path:
    run_dir = lab / "runs"
    paths = list(run_dir.glob("*sp364-op-base215*__UE__p100__s0007/config.yaml"))
    if len(paths) != 1:
        raise ValueError(f"원본 피크 UE config가 정확히 하나가 아님: {run_dir}: {paths}")
    return paths[0]


def get_units(root: Path) -> tuple[list[dict], list[dict], dict]:
    """Read original simulated per-operator slot IDs. Recreate same runtime groups."""
    down = root / SOURCE_LABS["DOWN"] / "model_unit_to_operator.csv"
    up = root / SOURCE_LABS["UP"] / "model_unit_to_operator.csv"
    a, b = read_rows(down), read_rows(up)
    # Comparing rows is stronger than comparing reported 215 only.
    if sorted(a, key=lambda r: r["charger_id"]) != sorted(b, key=lambda r: r["charger_id"]):
        raise ValueError("DOWN/UP lab 운영사-충전기 단위 매핑이 서로 다름")
    units = {r["charger_id"]: r for r in a}
    if len(units) != len(a) or len(units) != 215:
        raise ValueError(f"가상 충전기 ID 누락/중복 또는 BASE215 불일치: {len(a)} / {len(units)}")
    counts = Counter(r["operator_bucket"] for r in a)
    if counts != Counter(EXPECTED):
        raise ValueError(f"ME95+NONME120 불일치: {counts}")
    per_direction = Counter()
    for r in a:
        if r["mapping_status"] != "SIMULATED_SLOT_NOT_FEB_PHYSICAL_ID":
            raise ValueError("신원이 확인된 물리 충전기라고 잘못 표시된 행")
        if r["operator_bucket"] != ("ME" if r["operator"] == "ME" else "NONME"):
            raise ValueError("운영사 분류 불일치")
        # All project station IDs encode corridor direction.
        if r["station_id"].startswith("gyeongbu_down_"):
            per_direction["DOWN"] += 1
        elif r["station_id"].startswith("gyeongbu_up_"):
            per_direction["UP"] += 1
        else:
            raise ValueError(f"알 수 없는 station_id: {r['station_id']}")
    if dict(per_direction) != DIRECTION_CAPACITY:
        raise ValueError(f"방향별 공급 112/103 불일치: {per_direction}")
    groups = defaultdict(list)
    for r in a:
        if "#" not in r["charger_id"]:
            raise ValueError("가상 단위 슬롯 ID #suffix 없음")
        group_id, unit_index = r["charger_id"].rsplit("#", 1)
        if not unit_index.isdigit():
            raise ValueError("단위 충전기 인덱스 오류")
        groups[group_id].append(r)
    overlay = []
    for group_id, members in sorted(groups.items()):
        first = members[0]
        if any(
            (r["station_id"], r["power_kw"], r["connector_type"], r["operator"])
            != (first["station_id"], first["power_kw"], first["connector_type"], first["operator"])
            for r in members
        ):
            raise ValueError("동일 그룹의 실제 가상 충전기 사양이 서로 다름")
        expected_ids = {f"{group_id}#{i}" for i in range(1, len(members) + 1)}
        if {r["charger_id"] for r in members} != expected_ids:
            raise ValueError(f"그룹 단위 슬롯 ID 연속성 불일치: {group_id}")
        overlay.append(
            {
                "charger_id": group_id,
                "station_id": first["station_id"],
                "power_kw": float(first["power_kw"]),
                "connector_type": first["connector_type"],
                "n_units": len(members),
                "is_active": 1,
            }
        )
    return a, overlay, units


def load_cfg(root: Path, direction: str, when: str, runner):
    lab = root / SOURCE_LABS[direction]
    cfg_path = source_config(lab, direction)
    base = runner.ScenarioConfig.from_yaml(cfg_path) if hasattr(runner, "ScenarioConfig") else None
    if base is None:
        from evdt.config import ScenarioConfig

        base = ScenarioConfig.from_yaml(cfg_path)
    if base.direction != direction or base.policy.stage != "UE":
        raise ValueError(f"원본 피크 설정 방향/UE 아님: {cfg_path}")
    if base.vehicles.seed != 7 or base.time.start_min != 0 or base.time.end_min != 1440:
        raise ValueError("기존 seed7/1일 설정과 불일치")
    if "sp364-op-base215" not in base.scenario_id:
        raise ValueError("원본이 BASE215가 아님")
    target_dir = INPUT_DIR / direction.lower() / when
    paths = {
        "demand.volume_profile": target_dir / "volume.csv",
        "demand.through_profile": target_dir / "through.csv",
        "demand.entry_exit_profile": target_dir / "entry_exit.csv",
    }
    for p in paths.values():
        if not (root / p).is_file():
            raise FileNotFoundError(f"해당 날짜 수요 입력 없음: {p}")
    cfg = base.variant(f"day-{when}", {k: v.as_posix() for k, v in paths.items()})
    return cfg, paths, cfg_path


@contextmanager
def adapter(runner, overlay: list[dict]):
    original = runner.read_station_chargers
    allowed = {x["station_id"] for x in overlay}

    def mapped(conn, *, corridor_id=None, station_ids=None):
        stations, _ = original(conn, corridor_id=corridor_id, station_ids=station_ids)
        active = {s["station_id"] for s in stations}
        if not active <= allowed:
            raise ValueError("UE 시나리오가 BASE215 후보가 아닌 휴게소를 참조")
        result = [dict(r) for r in overlay if r["station_id"] in active]
        if (
            sum(int(c["n_units"]) for c in result)
            != DIRECTION_CAPACITY["DOWN" if corridor_id == "gyeongbu_down" else "UP"]
        ):
            raise ValueError("활성 코리도 공급 슬롯 수가 BASE215와 다름")
        return stations, result

    runner.read_station_chargers = mapped
    try:
        yield
    finally:
        runner.read_station_chargers = original


def setup_workspace(
    root: Path, direction: str, unit_csv: Path, source_cfg: Path
) -> tuple[Path, Path]:
    """Clone once *per direction*, never alter the source lab or public DB."""
    work = root / ROOT_OUTPUT / direction.lower()
    db = work / "isolated.db"
    marker = work / "setup.json"
    source_db = root / SOURCE_LABS[direction] / "isolated.db"
    fingerprint = {
        "base215_source_db_sha256": digest(source_db),
        "base215_unitmap_sha256": digest(unit_csv),
        "base215_config_sha256": digest(source_cfg),
        "reference_lab": SOURCE_LABS[direction],
    }
    if work.exists():
        if not db.is_file() or not marker.is_file():
            raise ValueError(f"작업 디렉터리가 이미 있지만 완성된 DB/메타가 없음: {work}")
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing != fingerprint:
            raise ValueError("기존 워크스페이스 원본·설정 해시 불일치; 자동 재사용 금지")
        return work, db
    work.mkdir(parents=True, exist_ok=False)
    try:
        with sqlite3.connect(f"{source_db.resolve().as_uri()}?mode=ro", uri=True) as con:
            with sqlite3.connect(db) as dest:
                con.backup(dest)
        marker.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Do not delete a partly-created worktree automatically; preserve evidence.
        raise
    return work, db


def run_day(
    root: Path, direction: str, when: str, runner, overlay: list[dict], unitlookup: dict
) -> None:
    validate_date(direction, when)
    cfg, inputs, src_cfg = load_cfg(root, direction, when, runner)
    source_lab = root / SOURCE_LABS[direction]
    work, db = setup_workspace(root, direction, source_lab / "model_unit_to_operator.csv", src_cfg)
    runs = work / "runs"
    run_id = runner.make_run_id(
        cfg.scenario_id, cfg.policy.stage, cfg.vehicles.seed, cfg.policy.participation
    )
    day_out = work / "days" / when
    summary_path = day_out / "run_summary.json"
    events_csv = day_out / "ue_charge_events_by_operator.csv"
    if summary_path.is_file() and events_csv.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("run_id") != run_id:
            raise ValueError("이미 있는 해당 날짜 결과의 run_id가 다름")
        print(f"[재사용] {direction} {when}: {run_id}", flush=True)
        return
    if day_out.exists():
        raise FileExistsError(f"부분 결과가 이미 있으니 수동 점검: {day_out}")
    with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True) as con:
        row = con.execute("SELECT status FROM run WHERE run_id = ?", (run_id,)).fetchone()
    if row is not None:
        raise RuntimeError(
            f"해당 날짜 run_id는 DB에 이미 존재함. 자동 덮어쓰기 금지: {run_id} ({row[0]})"
        )
    print(f"\n[UE 시작] {direction} {when} | 공급: BASE215 | seed7 | run_id={run_id}", flush=True)
    with adapter(runner, overlay):
        result_id = runner.run_once(
            cfg, seed=7, overwrite=False, db_path=db, runs_dir=runs, root=root, gap_plot=False
        )
    if result_id != run_id:
        raise ValueError(f"실행된 run_id 불일치: {result_id}")
    import pandas as pd

    event_parquet = runs / run_id / "charge_event.parquet"
    if not event_parquet.is_file():
        raise FileNotFoundError(f"충전 이벤트 파일 없음: {event_parquet}")
    raw = pd.read_parquet(event_parquet)
    items = []
    for row in raw.to_dict(orient="records"):
        cid = str(row["charger_id"])
        model = unitlookup.get(cid)
        if model is None or model["station_id"] != str(row["station_id"]):
            raise ValueError(f"충전기 식별자 운영사 매핑 실패: {cid}")
        items.append(
            {
                k: row[k]
                for k in (
                    "station_id",
                    "charger_id",
                    "t_start_min",
                    "t_end_min",
                    "wait_min",
                    "energy_kwh",
                )
            }
            | {"operator": model["operator"], "operator_bucket": model["operator_bucket"]}
        )
    day_out.mkdir(parents=True, exist_ok=False)
    write_rows(events_csv, items, EVENT_FIELDS)
    counts = Counter(x["operator_bucket"] for x in items)
    summary = {
        "run_id": run_id,
        "direction": direction,
        "input_date": when,
        "model": "BASE215, synthetic operator-group assignment",
        "source_reference_lab": SOURCE_LABS[direction],
        "charge_events_total": len(items),
        "charge_events_by_operator_group": dict(counts),
        "input_profiles_sha256": {k: digest(root / path) for k, path in inputs.items()},
        "observation_operator_scope_verified": False,
        "limitations": "ME allocation is synthetic, not confirmed Feb physical charger identity",
    }
    if summary_path.exists():
        raise FileExistsError(summary_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[UE 완료] {direction} {when}: 전체 {len(items)}, ME {counts['ME']}, NONME {counts['NONME']}",
        flush=True,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project-root", type=Path, default=Path.cwd())
    p.add_argument(
        "--check", action="store_true", help="18개 입력/기존 lab 설정 검증만 하고 종료 (쓰기 없음)"
    )
    p.add_argument("--run", action="store_true", help="UE 실행; 지정 없으면 --check와 동일")
    p.add_argument("--all", action="store_true", help="기존 피크일 제외 18개 날짜를 순차 실행")
    p.add_argument("--direction", choices=["DOWN", "UP"])
    p.add_argument("--date", help="YYYY-MM-DD")
    args = p.parse_args()
    root = args.project_root.resolve()
    if not (root / "src/evdt/runner.py").is_file():
        p.error(f"프로젝트 루트에서 실행 필요: {root}")
    sys.path.insert(0, str(root / "src"))
    from evdt import runner

    targets = all_targets() if args.all or args.check else [(args.direction, args.date)]
    if any(direction is None or when is None for direction, when in targets):
        p.error("--direction DOWN --date YYYY-MM-DD 또는 --all 또는 --check를 지정하세요")
    units, overlay, lookup = get_units(root)
    for direction, when in targets:
        validate_date(direction, when)
        cfg, paths, cfgfile = load_cfg(root, direction, when, runner)
        assert cfg.scenario_id.endswith("__day-" + when)
        if args.check or not args.run:
            print(f"[OK] {direction} {when} | {cfg.scenario_id}")
    if args.check or not args.run:
        print(
            f"[사전검사 통과] 입력 {len(targets)}조합 | ME95+NONME120=215 | DOWN112/UP103 | 기존 실행 2건 보존"
        )
        return 0
    for i, (direction, when) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {direction} {when}", flush=True)
        run_day(root, direction, when, runner, overlay, lookup)
    print("[완료] 모든 요청 날짜 UE 및 운영사 이벤트 출력 저장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
