"""할인 정책의 근거 — **어디를, 언제 깎아야 하나** (§8-1).

    python scripts/discount_policy.py --seeds 1 2 3 4 5
    python scripts/discount_policy.py --seeds 1 2 3 4 5 --out docs/figures/discount_chuseok.png

## 무엇을 묻나

`externality_rank.py` 가 **차마다** 낸 한계 외부효과를 받아, 요금으로 손댈 수
있는 단위 — **휴게소 × 시간대** — 로 접는다.

    요금 신호(s, h) = 그 칸에 한 대가 더 설 때 **남들에게 얹는 시간** (분/대)

피구세의 그대로다. 비싼 칸은 할증, 싼 칸은 할인. 차마다 값을 매길 수는 없지만
**휴게소와 시간대에는 매길 수 있다** — 그래서 이 단위로 접는다.

## ⚠ 더하지 말 것

한계값이라 합이 총량이 아니다 (`externality_rank.py` 머리말: 설 S0 에서 5.4배).
여기서 쓰는 것은 **칸끼리의 순서와 비율**뿐이다. "이 칸 요금 신호가 저 칸의
3배" 는 되고, "전부 더하면 n시간" 은 안 된다.

## ⚠ 이 그림은 바닥(MNL)에서 읽는다

할인은 **엔진이 없을 때** 쏠림을 손보는 수단이다. S4 칸을 같이 그리는 것은
"엔진이 이미 한 일을 요금이 또 하지 않는다" 를 보이기 위해서다 — 엔진이 돌면
요금 신호가 어디서 얼마나 평평해지는지 같이 보인다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.paths import RUNS_DIR, default_db_path  # noqa: E402
from evdt.viz.sources import Sources  # noqa: E402

SCENARIO = "chuseok_2026_up_congested"
CORRIDOR = "gyeongbu_up"

#: 그림에 쓸 두 칸. (이름, CSV 태그 꼴)
ARMS = (
    ("MNL (엔진 없음)", "externality_chuseok_congested_MNL__s{seed:04d}.csv"),
    ("S4 (엔진 있음)", f"externality_{SCENARIO}__S4__p100__s{{seed:04d}}.csv"),
)

#: 요금표를 몇 시간 단위로 끊나. 1시간은 너무 잘고 6시간은 피크를 뭉갠다
HOUR_BIN = 2


def load_arm(pattern: str, seeds: list[int]) -> tuple[pd.DataFrame, list[Path]]:
    """시드별 CSV 를 읽어 한 표로. **시드는 평균한다** — 한 시드는 하루다."""

    acc, paths = [], []
    for sd in seeds:
        p = RUNS_DIR / "sweeps" / pattern.format(seed=sd)
        paths.append(p)
        if not p.is_file():
            raise SystemExit(f"[중단] {p} 가 없다 — externality_rank.py 를 먼저 돌릴 것")
        acc.append(pd.read_csv(p).assign(seed=sd))
    return pd.concat(acc, ignore_index=True), paths


def fold(t: pd.DataFrame) -> pd.DataFrame:
    """정차 표를 **휴게소 × 시간대 묶음**으로. 값은 `분/대` (합이 아니다)."""

    t = t.assign(hb=((t["arrival_min"] // 60).astype(int) % 24 // HOUR_BIN) * HOUR_BIN)
    # 시드마다 하루치가 들어 있다. 먼저 시드 안에서 평균을 내고 그 다음 시드를
    # 평균한다 — 그냥 전부 평균하면 차가 많은 시드가 더 무거워진다
    per_seed = (t.groupby(["seed", "station_id", "hb"])
                 .agg(ext_per_car=("ext_min", "mean"), n=("ext_min", "size"))
                 .reset_index())
    return (per_seed.groupby(["station_id", "hb"])
                    .agg(ext_per_car=("ext_per_car", "mean"),
                         n=("n", "mean"), n_seeds=("seed", "nunique"))
                    .reset_index())


def station_names(ids: set[str]) -> dict[str, tuple[str, float]]:
    with get_conn(default_db_path(), readonly=True) as conn:
        rows = pd.read_sql(
            "SELECT station_id, name, offset_km FROM station WHERE corridor_id = ?",
            conn, params=(CORRIDOR,))
    return {r.station_id: (r.name, float(r.offset_km))
            for r in rows.itertuples(index=False) if r.station_id in ids}


def weighted_mean(g: pd.DataFrame) -> float:
    """방문 수로 가중한 평균 — 차 한 대가 겪는 값이다. 칸을 그냥 평균하면
    하루 12대 서는 칸과 650대 서는 칸이 같은 무게를 갖는다."""

    return float((g["ext_per_car"] * g["n"]).sum() / g["n"].sum())


def draw(grids: list[tuple[str, pd.DataFrame]], names: dict[str, tuple[str, float]],
         out: Path) -> None:
    """휴게소 × 시간대 요금 신호. **발산 색 · 로그 배율** — 1배가 가운데 회색.

    ## 왜 분/대 가 아니라 **배율**을 칠하나

    값이 0 에서 694분/대까지 간다. 분을 그대로 선형으로 칠하면 바닥 평균(78)
    아래가 전부 같은 색으로 뭉개져서 "거의 다 공짜" 로 읽힌다 — 그런데 할인
    정책이 묻는 것은 바로 그 아래쪽이다 (**어디를 깎나**).

    그래서 바닥 평균 대비 **배율**을 로그 축으로 칠한다. "평균의 절반" 과
    "평균의 두 배" 가 중앙에서 같은 거리에 놓인다. 요금은 곱셈이므로 이게 맞다.
    색 눈금이 그대로 할인·할증 배수로 읽힌다 (0.1배 … 1배 … 8배).

    ## 왜 발산인가

    묻는 것이 크기가 아니라 **부호**다. 평균보다 비싼 칸(할증)과 싼 칸(할인)이
    한눈에 갈려야 정책이 읽힌다. 순차 색이면 "다 비싸 보인다" 가 된다.
    가운데는 회색이다 — 색상이 들어가면 그 자체가 뜻을 갖는다.
    """

    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    from evdt.viz.plots import _korean_font

    _korean_font(plt)

    base = weighted_mean(grids[0][1])             # 바닥(MNL) 평균 = 배율 1.0
    LO, HI = -1.0, 1.0                            # 0.1배 ~ 10배 (log10)
    # 0 분/대인 칸(아무도 안 밀었다)은 로그가 -inf 다. 눈금 바닥으로 보낸다
    FLOOR = base * 10 ** LO

    # 발산: 파랑 ← 회색 → 빨강. 가운데는 **색상이 없다** (dataviz 규칙)
    cmap = LinearSegmentedColormap.from_list(
        "discount", ["#1b4f8a", "#5b9bd5", "#bcd4ea", "#e8e8e6",
                     "#f4c4b4", "#dd7a5a", "#a32d1f"])
    cmap.set_bad("#f5f5f3")                       # 그 시간대에 아무도 안 선 칸
    norm = Normalize(vmin=LO, vmax=HI)

    order = sorted(names, key=lambda s: names[s][1])
    hours = sorted({int(h) for _, g in grids for h in g["hb"]})

    # 제목 세 줄이 들어갈 자리를 **먼저** 비운다. bbox_inches="tight" 에 맡기고
    # y>1 로 띄우면 글이 겹친다 (전 판본에서 실제로 겹쳤다)
    fig, axes = plt.subplots(1, len(grids), figsize=(6.6 * len(grids), 7.4),
                             sharey=True)
    axes = [axes] if len(grids) == 1 else list(axes)
    fig.subplots_adjust(top=0.845, bottom=0.075, left=0.095, right=0.885,
                        wspace=0.07)

    n_blank = 0
    for ax, (label, g) in zip(axes, grids, strict=True):
        m = (g.pivot(index="station_id", columns="hb", values="ext_per_car")
              .reindex(index=order, columns=hours))
        ratio = np.clip(m.to_numpy(dtype=float), FLOOR, None) / base
        z = np.log10(np.ma.masked_invalid(ratio))
        n_blank += int(np.ma.count_masked(z))
        im = ax.imshow(z, aspect="auto", cmap=cmap, norm=norm,
                       interpolation="nearest")

        # ★ 색만으로 읽히지 않게 — 3배 넘는 칸은 배수를 직접 적는다.
        #   발표에서 "저기 빨간 칸" 이 아니라 "칠곡 8시, 8배" 로 말하게 된다
        for i in range(z.shape[0]):
            for j in range(z.shape[1]):
                if np.ma.is_masked(z[i, j]) or ratio[i, j] < 3.0:
                    continue
                ax.text(j, i, f"{ratio[i, j]:.0f}×", ha="center", va="center",
                        fontsize=7.5, color="white")

        ax.set_xticks(range(len(hours)))
        ax.set_xticklabels([f"{h}시" for h in hours], fontsize=9)
        ax.set_title(f"{label}   평균 {weighted_mean(g):.0f}분/대",
                     fontsize=12.5, loc="left", pad=8)
        ax.set_xlabel("도착 시각", fontsize=10)
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.tick_params(length=0)

    axes[0].set_yticks(range(len(order)))
    axes[0].set_yticklabels([names[s][0] for s in order], fontsize=9)

    ticks = [-1, -np.log10(4), -np.log10(2), 0, np.log10(2), np.log10(4), 1]
    cax = fig.add_axes((0.905, 0.075, 0.015, 0.73))
    cb = fig.colorbar(im, cax=cax, ticks=ticks)
    cb.ax.set_yticklabels(["0.1배\n이하", "0.25배", "0.5배", "1배\n바닥 평균",
                           "2배", "4배", "10배"], fontsize=8.5)
    cb.outline.set_visible(False)
    cb.ax.tick_params(length=0)
    # 세로로 돌린 글씨는 슬라이드에서 안 읽힌다 — 막대 **위**에 가로로 적는다
    cb.ax.set_title("바닥 평균\n대비 배율", fontsize=9, pad=8, color="#444")

    blank = "  ·  회색은 그 시간대에 아무도 서지 않은 칸" if n_blank else ""
    fig.suptitle("할인·할증을 어디에, 언제 걸어야 하나 — 휴게소 × 시간대 요금 신호",
                 fontsize=15.5, x=0.012, ha="left", y=0.985, va="top")
    fig.text(0.012, 0.940,
             f"한 대가 더 설 때 남들에게 얹는 시간. 바닥(엔진 없음) 평균 "
             f"{base:.0f}분/대 = 1배{blank}",
             fontsize=10, color="#444", ha="left", va="top")
    fig.text(0.012, 0.910,
             "붉을수록 크다 → 할증  ·  푸를수록 작다 → 할인  ·  "
             "한계값이라 더할 수 없다 — 칸끼리의 순서와 비율로만 읽는다",
             fontsize=10, color="#777", ha="left", va="top")

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor="white")
    plt.close(fig)
    print(f"\n  저장: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    ap.add_argument("--top", type=int, default=8, help="표에 찍을 칸 수")
    ap.add_argument("--out", default="docs/figures/discount_chuseok.png")
    args = ap.parse_args()

    src = Sources(SCENARIO, seeds=args.seeds,
                  config="config/scenario_chuseok_up_congested.yaml")
    src.column("externality_*.csv:ext_min",
               "그 정차 하나를 빼면 **남은 차들**의 종료 시각이 얼마나 당겨지나")
    src.note("단위", f"휴게소 × {HOUR_BIN}시간 묶음", "요금으로 손댈 수 있는 가장 잔 단위")

    grids, all_ids = [], set()
    for label, pattern in ARMS:
        t, paths = load_arm(pattern, args.seeds)
        src.files(label.split(" ")[0], paths, label)
        g = fold(t)
        grids.append((label, g))
        all_ids |= set(g["station_id"])

    names = station_names(all_ids)
    missing = all_ids - set(names)
    if missing:
        raise SystemExit(f"[중단] DB 에 없는 휴게소: {sorted(missing)[:3]}")

    # -- 휴게소 단위 요금표 ---------------------------------------------------
    for label, g in grids:
        per_st = (g.groupby("station_id")
                   .apply(lambda d: pd.Series({
                       "ext_per_car": (d["ext_per_car"] * d["n"]).sum() / d["n"].sum(),
                       "n": d["n"].sum()}), include_groups=False)
                   .sort_values("ext_per_car", ascending=False))
        avg = (per_st["ext_per_car"] * per_st["n"]).sum() / per_st["n"].sum()
        print(f"\n=== {label} — 휴게소 요금 신호 (평균 {avg:.1f}분/대) ===")
        print(f"  {'휴게소':<12}{'분/대':>9}{'평균 대비':>10}{'하루 방문':>10}  정책")
        for sid, r in per_st.head(args.top).iterrows():
            ratio = r.ext_per_car / avg if avg else float("nan")
            tag = "할증" if ratio > 1.2 else ("할인" if ratio < 0.8 else "그대로")
            print(f"  {names[sid][0]:<12}{r.ext_per_car:>9.1f}{ratio:>9.2f}배"
                  f"{r.n:>10.0f}  {tag}")
        cheap = per_st.tail(3).iloc[::-1]
        print(f"  {'…':<12}")
        for sid, r in cheap.iterrows():
            ratio = r.ext_per_car / avg if avg else float("nan")
            print(f"  {names[sid][0]:<12}{r.ext_per_car:>9.1f}{ratio:>9.2f}배"
                  f"{r.n:>10.0f}  할인")

    # -- 가장 비싼 칸 (휴게소 × 시간대) ---------------------------------------
    label0, g0 = grids[0]
    hot = g0.sort_values("ext_per_car", ascending=False).head(args.top)
    base_avg = (g0["ext_per_car"] * g0["n"]).sum() / g0["n"].sum()
    print(f"\n=== {label0} — 가장 비싼 칸 (휴게소 × {HOUR_BIN}시간) ===")
    print(f"  {'휴게소':<12}{'시간대':>10}{'분/대':>9}{'평균 대비':>10}{'방문':>8}")
    for r in hot.itertuples(index=False):
        print(f"  {names[r.station_id][0]:<12}"
              f"{f'{int(r.hb)}–{int(r.hb) + HOUR_BIN}시':>10}"
              f"{r.ext_per_car:>9.1f}{r.ext_per_car / base_avg:>9.2f}배{r.n:>8.0f}")

    # -- 엔진이 돌면 요금 신호가 얼마나 평평해지나 -----------------------------
    if len(grids) == 2:
        (_, a), (_, b) = grids
        ma, mb = weighted_mean(a), weighted_mean(b)
        print("\n=== 엔진이 돌면 요금 신호가 어떻게 되나 ===")
        for nm, g, mu in (("MNL (엔진 없음)", a, ma), ("S4 (엔진 있음)", b, mb)):
            top = g.sort_values("ext_per_car", ascending=False).iloc[0]
            print(f"  {nm:<16} 평균 {mu:>6.1f}분/대 · 가장 비싼 칸 "
                  f"{top.ext_per_car:>6.1f} ({names[top.station_id][0]} "
                  f"{int(top.hb)}–{int(top.hb) + HOUR_BIN}시)")

        # ★ "엔진이 다 해결한다" 로 읽히면 안 된다. 수준은 내려가지만 **남은
        #   쏠림은 한 곳에 더 몰린다** — 그 곳이 요금이 손댈 자리다
        sa = (a.groupby("station_id")
               .apply(lambda d: weighted_mean(d), include_groups=False))
        sb = (b.groupby("station_id")
               .apply(lambda d: weighted_mean(d), include_groups=False))
        print(f"\n  ① 수준     평균 {ma:.0f} → {mb:.0f}분/대 "
              f"({(1 - mb / ma) * 100:.0f}% 감소) — 엔진이 혼잡 자체를 줄인다")
        print(f"  ② 쏠림     휴게소 최대/평균 {sa.max() / ma:.1f}배 → "
              f"{sb.max() / mb:.1f}배 — **더 한 곳으로 몰린다**")
        print(f"  ③ 그 한 곳  MNL {names[sa.idxmax()][0]} → S4 {names[sb.idxmax()][0]}")
        print("\n  → 엔진과 요금은 **서로 다른 곳을 고친다**. 엔진이 돌고도 남는 칸이"
              "\n    할인·할증이 손댈 진짜 자리다.")

    draw(grids, names, Path(args.out))
    src.print()

    print("\n  ⚠ 이 표의 숫자는 **더하면 안 된다** (한계값 · externality_rank.py 머리말).")
    print("    '칠곡이 옥천의 n배' 는 되고 '전부 더하면 n시간' 은 안 된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
