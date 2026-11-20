"""Select and verify immutable BCER group results from the project release."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def api_json(*parts):
    return json.loads(subprocess.check_output(['gh',*parts]))


def checkpoint_assets(tag):
    repo=os.environ['GITHUB_REPOSITORY']
    rid=api_json('release','view',tag,'--json','databaseId')['databaseId']
    pages=api_json('api','--paginate','--slurp',f'repos/{repo}/releases/{rid}/assets?per_page=100')
    assets={}
    for a in (a for page in pages for a in page):
        match=re.fullmatch(r'group-(\d+)-\d+-\d+\.sqlite\.gz',a['name'])
        if match:
            group=int(match[1])
            if group not in assets or a['created_at']>assets[group]['created_at']:assets[group]=a
    return assets


def download_db(asset,folder):
    gz=Path(folder)/'data.gz';db=Path(folder)/'data.sqlite'
    with gz.open('wb') as f:subprocess.run(['gh','api','-H','Accept: application/octet-stream',f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/assets/{asset['id']}"],stdout=f,check=True)
    if gz.stat().st_size!=asset['size']:raise ValueError('Checkpoint transfer size mismatch')
    if asset.get('digest',''):
        with gz.open('rb') as f:
            if asset['digest']!='sha256:'+hashlib.file_digest(f,'sha256').hexdigest():raise ValueError('Checkpoint transfer hash mismatch')
    with gzip.open(gz,'rb') as source,db.open('wb') as dest:shutil.copyfileobj(source,dest,1<<20)
    return db


