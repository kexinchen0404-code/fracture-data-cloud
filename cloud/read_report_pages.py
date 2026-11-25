"""Extract source-labelled page evidence from all assigned current completion reports."""
import argparse
import csv
import gzip
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from collections import defaultdict

from bcer_bulk import REPORT, parameter_candidates, revision, stage_label, vector_features
from bcer_client import BCERClient

STAGE = re.compile(r"\b(?:frac(?:ture)?\s+stage|stage\s*#|interval\s*#)\s*[:#-]?\s*(\d+(?:\.\d+)?)\b",re.I)
CLUSTER = re.compile(r"\b(?:perforated\s+(\d+)\s*/\s*(\d+)\s+proposed\s+clusters|(?:number\s+of\s+(?:perforation\s+)?clusters|cluster\s+count)\s*[:=]\s*(\d+))\b",re.I)
PERF = re.compile(r"^\s*(\d+/\d+/\d{2,4})\s+Perforated\s+(\d+(?:\.\d+)?)\s+(\d+)\s+([\d,.]+)\s+([\d,.]+)\s+(.+?)\s*$",re.I)
EVIDENCE = re.compile(r"frac[- ]?hit|fracture[- ]driven|offset.{0,55}pressure|pressure.{0,55}offset|\b(?:upper|middle|lower)\s+montney\b|liquid\s+nitrogen|\b(?:nitrogen|carbon dioxide|CO2|N2)\b.{0,35}(?:pumped|injected|m[³3])",re.I)
CHART = re.compile(r"entire\s+treatment|treatment\s+(?:plot|chart|part\s+[ab])|pressure\s+(?:vs\.?|versus)\s+time",re.I)


def collect_page(body):
    stages=set(STAGE.findall(body))
    st=next(iter(stages)) if len(stages)==1 else stage_label(body) if not stages else None
    records=[]
    for key,val,unit,raw in parameter_candidates(body):
        records.append((st,key,val,unit,raw))
    for line in body.splitlines():
        for m in CLUSTER.finditer(line):
            records.append((st,"executed_cluster_count" if m[1] else "labelled_cluster_count",int(m[1] or m[3]),"clusters",line.strip()[:460]))
        if EVIDENCE.search(line):
            records.append((st,"formation_interaction_or_fluid_text",None,None,line.strip()[:600]))
    if re.search(r"Interval Number\s+Cluster #\s+Top Depth \(mKB\)\s+Btm(?: Depth)? \(mKB\)",body,re.I):
        for line in body.splitlines():
            m=PERF.match(line)
            if m:
                top,btm=(float(v.replace(",","")) for v in m.group(4,5))
                evidence=dict(date=m[1],cluster=int(m[3]),top_mKB=top,bottom_mKB=btm,gun_fields=m[6])
                records.append((m[2],"perforated_cluster_row",None,"mKB",json.dumps(evidence,ensure_ascii=False)))
    return st,records


def ocr(page):
    import pymupdf as fitz
    sample=page.get_pixmap(matrix=fitz.Matrix(.65,.65),colorspace=fitz.csGRAY,alpha=False)
    if min(sample.samples,default=255)>=250:
        return "", "blank_render"
    pix=page.get_pixmap(dpi=250,colorspace=fitz.csGRAY,alpha=False)
    proc=subprocess.run(["tesseract","stdin","stdout","-l","eng","--psm","3"],input=pix.tobytes("png"),capture_output=True,timeout=120)
    if proc.returncode:
        raise ValueError("ocr_failed")
    return proc.stdout.decode("utf8","replace"),"ocr"


def read_pdf(db,wa,record,client,path):
    import pymupdf
    receipt=client.download(record,path)
    if receipt["sha256"] in BLOCKED:
        return receipt["sha256"],0,"known_damaged_source"
    with pymupdf.open(path) as pdf:
        if not len(pdf) or pdf.needs_pass:
            raise ValueError("unreadable_page_tree")
        for pg,page in enumerate(pdf,1):
            body=page.get_text("text",sort=True)
            # Poppler layout keeps the columns of executed perforation tables.
            if re.search(r"Cluster\s*#|Perforated\s+\d",body,re.I):
                layout=subprocess.run(["pdftotext","-f",str(pg),"-l",str(pg),"-layout","-enc","UTF-8",str(path),"-"],capture_output=True,timeout=90)
                if not layout.returncode:
                    body=layout.stdout.decode("utf8","replace")
            source="native_text"
            if len(re.sub(r"\s","",body))<40:
                body,source=ocr(page)
            st,facts=collect_page(body)
            ids={str(int(v)) for v in re.findall(r"\bLicen[cs]e\s*(?:No\.?|#)\s*[:#]?\s*(\d{3,6})\b",body,re.I)}
            identity="matched_page_well" if ids=={str(int(wa))} else "page_well_conflict" if ids else "folder_only"
            url=record["full_path"]
            db.executemany("insert into evidence values(?,?,?,?,?,?,?,?,?)",[(url,pg,stage,k,v,unit,raw,source,identity) for stage,k,v,unit,raw in facts])
            chart=None
            if CHART.search(body):
                chart="chart_requires_calibration"
                if source=="native_text" and st and identity=="matched_page_well":
                    try:
                        pts=vector_features(path,pg,body)
                    except (ValueError,KeyError,IndexError,TypeError):
                        pts=[]
                    if pts:
                        chart="vector_features_extracted"
                        db.executemany("insert into points values(?,?,?,?)",[(url,pg,st,json.dumps(p)) for p in pts])
            db.execute("insert into pages values(?,?,?,?,?)",(url,pg,source,len(body.strip()),chart))
    return receipt["sha256"],pg,"processed"


# These exact source bytes have already been unsuccessfully recovered by
# PDFium, MuPDF, Poppler, pypdf and qpdf. A replacement hash is processed normally.
BLOCKED=set()


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--group",type=int,required=True)
    p.add_argument("--queue",default="wells.csv.gz")
    p.add_argument("--database",default="pages.sqlite")
    p.add_argument("--seconds",type=int,default=17300)
    args=p.parse_args()
    if Path("held-source-hashes.txt").is_file():
        BLOCKED.update(Path("held-source-hashes.txt").read_text().split())
    with gzip.open(args.queue,"rt",encoding="utf8",newline="") as fp:
        work=[r for r in csv.DictReader(fp) if int(r["group"])==args.group]
    if not work:raise ValueError("empty group")
    db=sqlite3.connect(args.database)
    db.executescript("""create table if not exists wells(path text primary key, state text);
    create table if not exists reports(path text primary key,wa text,mtime text,sha text,pages integer,state text);
    create table if not exists pages(path text,page integer,source text,chars integer,chart text);
    create table if not exists evidence(path text,page integer,stage text,field text,value real,unit text,literal text,source text,identity text);
    create table if not exists points(path text,page integer,stage text,point text);
    create index if not exists pages_path on pages(path);
    create index if not exists evidence_path on evidence(path);
    create index if not exists points_path on points(path);
    """)
    client=BCERClient()
    client.login()
    start=time.monotonic()
    total=0
    for row in work:
        if time.monotonic()-start>=args.seconds:break
        folder,wa=row["path"],row["wa"]
        if db.execute("select 1 from wells where path=? and state='complete'",(folder,)).fetchone():continue
        versions=defaultdict(list)
        for f in client.list_folder(folder):
            if not f.get("IsFolder") and f["Name"].lower().endswith(".pdf") and REPORT.search(f["Name"]):
                family,key=revision(f)
                versions[family].append((key[:2],f))
        current={f["full_path"] for items in versions.values() for key,f in items if key==max(k for k,_ in items)}
        with db:
            for (previous,) in db.execute("select path from reports where wa=?",(wa,)).fetchall():
                if previous not in current:
                    for table in ("evidence","points","pages","reports"):
                        db.execute("delete from "+table+" where path=?",(previous,))
        finished=True
        for family,items in sorted(versions.items()):
            if time.monotonic()-start>=args.seconds:
                finished=False
                break
            latest=max(key for key,f in items)
            for key,f in items:
                if key!=latest:continue
                path=f["full_path"];mtime=client.iso_date(f["LastModified"])
                prior=db.execute("select mtime,state from reports where path=?",(path,)).fetchone()
                if prior and prior[0]==mtime and prior[1] in ("processed","known_damaged_source","revision_tie"):continue
                if sum(k==latest for k,_ in items)>1:
                    with db:
                        for table in ("evidence","points","pages"):
                            db.execute("delete from "+table+" where path=?",(path,))
                        db.execute("insert or replace into reports values(?,?,?,?,?,?)",(path,wa,mtime,None,None,"revision_tie"))
                    continue
                try:
                    with db,tempfile.TemporaryDirectory() as scratch:
                        for table in ("pages","evidence","points"):
                            db.execute("delete from "+table+" where path=?",(path,))
                        digest,n,state=read_pdf(db,wa,f,client,Path(scratch)/"report.pdf")
                        db.execute("insert or replace into reports values(?,?,?,?,?,?)",(path,wa,mtime,digest,n,state))
                except Exception as exc:
                    finished=False
                    with db:
                        for table in ("evidence","points","pages"):
                            db.execute("delete from "+table+" where path=?",(path,))
                        db.execute("insert or replace into reports values(?,?,?,?,?,?)",(path,wa,mtime,None,None,"retry_"+type(exc).__name__))
                total+=1
                if total%20==0:print("Reports attempted",total,flush=True)
        with db:db.execute("insert or replace into wells values(?,?)",(folder,"complete" if finished else "incomplete"))
    complete=db.execute("select count(*) from wells where state='complete'").fetchone()[0]
    summary=dict(group=args.group,queued_wells=len(work),checked_wells=complete,report_states=dict(db.execute("select state,count(*) from reports group by state")),page_states=dict(db.execute("select source,count(*) from pages group by source")),evidence=db.execute("select count(*) from evidence").fetchone()[0])
    db.close()
    Path("group-summary.json").write_text(json.dumps(summary))
    print("GROUP_SUMMARY",json.dumps(summary),flush=True)


if __name__=="__main__":main()
