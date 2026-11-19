"""Checkpointed all-well BCER report extraction; no well-count sampling limit.

Raw PDFs are temporary. Facts and curve points keep source/version/page/units.
Unsupported chart templates and scanned pages remain explicitly unresolved.
"""
from pathlib import Path
import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time

from bcer_client import BCERClient, SourceError

FRACTURE = re.compile(r'fractur|fracc?ing|stimulation|proppant|\bISIP\b|frac[- ]?hit|offset.{0,30}pressure',re.I)
REPORT = re.compile(r'_(?:COMP|FRAC|STIM|PERF)(?:_|\.)',re.I)
PARAMETERS = [
 ('breakdown_pressure',r'(?:BD|(?:formation\s+)?break\s*down)\s+pressure','pressure'),
 ('isip',r'(?:surface\s+)?(?:I\.?S\.?I\.?P\.?|instant(?:aneous)?\s+shut[- ]?in\s+pressure)','pressure'),
 ('maximum_treating_pressure',r'max(?:imum)?\.?\s+treat(?:ing|ment)\s+pressure','pressure'),
 ('average_treating_pressure',r'avg\.?\s+treat(?:ing|ment)\s+pressure|average\s+treat(?:ing|ment)\s+pressure','pressure'),
 ('average_rate',r'(?:avg\.?|average)\s+(?:slurry\s+|treat(?:ing|ment)\s+)?rate','rate'),
 ('maximum_treating_rate',r'max(?:imum)?\.?\s+(?:slurry\s+|treat(?:ing|ment)\s+)rate','rate'),
 ('breakdown_rate',r'(?:BD|break\s*down)\s+rate','rate'),
 ('cluster_count_reported',r'(?:number\s+of\s+(?:perf(?:oration)?\s+)?clusters|no\.?\s+of\s+clusters)','count'),
]

def state_db(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(folder/'extraction.sqlite');db.execute('PRAGMA journal_mode=DELETE')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS directories(path TEXT PRIMARY KEY,done INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS wells(path TEXT PRIMARY KEY,wa TEXT,done INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,wa TEXT,name TEXT,modified TEXT,size INTEGER,
      family TEXT,is_current INTEGER,status TEXT,sha256 TEXT,pages INTEGER,template TEXT,error TEXT);
    CREATE TABLE IF NOT EXISTS pages(path TEXT,page INTEGER,stage_label TEXT,text_chars INTEGER,
      status TEXT,formation TEXT,frac_hit_mention INTEGER,PRIMARY KEY(path,page));
    CREATE TABLE IF NOT EXISTS facts(path TEXT,page INTEGER,stage_label TEXT,parameter TEXT,value REAL,
      unit TEXT,raw_text TEXT,status TEXT);
    CREATE TABLE IF NOT EXISTS points(path TEXT,page INTEGER,stage_label TEXT,series TEXT,unit TEXT,
      feature TEXT,path_id INTEGER,time_local TEXT,value REAL,x_pdf_pt REAL,y_pdf_pt REAL,
      axis_residual REAL,time_residual_seconds REAL,line_half_width REAL,status TEXT);
    ''')
    return db

def snapshot(db,folder,phase,shard):
    count=lambda sql:db.execute(sql).fetchone()[0]
    v={'phase':phase,'shard':shard,'updated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
       'scope':'All well directories in assigned BCER source partitions; no 100-well limit',
       'directories_queued':count('select count(*) from directories'),'directories_pending':count('select count(*) from directories where done=0'),
       'wells_queued':count('select count(*) from wells'),'wells_processed':count('select count(*) from wells where done=1'),
       'current_reports':count('select count(*) from files where is_current=1'),
       'reports_text_processed':count("select count(*) from files where status='text_processed' and is_current=1"),
       'reports_failed_or_deferred':count("select count(*) from files where status='deferred' and is_current=1"),
       'fracture_pages':count("select count(*) from pages where status like 'fracture%'"),
       'scanned_pages_pending_ocr':count("select count(*) from pages where status='scanned_pending_ocr'"),
       'curve_pages_pending_review':count("select count(*) from pages where status='fracture_chart_pending_review'"),
       'parameter_candidates':count('select count(*) from facts'),'curve_feature_points':count('select count(*) from points')}
    (Path(folder)/'progress.json').write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(v,ensure_ascii=False),flush=True)
    return v

def revision(record):
    name=record['Name'].upper()
    match=re.search(r'[_ ]REVISED(\d*)',name)
    rank=int(match[1] or 0)+1 if match else 0
    family=re.sub(r'[_ ]REVISED\d*','',name)
    family=re.sub(r'_INITIAL(?: COMP)?(?=\.PDF$)','',family)
    return family,(rank,record.get('LastModified',''),name)

def index_source(client,db,folder,shard,shards,seconds):
    started=time.monotonic()
    if not db.execute('select 1 from directories limit 1').fetchone():
        roots=client.list_folder('/WellData')
        roots=sorted((r for r in roots if r.get('IsFolder')),key=lambda r:r['Name'])
        for i,r in enumerate(roots):
            if i%shards==shard:db.execute('insert or ignore into directories(path) values(?)',(r['full_path'],))
        db.commit()
    completed=0
    while time.monotonic()-started<seconds:
        row=db.execute('select path from directories where done=0 order by path desc limit 1').fetchone()
        if not row:break
        path=row[0];children=client.list_folder(path)
        for r in children:
            if not r.get('IsFolder'):continue
            if re.fullmatch(r'\d{1,6}',r['Name']):
                db.execute('insert or ignore into wells(path,wa) values(?,?)',(r['full_path'],str(int(r['Name']))))
            else:db.execute('insert or ignore into directories(path) values(?)',(r['full_path'],))
        db.execute('update directories set done=1 where path=?',(path,));db.commit();completed+=1
        if completed%25==0:snapshot(db,folder,'indexing',shard)
    snapshot(db,folder,'index_ready' if not db.execute('select 1 from directories where done=0').fetchone() else 'index_checkpoint',shard)

def parameter_candidates(text):
    result=[]
    # Require the value and its physical unit on the same line; preserve native units.
    for parameter,label,kind in PARAMETERS:
        unit=r'(MPa|kPa|psi)' if kind=='pressure' else r'(m[³3]\s*/\s*min|bbl\s*/\s*min|bpm)' if kind=='rate' else r'()'
        pattern=r'(?im)\b(?:'+label+r')[^\S\r\n]*[:=]?[^\S\r\n]*(-?\d+(?:\.\d+)?)[^\S\r\n]*'+unit+(r'(?![A-Za-z])' if kind!='count' else r'(?![\d.])')
        for m in re.finditer(pattern,text):
            value=float(m[1])
            if math.isfinite(value):result.append((parameter,value,m[2] or 'count',m[0][:240]))
    return result

def stage_label(text):
    # "Stage Summary / Stage Number" often describes a pumping step, not a frac stage.
    labels=set(re.findall(r'(?im)\b(?:Interval|Frac(?:ture)?\s+Stage)\s*[:#-]?\s*(\d+[A-Za-z]?)\b',text))
    return next(iter(labels)) if len(labels)==1 else None

def vector_features(pdf_path,page_number,text):
    if not re.search(r'Interval\s+\d+\s*[–-]\s*(?:Entire Treatment|Treatment Part)',text):
        return []
    # Restrict the previously checked adapter to its actual axes, units and legend.
    if not all(re.search(p,text,re.I) for p in [r'Treating\s+Pressure',r'Slurry\s+Rate',r'Proppant',r'MPa']):
        return []
    import pdfplumber
    from bcer_vectors import SERIES,calibrate,read_curves
    with pdfplumber.open(pdf_path,pages=[page_number]) as pdf:
        page=pdf.pages[0]
        try:
            words=page.extract_words(extra_attrs=['non_stroking_color'])
            required=(('Treating','Pressure'),('Slurry','Rate'),('Slurry','Proppant'),('BH','Proppant'))
            for color,tokens in zip(SERIES,required):
                legend={w['text'] for w in words if w.get('non_stroking_color')==color}
                if not set(tokens)<=legend:return []
            cal=calibrate(page)
            if len(cal['time']['anchors'])<3 or cal['time']['slope']<=0 or cal['time']['max_residual']>2:return []
            paths=read_curves(page,cal)
            output=[]
            for pid,path in enumerate(paths):
                points=path['points'];axis=cal['y_axes'][path['axis']]
                span=abs(axis['slope']*(cal['bounds'][3]-cal['bounds'][1]))
                if axis['max_residual']>0.001*max(span,1e-12):
                    raise ValueError('axis_calibration_requires_review')
                for label,p in [('path_first',points[0]),('path_last',points[-1]),
                                ('path_minimum',min(points,key=lambda v:v[3])),('path_maximum',max(points,key=lambda v:v[3]))]:
                    x,y,t,value=p
                    stamp=(dt.datetime.fromisoformat(cal['time_origin_local'])+dt.timedelta(seconds=t)).isoformat(timespec='microseconds')
                    output.append((path['series'],path['unit'],label,pid,stamp,value,x,y,axis['max_residual'],
                                   cal['time']['max_residual'],abs(axis['slope'])*path['linewidth_pt']/2,
                                   'native_vector_chart_point_not_instrument_sample'))
            return output
        finally:page.close()

def extract_pdf(path,remote,db):
    import pypdfium2 as pdfium
    document=pdfium.PdfDocument(path);templates=set();pages=len(document)
    try:
        for i in range(pages):
            page=document[i];textpage=page.get_textpage()
            try:text=textpage.get_text_bounded()
            finally:textpage.close();page.close()
            template=next((s for s in ['HALLIBURTON','CALFRAC','TRICAN','SCHLUMBERGER','STEP ENERGY','BAKER HUGHES'] if s in text.upper()),None)
            if template:templates.add(template)
            frac=bool(FRACTURE.search(text));stage=stage_label(text)
            state='scanned_pending_ocr' if len(text.strip())<40 else 'fracture_text' if frac else 'other_text'
            formation='; '.join(sorted(set(m.group(0) for m in re.finditer(r'\b(?:Upper|Middle|Lower)\s+Montney\b',text,re.I))))
            hit=bool(re.search(r'frac[- ]?hit|fracture[- ]driven|offset.{0,60}pressure|pressure.{0,60}offset',text,re.I))
            if frac:
                report_well={str(int(x)) for x in re.findall(r'(?i)\bLicen[sc]e\s*#\s*:?\s*(\d{3,6})\b',text)}
                same_well=report_well=={str(int(remote.rsplit('/',2)[-2]))} if remote.rsplit('/',2)[-2].isdigit() else False
                measured=stage and same_well and re.search(r'Interval\s+\w+\s*[–-]\s*Service Report',text,re.I) and 'FRACTURING SERVICE REPORT' in text.upper()
                for parameter,value,unit,raw in parameter_candidates(text):
                    db.execute('insert into facts values(?,?,?,?,?,?,?,?)',(remote,i+1,stage,parameter,value,unit,raw,'stage_service_report' if measured else 'literal_candidate_requires_context_review'))
                chart_hint=bool(re.search(r'Entire\s+Treatment|Treatment\s+Part\s+[AB]|Treatment\s+(?:Plot|Chart)|(?:time|pressure)\s+vs\.?\s+(?:time|pressure)',text,re.I))
                if chart_hint:
                    try:points=vector_features(path,i+1,text)
                    except Exception:points=[]
                    state='fracture_chart_vector_extracted' if points else 'fracture_chart_pending_review'
                    for point in points:
                        db.execute('insert into points values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(remote,i+1,stage,*point))
            db.execute('insert or replace into pages values(?,?,?,?,?,?,?)',(remote,i+1,stage,len(text),state,formation,int(hit)))
        return pages,'; '.join(sorted(templates)) or 'unclassified'
    finally:document.close()

def process_wells(client,db,folder,shard,seconds,stop_after_facts=False):
    started=time.monotonic();folder=Path(folder)
    while time.monotonic()-started<seconds:
        row=db.execute('select path,wa from wells where done=0 order by cast(wa as integer) desc limit 1').fetchone()
        if not row:break
        well_path,wa=row;records=client.list_folder(well_path)
        candidates=[r for r in records if not r.get('IsFolder') and r['Name'].upper().endswith('.PDF') and REPORT.search(r['Name'])]
        groups=collections.defaultdict(list)
        for r in candidates:groups[revision(r)[0]].append(r)
        latest=[]
        for family,items in groups.items():
            best=max(items,key=lambda r:revision(r)[1]);latest.append(best)
            for r in items:
                db.execute('insert into files(path,wa,name,modified,size,family,is_current,status) values(?,?,?,?,?,?,?,?) '
                           'on conflict(path) do update set is_current=excluded.is_current',
                           (r['full_path'],wa,r['Name'],r['LastModified'],r['Size'],family,int(r is best),'queued' if r is best else 'superseded'))
        db.commit();finished=True
        latest.sort(key=lambda r:(bool(re.search(r'FLOWBACK|PROD.?EQUIP|ABANDON',r['Name'],re.I)), -int(r.get('Size',0))))
        for record in latest:
            if time.monotonic()-started>=seconds:finished=False;break
            remote=record['full_path'];existing=db.execute('select status from files where path=?',(remote,)).fetchone()[0]
            if existing in ('text_processed','deferred'):continue
            try:
                with tempfile.TemporaryDirectory(prefix='bcer-report-') as scratch:
                    pdf=Path(scratch)/'source.pdf';meta=client.download(record,pdf)
                    if pdf.open('rb').read(5)!=b'%PDF-':raise SourceError('download_is_not_pdf')
                    with db:
                        for table in ('facts','points','pages'):db.execute(f'delete from {table} where path=?',(remote,))
                        pages,template=extract_pdf(pdf,remote,db)
                        db.execute("update files set status='text_processed',sha256=?,pages=?,template=?,error=null where path=?",(meta['sha256'],pages,template,remote))
            except Exception as exc:
                error=str(exc) if isinstance(exc,SourceError) else type(exc).__name__
                db.execute("update files set status='deferred',error=? where path=?",(error,remote));db.commit()
            status,error=db.execute('select status,error from files where path=?',(remote,)).fetchone()
            print(json.dumps({'wa':wa,'report':record['Name'],'status':status,'error':error}),flush=True)
            if stop_after_facts and db.execute('select 1 from facts limit 1').fetchone():
                snapshot(db,folder,'early_evidence',shard)
                return
        if finished:db.execute('update wells set done=1 where path=?',(well_path,));db.commit()
        snapshot(db,folder,'extracting',shard)
    snapshot(db,folder,'extraction_checkpoint',shard)

def main():
    p=argparse.ArgumentParser();p.add_argument('--phase',choices=['index','extract'],required=True)
    p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=4)
    p.add_argument('--seconds',type=int,default=18000);p.add_argument('--output',default='results');args=p.parse_args()
    if not 0<=args.shard<args.shards:raise ValueError('invalid_shard')
    db=state_db(args.output);client=BCERClient();client.login()
    try:
        if args.phase=='index':index_source(client,db,args.output,args.shard,args.shards,args.seconds)
        else:process_wells(client,db,args.output,args.shard,args.seconds)
    finally:db.close()

if __name__=='__main__':main()
