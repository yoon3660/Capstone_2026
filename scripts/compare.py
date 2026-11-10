from pathlib import Path
import sys,json,sqlite3,hashlib
import pandas as pd,numpy as np
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'data/processed/issue162_20261108'
MODEL=ROOT/'runs/issue162_rebuild/passenger266_rebuild' if '--passenger' in sys.argv else BASE
OUT=ROOT/'runs/issue162_comparison/passenger266' if '--passenger' in sys.argv else ROOT/'runs/issue162_comparison/default'
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'src'))
from evdt.world.sim import db_charger_id
manifest=pd.read_csv(MODEL/'run_manifest.csv');assert len(manifest)==8 and manifest.status.eq('DONE').all()
cross=pd.read_csv(MODEL/'model_charger_crosswalk.csv',dtype={'station_id':str,'charger_id':str})
frames=[]
for r in manifest.itertuples():
    p=MODEL/'runs'/r.run_id/'charge_event.parquet';f=pd.read_parquet(p)
    f['input_date']=r.date;f['direction']=r.direction
    f['start']=pd.Timestamp(r.date)+pd.to_timedelta(f.t_start_min,unit='m')
    f['end']=pd.Timestamp(r.date)+pd.to_timedelta(f.t_end_min,unit='m')
    f['model_charger_id']=f.charger_id.map(db_charger_id)
    f=f.rename(columns={'station_id':'model_station_id','charger_id':'model_unit_id'})
    f=f.merge(cross[['model_charger_id','station_id','charger_id','station_name']],on='model_charger_id',how='left',validate='many_to_one')
    assert f.station_name.notna().all();frames.append(f)
all_model=pd.concat(frames,ignore_index=True)
all_model.to_csv(OUT/'generated_charging_history_all_events.csv',index=False,encoding='utf-8-sig')
lo=pd.Timestamp('2026-09-24');hi=pd.Timestamp('2026-09-28')
model=all_model[all_model.start.ge(lo)&all_model.start.lt(hi)].copy()
model.to_csv(OUT/'generated_chuseok_charging_history.csv',index=False,encoding='utf-8-sig')
obs=pd.read_csv(BASE/'observed_chuseok_demand_history.csv',dtype={'station_id':str,'charger_id':str},parse_dates=['start','end'])
obs=obs[~obs.duplicate_exact].copy()
obs['charge_min']=obs.reported_charge_min
keys=set(zip(obs.station_name,obs.direction))
common=set(zip(cross.station_id,cross.charger_id))&set(zip(obs.station_id,obs.charger_id))
for f in [obs,model]:
    f['date']=f.start.dt.strftime('%Y-%m-%d');f['hour']=f.start.dt.hour
    f['common_id']=[(a,b) in common for a,b in zip(f.station_id,f.charger_id)]
model['observed_station']=[(a,b) in keys for a,b in zip(model.station_name,model.direction)]
metrics=[];dist=[];peaks=[]
for scope,o,m in [('ALL_AT_OBSERVED_STATIONS',obs,model[model.observed_station]),('COMMON_ME_IDS_DIAGNOSTIC',obs[obs.common_id],model[model.common_id])]:
    for direction in ['BOTH','DOWN','UP']:
        a=o if direction=='BOTH' else o[o.direction.eq(direction)]
        b=m if direction=='BOTH' else m[m.direction.eq(direction)]
        metrics.append(dict(scope=scope,direction=direction,metric='total_bias_pct',value=(len(b)-len(a))/len(a)*100,observed=len(a),model=len(b),judgment='CONDITIONAL'))
        for label,group in [('station',['direction','station_name']),('day',['date']),('hour',['date','hour']),('station_day',['direction','station_name','date']),('station_hour',['direction','station_name','date','hour'])]:
            ix=pd.MultiIndex.from_tuples(sorted(set(zip(a.station_name,a.direction))),names=['station_name','direction']).to_frame(index=False)
            if 'date' in group:ix=ix.merge(pd.DataFrame({'date':pd.date_range(lo,hi,inclusive='left').strftime('%Y-%m-%d')}),how='cross')
            if 'hour' in group:ix=ix.merge(pd.DataFrame({'hour':range(24)}),how='cross')
            ix=ix[group].drop_duplicates()
            ac=a.groupby(group).size().rename('observed').reset_index();bc=b.groupby(group).size().rename('model').reset_index()
            t=ix.merge(ac,on=group,how='left').merge(bc,on=group,how='left').fillna({'observed':0,'model':0})
            t['error']=t.model-t.observed;t['abs_error']=t.error.abs()
            if direction=='BOTH':t.to_csv(OUT/f'comparison_{scope.lower()}_{label}.csv',index=False,encoding='utf-8-sig')
            metrics.append(dict(scope=scope,direction=direction,metric=label+'_wape_pct',value=t.abs_error.sum()/t.observed.sum()*100,observed=t.observed.sum(),model=t.model.sum(),judgment='CONDITIONAL'))
            if label=='station':
                t['observed_share']=t.observed/t.observed.sum();t['model_share']=t.model/t.model.sum();t['share_diff_pp']=(t.model_share-t.observed_share)*100
                metrics.append(dict(scope=scope,direction=direction,metric='station_tvd',value=(t.observed_share-t.model_share).abs().sum()/2,judgment='CONDITIONAL'))
                if direction=='BOTH':t.to_csv(OUT/f'comparison_{scope.lower()}_station.csv',index=False,encoding='utf-8-sig')
        for field in ['energy_kwh','charge_min','elapsed_min']:
            av=a.loc[a[field].ge(0) if field!='elapsed_min' else a[field].gt(0),field]
            bv=b['charge_min' if field=='elapsed_min' else field]
            for stat,fn in [('mean',lambda v:v.mean()),('p50',lambda v:v.quantile(.5)),('p90',lambda v:v.quantile(.9))]:
                x,y=fn(av),fn(bv);dist.append(dict(scope=scope,direction=direction,field=field,stat=stat,observed=x,model=y,difference=y-x,relative_pct=(y/x-1)*100,observed_n=len(av),model_n=len(bv),judgment='CONDITIONAL_UNPAIRED'))
        for day in sorted(a.date.unique()):
            ah=a[a.date.eq(day)].groupby('hour').size().reindex(range(24),fill_value=0)
            bh=b[b.date.eq(day)].groupby('hour').size().reindex(range(24),fill_value=0)
            ap=ah.index[ah.eq(ah.max())].tolist();bp=bh.index[bh.eq(bh.max())].tolist()
            peaks.append(dict(scope=scope,direction=direction,date=day,observed_peak_hours=json.dumps(ap),model_peak_hours=json.dumps(bp),min_circular_hour_gap=min(min(abs(x-y),24-abs(x-y)) for x in ap for y in bp)))
pd.DataFrame(metrics).to_csv(OUT/'comparison_metrics.csv',index=False,encoding='utf-8-sig')
pd.DataFrame(dist).to_csv(OUT/'comparison_distributions.csv',index=False,encoding='utf-8-sig')
pd.DataFrame(peaks).to_csv(OUT/'comparison_peak_hours.csv',index=False,encoding='utf-8-sig')
# Recorded intervals allow concurrent-session diagnostics, not real queue observations.
concur=[]
for key,g in all_model[all_model.start.lt(hi)&all_model.end.gt(lo)].groupby(['station_name','direction']):
    events=[]
    for r in g.itertuples():events.extend([(max(r.start,lo),1),(min(r.end,hi),-1)])
    active=peak=0
    for _,v in sorted(events):active+=v;peak=max(peak,active)
    concur.append(dict(station_name=key[0],direction=key[1],model_peak=peak))
con=pd.read_csv(BASE/'observed_concurrency.csv').merge(pd.DataFrame(concur),on=['station_name','direction'],how='left').fillna({'model_peak':0})
con['difference']=con.model_peak-con.peak_recorded_sessions;con.to_csv(OUT/'comparison_concurrency.csv',index=False,encoding='utf-8-sig')
counts=pd.read_csv(MODEL/'demand_counts.csv')
assert (counts.n_ev==counts.n_no_charge+counts.n_infeasible+counts.n_charge_plan).all()
checks=dict(observed_sessions=len(obs),common_ids=len(common),common_observed_sessions=int(obs.common_id.sum()),generated_ev=int(counts.n_ev.sum()),generated_charge_plan_vehicles=int(counts.n_charge_plan.sum()),model_all_events=len(all_model),model_events_in_window=len(model),model_events_at_unobserved_stations=int((~model.observed_station).sum()),model_events_outside_window=len(all_model)-len(model),common_model_events=int(model.common_id.sum()),unique_observed_source_rows=int(obs.source_row.nunique()),runs_done=int(manifest.status.eq('DONE').sum()),physical_occupancy_validation='NOT_IDENTIFIABLE',paired_session_time_MAE='NOT_IDENTIFIABLE_NO_PAIRED_VEHICLE_IDS')
(OUT/'comparison_checks.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print(json.dumps(checks),flush=True)
