"""Return the workbook's measured rows to the shared per-well research catalog."""
import collections
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile,ZIP_DEFLATED
from lxml import etree as ET

from prepare_pdf_sheets import PARAM

D=Path('tmp/curve_read/merged_sheets')


def data_rows(tabs,name):
    for part in tabs:
        if part['name']==name or (part['name'].startswith(name) and part['name'][len(name):].isdigit()):
            with (D/part['rows_file']).open(encoding='utf-8') as f:
                for line in f:yield json.loads(line)

def review_excerpt():
    """Keep the headers, widths, styles and first actual records of the changed views."""
    from merge_pdf_workbook import B,N,R,P,CT,APP,VT,tag,xml
    sample=io.BytesIO()
    names={'PDF施工段参数','PDF射孔簇数','PDF逐簇射孔明细','PDF未读报告','受损PDF片段','每井最新值','最新值取值依据','逐条缺失索引','缺失统计'}
    with ZipFile(B) as src,ZipFile(sample,'w',ZIP_DEFLATED) as out:
        book=ET.fromstring(src.read('xl/workbook.xml'));sheets=book.find(tag(N,'sheets'))
        for sheet in list(sheets):
            if sheet.get('name') not in names:sheets.remove(sheet)
        defs=book.find(tag(N,'definedNames'))
        if defs is not None:book.remove(defs)
        ids={s.get(tag(R,'id')) for s in sheets}
        rels=ET.fromstring(src.read('xl/_rels/workbook.xml.rels'));parts=set()
        for rel in list(rels):
            if rel.get('Type','').endswith('/worksheet'):
                if rel.get('Id') in ids:parts.add('xl/'+rel.get('Target'))
                else:rels.remove(rel)
        types=ET.fromstring(src.read('[Content_Types].xml'))
        for e in list(types):
            key=e.get('PartName','').lstrip('/')
            if key.startswith('xl/worksheets/') and key not in parts:types.remove(e)
        replacements={'xl/workbook.xml':xml(book),'xl/_rels/workbook.xml.rels':xml(rels),'[Content_Types].xml':xml(types)}
        for part in parts:
            with src.open(part) as f:
                kept=[];root=None
                for _,row in ET.iterparse(f,events=['end'],tag=tag(N,'row')):
                    if root is None:root=row.getroottree().getroot()
                    kept.append(deepcopy(row))
                    if len(kept)==6:break
                new=ET.Element(root.tag,root.attrib,nsmap=root.nsmap)
                for e in root:
                    if e.tag==tag(N,'sheetData'):break
                    e=deepcopy(e)
                    if e.tag==tag(N,'dimension'):e.set('ref','A1:'+kept[0][-1].get('r')[:-1]+kept[-1].get('r'))
                    new.append(e)
                data=ET.SubElement(new,tag(N,'sheetData'));data.extend(kept)
                f.seek(max(0,src.getinfo(part).file_size-16384));tail=f.read()
                end=re.search(rb'</(?:[\w.-]+:)?sheetData\s*>',tail)
                if not end:raise ValueError('Workbook formatting footer unavailable')
                wrapper=ET.tostring(ET.Element(root.tag,nsmap=root.nsmap))[:-2]+b'>'
                footer=ET.fromstring(wrapper+tail[end.end():])
                for e in footer.findall(tag(N,'conditionalFormatting')):
                    e=deepcopy(e);e.set('sqref',re.sub(r'\d+$',kept[-1].get('r'),e.get('sqref')));new.append(e)
                replacements[part]=xml(new)
        for n in src.namelist():
            if n in replacements:out.writestr(n,replacements[n])
            elif not n.startswith('xl/worksheets/'):out.writestr(n,src.read(n))
    return sample.getvalue()


def latest_catalog(archive,tabs,snapshot):
    meta=next(t for t in tabs if t['name']=='每井最新值');fields=meta['headers'][5:]
    if len(fields)!=len(set(fields)):raise ValueError('Duplicate physical parameter label')
    positions={label:i for i,label in enumerate(fields)}
    parts=[];batch=[];size=0;n=0;previous=-1;totals=collections.Counter()
    def save():
        if not batch:return
        name=f'latest-{len(parts)+1:04}.json'
        body=json.dumps({'default_state':'missing','columns':['wa','name','retained_values','ambiguous_field_ids'],'rows':batch},ensure_ascii=False,separators=(',',':')).encode('utf-8')
        archive.writestr(name,body)
        parts.append({'file':name,'first':batch[0][0],'last':batch[-1][0],'rows':len(batch),'sha256':hashlib.sha256(body).hexdigest()})
    for row in data_rows(tabs,'每井最新值'):
        wa,name,kept,absent,ambiguous,*vals=row
        if int(wa)<=previous:raise ValueError('Repeated or unordered well in the latest view')
        previous=int(wa);n+=1
        missing_ids=set(range(len(fields))) if absent=='全部工程字段' else set() if absent=='无' else {positions[k] for k in absent.split('、')}
        undecided=[] if ambiguous=='无' else [positions[k] for k in ambiguous.split('、')]
        values=[[i,v] for i,v in enumerate(vals) if v is not None]
        if len(vals)!=len(fields) or kept!=len(values) or missing_ids & set(undecided) or missing_ids|set(undecided)!={i for i,v in enumerate(vals) if v is None}:raise ValueError('Missing labels and retained values disagree')
        totals.update(retained=kept,missing=len(missing_ids),ambiguous=len(undecided))
        item=[wa,name,values,undecided];length=len(json.dumps(item,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+1
        if batch and size+length>22000:
            save();batch=[];size=0
        batch.append(item);size+=length
    save()
    assert (n,totals['retained'],totals['missing'],totals['ambiguous'])==(snapshot['latest_wells'],snapshot['well_values_retained'],snapshot['well_values_missing'],snapshot['well_values_unsortable'])
    explained=sum(r[5] for r in data_rows(tabs,'逐条缺失索引'))
    required=sum(r[3] for r in data_rows(tabs,'缺失统计') if r[0] in ('压裂段明细','射孔段参数','簇与净射孔区间','PDF施工段参数'))
    if explained!=required:raise ValueError('Missing-cell index does not reconcile with source and report counts')
    return {'latest_fields':fields,'latest_parts':parts,'well_count':n,'latest_totals':dict(totals),'indexed_source_and_stage_missing_cells':explained}


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
        summary.update(latest_catalog(out,tabs,snapshot))
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
        out.writestr('review.xlsx',review_excerpt())
    print('FIELD_COUNTS',json.dumps({k:v for k,v in summary.items() if k not in ('well_index','latest_parts','latest_fields')},ensure_ascii=False),flush=True)


if __name__=='__main__':export()
