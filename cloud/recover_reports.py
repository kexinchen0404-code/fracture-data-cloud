"""Retry only unfinished reports in an existing verified BCER group checkpoint."""
import argparse
import json
import os
from pathlib import Path
import sqlite3

from bcer_bulk import process_wells,state_db
from bcer_client import BCERClient
from report_checkpoints import checkpoint_assets,download_db


def restore(tag,group,folder):
    folder.mkdir(exist_ok=True)
    asset=checkpoint_assets(tag)[group]
    dbpath=download_db(asset,folder);dbpath.replace(folder/'extraction.sqlite')
    (folder/'data.gz').unlink()
    with sqlite3.connect(folder/'extraction.sqlite') as db:
        before=db.execute("select wa,path,status,error from files where is_current=1 and status='deferred' order by wa,path").fetchall()
        unresolved=db.execute('select count(*) from wells where done=2').fetchone()[0]
        queued=db.execute('select count(*) from wells where done=0').fetchone()[0]
        if queued:raise ValueError('Expected a completed first pass')
        db.execute('update wells set done=0 where done=2')
        db.execute("update files set status='queued' where is_current=1 and status='deferred'")
    (folder/'retry.json').write_text(json.dumps(before),'utf-8')
    print('CHECKPOINT',asset['name'],'WELLS',unresolved,'REPORTS',len(before),flush=True)


def retry(group,folder):
    items=json.loads((folder/'retry.json').read_text('utf-8'))
    db=state_db(folder)
    try:
        client=BCERClient()
        if db.execute('select 1 from wells where done=0 limit 1').fetchone():client.login()
        process_wells(client,db,folder,group,4800)
        db.row_factory=sqlite3.Row;receipt=[]
        for wa,path,_,old_error in items:
            row=dict(db.execute('select wa,path,name,is_current,status,sha256,pages,error from files where path=?',(path,)).fetchone())
            row['previous_error']=old_error
            row['page_classes']=dict(db.execute('select status,count(*) from pages where path=? group by status',(path,)))
            if row['status']=='text_processed' and (not row['sha256'] or not row['pages'] or sum(row['page_classes'].values())!=row['pages']):
                raise ValueError('Report page inventory did not reconcile')
            receipt.append(row)
        (folder/'receipts.json').write_text(json.dumps(receipt,ensure_ascii=False),'utf-8')
        print('RECEIPTS',json.dumps(receipt,ensure_ascii=False),flush=True)
    finally:db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['restore','retry']);p.add_argument('--group',type=int,required=True)
    p.add_argument('--out',default='result');args=p.parse_args();folder=Path(args.out)
    if args.mode=='restore':restore(os.environ['RESULT_TAG'],args.group,folder)
    else:retry(args.group,folder)
