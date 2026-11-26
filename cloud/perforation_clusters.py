"""Read explicitly executed, numbered perforations in the tabular daily reports."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zipfile import ZipFile

BASE = '0dfd5bb26dd78a10891925028f84d616fff36f19a2fac21b5ac185392aef7678'
COUNT = 8
NUM = r'([\d,]+(?:\.\d+)?)'
BLOCK = re.compile(r'Date\s+(\d+/\d+/\d+)\s+Type\s+Perforated\s+Interval Number\s+(\d+)\s+Cluster #\s+(\d+)\s+Top Depth \(mKB\)\s+'+NUM+r'\s+Btm \(mKB\)\s+'+NUM+r'(?=\s|$)',re.I)

def extract(path):
    from pypdf import PdfReader
    pdf=PdfReader(path);values=collections.defaultdict(lambda:collections.defaultdict(set))
    titles=set()
    for p,page in enumerate(pdf.pages,1):
        s=page.extract_text() or ''
        if 'Perforation Statuses' not in s or 'Daily Completion & Well Servicing Report' not in s:continue
        for date,st,cl,top,bottom in BLOCK.findall(s):
            a,b=(float(x.replace(',','')) for x in (top,bottom))
            if not (0<a<b and int(st)>0 and int(cl)>0):continue
            lines=s.strip().splitlines()
            if len(lines)>1:titles.add(lines[1].strip().upper())
            values[int(st),int(cl)][date,a,b].add(p)
    rows=[];conflicts=0
    for (st,cl),opts in sorted(values.items()):
        if len(opts)!=1 or len(titles)!=1:
            conflicts+=1;continue
        (date,a,b),pages=next(iter(opts.items()))
        rows.append([st,cl,a,b,date,sorted(pages)])
    return dict(rows=rows,conflicts=conflicts,conflict_stages=sorted({st for (st,_),v in values.items() if len(v)>1 or len(titles)!=1}),pages=len(pdf.pages))

def storage():
    import private_report_store as db
    db.PREFIX='cluster-review/'+BASE[:16]+'/'
    return db

def plan():
    from lxml import etree as E
    d=Path('baseline');p=next(d.glob('*.xlsx'))
    with p.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==BASE
    with ZipFile(d/'verified-stage-catalog.zip') as cat:
        ix=json.loads(cat.read('index.json'));assert ix['workbook_sha256']==BASE
        wanted=set()
        for wa in ix['well_index']:
            well=json.loads(cat.read('WA'+wa+'.json'))
            wanted.update((wa,name,sha) for name,sha in zip(well['source_files'],well['source_sha256']))
    located={}
    with ZipFile(p) as book:
        n='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
        sheet=next(t for t in E.fromstring(book.read('xl/workbook.xml')).iter(n+'sheet') if t.get('name')=='PDF报告清单')
        rid=next(v for k,v in sheet.attrib.items() if k.endswith('}id'))
        target=next(x.get('Target') for x in E.fromstring(book.read('xl/_rels/workbook.xml.rels')) if x.get('Id')==rid)
        with book.open(target.strip('/') if target.startswith('/') else 'xl/'+target) as f:
            for _,row in E.iterparse(f,tag=n+'row',events=['end']):
                cells={c.get('r').rstrip('0123456789'):''.join(c.itertext()) for c in row}
                key=tuple(cells.get(k) for k in ('A','B','E'))
                if key in wanted:located[key]=[key[0],key[1],key[2],cells['G']]
                row.clear()
                while row.getprevious() is not None:del row.getparent()[0]
    assert set(located)==wanted
    ordered=[located[k] for k in sorted(located)]
    storage().write([('plan/'+str(i),ordered[i::COUNT]) for i in range(COUNT)])
    print('SOURCE_REPORTS',len(ordered),'WELLS',len({r[0] for r in ordered}))

def scan(group):
    from bcer_client import BCERClient
    client=BCERClient();client.login();db=storage();folder_cache={};done=[]
    for wa,name,sha,path in db.get('plan/'+str(group)):
        if db.get('report/'+sha) is None:
            parent=path.rsplit('/',1)[0]
            if parent not in folder_cache:folder_cache[parent]={r['full_path']:r for r in client.list_folder(parent)}
            with tempfile.TemporaryDirectory() as tmp:
                pdf=Path(tmp)/'source.pdf';receipt=client.download(folder_cache[parent][path],pdf)
                if receipt['sha256']!=sha:raise ValueError('Source file changed since workbook revision')
                result=extract(pdf)
            rows=result.pop('rows');parts=list(db.chunks(rows))
            db.write([('rows/'+sha+'/'+str(j),part) for j,part in enumerate(parts)])
            db.write([('report/'+sha,dict(**result,part_count=len(parts),row_count=len(rows)))])
        done.append(sha)
    print('VERIFIED_REPORTS',len(done))

def collect():
    db=storage();reports=[]
    for i in range(COUNT):
        for wa,name,sha,path in db.get('plan/'+str(i)):
            m=db.get('report/'+sha)
            if m is None:raise ValueError('Missing report receipt')
            rows=[r for j in range(m['part_count']) for r in db.get('rows/'+sha+'/'+str(j))]
            if len(rows)!=m['row_count']:raise ValueError('Incomplete cluster list')
            reports.append(dict(wa=wa,name=name,sha=sha,rows=rows,conflict_stages=m['conflict_stages']))
    Path('checked-clusters.json').write_text(json.dumps(reports,ensure_ascii=False),'utf8')
    print('CLUSTER_TABLES',len(reports),'ROWS',sum(len(x['rows']) for x in reports))

if __name__=='__main__':
    if sys.argv[1]=='plan':plan()
    elif sys.argv[1]=='scan':scan(int(sys.argv[2]))
    elif sys.argv[1]=='collect':collect()
