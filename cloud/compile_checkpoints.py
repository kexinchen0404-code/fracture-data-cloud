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
from latest_values import compile_latest,defs as latest_definitions
from revision_audit import outdated

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
    unreadable=dict(name='PDF未读报告',headers=['井号 WA','报告文件','目录大小 B','已验证源文件 SHA-256','源目录修改时间','源路径','读取状态','具体原因'],widths=[13,54,19,73,30,102,20,84],template_index=6,use_widths=True)
    frag=dict(name='受损PDF片段',headers=['井号 WA','报告文件','原始对象号','对象代次','原文件字节偏移','识别方式或缺失原因','可读字符数','段标签候选','原文参数候选（含单位）','原文件 SHA-256'],widths=[14,58,17,13,25,40,20,23,90,74],template_index=6,use_widths=True)
    bench=dict(name='层位与邻井压力线索',headers=['井号 WA','原页段标签','PDF 页','原页面出现的 Montney 层位','邻井压力或压窜文字命中','报告文件','报告 SHA-256'],widths=[15,18,13,52,36,62,74],template_index=6,use_widths=True)
    detail=dict(name='PDF逐簇射孔明细',headers=['井号 WA','施工段号','簇号','射孔顶深 mKB','射孔底深 mKB','射孔日期（报告月/日/年）','原报告页码','报告文件','报告 SHA-256'],widths=[14,15,12,23,23,30,25,60,74],template_index=6,use_widths=True)
    tables=[*TABS,extra,clusters,unreadable,frag,bench,*latest_definitions(),detail]
    for t in tables:
        if t['name'] in ('PDF施工段参数','PDF曲线特征点','PDF与大表核对','PDF报告清单','PDF参数待核对','PDF射孔簇数','待复核图页','PDF未读报告','受损PDF片段','层位与邻井压力线索'):
            t['missing_range']=[1,len(t['headers'])]
    sinks={t['name']:Sink(t,i) for i,t in enumerate(tables)}
    seen=set();families={};cached={(r[0],r[4]):r for r in rows('PDF报告清单')};stats=collections.Counter();wells=set();plot_wells=set();plot_pages=0;all_wells=set();tie_keys=set();held_sources=set();blocked_by_wa={}
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
                all_wells.update(wa for wa, in db.execute('select wa from wells'))
                obsolete,ties=outdated(db)
                if obsolete:raise ValueError(f'Group {group} contains a newer unselected source revision for {len(obsolete)} wells')
                tie_keys.update(ties);group_ties=set(ties)
                has_frag=db.execute("select 1 from sqlite_master where type='table' and name='fragments'").fetchone()
                stats['wells_complete']+=counts.get(1,0);stats['wells_unresolved']+=counts.get(2,0);stats['wells_pending_in_saved_groups']+=counts.get(0,0)
                # Include current files even if their newly listed version failed to transfer.
                for r in db.execute('select * from files where is_current=1 order by wa,name'):
                    wa=str(r['wa']);name=r['name'];sha=r['sha256'];key=(wa,sha);path=r['path']
                    families[(wa,family(name))]=sha if r['status']=='text_processed' else None
                    ms=re.search(r'\d{10,}',r['modified'] or '')
                    stamp=dt.datetime.fromtimestamp(int(ms[0])/1000,dt.timezone.utc).isoformat() if ms else None
                    if (wa,r['family']) in group_ties or r['status']!='text_processed':
                        w=str(int(wa))
                        blocked_by_wa[w]=max(stamp or 'unknown',blocked_by_wa.get(w,''))
                    if (wa,r['family']) in group_ties:
                        held_sources.add(key);families[(wa,family(name))]=None
                        sinks['PDF未读报告'].add([wa,name,r['size'],sha,stamp,path,'修订先后待核','同一报告族最新修改时间和版本序号均并列，保留原记录线索。'])
                        continue
                    if r['status']!='text_processed' or not sha:
                        stats['reports_unresolved']+=1
                        sinks['PDF未读报告'].add([wa,name,r['size'],sha,stamp,path,r['status'],r['error']])
                        if has_frag:
                            for q in db.execute('select object_id,generation,byte_offset,status,text_chars,stage_label,parameters from fragments where path=? order by byte_offset',(path,)):
                                candidates=json.loads(q[6]);raw=' | '.join(x[3] for x in candidates)
                                state={'native_text':'原对象可编辑文字','ocr_text':'原对象图像 OCR，数值待复核','source_object_low_text':'原对象文字过少','incomplete_or_inherited_resources':'内容或原始显示资源不完整','source_object_only':'仅定位到原对象'}.get(q[3],q[3])
                                sinks['受损PDF片段'].add([wa,name,q[0],q[1],q[2],state,q[4],q[5],raw or None,sha])
                        continue
                    if key in seen:continue
                    seen.add(key)
                    sinks['PDF报告清单'].add([wa,name,r['pages'],r['size'],sha,stamp,path,'云端读取',r['template']])
                    for q in db.execute("select stage_label,page,formation,frac_hit_mention from pages where path=? and (coalesce(formation,'')!='' or frac_hit_mention=1) order by page",(path,)):
                        sinks['层位与邻井压力线索'].add([wa,q[0],q[1],q[2] or None,'命中词句，需核对所指井' if q[3] else '未命中本轮词式',name,sha])
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
    current_keys={k for k,r in cached.items() if k not in held_sources and (k in seen or families.get((k[0],family(r[1])),k[1])==k[1])}
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
    sinks['字段字典'].add(['每井最新值','最新可核实取值','同一井号及同一物理参数','有来源修改时间时比较报告 UTC 修改时间。并列的不同值或同时存在空白时保留待核标记。无修改时间时，仅有单项或各项完全一致才保留值。'])
    sinks['字段字典'].add(['逐条缺失索引','缺失字段名称','原始 CSV 行或已确认报告段页面','原文空白保持可计算的空单元格。源表填报的 0 单列统计。未记录与无法判定另列。'])
    sinks['字段字典'].add(['最新值取值依据','当次作业值（参考）','源表最近施工日期及已知开始时钟','有多条同时刻记录时，只有同值或时间可唯一排序才显示该次作业参考值。源站报告修改时间与现场施工时间分别记录。'])
    sinks['字段字典'].add(['受损PDF片段','原始对象号和文件偏移','未能还原完整页序的公开报告','用源文件哈希及 PDF 原对象号回查。OCR 数值保留为线索。原始页序无法证明的片段不参与逐段定量汇总。'])
    sinks['字段字典'].add(['层位与邻井压力线索','层位与词句命中','报告原页匹配','可能提及本井或邻井，仅作为原页查找线索。井层归属、子母井配对、压窜事件须用原文和现场证据确认。'])
    cluster_path=Path('checked-clusters.json')
    if cluster_path.is_file():
        reports=json.loads(cluster_path.read_text(encoding='utf8'))
        assert all((r['wa'],r['sha']) in seen or (r['wa'],r['sha']) in current_keys for r in reports)
        stats['cluster_reports_checked']=len(reports)
        for r in reports:
            stages=collections.defaultdict(list)
            for st,cl,a,b,date,pages0 in r['rows']:
                stages[st].append((cl,pages0))
                sinks[detail['name']].add([r['wa'],st,cl,a,b,date,', '.join(map(str,pages0)),r['name'],r['sha']])
            for st,entries in stages.items():
                ids=sorted(cl for cl,_ in entries)
                if ids!=list(range(1,len(entries)+1)) or st in r['conflict_stages']:continue
                pp=sorted({p for _,ps in entries for p in ps})
                sinks[cluster_tab].add([r['wa'],str(st),pp[0],len(ids),'逐簇实射表连续编号','已定位簇号 1–'+str(ids[-1])+'，原报告页 '+', '.join(map(str,pp)),r['name'],r['sha']])
        sinks['字段字典'].add([detail['name'],'射孔深度及逐段簇数','Type=Perforated，Interval Number，Cluster #，Top Depth/Btm (mKB)','实射簇以井、报告、段号、簇号为键。重复出现保留全部页码。顶底深为以转盘方补心为基准的报告深度。逐段计数为所见表内不同连续簇号数。有效进液簇另需监测。'])
        stats['perforated_cluster_records']=sum(p['count'] for p in sinks[detail['name']].parts)
    def collected(name):
        sink=sinks[name];sink.f.flush()
        for t in sink.parts:
            with (D/t['rows_file']).open(encoding='utf-8') as f:
                for line in f:yield json.loads(line)
    stats.update(compile_latest(all_wells,sinks,rows('字段字典'),collected,blocked_by_wa))
    stats['same_time_revision_ties']=len(tie_keys)
    stats['directory_wells_indexed']=len(all_wells)
    stats['unread_or_revision_held_rows']=sum(p['count'] for p in sinks['PDF未读报告'].parts)
    stats['source_coverage_status']='source_exceptions_listed' if stats['unread_or_revision_held_rows'] else 'all_current_reports_read'
    stats.update(groups_saved=len(assets),groups_expected=128,total_directories=23702,measurement_wells=len(wells),curve_wells=len(plot_wells),curve_pages=plot_pages)
    stats.setdefault('reports_unresolved',0)
    stats.update({key:sum(p['count'] for p in sinks[name].parts) for key,name in [('reports','PDF报告清单'),('feature_points','PDF曲线特征点'),('review_pages','待复核图页'),('candidate_rows',pending)]})
    stats['reported_cluster_rows']=sum(p['count'] for p in sinks[cluster_tab].parts)
    stats['source_fragment_objects']=sum(p['count'] for p in sinks['受损PDF片段'].parts)
    stats['formation_or_offset_pages']=sum(p['count'] for p in sinks['层位与邻井压力线索'].parts)
    for row in rows('使用说明'):
        if row[0]=='PDF补充范围':
            row[1]=f"已汇总 {len(assets)}/128 个云端分组及已有核实报告。{len(wells)} 口井有明确页码的施工段参数，共 {stats['stage_rows']} 条报告段记录。曲线数值精度见原图半线宽与坐标残差。"
        sinks['使用说明'].add(row)
    sinks['使用说明'].add(['PDF批次覆盖',f"全目录 {stats['total_directories']} 口井。已完整筛查 {stats['wells_complete']} 口井。分组内待重试 {stats['wells_unresolved']} 口井。已读报告见报告清单，扫描页及未识别图件见待复核图页。"])
    sinks['使用说明'].add(['缺失值','浅黄色空格对应源值缺失或取值待核。逐条缺失索引可按井号、段及字段名查回 CSV 行或 PDF 页；空白和原文数字 0 分开统计。'])
    sinks['使用说明'].add(['一井一行','每井最新值按井号和物理参数只保留一项，最新值取值依据记录修改时间与选择状态。源表若没有逐条修订时间，作业日期仅供参考。独立施工段、射孔段和净区间仍可在明细表定位。'])
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
