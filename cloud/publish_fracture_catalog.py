"""Refresh the private catalog from the SHA-verified all-well workbook export."""
from collections import defaultdict
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
from zipfile import ZipFile

import private_report_store as db

def publish():
    db.PREFIX=''
    snap=json.loads(Path('BCER-report-coverage-current.json').read_text())
    sha=snap['workbook_sha256'];prefix='snapshots/'+sha[:16]
    base='https://github.com/'+os.environ['GITHUB_REPOSITORY']
    release=base+'/releases/download/untagged-5de28cdc76bd74ca7619/'
    run=base+'/actions/runs/'+os.environ['GITHUB_RUN_ID']
    def write_checked(items):
        db.write(items)
        for start in range(0,len(items),12):
            batch=items[start:start+12]
            read=db.pipeline([('SELECT result_json FROM project_results WHERE project_id=? AND result_key=?',(db.PROJECT,key)) for key,_ in batch])
            if any(not rows or json.loads(rows[0][0]['value'])!=expected for rows,(_,expected) in zip(read,batch)):
                raise ValueError('Catalog value failed independent readback')
    with ZipFile('verified-stage-catalog.zip') as z:
        index=json.loads(z.read('index.json'));assert index['workbook_sha256']==sha
        def verified(name,expected):
            raw=z.read(name)
            if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Shard checksum mismatch')
            return json.loads(raw)
        wells={};shards=[];items=[]
        for wa,meta in index['well_index'].items():
            key='current/pdf/stages/WA'+wa
            payload=verified('WA'+wa+'.json',meta['sha256'])
            items.append((key,dict(**payload,workbook_sha256=sha)));wells[wa]=dict(key=key,rows=meta['rows'])
        for part in index['latest_parts']:
            key=prefix+'/wells/'+part['file'][:-5]
            payload=verified(part['file'],part['sha256'])
            items.append((key,dict(**payload,workbook_sha256=sha)))
            shards.append({**{k:part[k] for k in ('first','last','rows')},'key':key})
        cluster_wells=defaultdict(list); cluster_parts=[]
        for r in json.loads(Path('checked-clusters.json').read_text('utf8')):
            if not r['rows']:continue
            for j,rows in enumerate(db.chunks(r['rows'])):
                key=f"{prefix}/clusters/{r['wa']}/{r['sha'][:16]}/{j}"
                cluster_parts.append((key,dict(well=r['wa'],source_file=r['name'],source_sha256=r['sha'],rows=rows,conflict_stages=r['conflict_stages'])))
                cluster_wells[r['wa']].append(dict(key=key,rows=len(rows)))
        write_checked(items+cluster_parts)
        well_index=dict(workbook_sha256=sha,wells=index['well_count'],fields=index['latest_fields'],shards=shards,default_state='missing',totals=index['latest_totals'],provenance_sheet='最新值取值依据')
        st_index=dict(workbook_sha256=sha,wells=wells,stage_rows=index['stage_rows'])
        cl_index=dict(workbook_sha256=sha,columns=['stage','cluster','top_mKB','bottom_mKB','reported_date','PDF_pages'],wells=cluster_wells,rows=snap['perforated_cluster_records'],reports_checked=snap['cluster_reports_checked'],scope='Current reports with verified stage measurements. Numbered intervals explicitly marked Perforated.')
        assert sum(p['rows'] for pp in cluster_wells.values() for p in pp)==cl_index['rows']
        latest_count=well_index['totals'];assert sum(latest_count.values())==well_index['wells']*len(well_index['fields'])
        write_checked([('current/well-latest',well_index),('current/pdf-stage-index',st_index),('current/pdf-cluster-index',cl_index)])
        manifest=db.get('current/manifest') or {};progress=db.get('current/pdf-progress') or {}
        info=dict(current_cloud_url=release+'BCER-fracture-stages-current.xlsx',coverage_json_url=release+'BCER-report-coverage-current.json',last_verified_sha256=sha,last_verified_bytes=snap['workbook_bytes'],last_verified_at=datetime.now(timezone.utc).isoformat(),sheet_rows={**manifest.get('workbook',{}).get('sheet_rows',{}),**index['sheet_rows']},build_url=run)
        manifest.update(workbook=info,pdf_verified_snapshot=snap,pdf_progress_key='current/pdf-progress',latest_policy='以来源修订时间为准。不同段各自保留。无法排序的多值标待核。',latest_index_key='current/well-latest',stage_index_key='current/pdf-stage-index',cluster_index_key='current/pdf-cluster-index',cloud_storage_status='工作簿存于项目所有者的 GitHub 草稿发布。每井最新值、已核实的逐段参数、逐簇实射数据已在 Turso 读回核验。')
        progress.update(verified_snapshot=snap,workbook_url=info['current_cloud_url'],assembly_run_url=run,verified_well_stage_keys=[w['key'] for w in wells.values()],comparison=index['comparisons'],as_of_utc=info['last_verified_at'],final_assembly=run)
        progress.pop('available_workbook_artifact',None)
        write_checked([('current/pdf-progress',progress),('current/manifest',manifest)])
        print('DATABASE_READBACK',well_index['wells'],len(wells),len(cluster_wells),cl_index['rows'],sha)

if __name__=='__main__':publish()
