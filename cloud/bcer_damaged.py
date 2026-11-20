"""Read an unencrypted, truncated, classic PDF without inventing original page numbers."""
import collections
import mmap
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from bcer_client import SourceError

OBJECT=re.compile(rb'(?<![^\r\n])[ \t]*(\d{1,9})[ \t]+(\d{1,5})[ \t]+obj\b')
OBJTYPE=re.compile(rb'/Type\s*/(Catalog|Pages|Page)\b')


def layout(path):
    offsets={};catalogs=[];roots=[];leaves=[]
    with open(path,'rb') as f,mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as raw:
        if raw.find(b'%%EOF')!=-1 or re.search(rb'/(?:Encrypt|ObjStm)\b',raw):
            raise SourceError('source_not_a_truncated_classic_pdf')
        headers=list(OBJECT.finditer(raw))
        for i,h in enumerate(headers):
            end=headers[i+1].start() if i+1<len(headers) else len(raw)
            e=raw.rfind(b'endobj',h.end(),end)
            if e<0:continue
            num,gen=map(int,h.groups())
            if num in offsets:raise SourceError('ambiguous_indirect_object_version')
            head=raw[h.end():min(e,h.end()+16384)]
            head=re.split(rb'(?:^|\s)stream(?:\r\n|\r|\n)',head,maxsplit=1)[0]
            offsets[num]=(h.start(1),gen)
            t=OBJTYPE.search(head)
            if not t:continue
            ref=(num,gen)
            if t[1]==b'Catalog':catalogs.append(ref)
            elif t[1]==b'Pages' and not re.search(rb'/Parent\b',head):roots.append(ref)
            elif t[1]==b'Page':leaves.append(ref)
    if not leaves:raise SourceError('no_intact_page_objects')
    return offsets,catalogs,roots,leaves


def ref_bytes(ref):return ('%d %d R'%ref).encode('ascii')


def indexed_copy(src,dest,parts,fragments):
    offsets,catalogs,roots,leaves=parts;index=dict(offsets);new=max(index)+1
    with open(src,'rb') as original,open(dest,'wb') as out:
        shutil.copyfileobj(original,out,1<<20);out.write(b'\n')
        def append(data):
            nonlocal new
            num=new;new+=1;index[num]=(out.tell(),0)
            out.write(str(num).encode()+b' 0 obj\n'+data+b'\nendobj\n')
            return (num,0)
        if fragments:
            # This traversal order is used only for inspection; exported records retain source object IDs.
            kids=b' '.join(ref_bytes(k) for k in leaves)
            tree=append(b'<< /Type /Pages /Count '+str(len(leaves)).encode()+b' /Kids ['+kids+b'] >>')
            root=append(b'<< /Type /Catalog /Pages '+ref_bytes(tree)+b' >>')
        elif len(catalogs)==1:root=catalogs[0]
        elif not catalogs and len(roots)==1:
            root=append(b'<< /Type /Catalog /Pages '+ref_bytes(roots[0])+b' >>')
        else:raise SourceError('original_page_tree_root_unavailable')
        xref=out.tell();out.write(b'xref\n0 1\n0000000000 65535 f \n')
        ids=sorted(index);start=0
        for i in range(1,len(ids)+1):
            if i<len(ids) and ids[i]==ids[i-1]+1:continue
            group=ids[start:i];start=i
            out.write(('%d %d\n'%(group[0],len(group))).encode())
            for n in group:out.write(('%010d %05d n \n'%index[n]).encode())
        out.write(b'trailer\n<< /Size '+str(max(index)+1).encode()+b' /Root '+ref_bytes(root)+b' >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n')


def original_tree_text(path):
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject
    parts=layout(path);offsets,_,_,leaves=parts
    with TemporaryDirectory() as d:
        fixed=Path(d)/'original-tree.pdf';indexed_copy(path,fixed,parts,False)
        try:
            with fixed.open('rb') as f:
                reader=PdfReader(f,strict=True);seen=set();ordered=[]
                def walk(ref,parent=None):
                    if not isinstance(ref,IndirectObject):raise ValueError('non-indirect page tree')
                    k=(ref.idnum,ref.generation)
                    if k in seen or k[0] not in offsets or offsets[k[0]][1]!=k[1]:raise ValueError('missing or repeated source object')
                    seen.add(k);node=ref.get_object()
                    if parent is not None and node.raw_get('/Parent')!=parent:raise ValueError('inconsistent source parent')
                    if node.get('/Type')=='/Page':ordered.append(k);return 1
                    if node.get('/Type')!='/Pages':raise ValueError('invalid source tree')
                    count=sum(walk(child,ref) for child in node['/Kids'])
                    if count!=node['/Count']:raise ValueError('incomplete original page count')
                    return count
                count=walk(reader.trailer['/Root'].raw_get('/Pages'))
                if count!=len(leaves) or ordered!=[(p.indirect_reference.idnum,p.indirect_reference.generation) for p in reader.pages]:
                    raise ValueError('source leaves and order differ')
                check=subprocess.run(['qpdf','--check',str(fixed)],capture_output=True,timeout=240)
                if check.returncode:raise ValueError('incomplete referenced streams or objects')
                for pg in reader.pages:yield pg.extract_text() or ''
        except SourceError:raise
        except Exception as exc:raise SourceError('original_page_tree_not_verified_'+type(exc).__name__) from None


def fragment_rows(path):
    """Read source fragments in their own namespace; the synthetic view's page index is never exported."""
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject
    import pymupdf
    parts=layout(path);offsets,_,_,leaves=parts
    with TemporaryDirectory() as d:
        fixed=Path(d)/'source-objects.pdf';indexed_copy(path,fixed,parts,True)
        with fixed.open('rb') as f:
            reader=PdfReader(f,strict=True)
            with pymupdf.open(fixed) as render:
                for i,(num,gen) in enumerate(leaves):
                    text='';status='source_object_only'
                    try:
                        page=reader.pages[i]
                        if (page.indirect_reference.idnum,page.indirect_reference.generation)!=(num,gen):raise ValueError('object identity')
                        original=reader.get_object(IndirectObject(num,gen,reader))
                        # Require original content, resources and page bounds before reading an isolated object.
                        if not all(k in original for k in ('/Contents','/Resources','/MediaBox')):raise ValueError('missing standalone resources')
                        if not original['/Resources'] or page.get_contents() is None:raise ValueError('missing content')
                        text=page.extract_text() or '';status='native_text' if len(text.strip())>=40 else 'source_object_low_text'
                        if status=='source_object_low_text' and render.page_xref(i)==num:
                            rp=render[i];size=max(rp.rect.width,rp.rect.height)
                            if size<=0:raise ValueError('missing bounds')
                            scale=min(2.5,2200/size)
                            pix=rp.get_pixmap(matrix=pymupdf.Matrix(scale,scale),colorspace=pymupdf.csGRAY,alpha=False)
                            proc=subprocess.run(['tesseract','stdin','stdout','-l','eng','--psm','3'],input=pix.tobytes('png'),capture_output=True,timeout=60)
                            ocr=proc.stdout.decode('utf-8',errors='replace').strip()
                            if proc.returncode==0 and len(ocr)>=40:text=ocr;status='ocr_text'
                    except Exception:
                        if not text:status='incomplete_or_inherited_resources'
                    yield num,gen,offsets[num][0],status,text
