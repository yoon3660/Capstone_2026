"""통행거리 분포가 실측과 맞는지 본다 (#82 B).

    python scripts/validate_trip_length.py
    python scripts/validate_trip_length.py --od data/processed/tcs_od_gyeongbu.parquet

## 왜 이게 결론의 절반인가

우리 결론은 *"진입 SoC 66.5% 면 약 190 km 를 가는데, 경부선 통행 대부분은 그보다
짧아서 충전이 필요 없다"* 다. **"통행 대부분이 짧다" 가 맞아야 성립한다.**

그리고 #82 A 에서 모순이 드러났다 — 충전 **시작** SoC 를 맞추면 **종료** 가 어긋나고,
반대도 마찬가지다. 실측 "30% → 85%" 는 *같은 차가 30% 까지 떨어졌다가 85% 까지* 인데,
그러려면 **그만큼 달려야** 한다. **우리 통행이 짧아서 둘 다 성립할 수 없다.**

→ 모순의 뿌리가 충전 규칙이 아니라 **통행거리**다. 여기가 맞는지를 먼저 봐야 한다.

## 지금 통행거리는 어디서 오나

실측 OD 가 아니라 **콘존 교통량의 증감으로 추정**한 것이다 (#54). 교통량이 늘어나는
구간은 진입, 줄어드는 구간은 진출로 보고 진입·진출 비율을 만든다.

실측 TCS OD(#61)가 들어왔으니 **대조할 수 있다.**

> ⚠ **동기화가 아니라 대조다.** OD 로 수요를 다시 만들지 않는다 — 그건 #63 이다.
> 여기서는 **우리 추정이 실측과 얼마나 맞는지**만 본다.

## OD 가 없으면

`--od` 가 가리키는 파일이 없으면 **우리 쪽 분포만 찍고 끝낸다.** 원본 CSV 는
`data/raw/` 라 저장소에 없어서(T-05 원칙) 팀원 로컬에만 있을 수 있다. 그때는 이
스크립트를 먼저 돌려 두고, 파일이 오면 같은 명령에 `--od` 만 붙이면 된다.
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.config import ScenarioConfig  # noqa: E402
from evdt.demand_layers import effective_ev_share  # noqa: E402
from evdt.io.db import get_conn  # noqa: E402
from evdt.io.entry_exit import (  # noqa: E402
    corridor_entry_hourly_from_profile,
    sample_entry_offsets,
    sample_exit_offsets,
)
from evdt.io.od_profile import load_od_profile, od_subset_for  # noqa: E402
from evdt.io.stations import read_station_chargers  # noqa: E402
from evdt.io.synthetic_ev import generate_evs  # noqa: E402
from evdt.io.vehicles import load_from_db  # noqa: E402
from evdt.paths import PROJECT_ROOT, default_db_path  # noqa: E402
from evdt.runner import DEST_STREAM, build_demand, with_seed  # noqa: E402
from evdt.world.sim import station_specs  # noqa: E402

#: 분포를 요약할 분위수. 평균 하나로는 "짧은 통행이 많다" 를 못 본다.
QUANTILES = (5, 10, 25, 50, 75, 90, 95, 99)

#: 이 거리 안쪽이면 진입 SoC 66.5%(약 190 km 주행) 로 충전 없이 간다.
#: 결론이 여기에 달려 있으므로 비율을 따로 센다.
NO_CHARGE_KM = 190.0


def ours(config: str, seed: int) -> pd.Series:
    """우리 수요 생성기가 만드는 통행거리 (진입 → 목적지)."""

    cfg = with_seed(ScenarioConfig.from_yaml(PROJECT_ROOT / config).variant("replay", {}), seed)
    with get_conn(default_db_path(), readonly=True) as conn:
        srows, crows = read_station_chargers(conn, corridor_id=cfg.corridor_id)
        vclasses, curves, temps = load_from_db(conn)
        end_km = float(conn.execute(
            "SELECT length_km FROM corridor WHERE corridor_id = ?", (cfg.corridor_id,)
        ).fetchone()[0])

    keep = {s.station_id for s in station_specs(srows, crows)}
    stations = [r for r in srows if r["station_id"] in keep]
    built, _, _ = build_demand(cfg, stations, vclasses, curves, temps, end_km, root=PROJECT_ROOT)

    # ⚠ `built.trips` 는 **충전이 필요한 차만** 담는다. 그걸로 통행거리를 재면
    # "긴 통행만 남은 표본" 을 보게 되어 **순환논증**이 된다 — 우리가 검증하려는 것이
    # 바로 "짧은 통행이 많아서 충전이 필요 없다" 이기 때문이다.
    #
    # 그래서 **생성기가 만든 전체 진입 EV** 를 쓴다. build_demand 가 그 중간 산출물을
    # 돌려주지 않으므로 생성기를 직접 부른다.
    volume = pd.read_csv(PROJECT_ROOT / cfg.demand.volume_profile)
    if cfg.demand.entry_exit_profile:
        profile = pd.read_csv(PROJECT_ROOT / cfg.demand.entry_exit_profile)
        points = corridor_entry_hourly_from_profile(profile, volume)
        volume = (points.groupby("hour")["entry_veh"].sum()
                  .reindex(range(24), fill_value=0.0).rename("volume_veh").reset_index())

    share = effective_ev_share(cfg.demand.layers, cfg.demand.ev_share)
    cfg2 = dataclasses.replace(cfg, demand=dataclasses.replace(cfg.demand, ev_share=share))
    evs = generate_evs(volume, cfg2, dest_offset_km=end_km)

    rng = np.random.default_rng([cfg.vehicles.seed, DEST_STREAM])
    if cfg.demand.entry_exit_profile:
        hours = ((evs["entry_time_min"] // 60).astype(int) % 24).to_numpy()
        entry = np.zeros(len(evs))
        dest = np.zeros(len(evs))

        # ⚠ runner 와 **같은 분기**여야 한다. 한쪽만 OD 를 쓰면 검증이 모델이 아닌
        # 것을 재게 된다 — #97 이 바로 그 실패였다
        od = None
        if cfg.demand.od_profile:
            p, dr = od_subset_for(cfg)
            od = load_od_profile(PROJECT_ROOT / cfg.demand.od_profile, p, dr)
            print(f"  목적지: 실측 OD  period={p} · direction={dr}  (#99)")
        else:
            print("  목적지: exit_share 위험률 모델 (od_profile 없음)")

        # 맞추기 **전**의 진입 지점. 맞춘 값으로 재면 거리가 0 으로 나온다
        raw_entry = np.zeros(len(evs))

        for hour in np.unique(hours):
            pick = hours == hour
            here = sample_entry_offsets(int(pick.sum()), int(hour), points, rng)
            raw_entry[pick] = here
            if od is None:
                entry[pick] = here
                dest[pick] = sample_exit_offsets(here, int(hour), profile, rng,
                                                 corridor_end_km=end_km)
            else:
                entry[pick], dest[pick] = od.sample(here, rng)

        if od is not None:
            print("  " + od.describe_snap(raw_entry))
    else:
        entry = np.zeros(len(evs))
        dest = np.full(len(evs), end_km)

    charging = len(built.trips)
    print(f"  (진입 EV {len(evs):,}건 중 충전 필요 {charging:,}건 — "
          f"**전체 진입 EV** 로 잰다)")
    return pd.Series(dest - entry, name="ours")


def od_subset(config: str) -> tuple[str, str]:
    """이 시나리오와 **같은 기간·같은 방향**의 OD 만 쓴다 (#97).

    전부 합쳐 쓰면 **설 연휴 시나리오를 설 + 평시를 섞은 것과 비교**한다. 그러면
    상행과 하행의 "실측" 열이 똑같은 숫자로 나오는데, 그게 틀렸다는 신호였다.

    실제로 재 보면 기간은 영향이 있고 방향은 거의 없다 — 그래도 둘 다 거른다.
    "거의 없다" 는 지금 자료에서 그렇다는 것이고, 바뀌면 조용히 틀리기 때문이다.

        설   평균 37.3km (DOWN 37.2 · UP 37.5)   <190km 98.1%
        평시 평균 33.6km                          <190km 98.8%
    """

    name = Path(config).stem
    period = "normal" if "base" in name or "weekend" in name else "holiday"
    direction = "UP" if "_up" in name else "DOWN"
    return period, direction


def measured(path: Path, period: str, direction: str) -> pd.Series | None:
    """실측 TCS OD 의 통행거리. 없으면 None."""

    if not path.exists():
        return None
    od = pd.read_parquet(path)

    # 기간·방향을 거른다. 열이 없으면 거르지 않고 **그 사실을 말한다**
    for col, want in (("period", period), ("direction", direction)):
        if col not in od.columns:
            print(f"  ⚠ OD 에 {col} 열이 없어 {col} 를 거르지 못했다 — 전체를 쓴다")
            continue
        have = set(od[col].astype(str).unique())
        if want not in have:
            raise SystemExit(
                f"\n[중단] OD 의 {col} 에 {want!r} 가 없다. 있는 값: {sorted(have)}\n"
                "  od_subset() 의 시나리오→OD 대응을 확인하라 (#97)."
            )
        od = od[od[col].astype(str) == want]

    if od.empty:
        raise SystemExit(f"\n[중단] {period}·{direction} 에 해당하는 OD 행이 없다.")

    cols = {c.lower(): c for c in od.columns}
    start = next((cols[c] for c in cols if "start" in c and "offset" in c), None)
    end = next((cols[c] for c in cols if "end" in c and "offset" in c), None)
    vol = next((cols[c] for c in cols if "volume" in c or c == "veh"), None)
    if not (start and end and vol):
        raise SystemExit(
            f"\n[중단] {path} 에서 거리 열을 못 찾았다.\n"
            f"  있는 열: {list(od.columns)}\n"
            "  start/end offset 과 volume 에 해당하는 열 이름을 확인하고 이 스크립트를 고쳐라."
        )
    dist = (od[end].astype(float) - od[start].astype(float)).abs()
    # 교통량 가중 — OD 한 행은 통행 한 건이 아니라 여러 대다
    return pd.Series(np.repeat(dist.to_numpy(), od[vol].astype(int).clip(lower=0).to_numpy()),
                     name="measured")


def describe(s: pd.Series) -> dict[str, float]:
    out = {f"p{q}": float(np.percentile(s, q)) for q in QUANTILES}
    out["mean"] = float(s.mean())
    out[f"<{NO_CHARGE_KM:.0f}km"] = float((s < NO_CHARGE_KM).mean())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/scenario_seollal_down.yaml")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--od", type=Path,
                    default=PROJECT_ROOT / "data/processed/tcs_od_gyeongbu.parquet")
    args = ap.parse_args()

    mine = ours(args.config, args.seed)
    a = describe(mine)

    period, direction = od_subset(args.config)
    print(f"\n{args.config}  seed={args.seed}  통행 {len(mine):,}건")
    print(f"실측 OD 부분집합: period={period} · direction={direction}  (#97)")
    print("\n=== 통행거리 (km) ===")
    real = measured(args.od, period, direction)

    if real is None:
        for k, v in a.items():
            print(f"  {k:<10} {v:8.1%}" if k.startswith("<") else f"  {k:<10} {v:8.1f}")
        print(f"\n⚠ 실측 OD 가 없다: {args.od}")
        print("  data/raw/ 는 저장소에서 제외되는 경로라(T-05) 팀원 로컬에만 있을 수 있다.")
        print("  받으면:  python scripts/build_tcs_od.py && python scripts/build_tcs_od_gyeongbu.py")
        print("  그 다음 같은 명령을 다시 돌리면 대조까지 나온다.")
        return 0

    b = describe(real)
    print(f"{'':12}{'우리':>10}{'실측':>10}{'차이':>10}")
    for k in a:
        if k.startswith("<"):
            print(f"  {k:<10}{a[k]:9.1%}{b[k]:10.1%}{(a[k] - b[k]) * 100:+9.1f}%p")
        else:
            d = a[k] - b[k]
            print(f"  {k:<10}{a[k]:9.1f}{b[k]:10.1f}{d:+9.1f}")

    # 분포가 얼마나 다른가 — 평균만 맞고 모양이 다를 수 있다
    mape = float(np.mean([abs(a[f"p{q}"] - b[f"p{q}"]) / max(b[f"p{q}"], 1e-9)
                          for q in QUANTILES])) * 100
    print(f"\n  분위수 MAPE {mape:.1f}%   (교통량 검증은 12.1% 였다)")

    gap = (a[f"<{NO_CHARGE_KM:.0f}km"] - b[f"<{NO_CHARGE_KM:.0f}km"]) * 100
    print(f"\n  결론에 직접 걸리는 값 — {NO_CHARGE_KM:.0f}km 미만 비율이 {gap:+.1f}%p")
    if abs(gap) > 10:
        print("  ❌ 우리 통행이 실측보다 " + ("짧다" if gap > 0 else "길다") + ".")
        print("     '대부분 짧아서 충전이 필요 없다' 는 결론을 다시 봐야 한다.")
        return 1
    print("  ✅ 결론을 떠받칠 만큼 맞는다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
