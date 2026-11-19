"""Partition every indexed well once, putting officially documented frac wells first."""
import argparse
import csv
import gzip
import json
import sqlite3
from pathlib import Path

from bcer_bulk import state_db


def partition(index_dir, queue, groups):
    priority=set(Path(__file__).with_name('structured_wells.txt').read_text().split())
    paths={}
    for path in sorted(Path(index_dir).glob('well-index-*/extraction.sqlite')):
        with sqlite3.connect(path) as source:
            if source.execute('select count(*) from directories where done=0').fetchone()[0]:
                raise ValueError('Directory index is unfinished')
            for well,wa in source.execute('select path,wa from wells'):
                if well in paths and paths[well]!=wa:raise ValueError('Conflicting well identity')
                paths[well]=wa
    if len(list(Path(index_dir).glob('well-index-*/extraction.sqlite')))!=4 or not paths:
        raise ValueError('Incomplete source partitions')
    rows=sorted(paths.items(), key=lambda item:(item[1] not in priority,-int(item[1]),item[0]))
    with gzip.open(queue,'wt',encoding='utf-8',newline='') as out:
        w=csv.writer(out);w.writerow(['group','path','wa'])
        w.writerows((i%groups,path,wa) for i,(path,wa) in enumerate(rows))
    counts={'well_directories':len(rows),'unique_wa':len(set(paths.values())),
            'official_wells_in_queue':len(priority.intersection(paths.values())), 'groups':groups}
    print('QUEUE_COUNTS',json.dumps(counts),flush=True)


def restore(queue, group, output):
    db=state_db(output)
    with gzip.open(queue,'rt',encoding='utf-8',newline='') as f, db:
        db.executemany('insert or ignore into wells(path,wa) values(?,?)',
                       ((r['path'],r['wa']) for r in csv.DictReader(f) if int(r['group'])==group))
        db.execute('update wells set done=0 where done=2')
        db.execute("update files set status='queued' where status='deferred' and is_current=1")
    print('GROUP_WELLS',db.execute('select count(*) from wells').fetchone()[0],flush=True)
    db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--indexes');p.add_argument('--queue',default='wells.csv.gz')
    p.add_argument('--groups',type=int,default=128);p.add_argument('--group',type=int)
    p.add_argument('--output',default='result')
    args=p.parse_args()
    if args.group is None:partition(args.indexes,args.queue,args.groups)
    else:restore(args.queue,args.group,args.output)
