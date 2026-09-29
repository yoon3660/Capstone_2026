"""#54 검증 — python scripts/validate_demand.py

#54 검증 — 우리가 만든 수요가 실측 구간 교통량을 재현하는가.

통과 기준은 **먼저 적고** 결과를 본다 (결과를 보고 기준을 바꾸지 않는다).
  1. 구간 x 시간 MAPE < 20%
  2. 치우침(평균 편차) 절댓값 < 10%  — 계통오차가 없어야 한다
  3. 하루 합계 오차 < 5%
"""

import pandas as pd
import yaml
from _bootstrap import ROOT  # noqa: E402,F401

from evdt.config import ScenarioConfig
from evdt.io.demand_profile import _complete_zones, _slice, peak_date
from evdt.paths import CONFIG_DIR
from evdt.runner import build_travel_field

END = 415.058
raw = yaml.safe_load((CONFIG_DIR / "scenario_seollal_down.yaml").read_text(encoding="utf-8"))
cfg = ScenarioConfig.from_dict(raw, source="validate")
ctm = build_travel_field(cfg, END, log=lambda *a: None)

state = pd.DataFrame(ctm.cell_state_rows)
edges = ctm.speed_field.edges_km
mid = (edges[:-1] + edges[1:]) / 2
cell_mid = dict(zip(sorted(state["cell_id"].unique()), mid, strict=True))
state["offset_km"] = state["cell_id"].map(cell_mid)
state["hour"] = (state["t_min"] // 60).clip(upper=23).astype(int)

# 시뮬레이션 구간 통과 대수 = 5분 평균 유량(대/h) x (5/60) 을 시간대로 합산
state["veh"] = state["flow_veh_h"] * (5.0 / 60.0)
sim = state.groupby(["hour", "offset_km"])["veh"].sum().reset_index()

traffic = pd.read_parquet("data/processed/traffic_gyeongbu.parquet")
day = peak_date(traffic, period="seollal2026", direction="DOWN")
meas = _complete_zones(_slice(traffic, "seollal2026", "DOWN"))
meas = meas[meas["date"] == day]

rows = []
for (start, end), grp in meas.groupby(["offset_km_start", "offset_km_end"]):
    inside = sim[(sim["offset_km"] >= start) & (sim["offset_km"] < end)]
    if inside.empty:
        continue
    sim_hourly = inside.groupby("hour")["veh"].mean()          # 구간 안 셀들의 평균
    obs_hourly = grp.groupby("hour")["volume_veh"].sum()
    for hour in range(24):
        if hour in obs_hourly.index and hour in sim_hourly.index and obs_hourly[hour] > 0:
            rows.append({"offset_km": start, "hour": hour,
                         "obs": float(obs_hourly[hour]), "sim": float(sim_hourly[hour])})

df = pd.DataFrame(rows)
df["err"] = df["sim"] - df["obs"]
df["pct"] = df["err"] / df["obs"]

mape = df["pct"].abs().mean()
bias = df["pct"].mean()
total = (df["sim"].sum() - df["obs"].sum()) / df["obs"].sum()

print(f"구간 {df['offset_km'].nunique()}개 x 시간 → 표본 {len(df):,}개\n")
print(f"1. MAPE        {mape:7.1%}   (기준 <20%)   {'OK' if mape < 0.20 else 'X'}")
print(f"2. 치우침      {bias:+7.1%}   (기준 |·|<10%) {'OK' if abs(bias) < 0.10 else 'X'}")
print(f"3. 하루 합계   {total:+7.1%}   (기준 |·|<5%)  {'OK' if abs(total) < 0.05 else 'X'}")

print("\n오차가 큰 구간 5개:")
worst = df.groupby("offset_km")["pct"].agg(["mean", "count"]).sort_values(
    "mean", key=abs, ascending=False)
for km, r in worst.head(5).iterrows():
    print(f"   {km:7.1f} km  평균 편차 {r['mean']:+7.1%}  ({int(r['count'])}시간)")

print("\n시간대별 편차:")
by_hour = df.groupby("hour")["pct"].mean()
for h in range(0, 24, 3):
    if h in by_hour.index:
        print(f"   {h:>2}시 {by_hour[h]:+7.1%}", end="")
print()
