"""MNL 보정 — 추석 실측 충전 시작의 휴게소 분배에 θ·w 를 맞춘다 (#168).

    python scripts/calibrate_mnl.py \\
        --case-dir C:/.../docs/data_validation/issue162_20261108/passenger266 \\
        --data-root C:/.../evdt --seeds 1-3

## 무엇을 맞추나

`#162` 가 만든 추석 재현 설정 8개(9/24~27 × 상·하행, 충전기 266기)를 **그대로** 쓰고,
운전자의 휴게소 선택만 MNL 로 바꿔 돌린다. 격자의 각 점 (θ, w) 에서 모델 충전 시작을
실측(ME 4,480건)과 **비중으로** 비교한다 (`evdt.eval.choice_fit`).

    목적    BOTH 방향 · 4일 합산 **휴게소 점유율 TVD** — 시드 평균이 가장 작은 (θ, w)
    보조    휴게소×시간 TVD · 시간대 TVD · 방향별 TVD
    보고만  총량 편향 · WAPE (모집단이 달라 판정에 쓰지 않는다, #162)

## 같이 놓는 기준

    S0          θ = inf, w = 1 (같은 실행기로 다시 돌린다 — MNL 의 극한)
    UE (#162)   #162 의 기존 결과 파일을 그대로 읽는다 (시드 7, 다시 돌리지 않는다)

## 하루 빼고 보정 (leave-one-day-out)

날짜 d 를 빼고 나머지 3일의 TVD 로 (θ, w) 를 고른 뒤, **뺀 날의 TVD** 를 잰다.
θ 를 한 날짜에 과적합했는지 보는 최소한의 확인이다. 설 독립 검증은 (2/2) 다.

## 하지 않는 것

- **MLE 가 아니다.** 실측에는 선택 집합·고르지 않은 대안이 없다 — 집계 분포에 대한
  시뮬레이션 기반 보정이다. 결과를 "운전자 선호가 검증된 계수" 라고 쓰지 않는다
- 원본 #162 폴더에 쓰지 않는다. DB 는 `--out` 에 **사본**을 만들어 읽기 전용으로 쓴다
- 기존 DB·run 기록에 쓰지 않는다

## 출력 (`--out`)

    manifest.json   입력 경로·격자·시드·git SHA
    grid.csv        (θ, w, 시드, 방향, 날짜) 별 지표
    summary.csv     (θ, w) 별 시드 평균 ± 95% CI · 이탈 수
    lodo.csv        하루 빼고 고른 (θ, w) 와 뺀 날의 TVD
    best.json       선택된 (θ, w) 와 S0·UE 대비 값
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import time
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401
from run_mnl_world import BASE, _ci, _git_sha, prepare, run_cell  # noqa: E402

import evdt.runner as R  # noqa: E402
from evdt.engine.mnl import ESCAPE_MODES, MnlSettings  # noqa: E402
from evdt.eval.choice_fit import fit_metrics, restrict_to_observed  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR  # noqa: E402

#: #162 의 비교 창 — 시작이 이 안이면 센다
WINDOW = (pd.Timestamp("2026-09-24"), pd.Timestamp("2026-09-28"))
DATES = ("2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27")

DEFAULT_THETAS = "0.02,0.05,0.1,0.2,0.5,1.0"
DEFAULT_WEIGHTS = "1.0,1.5,2.0"

CONFIG_RE = re.compile(r"config_(\d{4}-\d{2}-\d{2})_(UP|DOWN)\.yaml$")


def _floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def load_observed(case: Path) -> pd.DataFrame:
    obs = pd.read_csv(case / "observed_chuseok_demand_history.csv",
                      dtype={"station_id": str, "charger_id": str}, parse_dates=["start"])
    obs = obs[~obs["duplicate_exact"].astype(bool)]
    obs = obs[(obs["start"] >= WINDOW[0]) & (obs["start"] < WINDOW[1])].copy()
    obs["date"] = obs["start"].dt.strftime("%Y-%m-%d")
    obs["hour"] = obs["start"].dt.hour
    return obs[["direction", "station_name", "date", "hour"]]


def load_station_map(case: Path) -> dict[str, tuple[str, str]]:
    cross = pd.read_csv(case / "model_charger_crosswalk.csv", dtype=str)
    m = cross[["model_station_id", "station_name", "direction"]].drop_duplicates()
    if m["model_station_id"].duplicated().any():
        raise SystemExit("[중단] 모델 휴게소 하나가 실측 휴게소 여럿에 대응된다 — 대응표 확인")
    return {r.model_station_id: (r.station_name, r.direction) for r in m.itertuples()}


def to_frame(events: list[dict], date: str, smap: dict[str, tuple[str, str]]) -> pd.DataFrame:
    if not events:
        return pd.DataFrame(columns=["direction", "station_name", "date", "hour"])
    f = pd.DataFrame(events)[["station_id", "t_start_min"]]
    names = f["station_id"].map(smap)
    if names.isna().any():
        missing = sorted(f.loc[names.isna(), "station_id"].unique())
        raise SystemExit(f"[중단] 대응표에 없는 모델 휴게소: {missing}")
    start = pd.Timestamp(date) + pd.to_timedelta(f["t_start_min"], unit="m")
    out = pd.DataFrame({
        "direction": [n[1] for n in names], "station_name": [n[0] for n in names],
        "start": start,
    })
    out = out[(out["start"] >= WINDOW[0]) & (out["start"] < WINDOW[1])]
    out["date"] = out["start"].dt.strftime("%Y-%m-%d")
    out["hour"] = out["start"].dt.hour
    return out[["direction", "station_name", "date", "hour"]]


def load_ue_reference(case: Path) -> pd.DataFrame | None:
    p = case / "generated_chuseok_charging_history.csv"
    if not p.exists():
        return None
    ue = pd.read_csv(p, parse_dates=["start"])
    ue = ue[(ue["start"] >= WINDOW[0]) & (ue["start"] < WINDOW[1])].copy()
    ue["date"] = ue["start"].dt.strftime("%Y-%m-%d")
    ue["hour"] = ue["start"].dt.hour
    return ue[["direction", "station_name", "date", "hour"]]


def main() -> int:  # noqa: PLR0915 — 실행 순서가 곧 절차라 나누면 읽기 어렵다
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case-dir", required=True,
                    help="#162 passenger266 폴더 (config_*.yaml · scenario_sep18.sqlite · 대응표 · 실측)")
    ap.add_argument("--data-root", default=str(PROJECT_ROOT),
                    help="config 의 상대 경로(reports/issue160_ctm_daily/…)를 찾을 루트")
    ap.add_argument("--seeds", default="1-3")
    ap.add_argument("--thetas", default=DEFAULT_THETAS)
    ap.add_argument("--wait-weights", default=DEFAULT_WEIGHTS)
    ap.add_argument("--escape-mode", choices=ESCAPE_MODES, default="s0",
                    help="이탈 판정: s0 = 잡음 없이 S0 규칙 (기본) · noisy = 이탈에도 잡음 (#168 첫 실행 방식, 민감도)")
    ap.add_argument("--configs", default="", help="일부만 (예: 2026-09-25_DOWN). 비우면 8개 전부")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    case = Path(args.case_dir).resolve()
    root = Path(args.data_root).resolve()
    out = Path(args.out) if args.out else RUNS_DIR / "mnl_calibration" / time.strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    # DB 는 사본을 쓴다 — 원본 #162 폴더를 건드리지 않는다
    db_copy = out / "case_db_copy.sqlite"
    if not db_copy.exists():
        shutil.copyfile(case / "scenario_sep18.sqlite", db_copy)
    os.environ["EVDT_DB_PATH"] = str(db_copy)

    configs = sorted(p for p in case.glob("config_*.yaml") if CONFIG_RE.search(p.name))
    if args.configs:
        want = {c.strip() for c in args.configs.split(",")}
        configs = [p for p in configs if CONFIG_RE.search(p.name).group(1) + "_"
                   + CONFIG_RE.search(p.name).group(2) in want]
    if not configs:
        raise SystemExit(f"[중단] config_<날짜>_<방향>.yaml 이 없다: {case}")

    seeds = R.parse_seeds(args.seeds)
    grid = [(th, w) for th in _floats(args.thetas) for w in _floats(args.wait_weights)]
    grid.append((math.inf, 1.0))     # S0 — MNL 의 극한

    obs = load_observed(case)
    smap = load_station_map(case)
    print(f"실측 {len(obs):,}건 · 관측 휴게소 {obs.groupby(['direction', 'station_name']).ngroups}곳"
          f" · 설정 {len(configs)}개 · 시드 {seeds} · 격자 {len(grid)}점")

    manifest = {
        "issue": 168, "script": "scripts/calibrate_mnl.py", "case_dir": str(case),
        "data_root": str(root), "db_copy": str(db_copy), "configs": [p.name for p in configs],
        "seeds": seeds, "thetas": _floats(args.thetas), "wait_weights": _floats(args.wait_weights),
        "escape_mode": args.escape_mode,
        "objective": "BOTH·ALL 휴게소 점유율 TVD 의 시드 평균 (관측 28곳 안)",
        "window": [str(w) for w in WINDOW], "git": _git_sha(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "집계 분포에 대한 시뮬레이션 기반 보정. MLE 아님 — 운전자 선호 검증으로 읽지 말 것",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    # (θ, w, seed) → 이벤트 조각들
    pieces: dict[tuple[float, float, int], list[pd.DataFrame]] = {}
    escapes: dict[tuple[float, float, int], int] = {}
    quiet = lambda *_: None  # noqa: E731

    for path in configs:
        date, direction = CONFIG_RE.search(path.name).groups()
        cfg = R.load_config(path)
        for seed in seeds:
            t0 = time.perf_counter()
            world = prepare(cfg, seed, root=root, log=quiet)
            for th, w in grid:
                mnl = MnlSettings(theta=th, wait_weight=w, seed=seed,
                                  escape_mode=args.escape_mode)
                row, ev = run_cell(world, BASE, p=0.0, mnl=mnl, strength=0.0, oracle=None,
                                   s3_window=None, log=quiet)
                pieces.setdefault((th, w, seed), []).append(to_frame(ev, date, smap))
                escapes[(th, w, seed)] = escapes.get((th, w, seed), 0) + int(row["n_escaped"])
            print(f"  {date} {direction} 시드 {seed}: 충전 필요 {len(world.built.trips):,}대 · "
                  f"{len(grid)}점 {time.perf_counter() - t0:.0f}초")

    rows = []
    for (th, w, seed), frames in pieces.items():
        model = restrict_to_observed(pd.concat(frames, ignore_index=True), obs)
        for r in fit_metrics(obs, model, dates=DATES):
            rows.append({"model": "S0" if math.isinf(th) else "MNL", "theta": th,
                         "wait_weight": w, "seed": seed,
                         "n_escaped_all_configs": escapes[(th, w, seed)], **r})
    ue = load_ue_reference(case)
    if ue is not None:
        for r in fit_metrics(obs, restrict_to_observed(ue, obs), dates=DATES):
            rows.append({"model": "UE(#162)", "theta": float("nan"), "wait_weight": float("nan"),
                         "seed": 7, "n_escaped_all_configs": float("nan"), **r})
    g = pd.DataFrame(rows)
    g.to_csv(out / "grid.csv", index=False, encoding="utf-8-sig")

    # (θ, w) 별 요약 — BOTH · 4일 합산
    head = g[(g["direction"] == "BOTH") & (g["date"] == "ALL")]
    summ = []
    for (model, th, w), s in head.groupby(["model", "theta", "wait_weight"], dropna=False):
        rec = {"model": model, "theta": th, "wait_weight": w, "n_seeds": len(s)}
        for m in ("station_tvd", "station_hour_tvd", "hour_tvd", "total_bias_pct",
                  "station_wape_pct", "n_escaped_all_configs"):
            rec[m] = s[m].mean()
            rec[f"{m}_ci95"] = _ci(s[m])
        for d in ("DOWN", "UP"):
            sub = g[(g["direction"] == d) & (g["date"] == "ALL") & (g["model"] == model)
                    & (g["theta"].fillna(-1) == (-1 if pd.isna(th) else th))
                    & (g["wait_weight"].fillna(-1) == (-1 if pd.isna(w) else w))]
            rec[f"station_tvd_{d}"] = sub["station_tvd"].mean()
        summ.append(rec)
    summary = pd.DataFrame(summ).sort_values("station_tvd")
    summary.to_csv(out / "summary.csv", index=False, encoding="utf-8-sig")

    mnl_only = summary[summary["model"] == "MNL"]
    best = mnl_only.iloc[0]

    # 하루 빼고 보정
    daily = g[(g["direction"] == "BOTH") & (g["date"] != "ALL") & (g["model"] == "MNL")]
    lodo = []
    for d in DATES:
        train = (daily[daily["date"] != d].groupby(["theta", "wait_weight"])["station_tvd"]
                 .mean().sort_values())
        th, w = train.index[0]
        test = daily[(daily["date"] == d) & (daily["theta"] == th) & (daily["wait_weight"] == w)]
        s0 = g[(g["direction"] == "BOTH") & (g["date"] == d) & (g["model"] == "S0")]
        lodo.append({"held_out": d, "theta": th, "wait_weight": w,
                     "train_tvd": float(train.iloc[0]),
                     "held_out_tvd": float(test["station_tvd"].mean()),
                     "s0_held_out_tvd": float(s0["station_tvd"].mean())})
    lodo_df = pd.DataFrame(lodo)
    lodo_df.to_csv(out / "lodo.csv", index=False, encoding="utf-8-sig")

    def pick(model: str) -> dict:
        r = summary[summary["model"] == model]
        return {} if r.empty else {k: float(r.iloc[0][k]) for k in
                                   ("station_tvd", "station_hour_tvd", "hour_tvd",
                                    "total_bias_pct")}

    best_json = {
        "theta": float(best["theta"]), "wait_weight": float(best["wait_weight"]),
        "escape_mode": args.escape_mode,
        "station_tvd": float(best["station_tvd"]),
        "station_tvd_ci95": float(best["station_tvd_ci95"]),
        "station_hour_tvd": float(best["station_hour_tvd"]),
        "n_escaped_all_configs": float(best["n_escaped_all_configs"]),
        "S0": pick("S0"), "UE(#162)": pick("UE(#162)"),
        "lodo_theta_stable": bool(lodo_df["theta"].nunique() == 1),
        "on_grid_edge": bool(best["theta"] in (min(_floats(args.thetas)),
                                               max(_floats(args.thetas)))),
    }
    (out / "best.json").write_text(json.dumps(best_json, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    manifest["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    show = ["model", "theta", "wait_weight", "station_tvd", "station_tvd_ci95",
            "station_tvd_DOWN", "station_tvd_UP", "station_hour_tvd", "n_escaped_all_configs"]
    print("\n=== 휴게소 점유율 TVD (작을수록 실측 분배에 가깝다) ===")
    print(summary[show].head(12).to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print("\n=== 하루 빼고 보정 ===")
    print(lodo_df.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print(f"\n선택: θ={best_json['theta']:g} · w={best_json['wait_weight']:g} · "
          f"TVD {best_json['station_tvd']:.4f}")
    if best_json["on_grid_edge"]:
        print("  ⚠ 선택된 θ 가 격자 끝이다 — 격자를 넓혀 다시 볼 것")
    print(f"출력: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
