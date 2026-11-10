"""Read the user-supplied September workbook; reuse #64 matching, never its writer."""
from pathlib import Path
import sys,json,hashlib,collections,math
import pandas as pd,numpy as np
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/processed/issue162_20261108'
OUT.mkdir(parents=True, exist_ok=True)
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'scripts')]
from analyze_sp3_64_charging_history import iter_xlsx_rows
from evdt.io.charger_ingest import resolve_charger_key
P = ROOT / 'data/raw/issue162_chuseok/charging_history.xlsx'
if not P.is_file():
    raise FileNotFoundError(f'원본 충전이력 XLSX 없음: {P}')
sha=hashlib.file_digest(P.open('rb'),'sha256').hexdigest()
supply=pd.read_csv(ROOT/'data/processed/sp3_64_current_charger_supply.csv')
known=set(zip(supply.station_name,supply.direction)); assert len(known)==35
sep=pd.read_csv(ROOT/'data/processed/issue145_chuseok/sep18_gyeongbu_supply_candidates.csv',dtype={'station_id':str,'charger_id':str})
sep['key']=sep.rest_name.map(lambda x:resolve_charger_key(x,known))
sidmap={sid:g.key.iloc[0] for sid,g in sep.groupby('station_id') if g.key.nunique()==1 and g.key.iloc[0] is not None}
ids=set(zip(sep.station_id,sep.charger_id.str.zfill(2)))
it=iter_xlsx_rows(P); header=next(it); cols={str(x).strip():i for i,x in enumerate(header)}
assert {'충전소ID','충전기ID','충전시작일시','충전종료일시','충전량','충전시간'}<=set(cols)
counter=collections.Counter(); prefix=collections.Counter(); unmatched=collections.Counter(); selected=[]; clashes=[]; startmin=None;startmax=None
for row in it:
    counter['all_rows']+=1
    if counter['all_rows']%250000==0:print('Read',counter['all_rows'],flush=True)
    get=lambda k:str(row[cols[k]] if cols[k]<len(row) else '').strip()
    start=get('충전시작일시');sid=get('충전소ID');cid=get('충전기ID').zfill(2);name=get('충전소명')
    prefix[sid[:2]]+=1
    if len(start)==14 and start.isdigit():
        startmin=min(startmin or start,start);startmax=max(startmax or start,start)
        counter['outside_september']+=int(not '20260901000000'<=start<'20261001000000')
    else:counter['invalid_start_format']+=1
    key_name=resolve_charger_key(name,known);key_id=sidmap.get(sid)
    if key_name and key_id and key_name!=key_id:clashes.append([sid,name,key_name,key_id]);continue
    key=key_id or key_name
    if key is None:
        if '휴게소' in name:unmatched[name]+=1
        continue
    selected.append(dict(station_name=key[0],direction=key[1],source_station_name=name,station_id=sid,charger_id=cid,address=get('주소'),start_raw=start,end_raw=get('충전종료일시'),charge_time_raw=get('충전시간'),energy_raw=get('충전량'),charger_power=get('충전기용량(KW)'),id_in_sep18=(sid,cid) in ids,mapping_basis='sep18_station_id' if key_id else 'issue64_name_direction'))
d=pd.DataFrame(selected)
d['start']=pd.to_datetime(d.start_raw,format='%Y%m%d%H%M%S',errors='coerce');d['end']=pd.to_datetime(d.end_raw,format='%Y%m%d%H%M%S',errors='coerce')
d['energy_kwh']=pd.to_numeric(d.energy_raw,errors='coerce');d['elapsed_min']=(d.end-d.start).dt.total_seconds()/60
d['reported_charge_min']=pd.to_timedelta(d.charge_time_raw,errors='coerce').dt.total_seconds()/60
d['duplicate_exact']=d.duplicated(['station_id','charger_id','start_raw','end_raw','charge_time_raw','energy_raw'],keep='first')
d['valid_elapsed']=d.elapsed_min.gt(0)&np.isfinite(d.elapsed_min);d['valid_energy']=d.energy_kwh.ge(0)&np.isfinite(d.energy_kwh)
d['date']=d.start.dt.strftime('%Y-%m-%d');d['hour']=d.start.dt.hour
holiday=d[d.start.ge('2026-09-24')&d.start.lt('2026-09-28')].copy()
holiday.to_csv(OUT/'sp5_charging_chuseok_sessions.csv',index=False,encoding='utf-8-sig')
clean=holiday[~holiday.duplicate_exact].copy()
def stats(g):
    out={'session_starts':len(g),'valid_energy_n':int(g.valid_energy.sum()),'valid_elapsed_n':int(g.valid_elapsed.sum()),'zero_reported_charge_n':int(g.reported_charge_min.eq(0).sum()),'cross_period_end_n':int(g.end.ge('2026-09-28').sum())}
    for field,mask in [('energy_kwh',g.valid_energy),('elapsed_min',g.valid_elapsed),('reported_charge_min',g.reported_charge_min.ge(0))]:
        a=g.loc[mask,field]; out.update({field+'_mean':a.mean(),field+'_p50':a.quantile(.5),field+'_p90':a.quantile(.9)})
    return out
tables={}
for label,keys in [('station',['direction','station_name']),('daily',['date','direction']),('hourly',['date','hour','direction']),('station_hourly',['date','hour','direction','station_name'])]:
    tables[label]=pd.DataFrame([{**dict(zip(keys,k if isinstance(k,tuple) else [k])),**stats(g)} for k,g in clean.groupby(keys)])
    tables[label].to_csv(OUT/f'sp5_charging_{label}.csv',index=False,encoding='utf-8-sig')
coverage=supply[['direction','station_name']].merge(tables['station'],on=['direction','station_name'],how='left')
coverage['observation_status']=np.where(coverage.session_starts.notna(),'OBSERVED','NOT_OBSERVED_NA')
coverage.to_csv(OUT/'sp5_charging_station_coverage.csv',index=False,encoding='utf-8-sig')
cross=d.groupby(['direction','station_name','station_id','charger_id','source_station_name','address','mapping_basis','id_in_sep18'],dropna=False).size().rename('september_rows').reset_index()
cross.to_csv(OUT/'sp5_charging_identity_crosswalk.csv',index=False,encoding='utf-8-sig')
pd.DataFrame(unmatched.items(),columns=['unmatched_rest_name','rows']).to_csv(OUT/'sp5_charging_unmatched_names.csv',index=False,encoding='utf-8-sig')
summary=dict(source_path=str(P),sha256_before=sha,sha256_after=hashlib.file_digest(P.open('rb'),'sha256').hexdigest(),columns=header,counters=dict(counter),station_prefix_counts=dict(prefix),start_min=startmin,start_max=startmax,matched_september_rows=len(d),holiday_raw_rows=len(holiday),holiday_exact_duplicate_rows=int(holiday.duplicate_exact.sum()),holiday_unique_rows=len(clean),observed_stations=int(coverage.session_starts.notna().sum()),mapping_conflicts=clashes,holiday_id_in_sep18=int(clean.id_in_sep18.sum()),missing_end=int(clean.end.isna().sum()),nonpositive_elapsed=int(clean.elapsed_min.le(0).sum()),negative_energy=int(clean.energy_kwh.lt(0).sum()),missing_energy=int(clean.energy_kwh.isna().sum()),reported_charge_exceeds_elapsed=int((clean.reported_charge_min>clean.elapsed_min).sum()),**stats(clean))
assert summary['sha256_before']==summary['sha256_after']
(OUT/'sp5_charging_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=True),flush=True)
