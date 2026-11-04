"""휴게소별 대기 + 아래로 뻗는 이탈 — 사다리를 가로로 나열한다 (중간 발표 §6).

    python scripts/plot_station_bars.py
    python scripts/plot_station_bars.py --stages S0,S1,S2,S3 --seeds 1-20

    위쪽 막대   그 휴게소에서 충전한 차들이 **실제로 기다린** 평균 시간(분)
    아래쪽 막대 그 휴게소가 **밀어낸** 차 수 (balked)
    가로축      휴게소 — 첫 칸의 대기 **내림차순**으로 고정
    패널        S0 → S1 → S2 (→ S3) 를 가로로

## 이 그림이 하는 말

발표 1장의 주장은 *"총량이 아니라 배분"* 이다. **이 그림 하나가 그걸 증명한다.**

    최악이 깎인다        건천 157.3 → 67.0 → 53.0분
    가운데가 올라간다    안성 13.6 → 25.0 · 신탄진 12.3 → 20.8
    아래 막대가 무너진다 칠곡 95.8 → 12.7 → 3.9대

전체가 같이 좋아진 것이 아니라 **차가 옮겨 간 것**이다. 한가한 휴게소가 쓰이기
시작했고, 그 대가로 중간 휴게소의 대기가 올라갔다.

## 대기시간은 **실제로 기다린 시간**이다

`charge_event.wait_min` 을 쓴다 — 2D 히트맵(`plot_ladder.py`)과 **같은 출처**다.
그림마다 다른 정의를 쓰면 나란히 못 놓는다.

> 스냅샷의 `wait_min`(앱 화면에 뜨는 값)으로도 그릴 수 있지만, 그것은 차가 한 대도
> 없는 프레임까지 평균에 넣어 값이 희석된다. 발표에서 *"평균 몇 분 기다렸나"* 로
> 말할 숫자는 이쪽이다.

## 이탈은 **balked 만** 휴게소에 붙인다

    balked    줄이 길어서 나갔다 — `best_station_id` 가 있다 (남았다면 갔을 곳)
    no_plan   닿는 휴게소가 아예 없었다 — **휴게소가 없다.** 코리도 전체 숫자다

`no_plan` 을 어느 휴게소에 붙이면 **그 휴게소가 밀어낸 것처럼 보인다.** 그건 충전기
간격의 문제이고 엔진이 못 고치는 몫이다 (`docs/charger_gaps.md`). 그래서 패널 제목에
따로 적는다.

## ★ 숫자와 그림이 어긋나면 **저장하지 않는다**

`plot_ladder.py` 와 같은 방어다. 그리는 막대를 다시 합쳐서 `run_kpi` 와 대조한다.

    휴게소별 balked 합  ==  n_escaped_balked
    no_plan 수          ==  n_escaped − n_escaped_balked

이 그림의 쓸모는 *"막대를 세 보라"* 는 것이다. 어긋난 채로 내놓으면 심사에서
세는 순간 무너진다.

## 척도를 공유한다

위·아래 축 모두 **모든 패널에서 같은 범위**를 쓰고 그 값을 범례에 적는다.
각자 정규화하면 **두 그림이 똑같아 보인다** — #93 에서 겪은 그 고장이다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.loaders import duck_connect  # noqa: E402
from evdt.io.run_registry import make_run_id  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.runner import load_config, parse_seeds  # noqa: E402
from evdt.viz.plots import _korean_font  # noqa: E402

DEFAULT_STAGES = ("S0", "S1", "S2")

#: 스테이지가 반드시 만족해야 하는 params. `gap_table.py` 와 **같은 규칙**이어야 한다
#: — 한쪽만 느슨하면 표와 그림이 다른 런을 보게 된다
def expected(s3_window: int) -> dict[str, dict]:
    return {
        "S2": {"forecast_kind": "observed"},
        "S3": {"forecast_kind": "observed", "s3_window": s3_window},
        "PERFECT_ROLLING": {"forecast_kind": "oracle", "oracle_blur": False},
    }


#: 위쪽 막대 색 — 단계가 올라갈수록 짙어진다 (viz.plots.RAMP 계열과 같은 톤)
BAR_COLORS = ("#9ec5f4", "#3987e5", "#1c5cab", "#0d366b", "#06203f")

#: 아래쪽 막대(이탈) 색 — 3D 뷰어의 이탈 기둥과 **같은 빨강**
ESCAPE_COLOR = "#c45646"


def fetch(db: Path, run_ids: list[str]) -> tuple[pd.DataFrame, dict[str, dict]]:
    """run_kpi 와 params_json 을 가져온다."""

    holes = ",".join("?" * len(run_ids))
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
        kpi = pd.read_sql(
            f"SELECT run_id, metric, value FROM run_kpi WHERE run_id IN ({holes})",  # noqa: S608
            con, params=run_ids)
        rows = con.execute(
            f"SELECT run_id, status, params_json FROM run WHERE run_id IN ({holes})",  # noqa: S608
            run_ids).fetchall()
    params = {r[0]: {"status": r[1], **json.loads(r[2] or "{}")} for r in rows}
    return kpi.pivot(index="run_id", columns="metric", values="value"), params


def collect(cfg, stages: list[str], seeds: list[int], s3_window: int):
    """칸·시드별 런을 모은다. **params 가 안 맞거나 안 끝난 런은 뺀다.**"""

    db = default_db_path()
    ids = {(st, s): make_run_id(cfg.scenario_id, st, s, cfg.policy.participation)
           for st in stages for s in seeds}
    kpi, params = fetch(db, sorted(set(ids.values())))

    ok, dropped = {}, []
    for (st, s), rid in ids.items():
        p = params.get(rid)
        if p is None or p["status"] != "DONE":
            continue
        bad = {k: (p.get(k), v) for k, v in expected(s3_window).get(st, {}).items()
               if p.get(k) != v}
        if bad:
            dropped.append(f"{rid} — " + " · ".join(
                f"{k}: 기록 {a!r} ≠ 기대 {b!r}" for k, (a, b) in bad.items()))
            continue
        if rid not in kpi.index:
            continue
        ok[(st, s)] = rid

    # **모든 칸이 끝낸 시드만** 쓴다 (#59). 안 그러면 칸마다 다른 시드 집합을 비교한다
    shared = sorted({s for s in seeds if all((st, s) in ok for st in stages)})
    return {(st, s): ok[(st, s)] for st in stages for s in shared}, shared, kpi, dropped


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_up_adoption.yaml")
    ap.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                    help="가로로 놓을 칸. 첫 칸의 대기 내림차순으로 가로축을 고정한다")
    ap.add_argument("--seeds", default="1-20")
    ap.add_argument("--s3-window", type=int, default=4,
                    help="이 윈도우로 돌린 S3 만 쓴다 (#139)")
    ap.add_argument("--top", type=int, default=0,
                    help="대기 상위 N 곳만 (0 이면 전부). 발표 슬라이드는 12 정도가 읽힌다")
    ap.add_argument("--no-ghost", action="store_true",
                    help="첫 칸의 실루엣을 뒤 칸에 겹치지 않는다")
    ap.add_argument("--out", default="docs/figures/station_bars.png")
    args = ap.parse_args()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    cfg = load_config(ROOT / args.config)
    runs, shared, kpi, dropped = collect(cfg, stages, parse_seeds(args.seeds),
                                         args.s3_window)

    if dropped:
        print(f"\n  [제외] params 가 안 맞는 런 {len(dropped)}개")
        for d in dropped[:6]:
            print(f"    {d}")

    if not shared:
        raise SystemExit(
            "\n[중단] 모든 칸이 끝낸 공통 시드가 없다.\n"
            f"  요청한 칸: {stages}\n"
            "  칸마다 다른 시드로 그리면 **시드 변동을 정책 차이로 보여주게 된다** (#59).")

    print(f"\n{cfg.scenario_id}   칸 {' │ '.join(stages)}")
    print(f"**공통 시드 {len(shared)}개** {shared}")

    # --- 같은 세계인지 ------------------------------------------------------
    # 진입 EV 가 칸마다 다르면 다른 수요를 비교하는 것이다 (#59)
    ev = {}
    for s in shared:
        vals = {round(float(kpi.loc[runs[(st, s)], "n_ev_charging"])) for st in stages}
        if len(vals) > 1:
            ev[s] = sorted(vals)
    if ev:
        raise SystemExit(f"\n[중단] 같은 시드인데 칸마다 충전 필요 EV 가 다르다: {ev}\n"
                         "  stage 는 세계를 바꾸지 않는다.")

    # --- 집계 --------------------------------------------------------------
    ids = sorted(set(runs.values()))
    con = duck_connect(db_path=default_db_path(), run_ids=ids, runs_dir=RUNS_DIR)
    names = dict(con.sql("SELECT station_id, name FROM station").fetchall())

    wait = con.sql(
        "SELECT run_id, station_id, AVG(wait_min) AS wait_min, COUNT(*) AS n_ev "
        "FROM charge_event GROUP BY 1, 2").df()
    balked = con.sql(
        "SELECT run_id, best_station_id AS station_id, COUNT(*) AS balked "
        "FROM escape_event WHERE reason = 'balked' GROUP BY 1, 2").df()
    noplan = con.sql(
        "SELECT run_id, COUNT(*) AS no_plan FROM escape_event "
        "WHERE reason = 'no_plan' GROUP BY 1").df().set_index("run_id")["no_plan"]

    panels, mismatch = [], []
    for st in stages:
        rids = [runs[(st, s)] for s in shared]
        w = (wait[wait.run_id.isin(rids)].groupby("station_id")["wait_min"].sum()
             / len(shared))
        b = (balked[balked.run_id.isin(rids)].groupby("station_id")["balked"].sum()
             / len(shared))
        npl = float(noplan.reindex(rids).fillna(0).sum() / len(shared))

        # ★ 그리는 막대를 **다시 합쳐서** run_kpi 와 대조한다
        rec_b = float(kpi.loc[rids, "n_escaped_balked"].mean())
        rec_np = float((kpi.loc[rids, "n_escaped"] - kpi.loc[rids, "n_escaped_balked"]).mean())
        if abs(b.sum() - rec_b) > 0.05:
            mismatch.append(f"{st}: 그림의 balked 합 {b.sum():.1f} ≠ 기록 {rec_b:.1f}")
        if abs(npl - rec_np) > 0.05:
            mismatch.append(f"{st}: 그림의 no_plan {npl:.1f} ≠ 기록 {rec_np:.1f}")

        panels.append((st, w, b, npl, float(kpi.loc[rids, "bottleneck_slots"].mean())))

    if mismatch:
        raise SystemExit(
            "\n[중단] 그림에서 센 이탈과 run_kpi 가 다르다 — **그림을 저장하지 않는다**:\n  "
            + "\n  ".join(mismatch)
            + "\n\n  이 그림의 쓸모는 '막대를 세 보라' 는 것이다. 어긋난 채로 내놓으면"
              "\n  심사에서 세는 순간 무너진다. 집계를 먼저 맞출 것.")

    # --- 가로축: 첫 칸의 대기 내림차순 --------------------------------------
    order = panels[0][1].sort_values(ascending=False).index.tolist()
    extra = [s for _, w, b, _, _ in panels for s in list(w.index) + list(b.index)
             if s not in order]
    order += sorted(dict.fromkeys(extra))
    if args.top:
        order = order[:args.top]

    # --- 척도 공유 ---------------------------------------------------------
    wmax = max(float(w.reindex(order).fillna(0).max()) for _, w, _, _, _ in panels)
    bmax = max(float(b.reindex(order).fillna(0).max()) for _, _, b, _, _ in panels)
    print(f"\n  척도 공유 — 대기 0~{wmax:.0f}분 · 이탈 0~{bmax:.0f}대 (모든 칸 동일)")

    print(f"\n{'휴게소':<12}" + "".join(f"{st:>9}" for st in stages)
          + "   │" + "".join(f"{st+' 이탈':>11}" for st in stages))
    for sid in order:
        row = f"{names.get(sid, sid)[:11]:<12}"
        row += "".join(f"{float(w.get(sid, 0)):>9.1f}" for _, w, _, _, _ in panels)
        row += "   │" + "".join(f"{float(b.get(sid, 0)):>11.1f}"
                                for _, _, b, _, _ in panels)
        print(row)
    print(f"{'합계/평균':<12}"
          + "".join(f"{float(w.reindex(order).fillna(0).mean()):>9.1f}"
                    for _, w, _, _, _ in panels)
          + "   │" + "".join(f"{float(b.reindex(order).fillna(0).sum()):>11.1f}"
                             for _, _, b, _, _ in panels))

    # --- 그림 --------------------------------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    _korean_font(plt)

    #: 아래쪽(이탈)이 차지하는 높이 — 위쪽(대기) 대비 비율.
    #  1.0 으로 두면 아래 절반이 거의 비어서 **대기 막대가 눌린다**. 칠곡 하나만
    #  크고 나머지는 작기 때문이다. 0.45 가 둘 다 읽힌다.
    LOWER = 0.45

    def nice(hi: float, n: int = 4) -> list[float]:
        """0 부터 hi 까지 읽기 좋은 눈금."""
        import math
        raw = hi / n
        mag = 10 ** math.floor(math.log10(max(raw, 1e-9)))
        step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw),
                   default=mag * 10)
        out, v = [], 0.0
        while v <= hi + step * 0.01:
            out.append(round(v, 6))
            v += step
        return out

    n = len(panels)
    fig, axes = plt.subplots(1, n, figsize=(5.4 * n, 7.6), sharey=True)
    if n == 1:
        axes = [axes]
    labels = [names.get(s, s).replace("휴게소", "")[:4] for s in order]
    x = range(len(order))
    lo = wmax * LOWER                      # 아래쪽이 쓰는 세로 높이
    to_px = lo / max(bmax, 1e-9)           # 이탈 대수 → 세로 길이

    for i, ((st, w, b, npl, btl), ax) in enumerate(zip(panels, axes, strict=True)):
        up = [float(w.get(s, 0.0)) for s in order]
        dn = [-float(b.get(s, 0.0)) * to_px for s in order]

        # ★ 첫 칸의 실루엣을 **뒤에 겹친다.** 패널을 눈으로 왕복하지 않아도
        #   "얼마나 깎였나" 가 한 칸 안에서 보인다. 척도가 같아야 성립한다
        if i and not args.no_ghost:
            g_up = [float(panels[0][1].get(s, 0.0)) for s in order]
            g_dn = [-float(panels[0][2].get(s, 0.0)) * to_px for s in order]
            ax.bar(x, g_up, width=0.74, color="none", edgecolor="#b9b6b1",
                   lw=0.9, linestyle=(0, (3, 2)), label=f"{panels[0][0]} (참고)")
            ax.bar(x, g_dn, width=0.74, color="none", edgecolor="#d9b0a8",
                   lw=0.9, linestyle=(0, (3, 2)))

        ax.bar(x, up, color=BAR_COLORS[min(i, len(BAR_COLORS) - 1)], width=0.74,
               label="평균 대기(분)")
        ax.bar(x, dn, color=ESCAPE_COLOR, width=0.74, label="밀어낸 차(대)")
        ax.axhline(0, color="#44423e", lw=1.1)

        ax.set_title(
            f"{st}   평균 대기 {float(w.reindex(order).fillna(0).mean()):.1f}분 · "
            f"밀어냄 {b.sum():.0f}대 · 병목 {btl:.0f}칸\n"
            f"(닿는 곳 없어 나간 차 {npl:.0f}대 — 휴게소에 안 붙인다)",
            fontsize=10, loc="left")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=90, fontsize=8)
        ax.set_ylim(-lo * 1.10, wmax * 1.08)
        ax.grid(axis="y", color="#e8e6e2", lw=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

        # 눈금을 **두 단위로 따로** 매긴다 — 위는 분, 아래는 대수
        wt = nice(wmax)
        bt = [v for v in nice(bmax) if v > 0]
        ax.set_yticks(wt + [-v * to_px for v in bt])
        ax.set_yticklabels([f"{v:.0f}" for v in wt] + [f"{v:.0f}" for v in bt],
                           fontsize=8)
        ax.legend(loc="upper right", fontsize=8, frameon=False)

    # 축 이름은 **위·아래에 따로** 쓴다. 한 문자열로 쓰면 회전하면서 화살표가 뒤집힌다
    a0 = axes[0]
    a0.text(-0.085, 0.74, "↑ 평균 대기 (분)", transform=a0.transAxes, rotation=90,
            va="center", ha="center", fontsize=9, color="#2b4e86")
    a0.text(-0.085, 0.16, "↓ 밀어낸 차 (대)", transform=a0.transAxes, rotation=90,
            va="center", ha="center", fontsize=9, color=ESCAPE_COLOR)

    fig.suptitle(
        f"휴게소별 대기와 밀어낸 차 — {' │ '.join(stages)}   "
        f"(공통 시드 {len(shared)}개 평균 · 척도 공유: 대기 0~{wmax:.0f}분 · "
        f"이탈 0~{bmax:.0f}대)",
        fontsize=12, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.96))

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor="white")
    plt.close(fig)
    print(f"\n  저장 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
