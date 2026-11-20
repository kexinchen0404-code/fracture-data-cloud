"""One value per well and physical parameter, with explicit missing and revision evidence."""
import collections
import csv
from functools import lru_cache
import io
import re
from zipfile import ZipFile

from prepare_pdf_sheets import PARAM

TABLES={'hydraulic_fracture.csv':'压裂段明细','perf.csv':'射孔段参数','perf_net_interval.csv':'簇与净射孔区间'}
PREFIX={'hydraulic_fracture.csv':'压裂','perf.csv':'射孔','perf_net_interval.csv':'净区间'}
MONTHS={x:i+1 for i,x in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split())}
MULTI=object()


@lru_cache(maxsize=50000)
def daykey(value):
    try:
        day,month,year=value.upper().split('-');y=int(year)
        if len(year)==2:y+=2000 if y<=26 else 1900
        return f'{y:04}-{MONTHS[month]:02}-{int(day):02}'
    except (KeyError,ValueError):return ''


def same(a,b):return a if a==b else MULTI


class Evidence:
    __slots__=('n','present','value','src','second_src','date','recent','recent_src','unknown_date','clock_unknown','clock','clock_value','clock_src')
    def __init__(self):
        self.n=self.present=0;self.value=self.src=self.second_src=self.recent=self.recent_src=None
        self.date='';self.unknown_date=False;self.clock_unknown=False;self.clock='';self.clock_value=self.clock_src=None
    def add(self,v,source,day='',clock=''):
        if self.n==0:self.value,self.src=v,source
        else:
            if self.value is not MULTI and self.value!=v:self.second_src=source
            self.value=same(self.value,v)
        self.n+=1;self.present+=v is not None
        if not day:self.unknown_date=True;return
        if day>self.date:
            self.date,self.recent,self.recent_src=day,v,source
            self.clock_unknown=not clock;self.clock,self.clock_value,self.clock_src=clock,v,source
        elif day==self.date:
            self.recent=same(self.recent,v)
            if not clock:self.clock_unknown=True
            elif clock>self.clock:self.clock,self.clock_value,self.clock_src=clock,v,source
            elif clock==self.clock:self.clock_value=same(self.clock_value,v)
    def last_operation(self):
        if self.unknown_date or not self.date:return None,None
        if self.recent is not MULTI:return self.recent,self.recent_src
        if self.clock and not self.clock_unknown and self.clock_value is not MULTI:return self.clock_value,self.clock_src
        return None,None


def numeric(h):return '(' in h or h in ('SHOTS PER METER','DEGREE OF PHASING','SHOTS PER INTERVAL')


def source_value(s,is_number):
    s=s.strip()
    if not s:return None
    if is_number:
        try:return float(s)
        except ValueError:pass
    return s


def defs():
    return [
        dict(name='每井最新值',headers=[],widths=[],template_index=3,use_widths=True),
        dict(name='最新值取值依据',headers=['井号 WA','参数（含单位）','保留值','状态或缺失原因','最新可用报告修改时间 UTC','报告来源及页码','同时间报告行数','官方源记录数','官方非空数','最近已知施工日期（参考）','当次作业值（参考）','官方表及 CSV 源行'],widths=[14,32,22,62,32,75,22,19,19,31,29,39],template_index=5,use_widths=True),
        dict(name='缺失统计',headers=['数据表','参数或字段','记录总数','空白数','空白比例（%）','原文零值数','统计口径'],widths=[22,40,19,19,20,21,84],template_index=2,use_widths=True),
        dict(name='逐条缺失索引',headers=['数据表','井号 WA','段标签','CSV 源行或 PDF 页','作业键或报告 SHA-256','缺失字段数','缺失字段名称'],widths=[22,15,16,24,74,20,130],template_index=2,use_widths=True),
    ]


def compile_latest(all_wells,sinks,dict_rows,tab_rows,blocked_by_wa):
    labels={(r[0],r[2]):r[1] for r in dict_rows if len(r)>=4}
    gaps=sinks['逐条缺失索引']
    native=collections.defaultdict(Evidence);names={};ids={};missing_stats=[]
    well_names={};known=set(str(int(w)) for w in all_wells)
    with ZipFile('data/bcer_current/hydraulic_fracture_csv.zip') as archive:
        for file,table in TABLES.items():
            with archive.open(file) as source:
                reader=csv.reader(io.TextIOWrapper(source,encoding='utf-8-sig',newline=''));headers=next(reader)
                cols={h:i for i,h in enumerate(headers)};end=len(headers);blank=[0]*end;zero=[0]*end
                is_num=[numeric(h) for h in headers]
                if file=='hydraulic_fracture.csv':fields=[h for h in headers if h in ('COMPLTN TOP DEPTH (m)','COMPLTN BASE DEPTH (m)','COMPLTN TYPE','FRAC START TIME') or cols[h]>=13]
                elif file=='perf.csv':fields=[h for h in headers if h in ('COMPLTN TOP DEPTH (m)','COMPLTN BASE DEPTH (m)','COMPLTN TYPE') or 12<=cols[h] and h!='PERF COMMENTS']
                else:fields=headers[12:]
                selected=[]
                zh=[labels[(table,h)] for h in headers]
                stage_pos=cols.get('FRAC STAGE NUM',cols.get('PERF STAGE NUM'))
                for h in fields:
                    key=file+'|'+h;label=labels[(table,h)]
                    if h.startswith('COMPLTN '):label=PREFIX[file]+label
                    names[key]=label;ids[(file,h)]=key;selected.append((cols[h],key))
                n=0
                for n,row in enumerate(reader,1):
                    wa=str(int(row[cols['WA NUM']]));known.add(wa)
                    well_names.setdefault(wa,row[cols['WELL NAME']].strip())
                    d=daykey(row[cols['COMPLTN DATE']]);clock=row[cols['FRAC START TIME']].strip() if 'FRAC START TIME' in cols else ''
                    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d',clock):clock=''
                    vals=[source_value(raw,flag) for raw,flag in zip(row,is_num)]
                    absent=[zh[j] for j,v in enumerate(vals) if v is None]
                    if absent:gaps.add([table,wa.zfill(5),row[stage_pos] or None,n+1,row[cols['COMPLETION WORKOVER KEY']] or None,len(absent),'、'.join(absent)])
                    for j,v in enumerate(vals):
                        blank[j]+=v is None
                        if is_num[j] and isinstance(v,float) and v==0:zero[j]+=1
                    for j,key in selected:
                        native[(wa,key)].add(vals[j],(table,n+1),d,clock)
                for j,h in enumerate(headers):
                    missing_stats.append([table,labels[(table,h)],n,blank[j],100*blank[j]/n if n else None,zero[j] if is_num[j] else None,'官方当前快照全部原始记录。空白与原报数字 0 分开统计。'])
    # A surface ISIP has a specified reference point; the regulator's ISIP column does not.
    keys={}
    for k,(label,unit,_,src,_) in PARAM.items():
        key=ids[('hydraulic_fracture.csv',src)] if src and k!='isip' else 'pdf|'+k
        keys[k]=key;names.setdefault(key,label+' '+unit)
    cl='pdf|cluster_count_reported';names[cl]='报告列示射孔簇数（簇）'
    report_keys=set(keys.values())|{cl}
    by_sha={(str(int(r[0])),r[4]):r[5] for r in tab_rows('PDF报告清单')}
    dated={};undated=collections.defaultdict(Evidence);stage_n=0;missing=collections.Counter();zero=collections.Counter()
    def add_pdf(wa,k,v,sha,name,page,stage):
        wa=str(int(wa));known.add(wa)
        key=(wa,k);stamp=by_sha.get((wa,sha))
        ref=f'{name}，PDF {page} 页，段 {stage or "未标"}'
        if not stamp:
            undated[key].add(v,ref);return
        prev=dated.get(key)
        if prev is None or stamp>prev[0]:prev=dated[key]=(stamp,Evidence())
        if stamp==prev[0]:prev[1].add(v,ref)
    for row in tab_rows('PDF施工段参数'):
        stage_n+=1
        wa,stage,page=row[:3]
        absent=[v[0] for idx,v in enumerate(PARAM.values(),3) if row[idx] is None]
        if absent:gaps.add(['PDF施工段参数',str(wa).zfill(5),stage,page,row[-1],len(absent),'、'.join(absent)])
        for idx,(k,code) in enumerate(keys.items(),3):
            v=row[idx];missing[k]+=v is None;zero[k]+=isinstance(v,(float,int)) and v==0
            add_pdf(wa,code,v,row[-1],row[-2],page,stage)
    for k,(label,unit,*_) in PARAM.items():
        missing_stats.append(['PDF施工段参数',label+' '+unit,stage_n,missing[k],100*missing[k]/stage_n if stage_n else None,zero[k],'仅以已定位到井、施工段和报告页的记录为分母。'])
    for r in tab_rows('PDF射孔簇数'):
        if r[4]=='同井同段施工服务报告':add_pdf(r[0],cl,r[3],r[-1],r[-2],r[2],r[1])
    latest=sinks['每井最新值'];ev=sinks['最新值取值依据'];stat=sinks['缺失统计']
    heads=['井号 WA','井名','有值字段数','源资料缺失字段','修订次序待核字段',*names.values()]
    widths=[14,54,20,84,84]+[max(20,min(38,len(h)*2+2)) for h in names.values()]
    for t in (latest.base,latest.parts[0]):t.update(headers=heads,widths=widths,missing_range=[6,len(heads)])
    totals=collections.Counter();by_field=collections.defaultdict(collections.Counter)
    for wa in sorted(known,key=int):
        cells=[];absent=[];ambig=[];wa_disp=wa.zfill(5)
        for k,label in names.items():
            nk=(wa,k);h=native.get(nk);u=undated.get(nk);doc=dated.get(nk);stamp=None;src=None;dn=0;v=None
            work_val,work_src=h.last_operation() if h else (None,None)
            if doc:
                stamp,cur=doc;src=cur.src;dn=cur.n
                if cur.second_src:src+=f' / {cur.second_src}'
                if cur.value is MULTI:reason='同一最新报告修改时刻存在不同值或同时含空白';state='ambiguous'
                elif cur.value is None:reason='最新报告该字段为空白';state='missing'
                else:
                    v=cur.value;state='kept';reason='最新可核实报告修改时间'
                    if h or u:reason+='。另有来源未给逐条修改时间'
                if state!='ambiguous' and any(x is not None and x.value!=cur.value for x in (h,u)):
                    v=None;state='ambiguous';reason='有日期报告与缺少逐条修改时间的来源取值不同，先后待核'
            elif h or u:
                available=[x for x in (h,u) if x]
                val=available[0].value
                for x in available[1:]:val=same(val,x.value)
                if val is MULTI:reason='缺少逐条修改时间，多值或有值与空白并存';state='ambiguous'
                elif val is None:reason='源记录该字段为空白';state='missing'
                else:
                    v=val;state='kept';reason='仅一个源值或各记录同值，源未提供逐条修改时间'
                    if u:src=u.src
            else:reason='本版无可归属的参数记录';state='missing'
            if k in report_keys and wa in blocked_by_wa and (not stamp or blocked_by_wa[wa]>=stamp):
                v=None;state='ambiguous';reason='存在同时间或更新的未读或修订并列报告，无法证实最新取值'
            cells.append(v);totals[state]+=1;by_field[k][state]+=1
            if v==0 and isinstance(v,(int,float)):by_field[k]['zero']+=1
            if state=='missing':absent.append(label)
            if state=='ambiguous':ambig.append(label)
            if doc or u or h and (h.present or work_src) or (wa in blocked_by_wa and k in report_keys):
                ref=work_src or (h.src if h and h.value is not MULTI else None)
                link=f'{ref[0]} 第{ref[1]}行' if ref else None
                if not link and h and h.second_src:link=f'{h.src[0]} 第{h.src[1]}行 / 第{h.second_src[1]}行'
                ev.add([wa_disp,label,v,reason,stamp,src,dn,h.n if h else 0,h.present if h else 0,h.date if h and h.date else None,work_val,link])
        latest.add([wa_disp,well_names.get(wa),len(names)-len(absent)-len(ambig),'全部工程字段' if len(absent)==len(names) else '、'.join(absent) or '无','、'.join(ambig) or '无',*cells])
    for r in missing_stats:stat.add(r)
    for k,label in names.items():
        cs=by_field[k];b=cs['missing']+cs['ambiguous']
        stat.add(['每井最新值',label,len(known),b,100*b/len(known),cs['zero'],f"源资料缺失 {cs['missing']} 口，修改次序待核 {cs['ambiguous']} 口。"])
    if sum(totals.values())!=len(known)*len(names):raise ValueError('Per-well value states did not reconcile')
    return {'latest_wells':len(known),'latest_fields':len(names),'well_values_retained':totals['kept'],
            'well_values_missing':totals['missing'],'well_values_unsortable':totals['ambiguous'],
            'verified_pdf_field_blanks':sum(missing.values()),'verified_pdf_possible_fields':stage_n*len(PARAM)}
