"""Read real well reports from the completed directory index and export compact evidence."""
import argparse
import json
from pathlib import Path
import sqlite3

from bcer_bulk import process_wells, state_db
from bcer_client import BCERClient


def export(db, output):
    db.row_factory = sqlite3.Row
    queries = {
        'reports': 'select wa,path,name,modified,size,sha256,pages,template,status,error from files where is_current=1 order by wa,name',
        'facts': 'select f.wa,f.sha256,a.* from facts a join files f using(path) where f.is_current=1 order by f.wa,a.page',
        'points': 'select f.wa,f.sha256,a.* from points a join files f using(path) where f.is_current=1 order by f.wa,a.page,a.path_id',
    }
    with (Path(output)/'evidence.json').open('w', encoding='utf-8') as dest:
        json.dump({key:[dict(row) for row in db.execute(query)] for key,query in queries.items()},dest,ensure_ascii=False)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--wa', required=True)
    p.add_argument('--out', default='result')
    args = p.parse_args()
    db = state_db(args.out)
    for index in sorted(Path('indexes').rglob('extraction.sqlite')):
        with sqlite3.connect(index) as source:
            found = source.execute('select path,wa from wells where wa=?',(args.wa,)).fetchall()
            db.executemany('insert or ignore into wells(path,wa) values(?,?)', found)
    db.commit()
    if not db.execute('select 1 from wells').fetchone():
        raise SystemExit('Well missing from directory index')
    client=BCERClient()
    client.login()
    # This is an early evidence export; the same wells remain in the full catalog.
    try:
        process_wells(client,db,args.out,0,480,stop_after_facts=True)
    finally:
        export(db,args.out)
    if not db.execute("select 1 from files where status='text_processed'").fetchone():
        print('REPORT_DIAGNOSTICS',json.dumps(db.execute('select name,error from files where error is not null limit 6').fetchall(),default=list))
        raise SystemExit('No real PDF was processed')
