"""Stream the current official BCER fracture/perforation bundle into one XLSX.

Keep source records and independently keyed operations. Cross-table fracture /
perforation links require a unique well/event/stage/depth candidate on both sides.
"""
from pathlib import Path
from datetime import datetime,date
from decimal import Decimal,InvalidOperation
import collections,csv,hashlib,io,json,re,time,zipfile
import xlsxwriter

ROOT=Path('data/bcer_current')
OUT=Path('outputs/BCER_全井压裂汇总_最新')
OUT.mkdir(parents=True,exist_ok=True)
ZIP=ROOT/'hydraulic_fracture_csv.zip'
SOURCE=json.loads((ROOT/'source_manifest.json').read_text(encoding='utf-8'))
Z=zipfile.ZipFile(ZIP)
SHEETS={'hydraulic_fracture.csv':'压裂段明细','perf.csv':'射孔段参数','perf_net_interval.csv':'簇与净射孔区间'}

COMMON={'WA NUM':'井号 WA','DRILLNG EVENT':'钻井事件 DE','COMPLTN EVENT':'完井事件 CE','UWI':'UWI','WELL NAME':'井名',
 'COMPLTN DATE':'作业日期（原文）','COMPLTN TOP DEPTH (m)':'作业顶深 mKB','COMPLTN BASE DEPTH (m)':'作业底深 mKB',
 'COMPLTN TYPE':'完井类型','COMPLETION WORKOVER KEY':'作业记录键','FRAC SUMMARY KEY':'本表提交批次键',
 'FRAC STAGE NUM':'压裂段标签（原文）','PERF STAGE NUM':'射孔段标签（原文）','FRAC START TIME':'压裂开始时钟',
 'BASE FLUID':'基液类型','VISCOSITY GEL TYPE':'黏度体系','ENERGIZER':'增能形式','ENERGIZER TYPE':'增能气体类型',
 'PLUG BACK TOTAL DEPTH (m)':'封隔深度 mKB','ACID SPEARHEAD AMOUNT (m3)':'前置酸量 m³','ACID TYPE':'酸类型',
 'BREAK DOWN PRESSURE (MPa)':'破裂压力 MPa','INST SHUT IN PRESSURE (MPa)':'瞬时停泵压力 MPa','MAX TREATING PRESSURE (MPa)':'最大施工压力 MPa',
 'AVG TREATING PRESSURE (MPa)':'平均施工压力 MPa','AVG RATE (m3/min)':'平均排量 m³/min','FRAC GRADIENT (KPa/m)':'压裂梯度 kPa/m',
 'SALINE FLUID PUMPED (m3)':'盐水泵注量 m³','NONSALINE FLUID PUMPED (m3)':'非盐水泵注量 m³','TOTAL FLUID PUMPED (m3)':'原总液量字段 m³',
 'TOTAL CO2 PUMPED (m3)':'CO₂ 泵注量 m³','TOTAL N2 PUMPED (scm)':'N₂ 泵注量 标准m³','TOTAL CH4 PUMPED (e3m3)':'CH₄ 泵注量 千m³',
 'RADIOACTIVE FLAG':'放射性示踪标记','RADIOACTIVE TRACER TYPE':'放射性示踪剂','CHEMICAL TRACER FLAG':'化学示踪标记','CHEMICAL TRACER TYPE':'化学示踪剂',
 'CHARGE TYPE':'射孔弹类型','CHARGE SIZE (g)':'射孔弹装药量 g','SHOTS PER METER':'孔密 孔/m','DEGREE OF PHASING':'相位角 °','PERF COMMENTS':'射孔作业备注',
 'INTERVAL TOP DEPTH (m)':'净区间顶深 mKB','INTERVAL BASE DEPTH (m)':'净区间底深 mKB','SHOTS PER INTERVAL':'本区间射孔数'}
for i in range(1,5):
    COMMON[f'PROPPANT TYPE{i}']=f'支撑剂{i}类型'
    COMMON[f'PROPPANT TYPE{i} PUMPED (t)']=f'支撑剂{i}泵入 t'
    COMMON[f'PROPPANT TYPE{i} PLACED (t)']=f'支撑剂{i}置入 t'
NUMERIC={h for h in COMMON if '(' in h or h in ['SHOTS PER METER','DEGREE OF PHASING','SHOTS PER INTERVAL']}
months={m:i+1 for i,m in enumerate(['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'])}

def rows(name):
    with Z.open(name) as f:
        yield from enumerate(csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig',newline='')),2)

def number(v):
    if not v or not v.strip():return None
    try:return float(v)
    except ValueError:return None

def day(v):
    if not v:return None
    try:
        d,m,y=v.upper().split('-');yy=int(y)
        if len(y)==2:yy+=2000 if yy<=26 else 1900
        return date(yy,months[m],int(d))
    except (ValueError,KeyError):return None

def ownkey(r):
    return tuple(r[h].strip() for h in ['WA NUM','DRILLNG EVENT','COMPLTN EVENT','COMPLETION WORKOVER KEY','FRAC SUMMARY KEY'])+(r.get('FRAC STAGE NUM',r.get('PERF STAGE NUM','')).strip(),)

def ident(prefix,k):return prefix+'|'+'|'.join(k)

def geometry(r):
    label=r.get('FRAC STAGE NUM',r.get('PERF STAGE NUM','')).strip().upper()
    try:top=Decimal(r['COMPLTN TOP DEPTH (m)']);base=Decimal(r['COMPLTN BASE DEPTH (m)'])
    except InvalidOperation:return None
    if not label or top<=0 or base<=top:return None
    return (r['WA NUM'],r['DRILLNG EVENT'],r['COMPLTN EVENT'],label,top,base)

def kind(label):
    u=label.upper()
    if 'DFIT' in u:return 'DFIT 标签'
    if 'INJ' in u:return '注入试验标签'
    if 'TOE' in u:return '趾端操作标签'
    if re.fullmatch(r'\d+[A-Za-z]+',label):return '字母后缀施工尝试'
    if label.isdigit():return '数字段标签'
    return '其他标签/待核对'

def unique_lookup(d,k,v):
    if k is None:return
    if k not in d:d[k]=v
    else:d[k]=None

def issues(r):
    flags=[]
    if not r.get('FRAC STAGE NUM',r.get('PERF STAGE NUM','')).strip():flags.append('段标签缺失')
    top=number(r['COMPLTN TOP DEPTH (m)']);base=number(r['COMPLTN BASE DEPTH (m)'])
    if top is None or base is None or base<=top:flags.append('总区间深度需核对')
    if any(number(r.get(h,'')) is not None and number(r[h])<0 for h in NUMERIC if h in r):flags.append('存在负值')
    if not day(r['COMPLTN DATE']):flags.append('日期未解析')
    return flags

# Only compact identifiers/aggregates are retained; full records are streamed.
fracgeom={};perfgeom={};fracdate={};perfdate={};perfrow={};perfmeta={};headers={};counts={};wells={};missing={}
def well(wa,name):
    if wa not in wells:wells[wa]={'name':name,'frac':0,'perf':0,'net':0,'matched':0,'fracgroups':set(),'perfkeys':set(),'labels':set(),'dates':[], 'N2_positive':0,'CO2_positive':0}
    return wells[wa]

for name in ['hydraulic_fracture.csv','perf.csv']:
    n=0;seen=set();miss=collections.Counter()
    for line,r in rows(name):
        if not headers.get(name):headers[name]=list(r)
        n+=1;k=ownkey(r)
        if k in seen:raise ValueError(f'Duplicate operation key in {name}: row {line}')
        seen.add(k);w=well(r['WA NUM'],r['WELL NAME']);g=geometry(r);dt=day(r['COMPLTN DATE'])
        miss.update(h for h,v in r.items() if not v.strip())
        if name.startswith('hydraulic'):
            unique_lookup(fracgeom,g,k);fracdate[k]=dt;w['frac']+=1;w['fracgroups'].add(r['FRAC SUMMARY KEY']);w['labels'].add(kind(k[-1]))
            if dt:w['dates'].append(dt)
            w['N2_positive']+=int((number(r['TOTAL N2 PUMPED (scm)']) or 0)>0)
            w['CO2_positive']+=int((number(r['TOTAL CO2 PUMPED (m3)']) or 0)>0)
        else:
            unique_lookup(perfgeom,g,k);perfdate[k]=dt;perfrow[k]=line;w['perf']+=1
            text=(r['CHARGE TYPE']+' '+r['PERF COMMENTS']).upper()
            perfmeta[k]='滑套/端口线索' if re.search(r'SLEEVE|PORT|TOE',text) else '净射孔区间/簇候选'
    counts[name]=n;missing[name]=dict(miss)
    print(json.dumps({'indexed':name,'rows':n,'wells_so_far':len(wells)}),flush=True)

links={};reverse={};linkstats=collections.Counter()
for g,pk in perfgeom.items():
    fk=fracgeom.get(g)
    if pk is None or fk is None:continue
    if not perfdate[pk] or not fracdate[fk]:linkstats['date_unavailable']+=1;continue
    if perfdate[pk]>fracdate[fk]:linkstats['perf_after_frac']+=1;continue
    links[fk]=pk;reverse[pk]=fk;linkstats['unique_geometry_date_order_matches']+=1;wells[fk[0]]['matched']+=1

netagg={};netmiss=collections.Counter();netcount=0
for line,r in rows('perf_net_interval.csv'):
    if 'perf_net_interval.csv' not in headers:headers['perf_net_interval.csv']=list(r)
    netcount+=1;k=ownkey(r);w=well(r['WA NUM'],r['WELL NAME']);w['net']+=1;w['perfkeys'].add(k)
    netmiss.update(h for h,v in r.items() if not v.strip())
    a=netagg.setdefault(k,{'rows':0,'geometry':set(),'reported_shots':0,'shot_rows':0,'invalid':0})
    a['rows']+=1;top=number(r['INTERVAL TOP DEPTH (m)']);base=number(r['INTERVAL BASE DEPTH (m)']);shots=number(r['SHOTS PER INTERVAL'])
    if top is not None and base is not None:a['geometry'].add((top,base))
    if top is None or base is None or base<top:a['invalid']+=1
    if shots is not None:a['reported_shots']+=shots;a['shot_rows']+=1
counts['perf_net_interval.csv']=netcount;missing['perf_net_interval.csv']=dict(netmiss)
assert counts=={'hydraulic_fracture.csv':179470,'perf.csv':106691,'perf_net_interval.csv':576623},'Source changed; review inventory before export'
net_unmatched=sum(a['rows'] for k,a in netagg.items() if k not in perfrow)
for a in netagg.values():a['unique_intervals']=len(a.pop('geometry'))
stats={'source':SOURCE,'source_data_through':'2026-11-10','rows':counts,'union_wells':len(wells),
 'frac_wells':sum(w['frac']>0 for w in wells.values()),'perf_wells':sum(w['perf']>0 for w in wells.values()),
 'net_interval_wells':sum(w['net']>0 for w in wells.values()),'net_interval_parent_unmatched_rows':net_unmatched,
 'linked_frac_perf_records':len(links),'frac_without_unique_perf_link':counts['hydraulic_fracture.csv']-len(links),
 'link_checks':dict(linkstats),'missing':missing,'net_stage_groups':len(netagg),
 'known_stage_labels':dict(collections.Counter(kind(k[-1]) for k in fracdate)),
 'scope':'All rows in the current official BCER bundle; no well or formation filtering. Full eLibrary PDFs not batch-processed.',
 'link_rule':'Same WA, DE, CE, case-normalized stage label and exact positive top/base depth; unique on both sides; perf date <= frac date. Automatic candidate link, not manual confirmation.',
 'cluster_definition':'Net perforation intervals are cluster candidates; some are frac sleeves/ports. Counts do not prove active cluster counts.'}
(ROOT/'all_wells_summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'ready_to_export':True,**{k:v for k,v in stats.items() if k not in ['source','missing']}}),flush=True)

# Constant-memory XLSX writing keeps the 0.86 million source rows off the RAM heap.
target=OUT/'BCER_全井压裂_段与簇汇总.xlsx'
temp=OUT/'BCER_全井压裂_段与簇汇总.building.xlsx'
wb=xlsxwriter.Workbook(str(temp),{'constant_memory':True,'strings_to_formulas':False,'strings_to_urls':False,'tmpdir':str(ROOT)})
fmt_head=wb.add_format({'bold':True,'font_color':'white','bg_color':'#203E5B','text_wrap':True,'valign':'vcenter','align':'center','font_name':'Microsoft YaHei','font_size':10})
fmt_title=wb.add_format({'bold':True,'font_name':'Microsoft YaHei','font_size':14,'font_color':'#203E5B'})
fmt_text=wb.add_format({'font_name':'Microsoft YaHei','font_size':10,'valign':'vcenter'})
fmt_num=wb.add_format({'font_name':'Microsoft YaHei','font_size':10,'num_format':'0.###'})
fmt_int=wb.add_format({'font_name':'Microsoft YaHei','font_size':10,'num_format':'0'})
fmt_date=wb.add_format({'font_name':'Microsoft YaHei','font_size':10,'num_format':'yyyy-mm-dd'})
fmt_note=wb.add_format({'font_name':'Microsoft YaHei','font_size':10,'text_wrap':True,'valign':'top'})
fmt_missing=wb.add_format({'bg_color':'#FFF1C2'})
samples={};sheetrows={};numeric_errors=collections.Counter();quality=collections.Counter()

def sheet(name,hs,width=16):
    s=wb.add_worksheet(name);s.hide_gridlines(2);s.freeze_panes(1,1);s.set_default_row(19)
    s.set_column(0,len(hs)-1,width,fmt_text);s.set_column(0,0,11);s.set_row(0,48)
    s.write_row(0,0,hs,fmt_head);samples[name]={'headers':hs,'rows':[]};sheetrows[name]=0
    return s

def write(s,index,values):
    for j,v in enumerate(values):
        if v is None or v=='':continue
        if isinstance(v,date):s.write_datetime(index,j,datetime.combine(v,datetime.min.time()),fmt_date)
        elif isinstance(v,(int,float)):s.write_number(index,j,v,fmt_num if isinstance(v,float) else fmt_int)
        else:s.write_string(index,j,str(v),fmt_text)
    sheetrows[s.name]=index
    if len(samples[s.name]['rows'])<4:samples[s.name]['rows'].append([v.isoformat() if isinstance(v,date) else v for v in values])

def original(r,hs):
    values=[]
    for h in hs:
        v=r[h]
        if h in NUMERIC and v.strip():
            n=number(v)
            if n is None:numeric_errors[h]+=1;values.append(v)
            else:values.append(n)
        else:values.append(v)
    return values

intro=wb.add_worksheet('使用说明');intro.hide_gridlines(2);intro.set_column(0,0,24);intro.set_column(1,1,112)
intro.write(1,0,'BCER 全井压裂、段与簇数据',fmt_title)
notes=[
 ('覆盖范围',f"当前官方数据包全部 {len(wells):,} 口井；179,470 条压裂记录，106,691 条射孔段记录，576,623 条净射孔区间记录。无单井、100井或地层筛选。"),
 ('数据日期','包内 readme 截至 2026-11-10；服务器 Last-Modified 为 2026-11-11；本次获取日期 2026-11-17。'),
 ('工作簿组织','井级覆盖、压裂段明细、射孔段参数、簇与净射孔区间、字段字典。各层级分别保存，避免把同一段的液量和砂量按簇重复累加。'),
 ('簇的口径','净射孔区间可作为簇级分析单元，但可能包含滑套或端口。原始资料未提供统一的物理簇编号；本表不把区间数宣称为有效产流簇数。'),
 ('段的口径','保留 1、1A、1B、DFIT、INJ、TOE 等源标签。字母后缀可能表示再次施工或尝试，不能直接并为同一物理段。全零记录也保留，未认定已施工成功。'),
 ('关联规则',f"{len(links):,} 条压裂记录与射孔段形成规则唯一候选关联：井号、DE、CE、段标签、顶底深度相同，双方候选均唯一，且射孔日期不晚于压裂日期。未经逐份 PDF 人工确认。"),
 ('独立键','FRAC 与 PERF 的提交批次键和作业记录键各自独立，不能用同名键直接连接。净射孔区间到 PERF 则按原始作业记录键、批次键等严格连接。'),
 ('更新规则','只使用本次最新下载快照；旧的本地压裂时间表未混入。保留当前快照内独立施工、修井、重复尝试；不把后期作业误当成旧作业的修订。'),
 ('液量口径','保留原总液量及盐水/非盐水字段。两类水量都有值时，新增合并液量取两者之和；否则使用原总液量，并标明口径。原字段不覆盖。'),
 ('缺失与零','空白保留为空白，原始零值保留为零。N₂/CO₂ 没有记录不等于未使用；氮气标准m³、CO₂ m³、甲烷千m³分别保留。'),
 ('日期与单位','深度按源说明为 mKB；压力 MPa；排量 m³/min；砂量 t。作业日期转为 Excel 日期供排序，原日期和开始时钟另列保留，时区未推断。'),
 ('未覆盖内容','此工作簿覆盖官方结构化数据包，不代表逐一提取了 eLibrary 全部 PDF。图件曲线、Upper/Middle/Lower Montney、簇级实际分配量等仅在 PDF 中出现的信息仍需报告补充。'),
 ('来源定位','每张明细表保留 CSV 源行号与本表操作 ID；完整字段对照在字段字典，下载地址和校验值如下。'),
 ('官方下载',SOURCE['source_url']),('来源 SHA-256',SOURCE['sha256']),
 ('官方口径指南','https://www.bc-er.ca/files/operations-documentation/Energy-Resource-Activity-Operations-Manual/Supporting-Documents/Hydraulic-Fracture-CSV-Guide.pdf'),
]
for i,(a,b) in enumerate(notes,4):intro.set_row(i,49 if len(b)<145 else 65);intro.write(i,0,a,fmt_head);intro.write(i,1,b,fmt_note)

wh=['井号 WA','井名','压裂记录数','压裂提交批次数','射孔段记录数','净射孔区间记录数','有净区间的射孔作业数','规则唯一候选关联数','最早压裂作业日期','最近压裂作业日期','N₂正用量记录数','CO₂正用量记录数','覆盖说明']
ws=sheet('井级覆盖',wh);ws.set_column(1,1,52);ws.set_column(12,12,35)
for i,(wa,w) in enumerate(sorted(wells.items()),1):
    status='压裂+射孔段+净区间' if w['frac'] and w['perf'] and w['net'] else '缺少净射孔区间' if w['frac'] and w['perf'] else '仅压裂记录' if w['frac'] else '仅射孔相关记录'
    write(ws,i,[wa,w['name'],w['frac'],len(w['fracgroups']),w['perf'],w['net'],len(w['perfkeys']),w['matched'],min(w['dates']) if w['dates'] else None,max(w['dates']) if w['dates'] else None,w['N2_positive'],w['CO2_positive'],status])
ws.autofilter(0,0,len(wells),len(wh)-1)

for name in ['hydraulic_fracture.csv','perf.csv','perf_net_interval.csv']:
    hs=headers[name]
    extra=(['本表操作 ID','作业日期','记录标签类型','合并液量 m³','合并液量口径','已报泵入砂量合计 t','关联射孔操作 ID','净区间记录数','唯一顶底深度区间数','关联状态','质量标记','CSV 源行号'] if name.startswith('hydraulic') else
           ['本表操作 ID','射孔日期','关联压裂操作 ID','净区间记录数','唯一顶底深度区间数','入口形式线索','关联状态','质量标记','CSV 源行号'] if name=='perf.csv' else
           ['父射孔操作 ID','区间在父作业内顺序','净区间长度 m','关联压裂操作 ID','入口形式线索','父射孔键状态','质量标记','CSV 源行号'])
    s=sheet(SHEETS[name],[COMMON[h] for h in hs]+extra);s.set_column(3,3,22);s.set_column(4,4,48)
    s.set_column(len(hs),len(hs),52);sequence=collections.Counter()
    for i,(line,r) in enumerate(rows(name),1):
        k=ownkey(r);flags=issues(r);dt=day(r['COMPLTN DATE'])
        if name.startswith('hydraulic'):
            pk=links.get(k);a=netagg.get(pk,{})
            salt=number(r['SALINE FLUID PUMPED (m3)']);fresh=number(r['NONSALINE FLUID PUMPED (m3)']);total=number(r['TOTAL FLUID PUMPED (m3)'])
            if salt is not None and fresh is not None:fluid=salt+fresh;basis='盐水+非盐水'
            else:fluid=total;basis='原总液量字段' if total is not None else '缺失'
            sand=[number(r[f'PROPPANT TYPE{j} PUMPED (t)']) for j in range(1,5)];sandval=sum(x for x in sand if x is not None) if any(x is not None for x in sand) else None
            if any(r[f'PROPPANT TYPE{j}'] and sand[j-1] is None for j in range(1,5)):flags.append('部分砂量缺失')
            if not r['FRAC START TIME']:flags.append('开始时钟缺失')
            state='规则唯一候选' if pk else '未唯一关联'
            vals=[ident('F',k),dt,kind(k[-1]),fluid,basis,sandval,ident('P',pk) if pk else None,a.get('rows'),a.get('unique_intervals'),state,'；'.join(flags),line]
        elif name=='perf.csv':
            fk=reverse.get(k);a=netagg.get(k,{})
            vals=[ident('P',k),dt,ident('F',fk) if fk else None,a.get('rows'),a.get('unique_intervals'),perfmeta[k],'规则唯一候选' if fk else '未唯一关联','；'.join(flags),line]
        else:
            sequence[k]+=1;top=number(r['INTERVAL TOP DEPTH (m)']);base=number(r['INTERVAL BASE DEPTH (m)']);gross_top=number(r['COMPLTN TOP DEPTH (m)']);gross_base=number(r['COMPLTN BASE DEPTH (m)'])
            if top is None or base is None or base<top:flags.append('净区间深度需核对')
            if top is not None and base is not None and gross_top is not None and gross_base is not None and (top<gross_top or base>gross_base):flags.append('净区间超出总区间')
            fk=reverse.get(k)
            vals=[ident('P',k),sequence[k],base-top if base is not None and top is not None else None,ident('F',fk) if fk else None,perfmeta.get(k,'待核对'),'严格键匹配' if k in perfrow else '父键未匹配','；'.join(flags),line]
        quality.update(flags);write(s,i,original(r,hs)+vals)
        if i%100000==0:print(json.dumps({'writing':name,'rows':i}),flush=True)
    s.autofilter(0,0,i,len(hs)+len(extra)-1)
    s.conditional_format(1,0,i,len(hs)+len(extra)-1,{'type':'blanks','format':fmt_missing})
    print(json.dumps({'written':name,'rows':i}),flush=True)

d=sheet('字段字典',['数据表','中文字段','源字段/计算方式','单位/说明'],24);d.set_column(2,2,52);d.set_column(3,3,75)
di=0
for name,hs in headers.items():
    for h in hs:
        di+=1
        desc='原始字段，空白不填零。'
        if h=='COMPLTN DATE':desc='FRAC 为压裂作业日期；PERF/净区间为射孔日期。不同表的日期允许不同。'
        elif h=='FRAC SUMMARY KEY':desc='仅在本来源表族中识别提交批次。FRAC 与 PERF 同名键不能直接连接。'
        elif h=='COMPLETION WORKOVER KEY':desc='来源系统的作业记录键；FRAC 与 PERF 是不同操作，不按同名键强制合并。'
        elif 'DEPTH' in h:desc='mKB。原值保留；未擅自换成 TVD 或推算地层。'
        elif 'STAGE NUM' in h:desc='保留原标签和字母后缀；一条记录可能为施工尝试，不一定对应一个独立物理段。'
        elif h=='TOTAL FLUID PUMPED (m3)':desc='原字段保留。2024-08 起水量改分盐水/非盐水；合并液量优先使用完整的两类分量。'
        elif h=='SHOTS PER INTERVAL':desc='源报告的区间射孔数。空白保持缺失，不用长度×孔密补造成实测值。'
        write(d,di,[SHEETS[name],COMMON[h],h,desc])
for field,rule,unit in [
 ('规则唯一候选关联','同井、DE、CE、段标签与顶底深度完全匹配，双向唯一，射孔日期≤压裂日期','自动推定关系；未逐份 PDF 人工确认'),
 ('唯一顶底深度区间数','同一射孔操作下按净区间顶底深度去重计数','包括端口/滑套的可能；不等于有效簇数'),
 ('已报泵入砂量合计','四类 PROPPANT PUMPED 有值项之和','t；类型存在但质量缺失时另标部分缺失'),
 ('合并液量','盐水、非盐水均有值则相加，否则沿用原总液量','m³；原字段保留，不对同一液量重复相加'),
 ('区间在父作业内顺序','净区间源文件在父射孔键内的出现顺序','本次导出顺序；不是官方簇编号或趾跟序'),
 ('入口形式线索','CHARGE TYPE / PERF COMMENTS 中查找 SLEEVE、PORT、TOE','关键词线索，不作为硬件类型最终判定')]:
    di+=1;write(d,di,['派生字段',field,rule,unit])
d.autofilter(0,0,di,3)
q=sheet('质量与覆盖',['项目','数量/状态','说明'],26);q.set_column(0,0,54);q.set_column(2,2,95)
qr=[['数据包截止日期','2026-11-10','源包 readme；获取日期不是每条记录的修订时间'],['压裂表记录',counts['hydraulic_fracture.csv'],'全部保留，含 DFIT/TOE/再次尝试等标签'],['射孔表记录',counts['perf.csv'],'全部保留'],['净射孔区间记录',netcount,'全部保留；区间与实际有效簇不同'],['全量唯一井数',len(wells),'三表井号并集'],['规则唯一关联',len(links),'候选关系，保留双方独立来源键'],['净区间父键未匹配',net_unmatched,'按 PERF 原始键严格连接'],['未形成唯一关联的压裂记录',stats['frac_without_unique_perf_link'],'保留原记录与缺失状态，不补成零簇']]
for name,miss in missing.items():
    for h in ['FRAC START TIME','BREAK DOWN PRESSURE (MPa)','INST SHUT IN PRESSURE (MPa)','TOTAL N2 PUMPED (scm)','TOTAL CO2 PUMPED (m3)','SHOTS PER INTERVAL']:
        if h in miss:qr.append([SHEETS[name]+'：'+COMMON[h]+'缺失',miss[h],'字段空白数，零值另行保留'])
qr.extend([[k,v,'原始异常保留，未自动修正'] for k,v in quality.items()])
for i,row in enumerate(qr,1):write(q,i,row)
q.autofilter(0,0,len(qr),2)
wb.close();temp.replace(target)
stats['workbook']={'path':str(target.resolve()),'bytes':target.stat().st_size,'sheet_rows':sheetrows,'numeric_parse_failures':dict(numeric_errors),'quality_flags':dict(quality)}
stats['workbook']['sha256']=hashlib.file_digest(target.open('rb'),'sha256').hexdigest()
(ROOT/'all_wells_summary.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
(ROOT/'workbook_preview_data.json').write_text(json.dumps(samples,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'complete':True,'workbook':stats['workbook']},ensure_ascii=False),flush=True)
