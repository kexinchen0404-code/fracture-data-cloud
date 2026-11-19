"""Prepare latest BCER report measurements for the shared all-well workbook."""
import collections
import csv
import datetime as dt
from decimal import Decimal
import gzip
import io
import json
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree as ET

ROOT=Path('tmp/curve_read')
DATA=Path('data/bcer_current')
OUT=ROOT/'merged_sheets'
OUT.mkdir(exist_ok=True)
NS='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
BOOK=next(Path('outputs/BCER_全井压裂汇总_最新').glob('*.xlsx'))
SOURCE='https://github.com/kexinchen0404-code/fracture-data-cloud/actions/runs/44543513320'
FULL_RUN='https://github.com/kexinchen0404-code/fracture-data-cloud/actions/runs/44544749226'

# Ordered short names, source units, meaning and the matching official CSV column.
PARAM={
 'breakdown_pressure':('破裂压力','MPa','报告记载地层首次破裂时的压力。','BREAK DOWN PRESSURE (MPa)','breakdown_pressure_MPa'),
 'isip':('地面瞬时停泵压力','MPa','Surface ISIP，停泵后瞬时地面压力。官方 ISIP 栏未指明测压位置。','INST SHUT IN PRESSURE (MPa)','surface_ISIP_MPa'),
 'maximum_treating_pressure':('最大施工压力','MPa','该段施工报告列出的最高施工压力。','MAX TREATING PRESSURE (MPa)','max_treating_pressure_MPa'),
 'average_treating_pressure':('平均施工压力','MPa','该段施工报告列出的平均施工压力，平均时段和算法以报告为准。','AVG TREATING PRESSURE (MPa)','avg_treating_pressure_MPa'),
 'average_rate':('平均施工排量','m³/min','该段施工期间平均每分钟泵入的体积。','AVG RATE (m3/min)','avg_treating_rate_m3_min'),
 'maximum_treating_rate':('最大施工排量','m³/min','该段施工报告列出的最大单位时间泵入体积。',None,'max_treating_rate_m3_min'),
 'breakdown_rate':('破裂时排量','m³/min','地层破裂时报告记录的泵注排量。',None,'breakdown_rate_m3_min'),
 'opening_pressure':('开启压力','MPa','报告所列 Opening Pressure，具体开启对象以原页工况为准。',None,'opening_pressure_MPa'),
 'sip_1min':('停泵后1分钟压力','MPa','施工服务报告中停泵 1 分钟时的压力。',None,'SIP_1min_MPa'),
 'sip_3min':('停泵后3分钟压力','MPa','施工服务报告中停泵 3 分钟时的压力。',None,'SIP_3min_MPa')}
SERIES={'treating_pressure':('施工压力','MPa',0),'slurry_rate':('浆液排量','m³/min',1),
        'surface_proppant_concentration':('地面支撑剂浓度','kg/m³',2),
        'bottomhole_proppant_concentration':('井底支撑剂浓度','kg/m³',2)}
FEATURE={'path_first':'路径起点','path_last':'路径终点','path_minimum':'路径最低点','path_maximum':'路径最高点'}
REVIEW={'scanned_pending_ocr':'页面文本过少，待核对扫描图或空白页',
        'fracture_chart_pending_review':'曲线、图例或坐标轴未通过已有模板校验'}


def read_table(z,n):
    rows=ET.fromstring(z.read(f'xl/worksheets/sheet{n}.xml')).find(NS+'sheetData')
    return [[c.findtext('.//'+NS+'t','') for c in row.findall(NS+'c')] for row in rows]


def field_meaning(k,table):
    v={'WA NUM':'BCER 给井分配的许可号，可连接同井的不同作业。','DRILLNG EVENT':'井号下的钻井事件编号，用于区分不同井眼事件。',
       'COMPLTN EVENT':'同一钻井事件下的完井事件编号，用于区分不同完井。','UWI':'对应钻井和完井事件的 16 位井标识，按文本保留。',
       'WELL NAME':'监管记录中的井名。','COMPLTN DATE':'压裂或射孔实施日期，保留原日期字符串。',
       'FRAC START TIME':'压裂开始的 24 小时时钟，须结合日期使用。','COMPLTN TYPE':'完井方式，如单层、多层或裸眼完井。',
       'COMPLETION WORKOVER KEY':'源系统完井或修井操作的标识，可定位本表的一次作业。',
       'FRAC SUMMARY KEY':'一次数据提交内部的批次键，需与井和作业标识共同使用。',
       'FRAC STAGE NUM':'报告原始压裂段或施工尝试标签，可能包括补压、诊断试验或趾端开启。',
       'PERF STAGE NUM':'报告原始射孔段或重复射孔标签，保留字母后缀和趾端等特殊作业。',
       'BASE FLUID':'压裂液的基础载液，如淡水、盐水、油或酸。','VISCOSITY GEL TYPE':'载液采用的增黏或凝胶体系。',
       'ENERGIZER':'流体是否增能，以及增能或泡沫形式。','ENERGIZER TYPE':'承担增能作用的气体种类或混合气体种类。',
       'PLUG BACK TOTAL DEPTH (m)':'第一段以下的井深或回填井深，后续段记录桥塞、砂塞或封隔器深度，单位 mKB。',
       'ACID SPEARHEAD AMOUNT (m3)':'该段前置酸处理液体积，单位 m³。','ACID TYPE':'该段前置酸所用酸液类型。',
       'BREAK DOWN PRESSURE (MPa)':'地层在压裂作业中发生破裂时的压力，单位 MPa。',
       'INST SHUT IN PRESSURE (MPa)':'停止泵注瞬间的关井压力，单位 MPa。源字段定义未注明测压位置。',
       'MAX TREATING PRESSURE (MPa)':'施工期间最高压力，单位 MPa。','AVG TREATING PRESSURE (MPa)':'施工期间平均压力，单位 MPa。',
       'AVG RATE (m3/min)':'施工期间单位时间的平均泵入体积，单位 m³/min。',
       'FRAC GRADIENT (KPa/m)':'每米深度对应的压裂压力。源指南给出的算法为（ISIP＋液柱压力）/深度，单位 kPa/m。',
       'SALINE FLUID PUMPED (m3)':'该段泵入的盐性液体体积，可包括返排回用水、采出水等，单位 m³。',
       'NONSALINE FLUID PUMPED (m3)':'该段泵入的非盐性液体体积，可包括地表淡水和可利用地下水，单位 m³。',
       'TOTAL FLUID PUMPED (m3)':'该段源表所报的泵入总液量，单位 m³。盐水与非盐水另列时应核对填报口径。',
       'TOTAL CO2 PUMPED (m3)':'该段填报的二氧化碳体积，单位沿用源表 m³。相态与计量条件需查原报告。',
       'TOTAL N2 PUMPED (scm)':'氮气标准状态体积，单位标准 m³。换算液氮体积还需相态、密度及标准状态。',
       'TOTAL CH4 PUMPED (e3m3)':'该段所报甲烷泵注量，单位千 m³。','RADIOACTIVE FLAG':'是否报告使用放射性示踪剂。',
       'RADIOACTIVE TRACER TYPE':'报告填报的放射性示踪元素或同位素。','CHEMICAL TRACER FLAG':'是否报告使用化学示踪剂。',
       'CHEMICAL TRACER TYPE':'报告填报的化学示踪剂种类。','CHARGE TYPE':'射孔弹类型代码，枪型需另查报告。',
       'CHARGE SIZE (g)':'单枚射孔弹的装药质量，单位 g。','SHOTS PER METER':'每米射孔长度的设计或填报孔数，单位 孔/m。',
       'DEGREE OF PHASING':'相邻射孔在井筒圆周方向的布孔相位角，单位 度。','PERF COMMENTS':'射孔施工结果备注，可记录哑炮、补射或滑套端口。',
       'INTERVAL TOP DEPTH (m)':'该净射孔区间的顶界深度，单位 mKB。','INTERVAL BASE DEPTH (m)':'该净射孔区间的底界深度，单位 mKB。',
       'SHOTS PER INTERVAL':'该净区间记录的射孔孔数，每孔与每簇分开计数。'}
    if k in ('COMPLTN TOP DEPTH (m)','COMPLTN BASE DEPTH (m)'):
        return ('压裂施工段' if table=='压裂段明细' else '该次射孔的总区间')+('顶' if 'TOP' in k else '底')+'界深度，基准为转盘补心，单位 mKB。'
    if re.fullmatch(r'PROPPANT TYPE[1-4](?: (?:PUMPED|PLACED) \(t\))?',k):
        return ('该类别支撑剂的泵入质量，单位 t。' if 'PUMPED' in k else '该类别支撑剂报告置入的质量，单位 t。' if 'PLACED' in k else '该类别支撑剂的型号或粒级，如 40/70 目。')
    return v.get(k)


def prepare():
    with zipfile.ZipFile(BOOK) as z:
        dictionary=read_table(z,6);intro=read_table(z,1)
    # Always rebuild PDF-specific descriptions when updating the same workbook.
    dictionary=[r[:4] for r in dictionary if len(r)>=4 and r[0] not in ('PDF施工段参数','PDF曲线特征点','PDF与大表核对')]
    for r in dictionary[1:]:
        if desc:=field_meaning(r[2],r[0]):r[3]=desc
        if r[3]=='原始字段，空白不填零。':raise ValueError('Unmapped definition '+r[2])
    for key,(label,unit,meaning,_,_) in PARAM.items():dictionary.append(['PDF施工段参数',label,key,meaning+' 单位 '+unit+'。'])
    for key,(label,unit,_) in SERIES.items():
        meaning={'treating_pressure':'施工压力曲线读值。','slurry_rate':'含支撑剂浆液的体积流量。','surface_proppant_concentration':'地面浆液单位体积内的支撑剂质量。','bottomhole_proppant_concentration':'报告图例标为井底的支撑剂浓度，井底直接测量与模型推算须另查原报告。'}[key]
        dictionary.append(['PDF曲线特征点',label,key,meaning+' 单位 '+unit+'。'])
    dictionary.extend([
        ['PDF曲线特征点','路径上的四类特征','原图独立绘图路径','分别保留起点、终点、最低点和最高点，同一顶点可有两个角色。路径断开处留空。'],
        ['PDF曲线特征点','纵轴最大残差','原图刻度回代','轴标定后各刻度的最大绝对偏差，单位同曲线数值。'],
        ['PDF曲线特征点','半线宽（数值或秒）','图中线宽的一半投影到对应轴','反映线条本身的读图分辨率。仪器误差与原始采样频率另计。'],
        ['PDF曲线特征点','时间轴最大残差','原图时间刻度回代','单位秒。原图当地时间保留原日期，时区按报告核实。'],
        ['PDF曲线特征点','原图横纵坐标','PDF 页面点','单位 pt，以页面左上角为原点向右、向下递增，可回到 PDF 核对。'],
        ['PDF与大表核对','差值','PDF 值减官方 CSV 值','保留源单位。小差值可能来自显示位数，零值、测压基准和平均时段需结合原页复核。']])
    M=json.loads((ROOT/'qa/workbook_data.json').read_text(encoding='utf-8'))
    stages={};reports=[];points=[];review=[];meta={};well_stats={}
    for file in sorted(ROOT.glob('evidence-*.json')):
        j=json.loads(file.read_text('utf-8'));wa=str(j['reports'][0]['wa']);well_stats[wa]=j['progress']
        for r in j['reports']:
            if r['status']!='text_processed':continue
            ms=re.search(r'\d{10,}',r['modified']);date=dt.datetime.fromtimestamp(int(ms[0])/1000,dt.timezone.utc).isoformat() if ms else None
            reports.append([wa,r['name'],r['pages'],r['size'],r['sha256'],date,r['path'],'云端读取',r['template']])
            meta[(wa,r['sha256'])]=r['name']
        for x in j['facts']:
            if x['status']!='stage_service_report' or not x['stage_label']:continue
            key=(wa,x['sha256'],x['stage_label'],x['page']);row=stages.setdefault(key,{})
            k=x['parameter']
            if k not in PARAM or x['unit'].replace('m3','m³')!=PARAM[k][1]:raise ValueError('Unmapped native field/unit')
            if k in row and row[k]!=x['value']:raise ValueError('Multiple measured values on one page')
            row[k]=x['value']
        by_chart=collections.defaultdict(list)
        for x in j['points']:by_chart[(x['path'],x['page'])].append(x)
        for pp in by_chart.values():
            l=min(pp,key=lambda x:x['x_pdf_pt']);r=max(pp,key=lambda x:x['x_pdf_pt']);dx=r['x_pdf_pt']-l['x_pdf_pt']
            scale_t=abs((dt.datetime.fromisoformat(r['time_local'])-dt.datetime.fromisoformat(l['time_local'])).total_seconds()/dx) if dx>1 else None
            scale_y={}
            for axis in range(3):
                arr=[q for q in pp if SERIES[q['series']][2]==axis]
                if arr:
                    lo=min(arr,key=lambda x:x['y_pdf_pt']);hi=max(arr,key=lambda x:x['y_pdf_pt']);dy=hi['y_pdf_pt']-lo['y_pdf_pt']
                    if dy>1:scale_y[axis]=abs((hi['value']-lo['value'])/dy)
            for q in pp:
                sy=scale_y.get(SERIES[q['series']][2]); half_t=q['line_half_width']*scale_t/sy if sy and scale_t else None
                points.append([wa,q['stage_label'],q['page'],SERIES[q['series']][0],SERIES[q['series']][1],q['path_id'],FEATURE[q['feature']],q['time_local'],q['value'],q['axis_residual'],q['line_half_width'],q['time_residual_seconds'],half_t,q['x_pdf_pt'],q['y_pdf_pt'],q['sha256']])
        for q in j['review_pages']:review.append([wa,q['stage_label'],q['page'],REVIEW[q['status']],meta[(wa,q['sha256'])],q['sha256']])
    wa=str(M['well_id']);sha=M['source_sha256'];name=Path(M['source_path']).name;meta[(wa,sha)]=name
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(M['source_path']) as doc:page_count=len(doc)
    reports.append([wa,name,page_count,Path(M['source_path']).stat().st_size,sha,None,'/WellData/36000-36999/36500-36599/'+wa+'/'+name,'已核验的现有原件','HALLIBURTON'])
    for st in M['stages']:
        d=st['direct_report_values']
        stages[(wa,sha,str(st['interval']),d['service_report_page'])]={k:d[v[4]] for k,v in PARAM.items() if d.get(v[4]) is not None}
    for ch in M['charts']:
        per_path={}
        with gzip.open(ROOT/'extracted'/ch['vertex_file'],'rt',encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                key=(r['series'],int(r['path_id'])); value=float(r['value'])
                data=(r['time_local'],value,float(r['x_pdf_pt']),float(r['y_pdf_pt']))
                if key not in per_path:per_path[key]=[data]*4
                v=per_path[key];v[1]=data
                if value<v[2][1]:v[2]=data
                if value>v[3][1]:v[3]=data
        for (series,pid),quad in per_path.items():
            cal=ch['calibration'];ss=ch['series'][series];label,unit,axis=SERIES[series]
            for tag,(stamp,val,x,y) in zip(FEATURE.values(),quad):
                points.append([wa,str(ch['interval']),ch['page'],label,unit,pid,tag,stamp,val,cal['y_axes'][axis]['max_residual'],ss['half_linewidth_in_units'],cal['time']['max_residual'],ss.get('half_linewidth_seconds'),x,y,sha])
    base=collections.defaultdict(list);focus={k[0] for k in stages}
    with zipfile.ZipFile(DATA/'hydraulic_fracture_csv.zip') as src,src.open('hydraulic_fracture.csv') as f:
        for line,r in enumerate(csv.DictReader(io.TextIOWrapper(f,encoding='utf-8-sig')),2):
            if r['WA NUM'] in focus:base[(r['WA NUM'],r['FRAC STAGE NUM'])].append((line,r))
    stage_rows=[];checks=[]
    for (wa,sha,stage,page),fields in sorted(stages.items(),key=lambda x:(x[0][0],x[0][1],x[0][3])):
        stage_rows.append([wa,stage,page,*[fields.get(k) for k in PARAM],meta[(wa,sha)],sha])
        candidates=base.get((wa,stage),[])
        for k,v in PARAM.items():
            if not v[3] or k not in fields:continue
            srcrow,src=candidates[0] if len(candidates)==1 else (None,{})
            original=src.get(v[3]);b=Decimal(original) if original and original.strip() else None
            a=Decimal(str(fields[k]));delta=a-b if b is not None else None
            status='数值相同' if delta==0 else '数值不同' if delta is not None else '大表缺值' if srcrow else '同井同段匹配待复核'
            checks.append([wa,stage,page,v[0],v[1],float(a),float(b) if b is not None else None,float(delta) if delta is not None else None,status,srcrow,src.get('COMPLTN DATE'),src.get('COMPLETION WORKOVER KEY'),sha])
    head=[v[0]+' '+v[1] for v in PARAM.values()]
    tabs={
        '字段字典':(dictionary[0],dictionary[1:],[21,33,49,96]),
        'PDF施工段参数':(['井号 WA','施工段标签','PDF 页',*head,'报告文件','报告 SHA-256'],stage_rows,[13,15,11]+[22]*len(PARAM)+[56,72]),
        'PDF曲线特征点':(['井号 WA','施工段标签','PDF 页','曲线','原图单位','连续路径号','特征点','原图当地时间','图上读值','纵轴最大残差','纵轴半线宽','时间轴最大残差 s','时间半线宽 s','原图横坐标 pt','原图纵坐标 pt','报告 SHA-256'],sorted(points,key=lambda x:(x[0],x[2],x[3],x[5])),[13,15,11,24,15,15,16,31,22,22,22,22,20,20,20,72]),
        'PDF与大表核对':(['井号 WA','施工段标签','PDF 页','参数','单位','PDF 值','大表值','差值（PDF 减大表）','数值核对','官方 CSV 源行','大表作业日期','大表作业记录键','报告 SHA-256'],checks,[13,15,11,24,13,16,16,24,25,18,22,22,72]),
        'PDF报告清单':(['井号 WA','报告文件','PDF 总页数','源文件字节','报告 SHA-256','源目录修改时间 UTC','BCER 源目录路径','读取来源','报告出现的服务商名称'],sorted(reports),[13,55,17,20,72,32,110,25,40]),
        '待复核图页':(['井号 WA','施工段标签','PDF 页','复核原因','报告文件','报告 SHA-256'],sorted(review,key=lambda x:(x[0],x[4],x[2])),[13,15,11,64,56,72])}
    coverage=collections.Counter(r[8] for r in checks)
    summary={'stage_rows':len(stage_rows),'wells':sorted(focus),'reports':len(reports),'facts':sum(len(v) for v in stages.values()),'feature_points':len(points),'curve_pages':len({(r[0],r[2],r[-1]) for r in points}),'review_pages':len(review),'comparison_counts':dict(coverage),'points_by_well':dict(collections.Counter(r[0] for r in points)),'stages_by_well':dict(collections.Counter(r[0] for r in stage_rows))}
    intro_updates={'工作簿组织':'先按井号查井级覆盖，再查压裂段、射孔段和净区间。PDF施工段参数和PDF与大表核对可按井号、段号、报告页联查。',
        '未覆盖内容':'报告补提进度见 PDF 报告清单和待复核图页。未验证的层位、有效簇数、簇间分配量与图线遮挡处保留空白。',
        '来源定位':'原始三表按 CSV 源行定位。报告数据按文件 SHA-256、PDF 页和段标签定位。报告页码从文件第一页起算。'}
    intro_out=[]
    for r in intro[1:]:
        if len(r)>=2 and r[0] not in ('PDF补充范围','报告与大表的比较','云端全井任务'):
            intro_out.append([r[0],intro_updates.get(r[0],r[1])])
    intro_out.extend([['PDF补充范围',f"本次已核对 {len(focus)} 口井、{len(stage_rows)} 个施工段。特征点为原图连续路径上的四类读值，精度参见坐标轴残差和半线宽。"],
        ['报告与大表的比较','仅对同井同段在官方全表中唯一的记录计算差值。报告原值与大表原值并列保存，差异可回到原页核查。'],
        ['云端全井任务',FULL_RUN]])
    tabs={'使用说明':(['项目','说明'],intro_out,[25,118]),**tabs}
    manifest=[]
    for i,(name,(headers,rows,widths)) in enumerate(tabs.items()):
        filename=str(i)+'.jsonl'
        with (OUT/filename).open('w',encoding='utf-8') as f:
            for row in rows:f.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
        manifest.append(dict(name=name,headers=headers,widths=widths,count=len(rows),rows_file=filename,preview=rows[:5]))
    (OUT/'tabs.json').write_text(json.dumps(manifest,ensure_ascii=False),'utf-8')
    (OUT/'snapshot.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
    payload={}
    for wa in sorted(focus):
        subset=[r for r in stage_rows if r[0]==wa]
        payload[wa]={'well':wa,'columns':['stage','source_page',*PARAM],'units':{k:v[1] for k,v in PARAM.items()},'source_files':sorted({r[-2] for r in subset}),'source_sha256':sorted({r[-1] for r in subset}),'rows':[r[1:-2] for r in subset]}
    (OUT/'turso-stages.json').write_text(json.dumps(payload,ensure_ascii=False),'utf-8')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':prepare()
