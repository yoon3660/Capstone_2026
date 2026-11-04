"""경부선 코리도 3D — 바닥(#88) + 휴게소 기둥·시간 스크러버(#89).

    python scripts/plot_map3d.py                      # 바닥만 (#88)
    python scripts/plot_map3d.py --run <run_id>       # 휴게소 기둥까지 (#89)

`--run` 없이는 **바닥만** 그린다. 주면 `station` 스냅샷을 올려 기둥을 세운다.
도로 혼잡(`cell`)은 #90 이 같은 바닥에 올라탄다.

## 높이와 색을 왜 따로 쓰나

**두 개가 다른 것을 말하기 때문이다** (#89).

    높이 = queue_len   지금 **몇 대가** 밀려 있나   — 양
    색   = wait_min    지금 가면 **얼마** 기다리나  — 세기

충전기 19기인 안성과 2기인 옥천은 **같은 줄 길이가 전혀 다른 뜻**이다. 하나로
합치면 그게 사라진다. 그래서 충전기 수는 **기둥 굵기**로 따로 보인다.

색 구간은 2D 히트맵과 **같은 램프**를 쓴다 (`viz.plots.WAIT_BINS_MIN` · `RAMP`).
그림마다 색이 다르면 나란히 못 놓는다. 2차 발표에서 히트맵 색을 **이용률로
오해**했던 적이 있어(칠곡·평사) 범례에 *"색은 대기시간"* 을 박아 둔다.

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

from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path  # noqa: E402
from evdt.viz.plots import RAMP, WAIT_BIN_LABELS, WAIT_BINS_MIN  # noqa: E402

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

#: 가장 긴 줄이 이 높이(m)가 되게 맞춘다. **정규화 기준은 화면에 적는다** —
#: 안 적으면 두 런을 비교할 때 높이가 서로 다른 축척이 된다 (#93 이 여기서 깨진다).
#:
#: 코리도 415km 의 10%. 절대 높이가 아니라 **길이 대비 비율**로 잡는 이유: 줌이
#: 달라져도 눈에 보이는 비율이 같다. 26km(6%)로 했더니 나란히 보기(#93)에서
#: 패널이 반으로 좁아지며 줌이 7.5 → 5.6 으로 떨어져 기둥이 11px 밖에 안 됐다
MAX_COLUMN_M = 41_500.0

#: 기둥 굵기 = 이 값 + 충전기수 × 증가분 (m). 2기와 19기가 눈으로 구분돼야 한다
COLUMN_BASE_R_M = 2_600.0
COLUMN_PER_CHARGER_M = 700.0


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


def station_frames(run_id: str, runs_dir: Path) -> dict:
    """`station` 스냅샷 → 휴게소별 프레임 배열 (#89).

    16곳 × 289프레임 × 4상태 = 18,496행. **전부 브라우저에 올려도 된다** — 새로
    뽑을 것도, 간솎을 것도 없다.

    위경도는 스냅샷이 직접 들고 있다 (설계 규칙: 모든 공간 개체가 lat/lon 을 함께
    나른다). 그래서 이 함수는 **DB 를 보지 않는다** — 뷰어의 입력은 스냅샷뿐이다.
    """

    path = runs_dir / run_id / "snapshot.parquet"
    if not path.is_file():
        raise SystemExit(f"\n[중단] 스냅샷이 없다: {path}")

    snap = pd.read_parquet(path)
    snap = snap[snap["entity_type"] == "station"]
    if snap.empty:
        raise SystemExit(f"\n[중단] {run_id} 에 station 스냅샷이 없다")

    wide = snap.pivot_table(index=["t_min", "entity_id", "lat", "lon"],
                            columns="state", values="value").reset_index()
    times = sorted(wide["t_min"].unique())
    index = {t: i for i, t in enumerate(times)}

    out = []
    for sid, g in wide.groupby("entity_id"):
        g = g.sort_values("t_min")
        queue = [0.0] * len(times)
        wait = [0.0] * len(times)
        for r in g.itertuples():
            queue[index[r.t_min]] = float(getattr(r, "queue_len", 0.0) or 0.0)
            wait[index[r.t_min]] = float(getattr(r, "wait_min", 0.0) or 0.0)
        out.append({
            "id": str(sid),
            "position": [float(g.iloc[0].lon), float(g.iloc[0].lat)],
            "chargers": int(g["chargers_total"].max()),
            "queue": [round(v, 1) for v in queue],
            "wait": [round(v, 1) for v in wait],
        })

    out.sort(key=lambda d: d["id"])
    return {"stations": out, "times": [float(t) for t in times],
            "max_queue": max((max(d["queue"]) for d in out), default=0.0),
            "run_id": run_id}


def same_world(panels: list[dict]) -> None:
    """나란히 놓을 런들이 **같은 세계**인가 (#93 · #59 와 같은 방어).

    다른 시나리오나 다른 시드를 나란히 놓으면 **비교가 아니라 여러 그림**이다.
    run_id 가 `<scenario>__<stage>__p<참여율>__s<시드>` 라서 거기서 바로 읽는다.

    두 칸이든 세 칸이든 같은 규칙이다 — **첫 칸을 기준으로 전부 대조한다.**

    ⚠ run_id 는 세계를 완전히 식별하지 못한다 (#101 — 같은 이름으로 다른 세계를
    돌릴 수 있다). 여기서는 **스테이지만 다르다**는 것까지만 본다. 그 이상은
    `stale_reason` 의 몫이고, 그림이 할 일이 아니다.
    """

    parts = []
    for p in panels:
        bits = p["run_id"].split("__")
        if len(bits) != 4:
            raise SystemExit(f"\n[중단] run_id 모양이 다르다: {p['run_id']}")
        parts.append(bits)

    head = parts[0]
    for bits, p in zip(parts[1:], panels[1:], strict=True):
        for i, what in ((0, "시나리오"), (2, "참여율"), (3, "시드")):
            if bits[i] != head[i]:
                raise SystemExit(
                    f"\n[중단] {what}가 다르다: {head[i]} ≠ {bits[i]}\n"
                    "  다른 세계를 나란히 놓으면 비교가 아니다 (#59). "
                    "스테이지만 다른 런들을 줄 것")
        if p["times"] != panels[0]["times"]:
            raise SystemExit(f"\n[중단] {p['run_id']} 의 프레임 시각이 다르다 — "
                             "슬라이더 하나로 같이 움직일 수 없다")

    stages = [b[1] for b in parts]
    if len(set(stages)) != len(stages):
        raise SystemExit(f"\n[중단] 스테이지가 겹친다: {stages}. 비교할 것이 없다")


def escape_frames(run_id: str, runs_dir: Path, times: list[float],
                  station_ids: list[str]) -> dict:
    """휴게소가 **밀어낸 차**를 프레임별 누적으로 (#92 의 값어치를 #91 없이).

    ## 왜 이게 가능한가 — 이탈은 휴게소에 귀속된다

    `escape_event` 의 `best_station_id` 가 *"남았다면 여기로 갔을 것"* 이다 (#54).
    `balked` 행에 **100% 채워져 있다.** 그래서 차량 스냅샷(8.9M 행, #91) 없이도
    *"어느 휴게소가 몇 대를 밀어냈나"* 를 그릴 수 있다.

    ## ⚠ balked 만 아래로 내린다

        balked    갈 수는 있는데 줄이 이탈 비용보다 길다 — **그 휴게소가 밀어낸 것**
        no_plan   닿는 휴게소가 아예 없다 — **휴게소 탓이 아니다** (충전기 공백·출발 SoC)

    둘을 합쳐 기둥에 넣으면 *"엔진이 이탈을 줄였다"* 고 할 때 **고칠 수 없는 몫까지
    성과로 세게 된다** (#54 가 둘을 가른 이유). `no_plan` 은 코리도 전체 수로만 센다.

    ## 위아래가 같은 축척인 이유

    둘 다 **그 시각의 상태(stock)** 다 —

        위   지금 줄 서 있는 차
        아래 지금까지 밀어낸 차 (누적)

    뜻은 다르지만 단위가 같은 "대" 라서 **1대 = 1대** 로 놓을 수 있다. 그래야
    *"칠곡은 지금 40대를 세워 두고 지금까지 112대를 돌려보냈다"* 가 길이로 읽힌다.
    """

    path = runs_dir / run_id / "escape_event.parquet"
    out = {sid: [0] * len(times) for sid in station_ids}
    blank = {"balked": out, "no_plan": [0] * len(times), "has_time": False}
    if not path.is_file():
        return blank

    esc = pd.read_parquet(path)
    if "escape_t_min" not in esc.columns or esc["escape_t_min"].isna().all():
        # 옛 런이거나 UE 다 — 시간축을 못 그린다. 조용히 0 을 그리면 "이탈이 없다"
        # 로 보이므로 호출자가 화면에 적을 수 있게 알린다
        return blank

    step = (times[1] - times[0]) if len(times) > 1 else 1.0

    def slot(t: float) -> int:
        return max(0, min(len(times) - 1, int((float(t) - times[0]) // step)))

    for r in esc.itertuples():
        if pd.isna(r.escape_t_min):
            continue
        i = slot(r.escape_t_min)
        if r.reason == "balked" and r.best_station_id in out:
            out[r.best_station_id][i] += 1
        elif r.reason == "no_plan":
            blank["no_plan"][i] += 1

    # 누적으로 바꾼다 — "지금까지 몇 대" 가 상태다
    for sid in out:
        run = 0
        for i in range(len(times)):
            run += out[sid][i]
            out[sid][i] = run
    run = 0
    for i in range(len(times)):
        run += blank["no_plan"][i]
        blank["no_plan"][i] = run

    blank["has_time"] = True
    return blank


def station_names(ids: list[str], db: Path) -> dict[str, str]:
    """기둥 툴팁에 쓸 이름. **스냅샷에 없는 유일한 것**이라 DB 에서 가져온다."""

    if not ids:
        return {}
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            f"SELECT station_id, name FROM station WHERE station_id IN "  # noqa: S608
            f"({','.join('?' * len(ids))})", ids).fetchall()
    return {str(a): str(b) for a, b in rows}


#: 배경 지도 — 시도 경계를 단순화해 둔 것. **발표용 맥락이지 분석 자료가 아니다.**
#: 좌표 정밀도가 약 100m 라 코리도 정렬에 쓰면 안 된다.
OUTLINE = Path(__file__).resolve().parents[1] / "src/evdt/viz/assets/korea_outline.json"


def korea_outline() -> list[list[list[float]]]:
    """대한민국 시도 경계 링. 파일이 없으면 빈 목록 — **그림은 그래도 그려진다.**"""

    if not OUTLINE.is_file():
        return []
    return json.loads(OUTLINE.read_text(encoding="utf-8"))["rings"]


def camera(cl: pd.DataFrame, outline: list | None = None) -> dict:
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

    lo = [float(cl["lon"].min()), float(cl["lat"].min())]
    hi = [float(cl["lon"].max()), float(cl["lat"].max())]

    # 바닥만 그릴 때(런 없음)는 **전국이 보이게** 맞춘다 — "코리도가 어디인가" 가
    # 그 그림의 목적이기 때문이다. 런이 있으면 코리도에 맞춘다: 전국에 맞추면
    # 기둥이 점으로 줄어든다
    if outline:
        xs = [x for r in outline for x, _ in r]
        ys = [y for r in outline for _, y in r]
        lo = [min(lo[0], min(xs)), min(lo[1], min(ys))]
        hi = [max(hi[0], max(xs)), max(hi[1], max(ys))]
        # ★ 전국을 보여줄 때는 **북쪽을 위로** 둔다. 코리도에 맞춘 베어링으로 돌리면
        #   한국이 기울어져서 "여기가 어디인가" 가 오히려 안 읽힌다. 기울기도 눕힌다
        return {"bearing": 0.0, "pitch": 0.0, "bounds": [lo, hi]}

    return {"bearing": bearing - 90, "pitch": 50, "bounds": [lo, hi]}


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
  #hudfold{position:absolute;right:7px;top:6px;width:22px;height:20px;border:0;
       border-radius:4px;cursor:pointer;background:#2c3140;color:#e8e6e1;font-size:12px}
  #hud.min{max-width:none}
  #hud.min #hudbody{display:none}
  #hudmini{display:none} #hud.min #hudmini{display:block}
  .muted{color:#9b9892;font-size:12px}
  #bar{position:absolute;left:0;right:0;bottom:0;z-index:9;display:flex;gap:12px;
       align-items:center;padding:11px 18px;background:rgba(19,22,28,.9);
       font:13px system-ui,'Malgun Gothic',sans-serif;color:#e8e6e1}
  #bar button{width:34px;height:28px;border:0;border-radius:5px;cursor:pointer;
       background:#2c3140;color:#e8e6e1;font-size:13px}
  #scrub{flex:1;accent-color:#6da7ec}
  #clock{font-variant-numeric:tabular-nums;min-width:52px}
  #bar.off{display:none}
  #panes{display:flex;height:100%}
  .pane{position:relative;flex:1;height:100%}
  .pane + .pane{border-left:1px solid #2c3140}
  .tag{position:absolute;left:12px;bottom:62px;z-index:8;color:#e8e6e1;
       font:600 15px system-ui,'Malgun Gothic',sans-serif;
       background:rgba(19,22,28,.8);padding:5px 10px;border-radius:5px}
  .tag small{display:block;font-weight:400;color:#9b9892;font-size:11px}
</style></head><body>
<div id="panes"></div>
<div id="hud">
  <button id="hudfold" title="범례 접기/펴기 (h)">−</button>
  <b>경부고속도로 코리도</b> __LEN__ km
  <!-- 접어도 **정규화 기준은 남는다.** 그 값이 화면에 없으면 패널 간 높이를
       비교할 수 없다 — 이 뷰어가 지켜야 하는 규칙이다 (#93) -->
  <div id="hudmini" class="muted"></div>
  <div id="hudbody">
  <br/>
  <span class="k" style="background:rgb(90,160,255)"></span>상행 (부산 → 서울)<br/>
  <span class="k" style="background:rgb(255,140,90)"></span>하행 (서울 → 부산)<br/>
  <span class="k" style="background:rgb(230,230,230)"></span>휴게소 __NSTA__곳<br/>
  <div id="legend"></div>
  <div class="muted" style="margin-top:7px">
    드래그 회전 · 휠 확대 · 두 줄 간격은 기호다(실제 도로폭 아님)
  </div>
  </div>
</div>
<div id="bar">
  <button id="play">▶</button>
  <input id="scrub" type="range" min="0" max="0" value="0" step="1"/>
  <span id="clock">--:--</span>
</div>
<script>
const D = __DATA__;
const {DeckGL, PathLayer, ScatterplotLayer, TextLayer, WebMercatorViewport} = deck;

// 창 크기에 맞춰 코리도 전체를 담는다. 줌을 파일에 박으면 다른 화면에서 깨진다
function fitView() {
  // ⚠ **패널 폭**으로 맞춘다. 창 전체 폭을 쓰면 나란히 놓았을 때(#93) 코리도가
  //   각 패널에서 양옆으로 잘린다
  const n = Math.max(D.panels.length, 1);

  // ⚠ 창이 0 으로 보고될 때가 있다 — 숨은 탭, 꺼진 보조 모니터, 백그라운드 창.
  //   그대로 fitBounds 에 넣으면 math.gl 이 assert 로 죽고 **스크립트 전체가
  //   멈춘다** (범례도 슬라이더도 안 뜬다). 발표 중에 나면 치명적이라 막는다.
  //   resize 가 붙어 있으므로 창이 보이는 순간 제대로 다시 맞춰진다
  const w = Math.max(innerWidth / n, 200);
  const h = Math.max(innerHeight, 200);

  const mid = [(D.camera.bounds[0][0] + D.camera.bounds[1][0]) / 2,
               (D.camera.bounds[0][1] + D.camera.bounds[1][1]) / 2];
  let f = {longitude: mid[0], latitude: mid[1], zoom: 6};
  try {
    f = new WebMercatorViewport({width: w, height: h})
          .fitBounds(D.camera.bounds, {padding: Math.min(70, w / 5)});
  } catch (e) {
    console.warn('fitBounds 실패 — 기본 줌으로 연다', e);
  }
  // 베어링을 돌리면 긴 축이 가로로 눕는다 — 그만큼 여유를 더 둔다
  return {longitude: f.longitude, latitude: f.latitude, zoom: f.zoom - 0.35,
          pitch: D.camera.pitch, bearing: D.camera.bearing};
}

// --- 바닥 (#88) — 기둥이 바뀌어도 다시 안 만든다 ------------------------
function baseLayers() {
  return [
    // 배경 지도 — 맨 먼저 그려서 뒤에 깔린다. 코리도가 전국 어디인지 보이게 한다.
    // 외부 타일이 아니라 **파일에 박아 둔 경계**다: 발표장 인터넷에 의존하지 않는다
    ...(D.outline.length ? [new PathLayer({id:'kr', data:D.outline.map(p=>({path:p})),
      getPath:d=>d.path, getColor:[108,112,124,170], widthMinPixels:1, getWidth:260,
      parameters:{depthTest:false}})] : []),
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
  ];
}

// --- 휴게소 기둥 (#89) --------------------------------------------------
// 높이 = queue_len (양) · 색 = wait_min (세기) · 굵기 = 충전기 수.
// 셋을 합치면 "19기인 안성과 2기인 옥천의 같은 줄 길이" 가 같은 뜻이 돼 버린다.
//
// 패널이 여럿이면(#93) **척도를 공유한다** — D.elevPerCar 가 모든 런의 공통
// 최대값에서 나온다. 각자 정규화하면 몇 배 차이가 똑같이 보인다.
const P = D.panels;              // 빈 배열이면 바닥만 (#88)
let frame = 0, timer = null;

function bandColor(w) {
  const B = D.waitBins, C = D.rampRGB;
  for (let i = B.length - 1; i >= 0; i--) if (w >= B[i]) return C[i];
  return C[0];
}

function columnLayer(p) {
  if (!p) return [];
  const rows = p.stations.map(s => ({
    position: s.position, name: p.names[s.id] || s.id, chargers: s.chargers,
    queue: s.queue[frame], wait: s.wait[frame],
    escaped: (s.escaped || [])[frame] || 0,
  }));
  const radius = d => D.colBaseR + d.chargers * D.colPerCharger;

  return [
    // 위 — 지금 줄 선 차
    new deck.ColumnLayer({
      id: 'col', data: rows, diskResolution: 14, extruded: true, pickable: true,
      getPosition: d => d.position,
      getElevation: d => d.queue * D.elevPerCar,
      getFillColor: d => bandColor(d.wait),
      getLineColor: [12, 14, 18, 180],
      getRadius: radius,
      radiusUnits: 'meters', elevationScale: 1, stroked: true, lineWidthMinPixels: 1,
      updateTriggers: {getElevation: frame, getFillColor: frame},
    }),
    // 아래 — 지금까지 밀어낸 차 (balked 만). **같은 축척**이라 길이로 비교된다.
    // 색을 대기 램프(파랑)와 다른 계열로 두어 "이건 다른 것" 이 바로 보이게 한다
    new deck.ColumnLayer({
      id: 'esc', data: rows.filter(d => d.escaped > 0), diskResolution: 14,
      extruded: true, pickable: true,
      getPosition: d => d.position,
      getElevation: d => -d.escaped * D.elevPerCar,
      getFillColor: [196, 86, 70, 225],
      getLineColor: [12, 14, 18, 180],
      getRadius: radius,
      radiusUnits: 'meters', elevationScale: 1, stroked: true, lineWidthMinPixels: 1,
      updateTriggers: {getElevation: frame},
    }),
  ];
}

const tooltip = ({object}) => {
  if (!object) return null;
  if (object.queue !== undefined) {
    return {text: `${object.name}\n충전기 ${object.chargers}기\n`
                + `줄 ${object.queue.toFixed(0)}대 · 대기 ${object.wait.toFixed(0)}분\n`
                + `지금까지 밀어낸 차 ${object.escaped}대`};
  }
  if (object.name) {
    return {text: `${object.name}\n${object.corridor} ${object.offset_km.toFixed(1)}km`};
  }
  return null;
};

// --- 패널 만들기 — 1개(#89) 또는 2개 나란히(#93) -------------------------
// ★ **카메라를 묶는다.** 각도가 다르면 높이 비교가 눈속임이 된다 (#93).
//   한쪽을 돌리면 다른 쪽이 같은 viewState 를 받는다
let view = fitView();
const decks = [];

(P.length ? P : [null]).forEach((p, i) => {
  const host = document.createElement('div');
  host.className = 'pane';
  const canvas = document.createElement('div');
  canvas.style.cssText = 'position:absolute;inset:0';
  host.appendChild(canvas);
  if (p) {
    const stage = p.run_id.split('__')[1];
    host.insertAdjacentHTML('beforeend',
      `<div class="tag">${stage}<small>${p.run_id}</small></div>`);
  }
  panes.appendChild(host);

  decks.push(new DeckGL({
    container: canvas, controller: true, viewState: view,
    layers: [...baseLayers(), ...columnLayer(p)],
    getTooltip: tooltip,
    onViewStateChange: ({viewState}) => { view = viewState; sync(); },
  }));
});

function sync() {
  decks.forEach((dk, i) => dk.setProps({
    viewState: view,
    layers: [...baseLayers(), ...columnLayer(P[i] || null)],
  }));
}

function render() {
  sync();
  if (P.length) {
    const t = P[0].times[frame];
    clock.textContent = String(Math.floor(t / 60) % 24).padStart(2, '0') + ':'
                      + String(Math.round(t % 60)).padStart(2, '0');
    scrub.value = frame;
  }
}

// --- 범례 · 스크러버 -----------------------------------------------------
if (P.length) {
  const shared = P.length > 1
    ? `<span class="muted">(${P.length}개 런 공통 최대 ${D.sharedMax.toFixed(0)}대 = 화면 최대 높이 — `
      + `각자 정규화하면 두 그림이 똑같아 보인다)</span>`
    : `<span class="muted">(가장 긴 줄 ${D.sharedMax.toFixed(0)}대 = 화면 최대 높이)</span>`;
  // 접었을 때 남길 한 줄 — **정규화 기준**과 색의 뜻만
  document.getElementById('hudmini').textContent =
    `높이 = 줄 선 차 수 (공통 최대 ${D.sharedMax.toFixed(0)}대) · 색 = 대기시간(이용률 아님)`;
  legend.innerHTML =
    `<div style="margin-top:8px"><b>높이</b> = 줄 선 차 수 ${shared}</div>`
    + '<div><b>색</b> = 지금 가면 기다리는 시간 <span class="muted">(이용률 아님)</span></div>'
    + '<div style="margin:4px 0 2px">'
    + D.rampHex.map((c, i) =>
        `<span class="k" style="background:${c}"></span><span class="muted">${D.binLabels[i]}</span>`
      ).join(' ')
    + '</div><div><b>굵기</b> = 충전기 수 <span class="muted">(2기 ↔ 19기)</span></div>'
    + '<div style="margin-top:6px"><span class="k" style="background:rgb(196,86,70)"></span>'
    + '<b>아래로</b> = 그 휴게소가 <b>밀어낸 차</b> <span class="muted">(지금까지 누적 · '
    + '위와 같은 축척 1대=1대)</span></div>'
    + '<div class="muted">줄이 이탈 비용보다 길어 포기한 차(balked)만. '
    + '닿는 곳이 없어 나간 차(no_plan)는 휴게소 탓이 아니라 제외</div>'
    + (P.some(p => !p.has_escape_time)
        ? '<div style="color:#e0a04a;margin-top:4px">⚠ 이탈 시각이 없는 런이 있다 '
          + '(UE 는 하루 단위 계획이라 "포기한 순간" 이 없다)</div>' : '')
    + (P.length > 1
        ? `<div class="muted" style="margin-top:5px">카메라·시간·척도를 ${P.length}개 패널이 공유한다</div>`
        : '');

  scrub.max = P[0].times.length - 1;
  scrub.oninput = () => { frame = +scrub.value; render(); };
  play.onclick = () => {
    if (timer) { clearInterval(timer); timer = null; play.textContent = '▶'; return; }
    play.textContent = '❚❚';
    timer = setInterval(() => {
      frame = (frame + 1) % P[0].times.length; render();
    }, 90);
  };
} else {
  bar.classList.add('off');
  legend.innerHTML = '<div class="muted" style="margin-top:7px">'
    + '바닥만 그린 상태 — <code>--run</code> 을 주면 휴게소 기둥이 선다 (#89)</div>';
}

// 범례가 첫 패널을 가린다는 지적이 있었다. 접을 수 있게 하되, 접어도
// **정규화 기준 한 줄은 남긴다** — 없으면 패널 간 높이 비교가 성립하지 않는다
const hud = document.getElementById('hud');
const fold = () => {
  hud.classList.toggle('min');
  document.getElementById('hudfold').textContent = hud.classList.contains('min') ? '+' : '−';
};
document.getElementById('hudfold').onclick = fold;
addEventListener('keydown', e => { if (e.key === 'h' || e.key === 'H') fold(); });

render();

// 창을 줄였다 늘렸다 해도 전체가 보이게 — 발표 중 화면을 바꿔도 깨지지 않는다
addEventListener('resize', () => { view = fitView(); sync(); });
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/figures/corridor3d.html")
    ap.add_argument("--root", type=Path, default=PROJECT_ROOT)
    ap.add_argument("--run", default=None,
                    help="run_id. 주면 휴게소 기둥과 시간 스크러버가 붙는다 (#89)")
    ap.add_argument("--vs", action="append", default=[],
                    help="나란히 놓을 run_id. **여러 번 줄 수 있다** — S0│S1│S2 처럼. "
                         "카메라·시간·척도를 전부 공유한다 (#93)")
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--basemap", choices=("auto", "on", "off"), default="auto",
                    help="배경 지도(시도 경계). auto = 바닥만 그릴 때만 켠다 — "
                         "런이 있으면 기둥과 경쟁해서 오히려 안 보인다")
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
    runs = [r for r in [args.run, *args.vs] if r]
    panels = [station_frames(r, args.runs_dir or RUNS_DIR) for r in runs]
    if len(panels) > 1:
        same_world(panels)

    # ★ **모든 런이 척도를 공유한다** (#93). 각자 자기 최대값으로 정규화하면 각
    #   런의 가장 긴 줄이 **전부 '꼭대기'** 로 그려져서, 몇 배 차이가 나도
    #   **똑같이 보인다.** 조용히 틀리는 종류라 공통 최대값으로 묶고 화면에 적는다
    shared_max = max((p["max_queue"] for p in panels), default=0.0)
    for p in panels:
        ids = [s["id"] for s in p["stations"]]
        p["names"] = station_names(ids, db)
        esc = escape_frames(p["run_id"], args.runs_dir or RUNS_DIR, p["times"], ids)
        for st in p["stations"]:
            st["escaped"] = esc["balked"][st["id"]]
        p["no_plan"] = esc["no_plan"]
        p["has_escape_time"] = esc["has_time"]
        p["max_escaped"] = max((v[-1] for v in esc["balked"].values()), default=0)

    # 위(줄)와 아래(밀어낸 차)가 **같은 축척**이다 — 1대 = 1대. 둘 다 "대" 단위의
    # 상태라 길이로 비교할 수 있다. 축척이 다르면 그 비교가 거짓말이 된다
    shared_max = max([shared_max, *(p["max_escaped"] for p in panels)], default=0.0)

    # 배경 지도는 **"이 코리도가 전국 어디인가"** 를 보여주는 장치다. 런이 있는
    # 그림의 질문은 그게 아니라 "어느 휴게소가 막히나" 이고, 거기에 전국 경계를
    # 깔면 선이 늘어 기둥이 묻힌다. 그래서 바닥만 그릴 때만 켠다
    want_map = args.basemap == "on" or (args.basemap == "auto" and not panels)
    outline = korea_outline() if want_map else []

    data = {
        "up": offset_path(cl, +RIBBON_SEP_M / 2),
        "down": offset_path(cl, -RIBBON_SEP_M / 2),
        "stations": sta,
        "markers": markers(cl),
        # 런이 없으면(바닥만) 전국이 보이게 맞춘다 — `camera()` 주석 참조
        "camera": camera(cl, outline if not panels else None),
        "outline": outline,
        "panels": panels,
        "sharedMax": shared_max,
        # 색 구간은 2D 히트맵과 **같은 램프**를 쓴다. 그림마다 다르면 못 겹쳐 본다
        "waitBins": [b for b in WAIT_BINS_MIN[:-1]],
        "binLabels": list(WAIT_BIN_LABELS),
        "rampHex": list(RAMP),
        "rampRGB": [[int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)] for c in RAMP],
        "colBaseR": COLUMN_BASE_R_M,
        "colPerCharger": COLUMN_PER_CHARGER_M,
        # 가장 긴 줄이 화면 최대 높이가 되게. **기준을 범례에 적는다** (#89 · #93)
        "elevPerCar": MAX_COLUMN_M / max(shared_max, 1.0),
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

    for p in panels:
        print(f"\n  [{p['run_id']}]")
        print(f"    기둥 {len(p['stations'])}곳 × {len(p['times'])}프레임 · "
              f"가장 긴 줄 {p['max_queue']:.0f}대")
        _cross_check(p, p["names"])

    if panels:
        print(f"\n  높이 정규화: **공통 최대 {shared_max:.0f}대** = "
              f"{MAX_COLUMN_M / 1000:.0f}km  (범례에 적힌다)")
    for p in panels:
        if p["max_escaped"]:
            print(f"  [{p['run_id'].split('__')[1]}] 밀어낸 차 누적 "
                  f"{sum(v[-1] for v in [s['escaped'] for s in p['stations']]):,}대 · "
                  f"한 휴게소 최대 {p['max_escaped']}대")
    if len(panels) > 1:
        qs = sorted(p["max_queue"] for p in panels)
        print(f"  런별 최대 줄 {' · '.join(f'{q:.0f}' for q in qs)}대 "
              f"(최대/최소 {qs[-1] / max(qs[0], 1):.1f}배) — 각자 정규화했으면 "
              "**전부 똑같은 높이로 보였다**")

    print("\n  외부 의존: deck.gl CDN 하나. API 키 없음. 빌드 없음")
    return 0


def _cross_check(frames: dict, names: dict) -> None:
    """16:40 에 어디가 밀려 있나 — 기존 2D 그림과 **같게 보여야** 한다 (#89 완료조건).

    눈으로만 대조하면 "비슷해 보인다" 로 끝난다. 숫자를 찍어 두면 다음 사람이
    2D 와 직접 맞춰 볼 수 있다.
    """

    want = 1000.0                       # 16:40
    times = frames["times"]
    i = min(range(len(times)), key=lambda k: abs(times[k] - want))
    rows = sorted(frames["stations"], key=lambda s: -s["queue"][i])[:5]

    print(f"\n  교차검증 — t={times[i]:.0f}분 "
          f"({int(times[i] // 60):02d}:{int(times[i] % 60):02d}) 상위 5곳")
    for s in rows:
        print(f"    {names.get(s['id'], s['id']):14} 줄 {s['queue'][i]:5.0f}대 · "
              f"대기 {s['wait'][i]:5.0f}분 · 충전기 {s['chargers']:2d}기")


if __name__ == "__main__":
    raise SystemExit(main())
