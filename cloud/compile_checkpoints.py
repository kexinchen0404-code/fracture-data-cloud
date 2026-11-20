"""Merge the newest cloud checkpoint per group with the verified PDF workbook tables."""
import argparse
import collections
import datetime as dt
from decimal import Decimal
import csv
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from zipfile import ZipFile

from prepare_pdf_sheets import PARAM,SERIES,FEATURE,REVIEW
from report_checkpoints import checkpoint_assets,download_db

D=Path('tmp/curve_read/merged_sheets')
TABS=json.loads((D/'tabs.json').read_text(encoding='utf-8'))
BY_NAME={t['name']:t for t in TABS}
for i,t in enumerate(TABS,1):t['template_index']=i


def rows(name):
    with (D/BY_NAME[name]['rows_file']).open(encoding='utf-8') as f:
        for line in f:yield json.loads(line)


class Sink:
    def __init__(self,base,index):
        self.base=base;self.index=index;self.parts=[];self.f=None
        self.open()
    def open(self):
        if self.f:self.f.close()
        part=len(self.parts)+1
        t=dict(self.base,count=0,preview=[],rows_file=f'compiled-{self.index}-{part}.jsonl')
        if part>1:t['name']+=str(part)
        self.parts.append(t);self.f=(D/t['rows_file']).open('w',encoding='utf-8')
    def add(self,row):
        if self.parts[-1]['count']>=1048575:self.open()
        t=self.parts[-1]
        if len(row)!=len(t['headers']):raise ValueError('Wrong column count '+t['name'])
        self.f.write(json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
        if t['count']<5:t['preview'].append(row)
        t['count']+=1


def official_stages():
    out=collections.defaultdict(list)
    fields=[k for k,v in PARAM.items() if v[3]]
    with ZipFile('data/bcer_current/hydraulic_fracture_csv.zip') as z,z.open('hydraulic_fracture.csv') as src:
        for line,r in enumerate(csv.DictReader(io.TextIOWrapper(src,encoding='utf-8-sig')),2):
            out[(str(int(r['WA NUM'])),r['FRAC STAGE NUM'])].append((line,r['COMPLTN DATE'],r['COMPLETION WORKOVER KEY'],{k:r[PARAM[k][3]] for k in fields}))
    return out


def family(name):
    name=re.sub(r'[_ ]REVISED\d*','',name.upper())
    return re.sub(r'_INITIAL(?: COMP)?(?=\.PDF$)','',name)


def compile_all(assets):
    native=official_stages()
    pending='PDF参数待核对'
    extra=dict(name=pending,headers=['井号 WA','施工段标签','PDF 页','报告参数代码','数值','原文单位','原文证据','报告文件','报告 SHA-256'],widths=[13,15,11,36,20,18,70,56,72],template_index=6)
    cluster_tab='PDF射孔簇数'
    clusters=dict(name=cluster_tab,headers=['井号 WA','报告段标签','PDF 页','报告列示簇数（簇）','归属证据','原文计数短语','报告文件','报告 SHA-256'],widths=[14,19,12,24,48,54,64,74],template_index=6,use_widths=True)
    tables=[*TABS,extra,clusters]
    sinks={t['name']:Sink(t,i) for i,t in enumerate(tables)}
    seen=set();families={};cached={(r[0],r[4]):r for r in rows('PDF报告清单')};stats=collections.Counter();wells=set();plot_wells=set();plot_pages=0
    def measured(wa,sha,name,stage,page,values):
        wells.add(wa);stats['stage_rows']+=1;stats['measured_values']+=len(values)
        sinks['PDF施工段参数'].add([wa,stage,page,*[values.get(k) for k in PARAM],name,sha])
        matches=native.get((str(int(wa)),stage),[])
        for k,val in values.items():
            label,unit,_,source,_=PARAM[k]
            if not source:continue
            line,date,key,original=matches[0] if len(matches)==1 else (None,None,None,{})
            b=original.get(k);b=Decimal(b) if b and b.strip() else None
            delta=Decimal(str(val))-b if b is not None else None
            status='数值相同' if delta==0 else '数值不同' if delta is not None else '大表缺值' if line else '同井同段匹配待复核'
            sinks['PDF与大表核对'].add([wa,stage,page,label,unit,val,float(b) if b is not None else None,float(delta) if delta is not None else None,status,line,date,key,sha])
    for group,asset in sorted(assets.items()):
        with tempfile.TemporaryDirectory() as td:
            file=download_db(asset,td)
            with sqlite3.connect(file) as db:
                db.row_factory=sqlite3.Row
                if db.execute('pragma quick_check').fetchone()[0]!='ok':raise ValueError('Damaged checkpoint')
                db.execute('create index if not exists compile_facts_path on facts(path,page)')
                db.execute('create index if not exists compile_points_path on points(path,page)')
                counts=dict(db.execute('select done,count(*) from wells group by done'))
                stats['wells_complete']+=counts.get(1,0);stats['wells_unresolved']+=counts.get(2,0);stats['wells_pending_in_saved_groups']+=counts.get(0,0)
                # Include current files even if their newly listed version failed to transfer.
                for r in db.execute('select * from files where is_current=1 order by wa,name'):
                    wa=str(r['wa']);name=r['name'];sha=r['sha256'];key=(wa,sha);path=r['path']
                    families[(wa,family(name))]=sha if r['status']=='text_processed' else None
                    if r['status']!='text_processed' or not sha:
                        stats['reports_unresolved']+=1;continue
                    if key in seen:continue
                    seen.add(key)
                    ms=re.search(r'\d{10,}',r['modified'] or '')
                    stamp=dt.datetime.fromtimestamp(int(ms[0])/1000,dt.timezone.utc).isoformat() if ms else None
                    sinks['PDF报告清单'].add([wa,name,r['pages'],r['size'],sha,stamp,path,'云端读取',r['template']])
                    for q in db.execute("select distinct page,stage_label,value,raw_text,status from facts where path=? and parameter='cluster_count_reported' and unit='count' order by page,stage_label,value,raw_text",(path,)):
                        value=q['value']
                        if value is None or value<0 or not float(value).is_integer():continue
                        matched=q['status']=='stage_service_report' and bool(q['stage_label'])
                        evidence='同井同段施工服务报告' if matched else '原文提及，所属段和设计或施工口径待核'
                        sinks[cluster_tab].add([wa,q['stage_label'],q['page'],int(value),evidence,q['raw_text'],name,sha])
                    if key in cached:continue
                    accepted=collections.defaultdict(dict);bad=set()
                    for q in db.execute('select * from facts where path=? order by page',(path,)):
                        label=q['stage_label'];k=q['parameter'];idx=(label,q['page']);unit=q['unit'].replace('m3','m³')
                        if q['status']=='stage_service_report' and label and k in PARAM and unit==PARAM[k][1]:
                            if k in accepted[idx] and accepted[idx][k]!=q['value']:bad.add((idx,k))
                            accepted[idx][k]=q['value']
                        else:sinks[pending].add([wa,label,q['page'],k,q['value'],q['unit'],q['raw_text'],name,sha])
                    for idx,vals in accepted.items():
                        for k in list(vals):
                            if (idx,k) in bad:
                                del vals[k]
                                for conflict in db.execute('select value,unit,raw_text from facts where path=? and page=? and parameter=?',(path,idx[1],k)):
                                    sinks[pending].add([wa,idx[0],idx[1],k,*conflict,name,sha])
                        if vals:measured(wa,sha,name,*idx,vals)
                    for q in db.execute("select page,stage_label,status from pages where path=? and status in ('scanned_pending_ocr','fracture_chart_pending_review')",(path,)):
                        sinks['待复核图页'].add([wa,q[1],q[0],REVIEW[q[2]],name,sha])
                    pages=set()
                    for q in db.execute('select * from points where path=? order by page,series,path_id',(path,)):
                        label,unit,_=SERIES[q['series']];pages.add(q['page']);plot_wells.add(wa)
                        sinks['PDF曲线特征点'].add([wa,q['stage_label'],q['page'],label,unit,q['path_id'],FEATURE[q['feature']],q['time_local'],q['value'],q['axis_residual'],q['line_half_width'],q['time_residual_seconds'],None,q['x_pdf_pt'],q['y_pdf_pt'],sha])
                    plot_pages+=len(pages)
            db.close()
        print('Saved group',group,'of',len(assets),flush=True)
    current_keys={k for k,r in cached.items() if k in seen or families.get((k[0],family(r[1])),k[1])==k[1]}
    for key in current_keys-seen:sinks['PDF报告清单'].add(cached[key])
    for row in rows('PDF施工段参数'):
        if (row[0],row[-1]) in current_keys:
            measured(row[0],row[-1],row[-2],row[1],row[2],{k:v for k,v in zip(PARAM,row[3:-2]) if v is not None})
    pages=set()
    for row in rows('PDF曲线特征点'):
        if (row[0],row[-1]) in current_keys:
            sinks['PDF曲线特征点'].add(row);plot_wells.add(row[0]);pages.add((row[-1],row[2]))
    plot_pages+=len(pages)
    for row in rows('待复核图页'):
        if (row[0],row[-1]) in current_keys:sinks['待复核图页'].add(row)
    for row in rows('字段字典'):sinks['字段字典'].add(row)
    sinks['字段字典'].add([pending,'原文证据','PDF 本页局部文字','需核实所指井、所属施工段、单位和实际施工工况后再用于定量分析。'])
    sinks['字段字典'].add([cluster_tab,'报告列示簇数','原文明确列出 number of clusters 等计数短语','单位为簇。同一页可能记载多次作业或设计参数，按页和原文回查。实际进液簇数另需监测证据。该页也可能出现在 PDF参数待核对。'])
    stats.update(groups_saved=len(assets),groups_expected=128,total_directories=23702,measurement_wells=len(wells),curve_wells=len(plot_wells),curve_pages=plot_pages)
    stats.setdefault('reports_unresolved',0)
    stats.update({key:sum(p['count'] for p in sinks[name].parts) for key,name in [('reports','PDF报告清单'),('feature_points','PDF曲线特征点'),('review_pages','待复核图页'),('candidate_rows',pending)]})
    stats['reported_cluster_rows']=sum(p['count'] for p in sinks[cluster_tab].parts)
    for row in rows('使用说明'):
        if row[0]=='PDF补充范围':
            row[1]=f"已汇总 {len(assets)}/128 个云端分组及已有核实报告。{len(wells)} 口井有明确页码的施工段参数，共 {stats['stage_rows']} 条报告段记录。曲线数值精度见原图半线宽与坐标残差。"
        sinks['使用说明'].add(row)
    sinks['使用说明'].add(['PDF批次覆盖',f"全目录 {stats['total_directories']} 口井。已完整筛查 {stats['wells_complete']} 口井。分组内待重试 {stats['wells_unresolved']} 口井。已读报告见报告清单，扫描页及未识别图件见待复核图页。"])
    new=[]
    for s in sinks.values():s.f.close();new.extend(s.parts)
    (D/'tabs.json').write_text(json.dumps(new,ensure_ascii=False),'utf-8')
    (D/'snapshot.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),'utf-8')
    print('COMPILED_COUNTS',json.dumps(stats),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--tag',required=True);p.add_argument('--first-completed',action='store_true');args=p.parse_args()
    assets=checkpoint_assets(args.tag)
    if not assets:raise SystemExit('No finished well group found')
    if args.first_completed:assets=dict(sorted(assets.items())[:1])
    compile_all(assets)
