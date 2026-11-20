"""Return the workbook's measured rows to the shared per-well research catalog."""
import collections
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

from prepare_pdf_sheets import PARAM

D=Path('tmp/curve_read/merged_sheets')


def data_rows(tabs,name):
    for part in tabs:
        if part['name']==name or (part['name'].startswith(name) and part['name'][len(name):].isdigit()):
            with (D/part['rows_file']).open(encoding='utf-8') as f:
                for line in f:yield json.loads(line)


def export():
    tabs=json.loads((D/'tabs.json').read_text(encoding='utf-8'))
    snapshot=json.loads((D/'snapshot.json').read_text(encoding='utf-8'))
    by_well=collections.defaultdict(list);units={k:v[1] for k,v in PARAM.items()}
    reported=collections.Counter()
    for wa,stage,page,*body in data_rows(tabs,'PDF施工段参数'):
        values=body[:-2];name,sha=body[-2:]
        if len(values)!=len(PARAM) or len(sha)!=64 or not page:raise ValueError('Incomplete report provenance')
        by_well[wa].append((stage,page,values,name,sha))
        reported.update(k for k,v in zip(PARAM,values) if v is not None)
    cluster_wells=set();anchored=0;cluster_rows=0
    for row in data_rows(tabs,'PDF射孔簇数'):
        cluster_wells.add(row[0]);cluster_rows+=1;anchored+=row[4]=='同井同段施工服务报告'
    comparisons=collections.defaultdict(collections.Counter)
    for row in data_rows(tabs,'PDF与大表核对'):
        comparisons[row[3]][row[8]]+=1
    summary={'wells':len(by_well),'stage_rows':sum(map(len,by_well.values())),
             'reported_fields':dict(reported),'cluster_rows':cluster_rows,'cluster_wells':len(cluster_wells),
             'clusters_with_matching_stage_service_page':anchored,
             'comparisons':dict(comparisons),'workbook_sha256':snapshot['workbook_sha256'],
             'sheet_rows':{t['name']:t['count'] for t in tabs},'well_index':{}}
    assert (summary['wells'],summary['stage_rows'],sum(reported.values()),cluster_rows)==(
        snapshot['measurement_wells'],snapshot['stage_rows'],snapshot['measured_values'],snapshot['reported_cluster_rows'])
    with ZipFile('verified-stage-catalog.zip','w',ZIP_DEFLATED) as out:
        for wa,entries in sorted(by_well.items()):
            sources=sorted({(sha,name) for _,_,_,name,sha in entries})
            index={s:i for i,s in enumerate(sources)}
            rows=[[s,p,*vals,index[(sha,name)]] for s,p,vals,name,sha in entries]
            payload={'well':wa,'columns':['stage','source_page',*PARAM,'source_id'],'units':units,
                     'source_files':[n for _,n in sources],'source_sha256':[h for h,_ in sources],
                     'rows':rows}
            body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            out.writestr('WA'+wa+'.json',body)
            summary['well_index'][wa]={'rows':len(rows),'sha256':hashlib.sha256(body).hexdigest()}
        out.writestr('index.json',json.dumps(summary,ensure_ascii=False))
    print('FIELD_COUNTS',json.dumps({k:v for k,v in summary.items() if k!='well_index'},ensure_ascii=False),flush=True)


if __name__=='__main__':export()
