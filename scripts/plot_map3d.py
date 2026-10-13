"""경부선 코리도를 입체 리본으로 — 3D 바닥 (#88).

    python scripts/plot_map3d.py
    python scripts/plot_map3d.py --out docs/figures/corridor3d.html

**아직 아무 데이터도 안 올린다 — 바닥만 만든다.** 휴게소 기둥은 #89, 도로 혼잡은
#90 이 이 위에 올라탄다.

## 설계 규칙

- **뷰어는 엔진을 모른다.** 이 파일은 `evdt.engine` 을 임포트하지 않는다 (#87).
  여기서 읽는 것은 중심선 좌표와 휴게소 위치뿐이다
- **빌드 단계를 만들지 않는다.** deck.gl 을 CDN 으로 쓰고 데이터를 HTML 안에 박는다 —
  발표 노트북에서 **파일 하나 열면 돈다** (`plot_map.py` 가 Leaflet 을 쓰는 방식과 같다)
- **지형(TerrainLayer)은 안 한다.** 고도 타일은 API 키를 요구한다. #59 에서 CARTO
  타일이 키를 요구해 OSM 으로 바꾼 적이 있다. 지형은 보기 좋지만 **아무 질문에도
  답하지 않는다** — 발표 전에 키가 필요한 의존을 새로 만들지 않는다

## ⚠ offset 이 방향마다 다른 데서 잰다

중심선 parquet 은 **부산이 0**이고 서울로 가면서 커진다. 그런데 휴게소 `offset_km` 은
**그 방향의 출발지**에서 잰다.

    상행 (부산→서울)   휴게소 offset  ≈  중심선 offset − 0.6
    하행 (서울→부산)   휴게소 offset  ≈  415.058 − 중심선 offset + 0.6

확인한 값 — 양산(상행) 10.2 ↔ 중심선 10.8 · 서울만남(하행) 0.6 ↔ 중심선 415.0 ·
통도사(하행) 384.9 ↔ 중심선 30.7.

**이걸 틀리면 리본이 거꾸로 깔리고 그 위의 모든 레이어가 같이 뒤집힌다.**
그래서 `station_to_centerline()` 한 곳에만 두고, #89·#90 이 그걸 가져다 쓴다.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

import pandas as pd
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.paths import PROJECT_ROOT, default_db_path  # noqa: E402

#: 중심선 parquet 의 offset 0 이 코리도 offset 0 보다 이만큼 앞서 있다 (km).
#: 양쪽 끝에서 따로 재서 둘 다 +0.6 이 나왔다
CENTERLINE_BIAS_KM = 0.6

#: 코리도 총연장 (#51). DB 의 corridor.length_km 와 대조한다
CORRIDOR_KM = 415.058

#: 상·하행 리본을 좌우로 벌리는 거리(m). 코리도 전체(415km)를 한 화면에 놓고도
#: 두 줄로 보여야 하므로 실제 도로폭보다 크게 잡는다 — **축척이 아니라 기호다**.
#: 400m 로 했더니 전체 보기에서 한 줄로 겹쳐 보여 2.5km 로 올렸다 (화면에서 ~3px)
RIBBON_SEP_M = 2_500.0

#: 거리 표지를 몇 km 마다 세우나
MARKER_EVERY_KM = 50

DECK_VERSION = "9.0.27"


def load_centerline(root: Path) -> pd.DataFrame:
    path = root / "data" / "processed" / "centerline_gyeongbu.parquet"
    if not path.is_file():
        raise SystemExit(f"\n[중단] 중심선이 없다: {path}")
    return pd.read_parquet(path).sort_values("offset_km").reset_index(drop=True)


def station_to_centerline(offset_km: float, corridor_id: str) -> float:
    """휴게소 offset → 중심선 offset. **방향마다 기준점이 다르다** (모듈 문서 참고).

    #89·#90 이 이 함수를 가져다 쓴다. 변환을 두 곳에 두면 레이어마다 다른 데
    그려진다.
    """

    if corridor_id.endswith("_up"):
        return float(offset_km) + CENTERLINE_BIAS_KM
    if corridor_id.endswith("_down"):
        return CORRIDOR_KM - float(offset_km) + CENTERLINE_BIAS_KM
    raise ValueError(f"방향을 알 수 없는 corridor_id: {corridor_id}")


def offset_path(cl: pd.DataFrame, metres: float) -> list[list[float]]:
    """중심선을 수직으로 `metres` 만큼 민 경로. 양수가 한쪽, 음수가 반대쪽.

    위경도에서 바로 밀면 위도에 따라 거리가 달라진다. 접선을 **미터 좌표**로
    구해서 법선을 만들고 다시 위경도로 돌린다.
    """

    lat = cl["lat"].to_numpy()
    lon = cl["lon"].to_numpy()
    out: list[list[float]] = []

    for i in range(len(cl)):
        j0, j1 = max(i - 1, 0), min(i + 1, len(cl) - 1)
        mlat = math.radians(lat[i])
        # 접선 (미터)
        dx = (lon[j1] - lon[j0]) * 111_320.0 * math.cos(mlat)
        dy = (lat[j1] - lat[j0]) * 110_540.0
        norm = math.hypot(dx, dy) or 1.0
        # 법선 = 접선을 90도 돌린 것
        nx, ny = -dy / norm, dx / norm
        out.append([
            lon[i] + nx * metres / (111_320.0 * math.cos(mlat)),
            lat[i] + ny * metres / 110_540.0,
        ])
    return out


def markers(cl: pd.DataFrame) -> list[dict]:
    """거리 표지. 격자 대신 이걸 둔다 — **offset_km 은 다른 모든 그림이 쓰는 축**이다."""

    out = []
    for km in range(0, int(cl["offset_km"].max()) + 1, MARKER_EVERY_KM):
        row = cl.iloc[(cl["offset_km"] - km).abs().idxmin()]
        out.append({"position": [float(row.lon), float(row.lat), 0.0], "text": f"{km}km"})
    return out


def stations(db: Path) -> list[dict]:
    with sqlite3.connect(db) as conn:
        df = pd.read_sql(
            "SELECT corridor_id, station_id, name, offset_km, lat, lon FROM station", conn)
    return [{"position": [float(r.lon), float(r.lat), 0.0], "name": str(r["name"]),
             "corridor": str(r.corridor_id), "offset_km": float(r.offset_km)}
            for _, r in df.iterrows()]


def camera(cl: pd.DataFrame) -> dict:
    """코리도 **전체가 보이는** 초기 각도 (완료조건).

    ⚠ **줌은 여기서 정하지 않는다.** 발표 노트북의 창 크기를 알 수 없기 때문이다.
    고정값 6.4 로 박았더니 화면의 1/8 만 찼고, 폭 1280px 로 역산했더니 672px 창에서
    양옆으로 넘쳤다. 그래서 **브라우저가 열릴 때 제 창 크기로 맞춘다**
    (`fitBounds`). 여기서는 방향(bearing)·기울기(pitch)와 **감싸야 할 범위**만 준다.
    """

    lat0, lon0 = float(cl.iloc[0].lat), float(cl.iloc[0].lon)
    lat1, lon1 = float(cl.iloc[-1].lat), float(cl.iloc[-1].lon)
    midlat = (lat0 + lat1) / 2

    # 코리도가 화면을 가로지르도록 베어링을 코리도 방향에 맞춘다
    dx = (lon1 - lon0) * math.cos(math.radians(midlat))
    bearing = (math.degrees(math.atan2(dx, lat1 - lat0)) + 360) % 360

    return {"bearing": bearing - 90, "pitch": 50,
            "bounds": [[float(cl["lon"].min()), float(cl["lat"].min())],
                       [float(cl["lon"].max()), float(cl["lat"].max())]]}


HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"/>
<title>경부선 코리도 3D</title>
<script src="https://unpkg.com/deck.gl@__DECK__/dist.min.js"></script>
<style>
  html,body,#map{margin:0;height:100%;background:#13161c;overflow:hidden}
  #hud{position:absolute;left:14px;top:12px;z-index:9;color:#e8e6e1;
       font:13px/1.55 system-ui,'Malgun Gothic',sans-serif;
       background:rgba(19,22,28,.82);padding:11px 14px;border-radius:7px;max-width:330px}
  #hud b{color:#fff} .k{display:inline-block;width:11px;height:11px;border-radius:2px;
       vertical-align:-1px;margin-right:6px}
  .muted{color:#9b9892;font-size:12px}
</style></head><body>
<div id="map"></div>
<div id="hud">
  <b>경부고속도로 코리도</b> __LEN__ km<br/>
  <span class="k" style="background:rgb(90,160,255)"></span>상행 (부산 → 서울)<br/>
  <span class="k" style="background:rgb(255,140,90)"></span>하행 (서울 → 부산)<br/>
  <span class="k" style="background:rgb(230,230,230)"></span>휴게소 __NSTA__곳<br/>
  <div class="muted" style="margin-top:7px">
    드래그 회전 · 휠 확대 · 두 줄 간격은 기호다(실제 도로폭 아님)<br/>
    바닥만 그린 상태 — 휴게소 기둥은 #89, 도로 혼잡은 #90
  </div>
</div>
<script>
const D = __DATA__;
const {DeckGL, PathLayer, ScatterplotLayer, TextLayer, WebMercatorViewport} = deck;

// 창 크기에 맞춰 코리도 전체를 담는다. 줌을 파일에 박으면 다른 화면에서 깨진다
function fitView() {
  const vp = new WebMercatorViewport({width: innerWidth, height: innerHeight});
  const f = vp.fitBounds(D.camera.bounds, {padding: 70});
  // 베어링을 돌리면 긴 축이 가로로 눕는다 — 그만큼 여유를 더 둔다
  return {longitude: f.longitude, latitude: f.latitude, zoom: f.zoom - 0.35,
          pitch: D.camera.pitch, bearing: D.camera.bearing};
}

const dk = new DeckGL({
  container: 'map',
  initialViewState: fitView(),
  controller: true,
  layers: [
    new PathLayer({id:'up', data:[{path:D.up}], getPath:d=>d.path,
      getColor:[90,160,255], widthMinPixels:2, getWidth:900, parameters:{depthTest:false}}),
    new PathLayer({id:'down', data:[{path:D.down}], getPath:d=>d.path,
      getColor:[255,140,90], widthMinPixels:2, getWidth:900, parameters:{depthTest:false}}),
    new ScatterplotLayer({id:'sta', data:D.stations, getPosition:d=>d.position,
      getFillColor:[245,243,238,230], getLineColor:[19,22,28,255], stroked:true,
      lineWidthMinPixels:1, getRadius:1800, radiusMinPixels:3.5, pickable:true}),
    new TextLayer({id:'km', data:D.markers, getPosition:d=>d.position, getText:d=>d.text,
      getSize:11, getColor:[155,152,146], getTextAnchor:'start',
      getPixelOffset:[7,0], fontFamily:'system-ui'})
  ],
  getTooltip: ({object}) => object && object.name
    ? {text: `${object.name}\\n${object.corridor} ${object.offset_km.toFixed(1)}km`} : null
});

// 창을 줄였다 늘렸다 해도 전체가 보이게 — 발표 중 화면을 바꿔도 깨지지 않는다
addEventListener('resize', () => dk.setProps({initialViewState: fitView()}));
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/figures/corridor3d.html")
    ap.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = ap.parse_args()

    cl = load_centerline(args.root)
    db = default_db_path()

    # 완료조건: 총연장이 DB 와 맞는가
    with sqlite3.connect(db) as conn:
        lens = {r[0]: float(r[1]) for r in conn.execute(
            "SELECT corridor_id, length_km FROM corridor")}
    for cid, km in lens.items():
        if abs(km - CORRIDOR_KM) > 0.01:
            raise SystemExit(f"\n[중단] {cid} 총연장 {km} ≠ {CORRIDOR_KM} (#51). "
                             "중심선 변환 상수를 먼저 맞출 것")

    sta = stations(db)
    data = {
        "up": offset_path(cl, +RIBBON_SEP_M / 2),
        "down": offset_path(cl, -RIBBON_SEP_M / 2),
        "stations": sta,
        "markers": markers(cl),
        "camera": camera(cl),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__DECK__", DECK_VERSION)
            .replace("__LEN__", f"{CORRIDOR_KM:,.1f}")
            .replace("__NSTA__", str(len(sta))),
        encoding="utf-8")

    print(f"\n  저장: {out}  ({out.stat().st_size / 1024:,.0f} KB)")
    print(f"  중심선 {len(cl):,}점 · 휴게소 {len(sta)}곳 · "
          f"거리표지 {len(data['markers'])}개")
    print(f"  부산쪽 끝 ({cl.iloc[0].lat:.4f}, {cl.iloc[0].lon:.4f}) · "
          f"서울쪽 끝 ({cl.iloc[-1].lat:.4f}, {cl.iloc[-1].lon:.4f})")
    print("\n  외부 의존: deck.gl CDN 하나. API 키 없음. 빌드 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
