"""Append verified report sheets without loading the 0.86 million source rows into RAM."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree as E

D=Path('tmp/curve_read/merged_sheets')
B=next(Path('outputs/BCER_全井压裂汇总_最新').glob('*.xlsx'))
N='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
APP='http://schemas.openxmlformats.org/officeDocument/2006/extended-properties'
VT='http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'
TABS=json.loads((D/'tabs.json').read_text(encoding='utf-8'))


def tag(ns,k):return '{'+ns+'}'+k
def col(i):
    s=''
    while i:i,k=divmod(i-1,26);s=chr(65+k)+s
    return s
def xml(root):return E.tostring(root,encoding='UTF-8',xml_declaration=True,standalone=True)

def data_row(name,idx,values):
    height=33 if idx==1 else 36 if name in ('使用说明','字段字典') else 18
    row=E.Element(tag(N,'row'),r=str(idx),ht=str(height),customHeight='1')
    for j,v in enumerate(values,1):
        if v is None:continue
        cell=E.SubElement(row,tag(N,'c'),r=col(j)+str(idx),s='2' if idx==1 else '4')
        if isinstance(v,(int,float)):E.SubElement(cell,tag(N,'v')).text=str(v)
        else:
            cell.set('t','inlineStr')
            E.SubElement(E.SubElement(cell,tag(N,'is')),tag(N,'t')).text=v
    return row


def worksheet(template,t,small,stream=None):
    root=copy.deepcopy(template);data=root.find(tag(N,'sheetData'))
    for row in list(data):data.remove(row)
    for c in root.findall('.//'+tag(N,'col')):c.attrib.pop('style',None)
    total=1+(len(t['preview']) if small else t['count'])
    ref='A1:'+col(len(t['headers']))+str(total)
    dim=root.find(tag(N,'dimension'))
    if dim is not None:dim.set('ref',ref)
    filt=root.find(tag(N,'autoFilter'))
    if filt is None:
        filt=E.Element(tag(N,'autoFilter'));data.addnext(filt)
    filt.set('ref',ref)
    if stream is not None:
        with E.xmlfile(stream,encoding='UTF-8',close=False) as writer:
            writer.write_declaration(standalone=True)
            with writer.element(root.tag,root.attrib,nsmap=root.nsmap):
                for child in root:
                    if child.tag!=tag(N,'sheetData'):
                        writer.write(child)
                        continue
                    with writer.element(child.tag,child.attrib):
                        writer.write(data_row(t['name'],1,t['headers']))
                        with (D/t['rows_file']).open(encoding='utf-8') as f:
                            for i,line in enumerate(f,2):writer.write(data_row(t['name'],i,json.loads(line)))
        return
    data.append(data_row(t['name'],1,t['headers']))
    if small:
        for i,r in enumerate(t['preview'],2):data.append(data_row(t['name'],i,r))
    else:
        with (D/t['rows_file']).open(encoding='utf-8') as f:
            for i,line in enumerate(f,2):data.append(data_row(t['name'],i,json.loads(line)))
    return xml(root)


def copy_part(src,out,info):
    with src.open(info) as before,out.open(copy.copy(info),'w') as after:
        shutil.copyfileobj(before,after,length=1<<20)


def create(base,dest,small):
    with ZipFile(base) as src,ZipFile(D/'report-tabs.xlsx') as donor,ZipFile(dest,'w',ZIP_DEFLATED,allowZip64=True) as out:
        workbook=E.fromstring(src.read('xl/workbook.xml'));rels=E.fromstring(src.read('xl/_rels/workbook.xml.rels'))
        types=E.fromstring(src.read('[Content_Types].xml'));app=E.fromstring(src.read('docProps/app.xml'))
        sheets=workbook.find(tag(N,'sheets'));defs=workbook.find(tag(N,'definedNames'))
        if defs is None:defs=E.SubElement(workbook,tag(N,'definedNames'))
        names={s.get('name'):s for s in sheets}
        rid_map={r.get('Id'):r.get('Target') for r in rels}
        next_rid=1+max(int(r[3:]) for r in rid_map if r.startswith('rId'))
        next_sheet=1+max(int(s.get('sheetId')) for s in sheets)
        replacements={}
        for i,t in enumerate(TABS,1):
            name=t['name'];last=len(t['preview'])+1 if small else t['count']+1
            if name in names:
                part='xl/'+rid_map[names[name].get(tag(R,'id'))]
            else:
                num=next_sheet;next_sheet+=1;rid=f'rId{next_rid}';next_rid+=1
                part=f'xl/worksheets/sheet{num}.xml'
                s=E.SubElement(sheets,tag(N,'sheet'),name=name,sheetId=str(num));s.set(tag(R,'id'),rid)
                names[name]=s
                E.SubElement(rels,tag(P,'Relationship'),Id=rid,Type=R+'/worksheet',Target=part.removeprefix('xl/'))
                E.SubElement(types,tag(CT,'Override'),PartName='/'+part,ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            pos=list(sheets).index(names[name])
            df=next((e for e in defs if e.get('name')=='_xlnm._FilterDatabase' and e.get('localSheetId')==str(pos)),None)
            if df is None:df=E.SubElement(defs,tag(N,'definedName'),name='_xlnm._FilterDatabase',localSheetId=str(pos),hidden='1')
            df.text="'"+name.replace("'","''")+"'!$A$1:$"+col(len(t['headers']))+'$'+str(last)
            replacements[part]=(i,t)
        titles=app.find('.//'+tag(APP,'TitlesOfParts')+'/'+tag(VT,'vector'))
        for el in list(titles):titles.remove(el)
        for s in sheets:E.SubElement(titles,tag(VT,'lpstr')).text=s.get('name')
        titles.set('size',str(len(sheets)));app.find('.//'+tag(APP,'HeadingPairs')+'//'+tag(VT,'i4')).text=str(len(sheets))
        part_data={'xl/workbook.xml':xml(workbook),'xl/_rels/workbook.xml.rels':xml(rels),'[Content_Types].xml':xml(types),'docProps/app.xml':xml(app)}
        for info in src.infolist():
            if info.filename not in replacements and info.filename not in part_data:copy_part(src,out,info)
        for part,content in part_data.items():out.writestr(part,content)
        for part,(i,t) in replacements.items():
            template=E.fromstring(donor.read(f"xl/worksheets/sheet{t.get('template_index',i)}.xml"))
            if not small and t['count']>20000:
                with out.open(part,'w',force_zip64=True) as stream:worksheet(template,t,small,stream)
            else:out.writestr(part,worksheet(template,t,small))
    return {t['name']:part for part,(_,t) in replacements.items()}


def decode_row(row,n):
    result=[None]*n
    for cell in row.findall(tag(N,'c')):
        address=cell.get('r');j=0
        for c in address:
            if not c.isalpha():break
            j=j*26+ord(c)-64
        if cell.get('t')=='inlineStr':v=cell.findtext('.//'+tag(N,'t'),'')
        else:v=float(cell.findtext(tag(N,'v')))
        result[j-1]=v
    return result


def verify(book,parts):
    with ZipFile(B) as old,ZipFile(book) as new:
        if new.testzip() is not None:raise ValueError('Workbook CRC mismatch')
        if len(new.namelist())!=len(set(new.namelist())):raise ValueError('Duplicate workbook part')
        for n in (2,3,4,5,7):
            p=f'xl/worksheets/sheet{n}.xml'
            if old.getinfo(p).CRC!=new.getinfo(p).CRC or old.getinfo(p).file_size!=new.getinfo(p).file_size:raise ValueError('Changed base data sheet')
        for t in TABS:
            num=0
            with new.open(parts[t['name']]) as xmlfile,(D/t['rows_file']).open(encoding='utf-8') as records:
                for _,row in E.iterparse(xmlfile,events=('end',),tag=tag(N,'row')):
                    expected=t['headers'] if num==0 else json.loads(next(records))
                    if decode_row(row,len(t['headers']))!=expected:raise ValueError(f"Cell mismatch {t['name']} {num+1}")
                    num+=1;row.clear()
                    while row.getprevious() is not None:del row.getparent()[0]
                if num!=t['count']+1 or next(records,None) is not None:raise ValueError('Incomplete sheet')


if __name__=='__main__':
    pending=B.with_suffix('.updating')
    try:
        parts=create(B,pending,False)
        verify(pending,parts)
        review_base=Path('data/bcer_current/workbook_review_excerpt.xlsx')
        if review_base.exists():create(review_base,D/'review.xlsx',True)
        pending.replace(B)
    finally:pending.unlink(missing_ok=True)
    with B.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
    stats=json.loads((D/'snapshot.json').read_text(encoding='utf-8'))
    stats.update(workbook_bytes=B.stat().st_size,workbook_sha256=sha)
    (D/'snapshot.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(stats,ensure_ascii=False))
