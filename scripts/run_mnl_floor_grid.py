"""MNL 바닥 격자를 **10시드**로 다시 잰다 (#168 2/2 작업 D 보강).

    python scripts/run_mnl_floor_grid.py --seeds 1-10
    python scripts/run_mnl_floor_grid.py --seeds 1-10 --skip-classic   # 바닥만

## 왜

#168 의 격차 축소율 판정이 **시드 4개**였다 (시드 5는 UE 미수렴으로 제외).
팀 보고가 직접 적어 뒀다 — *"시드가 4개라 CI 가 넓다. 그래서 '차이가 없다'가
아니라 **'차이를 확인하지 못했다'** 로 읽는다. 특히 w 1.5 쪽은 평균 차이가
+4~5%p 라, 시드를 늘리면 차이가 드러날 수 있다. 확정하려면 시드를 10개
이상으로 늘려야 한다."*

그 10개를 돌린다.

## 무엇을 도나 — **바닥만 바꾼다**

격차 축소율은 `(바닥 − 완전예측) / (바닥 − UE)` 이고, 천장과 UE 는 두 세계가
**같은 값**을 쓴다 (팀 보고 §5.1: "완전예측과 UE 는 두 경우에 같은 값을 쓰고
바닥만 바꿨다"). 그래서

    1회  --classic --ue      S0 바닥 · 완전예측 천장 · UE      ← 비싸다
    6회  --floor-only        MNL 바닥 (θ × w 격자)            ← 싸다

θ ∈ {0.01, 0.05, 0.1} × w ∈ {1.0, 1.5} 는 #168 이 쓴 격자 그대로다.

## ⚠ 포기(renege) 설정이 그때와 다를 수 있다

`scenario_seollal_up_adoption.yaml` 은 지금 `renege: true` 다 (#147). #168 의
27.5% 재현 주장은 **포기가 없던 때**의 숫자와 견준 것이다. 그래서

  · **바닥만 바꾼 짝지은 비교(S0 ↔ MNL)는 그대로 유효하다** — 두 팔이 같은
    설정을 쓰므로 포기 여부가 상쇄된다. 이 스크립트가 재는 것이 그것이다
  · **"25.5% 가 기존 27.5% 를 재현한다"는 확인은 포기를 켠 채로는 못 한다** —
    다른 세계다. 그 확인이 필요하면 `--renege-off` 로 따로 돌린다

`manifest.json` 에 설정을 적어 두니 나중에 헷갈리지 않는다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from _bootstrap import ROOT  # noqa: E402,F401

#: #168 이 쓴 격자 그대로
THETAS = (0.01, 0.05, 0.1)
WEIGHTS = (1.0, 1.5)

PY = sys.executable
RUNNER = "scripts/run_mnl_world.py"


def run(args: list[str], label: str) -> bool:
    # ★ `RUNNER` 를 빼먹으면 python 이 `--config` 를 **자기 옵션**으로 읽고
    #   "unknown option --config" 로 0초 만에 죽는다 (2026-11-13)
    cmd = [PY, "-X", "utf8", RUNNER, *args]
    print(f"\n{'=' * 70}\n▶ {label}\n  {' '.join(cmd[3:])}\n{'=' * 70}", flush=True)
    t0 = time.perf_counter()
    done = subprocess.run(cmd, cwd=ROOT, check=False)
    ok = done.returncode == 0
    print(f"\n  {'완료' if ok else '★ 실패'} ({time.perf_counter() - t0:,.0f}초)",
          flush=True)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--out", default="runs/mnl_world/seollal_up_adoption_v3")
    ap.add_argument("--skip-classic", action="store_true",
                    help="기존 세계·UE 를 건너뛴다 (이미 돌렸을 때)")
    args = ap.parse_args()

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    common = ["--config", args.config, "--seeds", args.seeds,
              "--rounds", str(args.rounds), "--escape-mode", "s0", "--p", "1.0"]

    plan = []
    if not args.skip_classic:
        plan.append((["--classic", "--ue", "--out", f"{args.out}/classic"],
                     "기존 세계 (S0 바닥 · 완전예측 천장 · UE)"))
    for th in THETAS:
        for w in WEIGHTS:
            tag = f"floor_theta{th:g}_w{w:g}".replace(".", "p")
            plan.append(([
                "--floor-only", "--theta", str(th), "--wait-weight", str(w),
                "--out", f"{args.out}/{tag}",
            ], f"MNL 바닥 θ={th:g} w={w:g}"))

    print(f"\nMNL 바닥 격자 · 시드 {args.seeds} · {len(plan)}회 실행")
    print(f"  config {args.config}")
    print(f"  출력   {out}\n")

    (out / "manifest.json").write_text(json.dumps({
        "script": "scripts/run_mnl_floor_grid.py", "config": args.config,
        "seeds": args.seeds, "thetas": list(THETAS), "wait_weights": list(WEIGHTS),
        "rounds": args.rounds, "escape_mode": "s0", "p": 1.0,
        "note": "격차 축소율 = (바닥 − 완전예측)/(바닥 − UE). 천장·UE 는 두 세계 공유, "
                "바닥만 바꾼다. 포기 설정은 config 를 따른다 — 짝지은 비교는 "
                "두 팔이 같은 설정이라 유효하다",
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [label for a, label in plan if not run([*common, *a], label)]
    print(f"\n{'=' * 70}")
    if failed:
        print(f"★ 실패 {len(failed)}개: {failed}")
        return 1
    print(f"전부 완료 — {len(plan)}회")
    print(f"\n다음: python scripts/analyze_ceiling_v2_168.py (출력 {args.out})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
