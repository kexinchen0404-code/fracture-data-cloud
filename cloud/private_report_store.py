"""Only the project owner's private Turso database receives structured evidence."""
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPRedirectHandler, Request

PROJECT="bcer-fracturing-all-wells"
PREFIX="full-read/20261125/"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):
        raise RuntimeError("Private store redirected")


def pipeline(statements):
    owner=os.environ["GITHUB_REPOSITORY_OWNER"].lower()
    if owner!="kexinchen0404-code":raise ValueError("Unexpected repository owner")
    url="https://research-catalog-"+owner+".aws-us-west-2.turso.io/v2/pipeline"
    token=os.environ["TURSO_AUTH_TOKEN"].strip()
    if not token:raise ValueError("Missing private destination credential")
    body={"requests":[{"type":"execute","stmt":{"sql":sql,"args":[{"type":"text","value":x} for x in args]}} for sql,args in statements]+[{"type":"close"}]}
    request=Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
    for i in range(4):
        try:
            with build_opener(NoRedirect()).open(request,timeout=75) as response:
                r=json.load(response)["results"][:len(statements)]
            if len(r)!=len(statements) or any(v["type"]!="ok" for v in r):raise RuntimeError("Private database statement rejected")
            return [v["response"]["result"]["rows"] for v in r]
        except HTTPError as e:
            if e.code not in (429,500,502,503,504) or i==3:raise RuntimeError("Private database HTTP "+str(e.code)) from None
            time.sleep(max(2**i,min(120,int(e.headers.get("Retry-After","0") or 0))))
        except (URLError,TimeoutError):
            if i==3:raise RuntimeError("Private database connection unavailable") from None
            time.sleep(2**i)


def encode(obj):
    return json.dumps(obj,ensure_ascii=False,separators=(",",":"),allow_nan=False)


def write(items):
    for start in range(0,len(items),8):
        group=items[start:start+8]
        values=[encode(v) for k,v in group]
        if any(len(v.encode())>62000 for v in values):raise ValueError("Private record exceeds chunk limit")
        sql="INSERT INTO project_results(project_id,result_key,result_json) VALUES (?,?,?) ON CONFLICT(project_id,result_key) DO UPDATE SET result_json=excluded.result_json,updated_at=CURRENT_TIMESTAMP RETURNING result_json"
        results=pipeline([(sql,(PROJECT,PREFIX+k,v)) for (k,_),v in zip(group,values)])
        if any(r[0][0]["value"]!=v for r,v in zip(results,values)):raise RuntimeError("Private write did not reconcile")


def get(key):
    rows=pipeline([("SELECT result_json FROM project_results WHERE project_id=? AND result_key=?",(PROJECT,PREFIX+key))])[0]
    return json.loads(rows[0][0]["value"]) if rows else None


def chunks(rows):
    batch=[];size=2
    for row in rows:
        width=len(encode(row).encode())+1
        if size+width>50000 and batch:
            yield batch
            batch=[];size=2
        batch.append(row);size+=width
    if batch:yield batch


def store_queue():
    with gzip.open("wells.csv.gz","rt",encoding="utf8",newline="") as f:
        rows=list(csv.DictReader(f))
    if len({r["path"] for r in rows})!=len(rows):raise ValueError("Duplicate well directory")
    groups=[[] for _ in range(128)]
    for r in rows:groups[int(r["group"])].append([r["path"],r["wa"]])
    write([("queue/"+str(i),g) for i,g in enumerate(groups)])
    write([("inventory",dict(well_directories=len(rows),groups=len(groups),checked_at=datetime.now(timezone.utc).isoformat()))])
    print("Verified private well inventory",len(rows),flush=True)


def version(record,client):
    source=[record["full_path"],client.iso_date(record["LastModified"]),int(record["Size"])]
    return "report/"+hashlib.sha256(encode(source).encode()).hexdigest()


def save_report(db,record,client,wa,receipt,pages,state):
    key=version(record,client)
    manifest=dict(wa=wa,path=record["full_path"],modified=client.iso_date(record["LastModified"]),source_sha256=receipt,bytes=int(record["Size"]),page_count=pages,state=state,parts={},counts={})
    for table,columns in [("pages","page,source,chars,chart"),("evidence","page,stage,field,value,unit,literal,source,identity"),("points","page,stage,point")]:
        entries=db.execute("select "+columns+" from "+table+" where path=? order by rowid",(record["full_path"],))
        parts=0;count=0;pending=[]
        for parts,chunk in enumerate(chunks(entries),1):
            pending.append((key+"/"+table+"/"+str(parts),chunk));count+=len(chunk)
            if len(pending)==8:write(pending);pending=[]
        if pending:write(pending)
        manifest["parts"][table]=parts;manifest["counts"][table]=count
    if state=="processed" and pages!=manifest["counts"]["pages"]:raise ValueError("Page count mismatch")
    write([(key,manifest)])
    return manifest


if __name__=="__main__":store_queue()
