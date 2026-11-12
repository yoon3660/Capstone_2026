import os
import re
import sys
import traceback
from pathlib import Path

import pandas as pd
import yaml

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runs/issue162_rebuild/passenger266_rebuild"
sys.path.insert(0, str(ROOT / "src"))
os.environ["EVDT_DB_PATH"] = str(OUT / "scenario_sep18.sqlite")
os.environ["MPLCONFIGDIR"] = str(OUT / "mpl_cache")
from evdt import runner  # noqa: E402
from evdt.config import load_scenario  # noqa: E402

# Only record the arguments/result of the existing demand builder; do not alter them.
original = runner.build_trip_demands


def capture(evs, *args, **kwargs):
    built = original(evs, *args, **kwargs)
    frame = pd.DataFrame(evs)
    frame["date"] = date
    frame["direction"] = direction
    frame["has_charge_plan"] = frame.ev_id.isin({t.ev_id for t in built.trips})
    frame.to_csv(OUT / f"demand_{date}_{direction}.csv", index=False, encoding="utf-8-sig")
    trips = pd.DataFrame(
        [
            {
                k: getattr(t, k)
                for k in [
                    "ev_id",
                    "vclass_id",
                    "entry_min",
                    "entry_offset_km",
                    "dest_offset_km",
                    "soc0",
                    "battery_kwh",
                    "vmax_kw",
                    "consumption_kwh_km",
                ]
            }
            | {"n_plans": len(t.plans), "date": date, "direction": direction}
            for t in built.trips
        ]
    )
    trips.to_csv(
        OUT / f"charge_plan_demand_{date}_{direction}.csv", index=False, encoding="utf-8-sig"
    )
    counters.append(
        dict(
            date=date,
            direction=direction,
            n_ev=built.n_ev,
            n_no_charge=built.n_no_charge,
            n_infeasible=built.n_infeasible,
            n_charge_plan=len(built.trips),
        )
    )
    return built


runner.build_trip_demands = capture
weather = []
for date in pd.date_range("2026-09-24", "2026-09-27").strftime("%Y-%m-%d"):
    for station in ["108", "133", "143", "159"]:
        for hour in ["00", "06", "12", "18"]:
            p = (
                ROOT
                / f"data/raw/issue145_chuseok/weather_kma/kma_{station}_{date[-2:]}_{hour}.html"
            )
            html = re.sub(r"<!--.*?-->", "", p.read_text(encoding="utf-8"), flags=re.S)
            body = html.split("<tbody>", 1)[1].split("</tr>", 1)[0]
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", body, flags=re.S)
            text = [re.sub("<[^>]+>", "", c).strip() for c in cells]
            assert text[0] == hour + ":00", (p, text[0])
            weather.append(
                dict(
                    date=date,
                    station=station,
                    hour=int(hour),
                    temp_c=float(text[3]),
                    source=str(p.relative_to(ROOT)),
                )
            )
pd.DataFrame(weather).to_csv(OUT / "temperature_samples.csv", index=False, encoding="utf-8-sig")
means = pd.DataFrame(weather).groupby("date").temp_c.mean().to_dict()
counters = []
runs = []
for date in means:
    for direction in ["DOWN", "UP"]:
        cfg = yaml.safe_load(
            (ROOT / f"config/scenario_seollal_{direction.lower()}.yaml").read_text(encoding="utf-8")
        )
        cfg["scenario_id"] = f"issue162_{OUT.name}_{date.replace('-', '')}_{direction.lower()}_utf8"
        cfg["label"] = (
            f"Issue162 Chuseok local baseline {date} {direction}; Sep18 registered supply proxy"
        )
        cfg["day_type"] = "CHUSEOK_PEAK"
        cfg["demand"]["period"] = "chuseok2026"
        base = f"reports/issue160_ctm_daily/inputs/{date}/{direction}"
        for key, file in [
            ("volume_profile", "volume"),
            ("through_profile", "through"),
            ("entry_exit_profile", "entry_exit"),
        ]:
            cfg["demand"][key] = base + "/" + file + ".csv"
        cfg["demand"]["travel_time"] = "fixed"
        cfg["environment"]["temp_c"] = float(means[date])
        cfg["output"]["write_snapshots"] = False
        path = OUT / f"config_{date}_{direction}.yaml"
        path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print("BEGIN", date, direction, "temp", means[date], flush=True)
        try:
            rid = runner.run_once(
                load_scenario(path),
                db_path=OUT / "scenario_sep18.sqlite",
                runs_dir=OUT / "runs",
                root=ROOT,
                gap_plot=False,
                log=lambda x: print(x, flush=True),
            )
            runs.append(
                dict(date=date, direction=direction, run_id=rid, status="DONE", temp_c=means[date])
            )
        except Exception as exc:
            traceback.print_exc()
            runs.append(dict(date=date, direction=direction, status="FAILED", error=str(exc)))
        pd.DataFrame(runs).to_csv(OUT / "run_manifest.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(counters).to_csv(OUT / "demand_counts.csv", index=False, encoding="utf-8-sig")
pd.concat(
    [pd.read_csv(p) for p in sorted(OUT.glob("demand_2026-*.csv"))], ignore_index=True
).to_csv(OUT / "generated_ev_demand_history.csv", index=False, encoding="utf-8-sig")
pd.concat(
    [pd.read_csv(p) for p in sorted(OUT.glob("charge_plan_demand_2026-*.csv"))], ignore_index=True
).to_csv(OUT / "generated_charge_plan_demand_history.csv", index=False, encoding="utf-8-sig")
