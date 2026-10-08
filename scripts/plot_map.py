"""스냅샷 → 지도 위 시간 애니메이션 (#48).

    python scripts/plot_map.py <run_id>
    python scripts/plot_map.py <run_id> --vs <비교 run_id>   # 좌우로 나란히
    python scripts/plot_map.py <run_id> --out map.html

휴게소를 점으로 찍고 **대기시간으로 색**, **큐 길이로 크기**를 준다. 시간 슬라이더를
끌면 빨개졌다 흩어지는 것이 보인다.

## 왜 이게 필요한가

히트맵(시간 × 거리)은 정확하지만 **공간을 모른다** — 어느 휴게소가 어디쯤인지, 옆
휴게소와 얼마나 떨어져 있는지가 안 보인다. 지도 위에서는 "앞쪽 다섯 곳만 빨갛고 뒤는
비어 있다" 가 한눈에 들어온다. #59 의 가동률 격차(앞 78~98% vs 뒤 7~33%)를 설명하는
데는 이쪽이 훨씬 빠르다.

## 설계

**의존성을 늘리지 않는다.** folium·plotly 를 쓰지 않고 Leaflet 을 CDN 에서 불러오는
HTML 한 장을 만든다. 데이터는 JSON 으로 파일 안에 박으므로 **열면 바로 돈다**
(서버·네트워크 불필요, 지도 타일만 온라인).

스냅샷 계약(설계 규칙 4)만 읽는다 — `(t_min, entity_type, entity_id, lat, lon,
state, value)`. 시뮬레이터가 무엇을 돌렸는지(UE·S0·S1) 몰라도 된다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.io.db import get_conn  # noqa: E402
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path  # noqa: E402

#: 대기시간 → 색. 히트맵과 같은 구간을 쓴다 (docs/experiment.md).
BANDS = [(0, "#2c7fb8"), (5, "#7fcdbb"), (15, "#fecc5c"), (30, "#fd8d3c"), (60, "#e31a1c")]


def load_snapshot(run_id: str, runs_dir: Path) -> pd.DataFrame:
    path = runs_dir / run_id / "snapshot.parquet"
    if not path.exists():
        raise SystemExit(
            f"\n[중단] 스냅샷이 없다: {path}\n"
            "  config 의 output.write_snapshots 가 true 인지 확인하고 다시 돌려라."
        )
    snap = pd.read_parquet(path)
    snap = snap[snap["entity_type"] == "station"]
    wide = snap.pivot_table(index=["t_min", "entity_id", "lat", "lon"],
                            columns="state", values="value").reset_index()
    for col in ("wait_min", "queue_len", "chargers_busy", "chargers_total"):
        if col not in wide:
            wide[col] = 0.0
    return wide


def station_names(ids: list[str]) -> dict[str, str]:
    with get_conn(default_db_path(), readonly=True) as conn:
        rows = conn.execute(
            f"SELECT station_id, name, offset_km FROM station WHERE station_id IN "  # noqa: S608
            f"({','.join('?' * len(ids))})", ids,
        ).fetchall()
    return {r[0]: {"name": r[1], "km": r[2]} for r in rows}


def centerline(root: Path) -> list[list[float]]:
    """노선 좌표. 점이 4천 개라 20개마다 하나만 쓴다 (지도에서 차이가 안 보인다)."""
    path = root / "data/processed/centerline_gyeongbu.parquet"
    if not path.exists():
        return []
    line = pd.read_parquet(path)
    lat = next((c for c in line.columns if "lat" in c.lower()), None)
    lon = next((c for c in line.columns if "lon" in c.lower() or "lng" in c.lower()), None)
    if lat is None or lon is None:
        return []
    return line.iloc[::20][[lat, lon]].round(5).values.tolist()


def frames(wide: pd.DataFrame, meta: dict) -> tuple[list[float], list[list]]:
    """시각별 [휴게소, 대기, 큐, 점유율] 묶음. 파일 크기를 줄이려 배열로 보낸다."""
    times = sorted(wide["t_min"].unique())
    order = sorted(wide["entity_id"].unique(), key=lambda s: meta.get(s, {}).get("km", 0.0))
    idx = {s: i for i, s in enumerate(order)}
    out = [[None] * len(order) for _ in times]
    tpos = {t: i for i, t in enumerate(times)}
    for r in wide.itertuples(index=False):
        busy, total = float(r.chargers_busy), float(r.chargers_total)
        out[tpos[r.t_min]][idx[r.entity_id]] = [
            round(float(r.wait_min), 1), int(r.queue_len),
            round(busy / total, 2) if total else 0.0,
        ]
    stations = [{"id": s, "name": meta.get(s, {}).get("name", s),
                 "km": round(float(meta.get(s, {}).get("km", 0.0)), 1),
                 "lat": float(wide[wide.entity_id == s].iloc[0]["lat"]),
                 "lon": float(wide[wide.entity_id == s].iloc[0]["lon"])} for s in order]
    return times, out, stations


HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body{{margin:0;font-family:system-ui,'Malgun Gothic',sans-serif;background:#fafafa;color:#1a1a1a}}
  header{{padding:14px 18px 8px}}
  h1{{margin:0;font-size:18px}} .sub{{color:#666;font-size:13px;margin-top:4px}}
  #bar{{display:flex;align-items:center;gap:14px;padding:8px 18px 12px}}
  #slider{{flex:1}} #clock{{font-variant-numeric:tabular-nums;font-weight:700;font-size:20px;min-width:70px}}
  button{{font-size:15px;padding:4px 12px;cursor:pointer}}
  .maps{{display:flex;gap:10px;padding:0 18px 12px}}
  .pane{{flex:1}} .pane h2{{margin:0 0 6px;font-size:14px}}
  .map{{height:{h}px;border:1px solid #ddd;border-radius:6px}}
  #legend{{padding:4px 18px 18px;color:#555;font-size:12px}}
  .sw{{display:inline-block;width:11px;height:11px;border-radius:50%;margin:0 4px 0 12px;vertical-align:-1px}}
</style></head><body>
<header><h1>{title}</h1><div class="sub">{sub}</div></header>
<div id="bar">
  <button id="play">▶ 재생</button>
  <input id="slider" type="range" min="0" max="0" value="0">
  <span id="clock">00:00</span>
</div>
<div class="maps">{panes}</div>
<div id="legend">
  점 <b>색 = 평균 대기</b>, <b>크기 = 줄 선 차</b>.
  <span class="sw" style="background:#2c7fb8"></span>0분
  <span class="sw" style="background:#7fcdbb"></span>5분+
  <span class="sw" style="background:#fecc5c"></span>15분+
  <span class="sw" style="background:#fd8d3c"></span>30분+
  <span class="sw" style="background:#e31a1c"></span>60분+
</div>
<script>
const DATA = {data};
const BANDS = {bands};
function colour(w){{ let c = BANDS[0][1]; for (const [t,v] of BANDS) if (w >= t) c = v; return c; }}
function radius(q){{ return 6 + Math.min(Math.sqrt(q) * 2.2, 22); }}

const panes = DATA.panes.map((p, i) => {{
  const map = L.map('map' + i, {{zoomControl: i === 0}}).setView([36.3, 127.6], 7);
  // CARTO 는 API 키를 요구한다. OSM 기본 타일은 키 없이 된다
  L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{attribution:'&copy; OpenStreetMap', maxZoom:18}}).addTo(map);
  if (DATA.line.length) L.polyline(DATA.line, {{color:'#999', weight:3, opacity:.7}}).addTo(map);
  const marks = p.stations.map(s => L.circleMarker([s.lat, s.lon],
      {{radius:7, color:'#fff', weight:1.5, fillOpacity:.9, fillColor:BANDS[0][1]}})
    .bindTooltip(s.name + ' (' + s.km + 'km)', {{direction:'top'}}).addTo(map));
  return {{map, marks, p}};
}});
// 두 지도를 같이 움직인다 — 같은 시각 같은 자리를 보지 않으면 비교가 안 된다
if (panes.length > 1) {{
  let lock = false;
  panes.forEach(a => a.map.on('move', () => {{
    if (lock) return; lock = true;
    panes.forEach(b => {{ if (b !== a) b.map.setView(a.map.getCenter(), a.map.getZoom(), {{animate:false}}); }});
    lock = false;
  }}));
}}

const slider = document.getElementById('slider'), clock = document.getElementById('clock');
slider.max = DATA.times.length - 1;
function draw(i) {{
  const t = DATA.times[i];
  clock.textContent = String(Math.floor(t / 60) % 24).padStart(2,'0') + ':' + String(Math.round(t % 60)).padStart(2,'0');
  panes.forEach(pane => pane.marks.forEach((m, k) => {{
    const v = pane.p.frames[i][k];
    if (!v) return;
    m.setStyle({{fillColor: colour(v[0])}});
    m.setRadius(radius(v[1]));
    m.setTooltipContent(pane.p.stations[k].name + ' (' + pane.p.stations[k].km + 'km)<br>'
      + '대기 ' + v[0] + '분 · 줄 ' + v[1] + '대 · 충전기 ' + Math.round(v[2]*100) + '%');
  }}));
}}
slider.oninput = () => draw(+slider.value);
let timer = null;
document.getElementById('play').onclick = function () {{
  if (timer) {{ clearInterval(timer); timer = null; this.textContent = '▶ 재생'; return; }}
  this.textContent = '⏸ 멈춤';
  timer = setInterval(() => {{
    const next = (+slider.value + 1) % DATA.times.length;
    slider.value = next; draw(next);
  }}, 120);
}};
draw(0);
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id")
    ap.add_argument("--vs", default=None, help="나란히 비교할 run_id")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    args = ap.parse_args()

    run_ids = [args.run_id] + ([args.vs] if args.vs else [])
    panes_html, panes_data = [], []
    for i, rid in enumerate(run_ids):
        wide = load_snapshot(rid, args.runs_dir)
        meta = station_names(sorted(wide["entity_id"].unique()))
        times, fr, stations = frames(wide, meta)
        panes_data.append({"frames": fr, "stations": stations})
        panes_html.append(f'<div class="pane"><h2>{rid}</h2><div id="map{i}" class="map"></div></div>')

    data = {"times": times, "line": centerline(PROJECT_ROOT), "panes": panes_data}
    out = args.out or (args.runs_dir / run_ids[0] / "map.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        HTML.format(
            title="휴게소 충전 대기 — 시간에 따라",
            sub=" vs ".join(run_ids),
            panes="".join(panes_html),
            data=json.dumps(data, separators=(",", ":")),
            bands=json.dumps(BANDS),
            h=560 if len(run_ids) == 1 else 520,
        ),
        encoding="utf-8",
    )
    size = out.stat().st_size / 1024
    print(f"저장 {out}  ({size:,.0f} KB · 휴게소 {len(panes_data[0]['stations'])}곳 · "
          f"{len(times)} 프레임)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
