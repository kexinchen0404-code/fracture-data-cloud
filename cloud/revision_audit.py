"""Check source modification order within each well/report family, including superseded listings."""
import argparse
import collections
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from bcer_bulk import revision
from report_checkpoints import checkpoint_assets,download_db


def outdated(db):
    families=collections.defaultdict(list)
    for path,wa,family,name,mtime,current in db.execute('select path,wa,family,name,modified,is_current from files'):
        key=(wa,path.rsplit('/',1)[0],family)
        families[key].append((path,revision({'Name':name,'LastModified':mtime})[1][:2],current))
    changes=[];ambiguous=[]
    for (wa,_,family),rows in families.items():
        current={r[0] for r in rows if r[2]}
        if not current or len(rows)<2:continue
        latest=max(r[1] for r in rows);best={r[0] for r in rows if r[1]==latest}
        if not current<=best:changes.append(wa)
        if len(best)>1:ambiguous.append((wa,family))
    return sorted(set(changes)),ambiguous


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--tag',required=True);a=p.parse_args()
    assets=checkpoint_assets(a.tag);changes={};ties=[]
    for group,asset in sorted(assets.items()):
        with tempfile.TemporaryDirectory() as tmp:
            with sqlite3.connect(download_db(asset,tmp)) as db:
                wells,ambiguous=outdated(db)
            db.close()
            if wells:changes[str(group)]=wells
            ties.extend(ambiguous)
    if len(assets)!=128:raise ValueError('Incomplete inventory for revision audit')
    audit={'groups_checked':len(assets),'groups_to_refresh':changes,'affected_wells':sum(map(len,changes.values())),'same_time_revision_ties':ties}
    Path('revision-audit.json').write_text(json.dumps(audit,ensure_ascii=False),'utf-8')
    print('REVISION_AUDIT',json.dumps({'groups':list(changes),'affected_wells':audit['affected_wells'],'same_time_revision_ties':len(ties)}))
