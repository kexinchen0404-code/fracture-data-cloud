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
import mmap
import os
import re
import shutil
import sqlite3
import subprocess
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
    CREATE TABLE IF NOT EXISTS fragments(path TEXT,object_id INTEGER,generation INTEGER,byte_offset INTEGER,
      status TEXT,text_chars INTEGER,stage_label TEXT,parameters TEXT,PRIMARY KEY(path,object_id,generation));
    ''')
    return db

def snapshot(db,folder,phase,shard):
    count=lambda sql:db.execute(sql).fetchone()[0]
    v={'phase':phase,'shard':shard,'updated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
       'scope':'All well directories in assigned BCER source partitions; no 100-well limit',
       'directories_queued':count('select count(*) from directories'),'directories_pending':count('select count(*) from directories where done=0'),
       'wells_queued':count('select count(*) from wells'),'wells_processed':count('select count(*) from wells where done=1'),
       'wells_unresolved':count('select count(*) from wells where done=2'),
       'wells_pending':count('select count(*) from wells where done=0'),
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
    modified=re.search(r'\d{10,}',str(record.get('LastModified','')))
    return family,(int(modified[0]) if modified else 0,rank,name)

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

def text_pages(path, reader):
    """Yield one complete page at a time, in original PDF order."""
    if reader == 'pdfium':
        import pypdfium2 as pdfium
        try:
            with pdfium.PdfDocument(path) as doc:
                for idx in range(len(doc)):
                    page=doc[idx]
                    try:
                        tp=page.get_textpage()
                        try:yield tp.get_text_bounded()
                        finally:tp.close()
                    finally:page.close()
        except pdfium.PdfiumError as exc:
            raise SourceError('pdfium_read_error_'+str(getattr(exc,'err_code','unknown'))) from None
    elif reader == 'mupdf':
        try:
            import pymupdf
        except ImportError:
            raise SourceError('mupdf_unavailable') from None
        try:
            with pymupdf.open(path) as doc:
                if doc.needs_pass:raise SourceError('pdf_requires_open_password')
                for page in doc:yield page.get_text('text',sort=True)
        except Exception as exc:
            if isinstance(exc,SourceError):raise
            raise SourceError('mupdf_'+type(exc).__name__) from None
    elif reader=='poppler':
        if not shutil.which('pdftotext') or not shutil.which('pdfinfo'):
            raise SourceError('poppler_unavailable')
        env={**os.environ,'LC_ALL':'C'}
        info=subprocess.run(['pdfinfo',str(path)],capture_output=True,env=env,timeout=90)
        if b'incorrect password' in info.stderr.lower():raise SourceError('pdf_requires_open_password')
        pg=re.search(rb'(?m)^Pages:\s*(\d+)\s*$',info.stdout)
        if info.returncode or not pg:raise SourceError('poppler_cannot_read_page_tree')
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'pages.txt'
            result=subprocess.run(['pdftotext','-layout','-enc','UTF-8',str(path),str(out)],
                                  capture_output=True,env=env,timeout=300)
            if result.returncode:raise SourceError('poppler_text_error_'+str(result.returncode))
            n=0;buffer=''
            with out.open(encoding='utf-8') as f:
                while chunk:=f.read(65536):
                    parts=(buffer+chunk).split('\f');buffer=parts.pop()
                    for page in parts:
                        n+=1;yield page
            if n!=int(pg[1]) or buffer.strip():raise SourceError('poppler_page_count_mismatch')
    elif reader=='pypdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            raise SourceError('pypdf_unavailable') from None
        try:
            with open(path,'rb') as f:
                doc=PdfReader(f,strict=False)
                for page in doc.pages:yield page.extract_text() or ''
        except Exception as exc:
            if type(exc).__name__ in ('FileNotDecryptedError','WrongPasswordError'):
                raise SourceError('pdf_requires_open_password') from None
            raise SourceError('pypdf_'+type(exc).__name__) from None
    elif reader=='qpdf':
        if not shutil.which('qpdf'):raise SourceError('qpdf_unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            fixed=Path(tmp)/'reconstructed.pdf'
            proc=subprocess.run(['qpdf',str(path),str(fixed)],capture_output=True,timeout=180)
            if b'invalid password' in proc.stderr.lower():raise SourceError('pdf_requires_open_password')
            if proc.returncode not in (0,3) or not fixed.is_file():raise SourceError('qpdf_cannot_reconstruct')
            yield from text_pages(fixed,'pdfium')
    elif reader=='original_tree':
        from bcer_damaged import original_tree_text
        yield from original_tree_text(path)


def source_structure(path):
    # Object streams can hide page markers. These counts diagnose the source only.
    with open(path,'rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as data:
        return {'bytes':len(data),'eof_marker':data.rfind(b'%%EOF'),'xref_marker':data.rfind(b'startxref'),
                'plain_page_markers':sum(1 for _ in re.finditer(rb'/Type\s*/Page\b',data)),
                'object_stream':bool(re.search(rb'/ObjStm\b',data)),
                'embedded_files':bool(re.search(rb'/(?:EmbeddedFiles|EmbeddedFile)\b',data)),
                'encrypted_marker':bool(re.search(rb'/Encrypt\b',data)),
                'additional_pdf_headers':sum(1 for _ in re.finditer(rb'%PDF-\d\.\d',data))-1}


def extract_pdf(path,remote,db,wa):
    errors=[]
    for reader in ('pdfium','mupdf','poppler','pypdf','qpdf','original_tree'):
        if errors:
            for table in ('facts','points','pages'):db.execute(f'delete from {table} where path=?',(remote,))
        try:
            pages,template=store_pdf_pages(path,remote,db,wa,text_pages(path,reader))
            if not pages:raise SourceError(reader+'_empty_page_tree')
        except (SourceError,subprocess.TimeoutExpired) as exc:
            reason=str(exc) if isinstance(exc,SourceError) else reader+'_timeout'
            errors.append(reason)
            if reason=='pdf_requires_open_password':break
            continue
        return pages,template,reader
    print('UNREADABLE_SOURCE',json.dumps({'wa':wa,'path':remote,**source_structure(path)}),flush=True)
    raise SourceError(' / '.join(errors))


def store_pdf_pages(path,remote,db,wa,text_iter):
    templates=set();pages=0
    try:
        for i,text in enumerate(text_iter):
            pages=i+1
            template=next((s for s in ['HALLIBURTON','CALFRAC','TRICAN','SCHLUMBERGER','STEP ENERGY','BAKER HUGHES'] if s in text.upper()),None)
            if template:templates.add(template)
            frac=bool(FRACTURE.search(text));stage=stage_label(text)
            state='scanned_pending_ocr' if len(text.strip())<40 else 'fracture_text' if frac else 'other_text'
            formation='; '.join(sorted(set(m.group(0) for m in re.finditer(r'\b(?:Upper|Middle|Lower)\s+Montney\b',text,re.I))))
            hit=bool(re.search(r'frac[- ]?hit|fracture[- ]driven|offset.{0,60}pressure|pressure.{0,60}offset',text,re.I))
            if frac:
                report_well={str(int(x)) for x in re.findall(r'(?i)\bLicen[sc]e\s*#\s*:?\s*(\d{3,6})\b',text)}
                same_well=report_well=={str(int(wa))}
                measured=stage and same_well and re.search(r'Interval\s+\w+\s*[–-]\s*Service Report',text,re.I) and 'FRACTURING SERVICE REPORT' in text.upper()
                for parameter,value,unit,raw in parameter_candidates(text):
                    db.execute('insert into facts values(?,?,?,?,?,?,?,?)',(remote,i+1,stage,parameter,value,unit,raw,'stage_service_report' if measured else 'literal_candidate_requires_context_review'))
                chart_hint=bool(re.search(r'Entire\s+Treatment|Treatment\s+Part\s+[AB]|Treatment\s+(?:Plot|Chart)|(?:time|pressure)\s+vs\.?\s+(?:time|pressure)',text,re.I))
                if chart_hint:
                    try:points=vector_features(path,i+1,text) if stage and same_well else []
                    except Exception:points=[]
                    state='fracture_chart_vector_extracted' if points else 'fracture_chart_pending_review'
                    for point in points:
                        db.execute('insert into points values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(remote,i+1,stage,*point))
            db.execute('insert or replace into pages values(?,?,?,?,?,?,?)',(remote,i+1,stage,len(text),state,formation,int(hit)))
        return pages,'; '.join(sorted(templates)) or 'unclassified'
    finally:text_iter.close()

def process_wells(client,db,folder,shard,seconds,stop_after_facts=False):
    started=time.monotonic();folder=Path(folder);processed=0
    while time.monotonic()-started<seconds:
        row=db.execute('select path,wa from wells where done=0 order by rowid limit 1').fetchone()
        if not row:break
        well_path,wa=row;records=[];visit=[well_path];visited=set()
        try:
            while visit:
                path=visit.pop()
                if path in visited:continue
                visited.add(path)
                for child in client.list_folder(path):
                    if child.get('IsFolder'):
                        if child['full_path'].startswith(well_path+'/'):visit.append(child['full_path'])
                    else:records.append(child)
        except SourceError as exc:
            print(json.dumps({'wa':wa,'directory_error':str(exc)}),flush=True)
            db.execute('update wells set done=2 where path=?',(well_path,));db.commit()
            continue
        candidates=[r for r in records if not r.get('IsFolder') and r['Name'].upper().endswith('.PDF') and REPORT.search(r['Name'])]
        groups=collections.defaultdict(list)
        for r in candidates:groups[(r['RelativePath'],revision(r)[0])].append(r)
        latest=[]
        db.execute('update files set is_current=0 where wa=? and path like ?', (wa,well_path+'/%'))
        for (_,family),items in groups.items():
            best=max(items,key=lambda r:revision(r)[1]);latest.append(best)
            for r in items:
                db.execute('insert into files(path,wa,name,modified,size,family,is_current,status) values(?,?,?,?,?,?,?,?) '
                           'on conflict(path) do update set is_current=excluded.is_current, '
                           'status=case when files.modified!=excluded.modified or files.size!=excluded.size '
                           "or (files.status='superseded' and excluded.is_current=1) then excluded.status else files.status end, "
                           'sha256=case when files.modified!=excluded.modified or files.size!=excluded.size then null else files.sha256 end, '
                           'modified=excluded.modified,size=excluded.size',
                           (r['full_path'],wa,r['Name'],r['LastModified'],r['Size'],family,int(r is best),'queued' if r is best else 'superseded'))
        db.commit();finished=True
        latest.sort(key=lambda r:(bool(re.search(r'FLOWBACK|PROD.?EQUIP|ABANDON|SLICKLINE|GASLIFT',r['Name'],re.I)), -int(r.get('Size',0))))
        for record in latest:
            if time.monotonic()-started>=seconds:finished=False;break
            remote=record['full_path'];existing,previous_sha=db.execute('select status,sha256 from files where path=?',(remote,)).fetchone()
            if existing in ('text_processed','deferred'):continue
            try:
                with tempfile.TemporaryDirectory(prefix='bcer-report-') as scratch:
                    pdf=Path(scratch)/'source.pdf';meta=client.download(record,pdf)
                    with pdf.open('rb') as handle:
                        if handle.read(5)!=b'%PDF-':raise SourceError('download_is_not_pdf')
                    with db:
                        db.execute('update files set sha256=?,pages=null,template=null where path=?',(meta['sha256'],remote))
                    print('SOURCE_SIZE_HASH',json.dumps({'wa':wa,'bytes':meta['bytes'],'sha256':meta['sha256'],'matches_prior_transfer':meta['sha256']==previous_sha}),flush=True)
                    try:
                        with db:
                            for table in ('facts','points','pages','fragments'):db.execute(f'delete from {table} where path=?',(remote,))
                            pages,template,reader=extract_pdf(pdf,remote,db,wa)
                            db.execute("update files set status='text_processed',sha256=?,pages=?,template=?,error=null where path=?",(meta['sha256'],pages,template,remote))
                            print(json.dumps({'wa':wa,'current_file':record['Name'],'pages':pages,'bytes':meta['bytes'],'sha256':meta['sha256'],'reader':reader}),flush=True)
                    except SourceError as exc:
                        if 'pdf_requires_open_password' not in str(exc) and source_structure(pdf)['eof_marker']==-1:
                            from bcer_damaged import fragment_rows
                            count=0
                            try:
                                with db:
                                    db.execute('delete from fragments where path=?',(remote,))
                                    for obj,gen,offset,state,txt in fragment_rows(pdf):
                                        values=parameter_candidates(txt)
                                        db.execute('insert into fragments values(?,?,?,?,?,?,?,?)',(remote,obj,gen,offset,state,len(txt),stage_label(txt),json.dumps(values,ensure_ascii=False)))
                                        count+=1
                                print('SOURCE_FRAGMENTS',json.dumps({'wa':wa,'objects':count}),flush=True)
                            except Exception as fragment_error:
                                print('SOURCE_FRAGMENT_ERROR',json.dumps({'wa':wa,'type':type(fragment_error).__name__}),flush=True)
                        raise
            except Exception as exc:
                error=str(exc) if isinstance(exc,SourceError) else type(exc).__name__
                db.execute("update files set status='deferred',error=? where path=?",(error,remote));db.commit()
            status,error=db.execute('select status,error from files where path=?',(remote,)).fetchone()
            print(json.dumps({'wa':wa,'report':record['Name'],'status':status,'error':error}),flush=True)
            if stop_after_facts and db.execute('select 1 from facts limit 1').fetchone():
                snapshot(db,folder,'early_evidence',shard)
                return
        if finished:
            unresolved=db.execute("select 1 from files where wa=? and is_current=1 and status='deferred' limit 1",(wa,)).fetchone()
            db.execute('update wells set done=? where path=?',(2 if unresolved else 1,well_path));db.commit()
        processed+=1
        if processed%12==0:snapshot(db,folder,'extracting',shard)
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
