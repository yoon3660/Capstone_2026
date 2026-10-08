"""스냅샷 → 코리도 지도 GIF. **타일·API·네트워크가 필요 없다.**

    python scripts/animate_map.py <run_id>
    python scripts/animate_map.py <S0 run_id> --vs <S1 run_id>
    python scripts/animate_map.py <run_id> --from 720 --to 1320 --every 1

## 왜 GIF 인가

`plot_map.py` 가 만드는 Leaflet HTML 은 **지도 타일을 온라인에서 받는다.** 저장소
밖에서 링크로 열면 타일이 안 와서 **지도가 안 보인다** — 받는 쪽 네트워크·차단
정책에 달려 있다.

여기서는 노선을 **우리가 가진 중심선 좌표**로 직접 그린다. 파일 하나로 어디서나
재생되고 발표 자료에 그대로 붙는다. 배경 지형은 없지만, 이 그림이 답하는 질문은
*"어느 휴게소가 빨개지고 어떻게 흩어지나"* 라서 지형이 필요 없다.

mp4 는 ffmpeg 이 있어야 한다. 지금 환경에 없어서 **GIF 만 만든다** — 의존성을
늘리지 않는다는 원칙(plot_map.py)과 같다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path  # noqa: E402
from evdt.viz.map_anim import animate, load_centerline, to_panel  # noqa: E402


def load_snapshot(run_id: str, runs_dir: Path) -> pd.DataFrame:
    path = runs_dir / run_id / "snapshot.parquet"
    if not path.exists():
        raise SystemExit(
            f"\n[중단] 스냅샷이 없다: {path}\n"
            "  config 의 output.write_snapshots 가 true 인지 확인하고 다시 돌려라."
        )
    return pd.read_parquet(path)


def station_meta(ids: list[str]) -> dict[str, dict]:
    with get_conn(default_db_path(), readonly=True) as conn:
        rows = conn.execute(
            f"SELECT station_id, name, offset_km FROM station WHERE station_id IN "  # noqa: S608
            f"({','.join('?' * len(ids))})", ids,
        ).fetchall()
    return {r[0]: {"name": r[1], "km": r[2]} for r in rows}


def demand_label(run_id: str) -> str:
    """가정 레이어 라벨. **모든 그림 부제에 찍는다** (#54).

    그림만 보고도 재현인지 가정인지 알 수 있어야 한다. 라벨은 `meta.json` 이 아니라
    DB 의 `run.params_json` 에 있다 (`demand_layers`).
    """

    with get_conn(default_db_path(), readonly=True) as conn:
        row = conn.execute(
            "SELECT params_json FROM run WHERE run_id = ?", (run_id,)).fetchone()

    if row is None:
        # DB 에 없으면 **빈 부제로 넘어가지 않는다** — 가정이 얹혔는지 모른 채로
        # 그림이 돌아다니면 재현과 가정이 섞인다
        raise SystemExit(
            f"\n[중단] run 이 DB 에 없다: {run_id}\n"
            "  가정 레이어 라벨을 못 읽으면 그림 부제가 비어 버린다 (#54)."
        )

    return str(json.loads(row[0] or "{}").get("demand_layers", ""))


def stage_of(run_id: str) -> str:
    """run_id 에서 단계만 꺼낸다 — `<scenario>__<stage>__p<..>__s<..>`."""
    parts = run_id.split("__")
    return parts[1] if len(parts) > 1 else run_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--vs", help="나란히 비교할 run_id (척도를 공유한다)")
    ap.add_argument("--out", type=Path, help="기본값: docs/figures/map_<...>.gif")
    ap.add_argument("--every", type=int, default=2, help="프레임 솎기 (기본 2 = 10분 간격)")
    ap.add_argument("--from", dest="t_from", type=float, help="시작 t_min")
    ap.add_argument("--to", dest="t_to", type=float, help="끝 t_min")
    ap.add_argument("--fps", type=int, default=10)
    args = ap.parse_args()

    run_ids = [args.run_id] + ([args.vs] if args.vs else [])
    snaps = [load_snapshot(r, RUNS_DIR) for r in run_ids]

    ids = sorted({str(x) for s in snaps
                  for x in s.loc[s["entity_type"] == "station", "entity_id"].unique()})
    meta = station_meta(ids)

    panels = [to_panel(s, stage_of(r), meta) for s, r in zip(snaps, run_ids, strict=True)]

    line = load_centerline(PROJECT_ROOT / "data/processed/centerline_gyeongbu.parquet")

    if args.out:
        out = args.out
    elif args.vs:
        out = PROJECT_ROOT / f"docs/figures/map_{stage_of(args.vs)}_vs_{stage_of(args.run_id)}.gif"
    else:
        out = PROJECT_ROOT / f"docs/figures/map_{args.run_id}.gif"

    title = " vs ".join(stage_of(r) for r in run_ids) if args.vs else args.run_id
    written = animate(
        panels, line, out,
        title=f"경부선 휴게소 충전 대기 — {title}",
        subtitle=demand_label(args.run_id),
        every=args.every, t_from=args.t_from, t_to=args.t_to, fps=args.fps,
    )

    size_mb = written.stat().st_size / 1e6
    print(f"\n{written}  ({size_mb:.1f} MB)")
    if size_mb > 15:
        print("  ⚠ 큰 파일이다. --every 를 올리거나 --from/--to 로 구간을 좁혀라.")
    print("  타일·API 없이 열린다 — 발표 자료에 그대로 붙여도 된다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
