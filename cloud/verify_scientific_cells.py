"""Hash complete scientific row payloads independently of worksheet display settings."""
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

def data_hash(z,part):
    start=b'<sheetData>';end=b'</sheetData>';active=False;buffer=b'';h=hashlib.sha256()
    with z.open(part) as source:
        while chunk:=source.read(1<<20):
            buffer+=chunk
            if not active:
                pos=buffer.find(start)
                if pos<0:
                    buffer=buffer[-len(start):];continue
                buffer=buffer[pos+len(start):];active=True
            pos=buffer.find(end)
            if pos>=0:
                h.update(buffer[:pos]);return h.hexdigest()
            h.update(buffer[:-len(end)]);buffer=buffer[-len(end):]
    raise ValueError('Missing worksheet data boundary '+part)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--record',action='store_true');a=p.parse_args()
    file=Path(__file__).with_name('native_rows_sha256.json')
    with ZipFile(next(Path('outputs/BCER_全井压裂汇总_最新').glob('*.xlsx'))) as z:
        got={f'sheet{i}':data_hash(z,f'xl/worksheets/sheet{i}.xml') for i in (2,3,4,5,7)}
    if a.record:file.write_text(json.dumps(got,indent=2),'utf-8')
    else:
        expected=json.loads(file.read_text('utf-8'))
        mismatch=[k for k in expected if got.get(k)!=expected[k]]
        if mismatch:raise ValueError('Scientific row payload mismatch '+str(mismatch))
    print('Verified complete rows in five regulator/coverage sheets; report sheets checked cell by cell during merge.')
