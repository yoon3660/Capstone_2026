"""MNL 세계의 천장값 — 고정 참여율 p 에서 Oracle 이 어디까지 내려가나 (#168).

    python scripts/run_mnl_world.py --config config/scenario_chuseok_down_adoption.yaml \\
        --seeds 1-5 --p 0.6 --theta 0.1 --stages S1,S2 --rounds 3

## 무엇을 재는가

    바닥   J_MNL       전원이 MNL 로 스스로 고른다 (참여율 0)
    엔진   J_Sk(p)     참여자는 S1~S4, 비참여자는 MNL
    천장값 J*(p)       참여자는 완전정보 Oracle, 비참여자는 MNL

**p 는 값 하나로 고정한다.** 참여율 스윕(30/60/90%)과 동적 참여율은 다른 담당 티켓의
범위다. 이 스크립트와 `engine/participation.py` 는 그 실험이 **p 만 바꿔 넣으면 되도록**
연결부를 제공한다.

Oracle 은 기존 `PERFECT_ROLLING` 그대로다 — S2 정책에 **실현된 미래 도착**을 넣고,
직전 회차의 도착을 다음 회차의 미래로 넣어 고정점까지 반복한다. 1회차의 미래는
**MNL 바닥 런**이다. 비참여자는 화면 대기에 반응하므로 배정표를 고정해 비용만 다시 잴
수 없다 — 회차마다 세계 전체를 다시 굴린다.

⚠ **천장값은 완전정보 벤치마크다. 상한이 아니다** (설계문서 §1.2 · §8.2). 최적성을
보장하지 않는다. 진짜 사후 최적해(§8.3 MILP)는 이 스크립트의 범위가 아니다.

## 기존 경로를 건드리지 않는다

- 정책·예측을 만드는 코드는 `runner._run_policy_loop` 를 **그대로** 부른다. 다른 것은
  시뮬레이터에 넘어가기 직전에 정책을 **감싸는 것** 하나뿐이다 (`_inject`)
- **DB 에 쓰지 않는다.** `run.stage` 에 CHECK 제약이 있고, 같은 run_id 로 기존 런을
  덮어쓸 위험이 있다. 결과는 `--out` 폴더에 manifest 와 함께 남긴다
- 총비용 정의는 기존과 같다: **총 체류 + 이탈 대수 × escape_cost_min**
  (`run_perfect_rolling.social_cost`)

## 출력 (`--out`, 기본 runs/mnl_world/<scenario_id>/)

    manifest.json        설정·시드·p·θ·git SHA·데이터 루트
    cells.csv            시드 × 칸 한 줄 — 비용·이탈·대기·참여자 수·회차·배정변화
    summary.csv          칸별 평균·95% CI, 천장 절감률, 엔진 격차 축소율 (시드 대응)
    seed<s>/<cell>.parquet   충전 이벤트 (다음 회차 오라클 · 보정 재료)
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import math
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

import evdt.runner as R  # noqa: E402
from evdt.engine.forecast import (  # noqa: E402
    ArrivalSpec,
    ObservedSpec,
    OracleForecast,
    OracleSource,
    describe_shift,
)
from evdt.engine.mnl import ESCAPE_MODES, MnlPolicy, MnlSettings, background_settings  # noqa: E402
from evdt.engine.participation import (  # noqa: E402
    NONPART_INFO,
    NonParticipantAwareForecast,
    ParticipationPolicy,
    participants,
)
from evdt.engine.ue import UESettings  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path  # noqa: E402

#: 오라클 행에 들어가는 열 (run_perfect_rolling.ARRIVAL_COLS 와 같다)
ARRIVAL_COLS = ["ev_id", "station_id", "t_arrive_min", "soc_in", "soc_out", "cold_factor"]

#: moved_share 가 이 밑이면 고정점 (run_perfect_rolling 과 같다)
FIXED_POINT_SHARE = 0.01

#: 관측 예측의 강도 — 이탈을 맞춘 값 (docs/compare_rule.md)
DEFAULT_STRENGTH = 0.30

ORACLE = "ORACLE"
BASE = "MNL"

#: 추석 보정에서 채택한 값 (2026-11-12, docs/data_validation/mnl_calibration_168.md).
#: TVD 최적은 격자 끝 θ=0.01 이지만 이탈이 615대로 비현실적이라, TVD 차이 0.008 안에서
#: 이탈이 가장 적은 0.05 를 쓴다. w 는 결과에 영향이 없어(차이 ≤ 0.0003) 1.0 으로 둔다
ADOPTED_THETA = 0.05


# ---------------------------------------------------------------------------
# 세계 — run_once 의 준비 부분과 같은 계산. DB 는 읽기만 한다
# ---------------------------------------------------------------------------


@dataclass
class World:
    cfg: Any
    built: Any
    specs: Any
    offsets: dict[str, float]
    settings: UESettings
    range_factor: float
    cpf: float
    escape_cost_min: float


def prepare(cfg, seed: int, *, root: Path, log: Callable[[str], None]) -> World:
    """`runner.run_once` 의 준비 부분을 **같은 함수들로** 다시 부른다 (DB 는 readonly)."""

    cfg = R.with_seed(cfg, seed)
    R.check_queue_config(cfg.queue.discipline, cfg.queue.charger_select)

    with R.get_conn(default_db_path(), readonly=True) as conn:
        station_rows, charger_rows = R.read_station_chargers(conn, corridor_id=cfg.corridor_id)
        vclasses, curves, temps = R.load_from_db(conn)
        corridor_end_km = float(conn.execute(
            "SELECT length_km FROM corridor WHERE corridor_id = ?", (cfg.corridor_id,)
        ).fetchone()[0])

    specs = R.station_specs(station_rows, charger_rows)
    chargers = {s.station_id: s.chargers for s in specs}
    stations = [r for r in station_rows if r["station_id"] in chargers]
    offsets = {r["station_id"]: float(r["offset_km"]) for r in stations}

    built, range_factor, cpf = R.build_demand(cfg, stations, vclasses, curves, temps,
                                              corridor_end_km, root=root, log=log)
    ue = cfg.policy.ue
    ctm = (R.build_travel_field(cfg, corridor_end_km, log=log)
           if cfg.demand.travel_time == "ctm" else None)
    settings = UESettings(
        max_iter=ue.max_iter, gap_tol=ue.gap_tol, min_gain_min=ue.min_gain_min,
        speed_kmh=cfg.demand.cruise_speed_kmh,
        travel=None if ctm is None else ctm.speed_field,
        escape_cost_min=cfg.demand.escape_cost_min,
    )
    log(f"  시드 {seed}: 진입 EV {built.n_ev:,} → 충전 필요 {len(built.trips):,}대")
    return World(cfg, built, specs, offsets, settings, range_factor, cpf,
                 float(cfg.demand.escape_cost_min))


# ---------------------------------------------------------------------------
# 정책 바꿔 끼우기 — runner 는 고치지 않는다
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _inject(transform: Callable[[Any, Any], tuple[Any, Any]]) -> Iterator[dict]:
    """`runner` 가 부르는 `run_corridor` 를 잠깐 감싼다.

    정책·예측 생성은 runner 가 하던 그대로고, 시뮬레이터에 넘어가기 직전에
    `transform(policy, forecast)` 로 바꿔 넣는다. 끝나면 반드시 원래대로 돌린다.
    """

    original = R.run_corridor
    seen: dict = {}

    def patched(stations, offsets, evs, policy, travel, amount, **kw):
        new_policy, new_forecast = transform(policy, kw.get("forecast"))
        kw["forecast"] = new_forecast
        seen["engine"], seen["policy"] = policy, new_policy
        return original(stations, offsets, evs, new_policy, travel, amount, **kw)

    R.run_corridor = patched
    try:
        yield seen
    finally:
        R.run_corridor = original


def run_cell(world: World, stage: str, *, p: float, mnl: MnlSettings,
             strength: float, oracle: OracleSource | None, s3_window: int | None,
             log: Callable[[str], None], classic: bool = False,
             nonpart_info: str = "none") -> tuple[dict, list[dict]]:
    """칸 하나. stage 는 "MNL"(바닥) · 엔진 스테이지 · "ORACLE".

    `classic=True` 면 **기존 사다리 세계**다 — MNL 도 참여율 래퍼도 없이 runner 가 만든
    정책을 그대로 쓴다. stage 는 "S0"(바닥) · 엔진 · "ORACLE"(= 기존 PERFECT_ROLLING).
    두 천장을 같은 시드·같은 지표로 나란히 놓기 위해서다.
    """

    seed = int(world.cfg.vehicles.seed)
    trip_ids = [t.ev_id for t in world.built.trips]
    if classic:
        part = frozenset(trip_ids)
    else:
        part = participants(seed, trip_ids, p) if stage != BASE else frozenset()
    non_part = frozenset(trip_ids) - part

    runner_stage = {BASE: "S0", ORACLE: "PERFECT_ROLLING"}.get(stage, stage)
    cfg = R.with_stage(world.cfg, runner_stage)
    observed = ObservedSpec(strength=strength) if runner_stage in ("S2", "S3", "S4") else None

    def transform(policy, forecast):
        if classic:
            return policy, forecast
        if stage == BASE:
            return MnlPolicy(background_settings(policy.settings), mnl=mnl), forecast
        bg = MnlPolicy(background_settings(policy.settings), mnl=mnl)
        if stage == ORACLE and non_part:
            # Oracle 만 전부 안다 — 원장에 없는 비참여자의 지난 도착까지 본다
            forecast = NonParticipantAwareForecast(forecast, non_part)
        # Oracle 은 비참여자를 이미 예측으로 전부 본다(NonParticipantAwareForecast). 원장에도
        # 올리면 두 번 센다 — 그래서 천장 칸은 정보 방식과 무관하게 "none" 이다
        info = "none" if stage == ORACLE else nonpart_info
        return ParticipationPolicy(policy, bg, p=p, seed=seed, nonpart_info=info), forecast

    t0 = time.perf_counter()
    with _inject(transform) as seen:
        events, _snaps, escapes, kpis = R._run_policy_loop(
            world.built, world.specs, world.offsets, cfg, world.settings,
            world.range_factor, world.cpf, oracle, observed, log,
            s3_window=s3_window if runner_stage in ("S3", "S4") else None)
    secs = time.perf_counter() - t0

    wrapper = seen.get("policy")
    counters = getattr(wrapper, "counters", {}) if (stage != BASE and not classic) else {}
    waits = np.array([e["wait_min"] for e in events]) if events else np.zeros(1)
    dwell_h = sum(e["dwell_min"] for e in events) / 60.0
    n_esc = len(escapes)
    esc_h = world.escape_cost_min / 60.0
    # ★ 예외 규칙의 재료 — **정차 한도를 다 쓰고 "갈 곳 없음" 으로 나간 차** (#168 진단).
    #   모든 정책이 같이 쓰는 충전 판단 규칙의 결함이라 배정의 질과 무관하다
    #   (docs/meeting_memo_stranded_escape_20261112.md). 지우지 않고 **따로 센다**
    stops = collections.Counter(e["ev_id"] for e in events)
    max_stops = int(world.cfg.policy.ue.max_stops)
    n_exhausted = sum(1 for r in escapes
                      if r["reason"] == "no_plan" and stops[r["ev_id"]] >= max_stops)
    if classic:
        cell = f"classic:{stage}"
    else:
        cell = stage if stage == BASE else f"{stage}@p{p:g}"
    row = {
        "seed": seed, "family": "classic" if classic else "mnl", "cell": cell,
        "stage": stage, "p": 1.0 if classic else (0.0 if stage == BASE else p),
        "theta": mnl.theta, "wait_weight": mnl.wait_weight, "escape_mode": mnl.escape_mode,
        "n_ev_charging": len(trip_ids), "n_participants": len(part),
        "dwell_total_h": dwell_h, "n_escaped": n_esc,
        "n_escaped_balked": sum(1 for r in escapes if r["reason"] == "balked"),
        "n_exhausted": n_exhausted,
        "escape_h": n_esc * esc_h,
        "total_h": dwell_h + n_esc * esc_h,
        # 통제 열: 정차 한도 소진 이탈을 뺀 비용 (예외 규칙 R2)
        "total_ex_h": dwell_h + (n_esc - n_exhausted) * esc_h,
        "wait_mean_min": float(waits.mean()), "wait_p95_min": float(np.percentile(waits, 95)),
        "n_charge_visits": len(events),
        "policy_stranded": float(kpis.get("policy_stranded", (0, ""))[0]),
        "engine_asked": counters.get("engine_asked", 0),
        "background_asked": counters.get("background_asked", 0),
        "nonpart_info": nonpart_info if (stage != BASE and not classic) else "",
        "nonpart_booked": counters.get("nonpart_booked", 0),
        "seconds": secs,
    }
    return row, list(events)


class _NullWriter:
    """`runner._run_ue` 는 반복 이력을 writer 에 쓴다. 이 스크립트는 DB·parquet 에 쓰지 않는다."""

    def append_many(self, *_args: object, **_kw: object) -> None:
        return None

    def append(self, *_args: object, **_kw: object) -> None:
        return None


def run_ue_cell(world: World, log: Callable[[str], None]) -> tuple[dict, list[dict]]:
    """기존 사다리의 **참고 천장 UE** — 27.5% 의 분모 끝점이다 (gap_table: FLOOR S0 · CEIL UE).

    UE 는 같은 차량·같은 수요로 따로 푼 균형이라 바닥이 S0 든 MNL 이든 **같은 값**이다.
    그래서 두 세계의 격차 축소율을 같은 끝점으로 잴 수 있다. 정차 한도 소진 이탈은
    UE 에서 구조적으로 0 이다 (`n_escaped_stranded`, #59).
    """

    seed = int(world.cfg.vehicles.seed)
    cfg = R.with_stage(world.cfg, "UE")
    chargers = {s.station_id: s.chargers for s in world.specs}
    t0 = time.perf_counter()
    events, _snaps, escapes, kpis = R._run_ue(world.built, chargers, world.specs,
                                              world.settings, cfg, _NullWriter(), log)
    secs = time.perf_counter() - t0
    stops = collections.Counter(e["ev_id"] for e in events)
    max_stops = int(world.cfg.policy.ue.max_stops)
    n_exhausted = sum(1 for r in escapes
                      if r["reason"] == "no_plan" and stops[r["ev_id"]] >= max_stops)
    waits = np.array([e["wait_min"] for e in events]) if events else np.zeros(1)
    dwell_h = sum(e["dwell_min"] for e in events) / 60.0
    esc_h = world.escape_cost_min / 60.0
    n_esc = len(escapes)
    return {
        "seed": seed, "family": "classic", "cell": "classic:UE", "stage": "UE", "p": 1.0,
        "theta": float("nan"), "wait_weight": float("nan"),
        "n_ev_charging": len(world.built.trips), "n_participants": len(world.built.trips),
        "dwell_total_h": dwell_h, "n_escaped": n_esc,
        "n_escaped_balked": sum(1 for r in escapes if r["reason"] == "balked"),
        "n_exhausted": n_exhausted, "escape_h": n_esc * esc_h,
        "total_h": dwell_h + n_esc * esc_h,
        "total_ex_h": dwell_h + (n_esc - n_exhausted) * esc_h,
        "wait_mean_min": float(waits.mean()), "wait_p95_min": float(np.percentile(waits, 95)),
        "n_charge_visits": len(events), "policy_stranded": 0.0,
        "engine_asked": 0, "background_asked": 0, "seconds": secs,
        "ue_final_gap": float(kpis.get("ue_final_gap", (float("nan"), ""))[0]),
    }, list(events)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def _dummy_specs(rows: list[dict]) -> dict:
    """`describe_shift` 는 휴게소·시각만 본다 (run_perfect_rolling._specs 와 같다)."""

    d = ArrivalSpec(60.0, 100.0, ())
    return {str(r["ev_id"]): d for r in rows}


def _arrivals(events: list[dict]) -> list[dict]:
    return [{k: e[k] for k in ARRIVAL_COLS if k in e} for e in events]


def _save(events: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(events).to_parquet(path, index=False)


def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True,
                             capture_output=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True,
                               capture_output=True, check=True).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _ci(x: pd.Series) -> float:
    """95% 신뢰구간 반폭 (t 분포). 시드 1개면 nan."""

    from scipy import stats

    n = int(x.notna().sum())
    if n < 2:
        return float("nan")
    return float(stats.t.ppf(0.975, n - 1) * x.std(ddof=1) / math.sqrt(n))


#: 이탈 맞춤 허용폭 (docs/compare_rule.md · gap_table.ESCAPE_TOL 과 같다)
ESCAPE_TOL = 0.35

#: 비용 기준 두 가지 — 예외 규칙 R2 (결론은 둘이 같은 방향일 때만 쓴다)
BASES = (("total_h", ""), ("total_ex_h", "_ex"))


def summarize(cells: pd.DataFrame) -> pd.DataFrame:
    """세계(family)별 칸 평균·CI + 시드 대응 지표 — **두 비용 기준**으로.

        바닥    mnl: "MNL"            classic: "S0"
        천장    mnl: "ORACLE@p…"      classic: "ORACLE" (= 기존 PERFECT_ROLLING)

        천장 절감률   (바닥 − 칸) / 바닥
        격차 축소율   (바닥 − 칸) / (바닥 − 천장)     — 엔진 칸만
        이탈 맞춤     |이탈 − S1 이탈| / S1 이탈 ≤ 35%  — S2~·천장 칸 (compare_rule)
    """

    last = cells[cells["final"]]
    ue = last[last["stage"] == "UE"].set_index("seed")
    rows = []
    for family, fam in last.groupby("family", sort=False):
        floor_stage = BASE if family == "mnl" else "S0"
        floor = fam[fam["stage"] == floor_stage].set_index("seed")
        for cell, g in fam.groupby("cell", sort=False):
            g = g.set_index("seed")
            rec = {"family": family, "cell": cell, "stage": g["stage"].iloc[0],
                   "p": g["p"].iloc[0], "n_seeds": len(g)}
            # 천장·S1 은 **같은 참여율**의 칸이다 (p 가 다른 실행을 합쳐도 섞이지 않게)
            same_p = fam[fam["p"] == rec["p"]]
            ceil = same_p[same_p["stage"] == ORACLE].set_index("seed")
            s1 = same_p[same_p["stage"] == "S1"].set_index("seed")["n_escaped"]
            for m in ("total_h", "total_ex_h", "dwell_total_h", "n_escaped", "n_exhausted",
                      "n_escaped_balked", "wait_mean_min", "wait_p95_min"):
                rec[m] = g[m].mean()
                rec[f"{m}_ci95"] = _ci(g[m])
            for col, sfx in BASES:
                f = floor[col].reindex(g.index)
                drop = (f - g[col]) / f * 100.0
                rec[f"drop_vs_floor{sfx}_pct"] = drop.mean()
                rec[f"drop_vs_floor{sfx}_pct_ci95"] = _ci(drop)
                if rec["stage"] not in (floor_stage, ORACLE, "UE") and not ceil.empty:
                    c = ceil[col].reindex(g.index)
                    gap = (f - g[col]) / (f - c) * 100.0
                    rec[f"gap_closed{sfx}_pct"] = gap.mean()
                    rec[f"gap_closed{sfx}_pct_ci95"] = _ci(gap)
                # ★ UE 를 끝점으로 둔 격차 축소율 — **27.5% 와 같은 식** (gap_table)
                #   (바닥 − 이 칸) / (바닥 − UE). 두 세계가 같은 UE 를 공유하므로 나란히 놓인다
                if not ue.empty and rec["stage"] not in (floor_stage, "UE"):
                    u = ue[col].reindex(g.index)
                    to_ue = (f - g[col]) / (f - u) * 100.0
                    rec[f"closed_to_ue{sfx}_pct"] = to_ue.mean()
                    rec[f"closed_to_ue{sfx}_pct_ci95"] = _ci(to_ue)
            if not s1.empty and rec["stage"] in ("S2", "S3", "S4", ORACLE):
                ref = s1.reindex(g.index)
                # 둘 다 0 이면 맞춘 것이다. S1 이 0 인데 이 칸이 0 이 아니면 못 맞춘 것이다
                ok = [(n == 0 and r == 0) or (r > 0 and abs(n / r - 1.0) <= ESCAPE_TOL)
                      for n, r in zip(g["n_escaped"], ref, strict=True)]
                rec["escape_vs_s1"] = float(g["n_escaped"].sum() / max(ref.sum(), 1e-9))
                rec["escape_matched"] = bool(all(ok))
            if rec["stage"] == ORACLE:
                rec["oracle_rounds"] = g["round"].max()
                rec["moved_share_last"] = g["moved_share"].mean()
            rows.append(rec)
    return pd.DataFrame(rows)


SHOW = ["cell", "n_seeds", "total_h", "total_ex_h", "n_escaped", "n_exhausted",
        "drop_vs_floor_pct", "drop_vs_floor_ex_pct", "gap_closed_pct", "gap_closed_ex_pct",
        "closed_to_ue_pct", "closed_to_ue_ex_pct", "escape_matched", "moved_share_last"]


def merge(dirs: list[str], out: Path) -> int:
    """따로 돌린 실행(시드·칸이 나뉜)의 cells.csv 를 합쳐 요약을 다시 낸다.

    같은 (seed, cell, round) 가 두 번 나오면 **멈춘다** — 다른 설정으로 돈 칸을 조용히
    섞으면 #123 의 덮어쓰기와 같은 고장이 난다.
    """

    frames = [pd.read_csv(Path(d) / "cells.csv") for d in dirs]
    cells = pd.concat(frames, ignore_index=True)
    # 같은 칸이 **같은 값**으로 두 번 나오면(바닥 MNL 처럼 결정적인 칸을 두 실행이 각각
    # 돌린 경우) 하나만 남긴다. 값이 다르면 설정이 다른 것이라 멈춘다
    key = ["seed", "cell", "round"]
    cells = cells.drop_duplicates(key + ["total_h", "n_escaped"])
    dup = cells.duplicated(key, keep=False)
    if dup.any():
        raise SystemExit("[중단] 같은 (seed, cell, round) 가 여러 실행에 있다:\n"
                         + cells.loc[dup, ["seed", "cell", "round"]].to_string(index=False))
    cells["final"] = cells["final"].astype(bool)
    bad = {s: g["n_ev_charging"].unique().tolist()
           for s, g in cells.groupby("seed") if g["n_ev_charging"].nunique() > 1}
    if bad:
        raise SystemExit(f"[중단] 같은 시드인데 칸마다 충전 필요 EV 가 다르다: {bad}")
    out.mkdir(parents=True, exist_ok=True)
    cells.to_csv(out / "cells_merged.csv", index=False, encoding="utf-8-sig")
    summary = summarize(cells)
    summary.to_csv(out / "summary_merged.csv", index=False, encoding="utf-8-sig")
    print(summary.reindex(columns=[c for c in SHOW if c in summary]).to_string(
        index=False, float_format=lambda v: f"{v:,.2f}"))
    (out / "merge_manifest.json").write_text(json.dumps(
        {"sources": [str(Path(d).resolve()) for d in dirs], "git": _git_sha(),
         "merged": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\n출력: {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="시나리오 config (--merge 가 아니면 필수)")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--p", type=float, default=None,
                    help="참여율 (값 하나로 고정). 참여율 스윕·동적 참여율은 이 티켓의 범위가 "
                         "아니다 — 담당 티켓에서 이 스크립트에 값을 바꿔 넣는다")
    ap.add_argument("--stages", default="",
                    help="엔진 칸 (예: S1,S2,S3,S4). 비우면 바닥과 천장만")
    ap.add_argument("--theta", type=float, default=ADOPTED_THETA,
                    help="MNL 척도 (1/분). 기본은 추석 보정 채택값 0.05 "
                         "(docs/data_validation/mnl_calibration_168.md). inf 면 S0 와 같다")
    ap.add_argument("--wait-weight", type=float, default=1.0)
    ap.add_argument("--escape-mode", choices=ESCAPE_MODES, default="s0",
                    help="이탈 판정: s0 = 잡음 없이 S0 규칙 (기본) · noisy = 이탈에도 잡음 (#168 첫 실행 방식, 민감도)")
    ap.add_argument("--nonpart-info", choices=NONPART_INFO, default="none",
                    help="엔진이 비참여자를 얼마나 아나: none(안 보임, 기존) · present(도착하면 보임) · "
                         "plans(고르면 보임). 결정 문서 조건 ②")
    ap.add_argument("--skip-ceiling", action="store_true",
                    help="MNL 세계의 천장을 생략한다 (엔진 칸만). 천장은 정보 방식과 무관해서 한 번만 재면 된다")
    ap.add_argument("--floor-only", action="store_true",
                    help="MNL 세계는 바닥만 잰다 (MNL 엔진·천장 생략). p=1 이면 둘 다 기존 세계와 같은 계산이다")
    ap.add_argument("--rounds", type=int, default=3, help="Oracle 고정점 최대 회차")
    ap.add_argument("--strength", type=float, default=DEFAULT_STRENGTH,
                    help="S2~S4 관측 예측 강도 (이탈 맞춤 기본 0.30)")
    ap.add_argument("--s3-window", type=int, default=None)
    ap.add_argument("--data-root", default=str(PROJECT_ROOT),
                    help="수요 프로파일(data/processed) 을 찾을 루트")
    ap.add_argument("--classic", action="store_true",
                    help="기존 사다리 세계(바닥 S0 · 천장 PERFECT_ROLLING)도 같은 시드로 돌려 "
                         "두 천장을 나란히 놓는다")
    ap.add_argument("--ue", action="store_true",
                    help="기존 사다리의 참고 천장 UE 도 같은 시드로 푼다 (27.5%% 와 같은 식의 끝점)")
    ap.add_argument("--merge", nargs="+", default=None,
                    help="실행하지 않고, 여러 출력 폴더의 cells.csv 를 합쳐 요약만 다시 낸다")
    ap.add_argument("--out", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.merge:
        return merge(args.merge, Path(args.out) if args.out else Path(args.merge[0]).parent)

    if args.config is None or args.p is None:
        raise SystemExit("[중단] --config 와 --p 가 필요하다 (--merge 만 예외)")
    cfg0 = R.load_config(args.config)
    seeds = R.parse_seeds(args.seeds)
    if not 0.0 < args.p <= 1.0:
        raise SystemExit(f"[중단] 참여율은 0 초과 1 이하여야 한다: {args.p} "
                         "(0 은 바닥 칸이 이미 잰다)")
    ps = [args.p]
    if args.p == 1.0:
        print("  ⚠ p=1 이면 비참여자가 없다 — MNL 은 바닥(분모)과 Oracle 1회차 미래에만 "
              "들어가고 천장값 자체에는 거의 영향이 없다")
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    bad = [s for s in stages if s not in ("S1", "S2", "S3", "S4", "MEC")]
    if bad:
        raise SystemExit(f"[중단] 엔진 칸이 아니다: {bad} (S1·S2·S3·S4·MEC)")

    out = Path(args.out) if args.out else RUNS_DIR / "mnl_world" / cfg0.scenario_id
    out.mkdir(parents=True, exist_ok=True)
    root = Path(args.data_root)
    quiet = print if args.verbose else (lambda *_: None)

    manifest = {
        "issue": 168, "script": "scripts/run_mnl_world.py", "config": str(args.config),
        "scenario_id": cfg0.scenario_id, "demand_label": cfg0.demand_label,
        "seeds": seeds, "p": ps, "stages": stages, "theta": args.theta,
        "wait_weight": args.wait_weight, "escape_mode": args.escape_mode,
        "oracle_rounds_max": args.rounds, "floor_only": args.floor_only,
        "nonpart_info": args.nonpart_info,
        "observed_strength": args.strength, "s3_window": args.s3_window,
        "escape_cost_min": cfg0.demand.escape_cost_min, "data_root": str(root),
        "db_path": str(default_db_path()), "git": _git_sha(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "천장값 = 완전정보 벤치마크(PERFECT_ROLLING 고정점). 상한 아님 (DESIGN §1.2)",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    print(f"\n{cfg0.scenario_id} · 시드 {seeds} · p {ps} · θ {args.theta:g} · "
          f"엔진 {stages or '-'} · Oracle 회차 ≤ {args.rounds}")
    print(f"수요: {cfg0.demand_label}\n출력: {out}\n")

    mnl_kw = {"theta": args.theta, "wait_weight": args.wait_weight,
              "escape_mode": args.escape_mode}
    rows: list[dict] = []

    def keep(row: dict, events: list[dict], name: str) -> None:
        _save(events, out / f"seed{row['seed']}" / f"{name}.parquet")
        rows.append(row)
        print(f"  {row['cell']:<16} 총비용 {row['total_h']:>10,.1f}h  이탈 {row['n_escaped']:>5,}  "
              f"평균대기 {row['wait_mean_min']:6.1f}분  ({row['seconds']:.0f}초)")
        pd.DataFrame(rows).to_csv(out / "cells.csv", index=False, encoding="utf-8-sig")

    for seed in seeds:
        print(f"--- 시드 {seed} ---")
        world = prepare(cfg0, seed, root=root, log=quiet)
        mnl = MnlSettings(seed=seed, **mnl_kw)
        common = {"mnl": mnl, "strength": args.strength, "s3_window": args.s3_window,
                  "log": quiet, "nonpart_info": args.nonpart_info}

        base_row, base_events = run_cell(world, BASE, p=0.0, oracle=None, **common)
        keep({**base_row, "round": 0, "final": True}, base_events, BASE)

        def ceiling(src_events: list[dict], src_id: str, *, p: float, classic: bool,
                    tag: str, world: World = world, common: dict = common) -> None:
            """천장값 — 1회차 미래는 src, 이후 직전 회차. 고정점이거나 회차가 차면 멈춘다."""

            src, prev = _arrivals(src_events), None
            for rnd in range(1, args.rounds + 1):
                oracle = OracleSource(rows=src, source_run_id=src_id, round_no=rnd)
                row, ev = run_cell(world, ORACLE, p=p, oracle=oracle, classic=classic,
                                   **common)
                now = _arrivals(ev)
                moved = float("nan")
                if prev is not None:
                    moved = describe_shift(
                        OracleForecast.from_rows(prev, _dummy_specs(prev), source_run_id="prev"),
                        OracleForecast.from_rows(now, _dummy_specs(now), source_run_id="now"),
                    )["moved_share"]
                done = rnd == args.rounds or (not math.isnan(moved)
                                              and moved < FIXED_POINT_SHARE)
                keep({**row, "round": rnd, "moved_share": moved, "final": done}, ev,
                     f"{tag}_r{rnd}")
                if done:
                    break
                src_id, src, prev = f"{tag}_r{rnd}", now, now

        # --- MNL 세계: 바닥 MNL · 엔진@p · 천장@p (1회차 미래 = MNL 바닥) -------------
        for p in ps:
            # --floor-only: MNL 세계는 바닥만. p=1 이면 엔진 칸은 기존 세계와 같은 계산이다
            for st in ([] if args.floor_only else stages):
                row, ev = run_cell(world, st, p=p, oracle=None, **common)
                keep({**row, "round": 0, "final": True}, ev, f"{st}_p{p:g}")
            if not (args.floor_only or args.skip_ceiling):
                ceiling(base_events, f"{BASE}@seed{seed}", p=p, classic=False,
                        tag=f"{ORACLE}_p{p:g}")

        # --- 기존 세계 (--classic): 바닥 S0 · 엔진 · 천장 = 기존 PERFECT_ROLLING ---------
        #     1회차 미래는 **S1** 이다 — run_perfect_rolling.py 와 같은 정의
        if args.classic:
            s1_events = None
            for st in ["S0", *[s for s in ("S1", *stages) if s != "S0"]]:
                if st in [r["stage"] for r in rows if r["seed"] == seed
                          and r["family"] == "classic"]:
                    continue
                row, ev = run_cell(world, st, p=1.0, oracle=None, classic=True, **common)
                keep({**row, "round": 0, "final": True}, ev, f"classic_{st}")
                if st == "S1":
                    s1_events = ev
            ceiling(s1_events or [], f"classic:S1@seed{seed}", p=1.0, classic=True,
                    tag=f"classic_{ORACLE}")

        # --- 참고 천장 UE (--ue): 27.5% 의 분모 끝점 -------------------------------
        if args.ue:
            row, ev = run_ue_cell(world, quiet)
            keep({**row, "round": 0, "final": True}, ev, "classic_UE")

    cells = pd.DataFrame(rows)

    # 같은 시드인데 칸마다 차가 다르면 비교가 성립하지 않는다 (run_perfect_rolling 과 같은 검사)
    bad_seed = {s: g["n_ev_charging"].unique().tolist()
                for s, g in cells.groupby("seed") if g["n_ev_charging"].nunique() > 1}
    if bad_seed:
        raise SystemExit(f"[중단] 같은 시드인데 칸마다 충전 필요 EV 가 다르다: {bad_seed}")
    if (cells["policy_stranded"] > 0).any():
        print("  ⚠ 길에서 멈춘 차가 있다 — 정책이 못 닿는 곳을 골랐다. 결과를 쓰지 말 것")

    summary = summarize(cells)
    summary.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")
    print("\n=== 요약 (마지막 회차) ===")
    print(summary.reindex(columns=[c for c in SHOW if c in summary]).to_string(
        index=False, float_format=lambda v: f"{v:,.2f}"))
    print("\n⚠ 천장값은 완전정보 벤치마크(고정점 근사)다 — 상한이 아니다. "
          "moved_share 가 1% 이상이면 고정점에 닿지 않은 값이다.")
    manifest["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
