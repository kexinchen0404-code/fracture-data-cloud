"""BCER's own HTTPS file API. Credentials/cookies remain in process memory."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = 'https://files.bc-er.ca'
API = HOST + '/ThinClient/WtmApiService.asmx'

class SourceError(RuntimeError):
    pass

class BCERClient:
    def __init__(self, delay=0.5):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.delay = delay
        self.last_request = 0.0

    def post(self, method, payload, allow_error=False):
        retry_read=method in ('GetFileTree','GetFileSubTree','GetDownloadingFileInfo','IsLoggedIn')
        for attempt in range(3 if retry_read else 2):
            time.sleep(max(0, self.delay-(time.monotonic()-self.last_request)))
            headers = {'Content-Type':'application/json; charset=UTF-8',
                       'Referer':HOST+'/ThinClient/WTM/public/main'}
            csrf = next((c.value for c in self.cookies if c.name=='WTM_CSRFToken'), None)
            if csrf:
                headers['WTM_CSRFHeader'] = urllib.parse.unquote(csrf)
            request = urllib.request.Request(API+'/'+method, data=json.dumps(payload).encode(), headers=headers)
            try:
                with self.opener.open(request, timeout=60) as response:
                    result = json.loads(response.read(16*1024*1024)).get('d', {})
            except urllib.error.HTTPError as exc:
                if retry_read and attempt<2 and exc.code in (429,500,502,503,504):
                    delay=exc.headers.get('Retry-After','')
                    if delay.isdigit() and int(delay)>120:raise SourceError(f'{method}: HTTP {exc.code} retry delayed') from None
                    time.sleep(max(2**attempt,int(delay) if delay.isdigit() else 0))
                    continue
                raise SourceError(f'{method}: HTTP {exc.code}') from None
            except (urllib.error.URLError,TimeoutError,ConnectionError) as exc:
                if retry_read and attempt<2:
                    time.sleep(2**attempt);continue
                raise SourceError(f'{method}: {type(exc).__name__}') from None
            except Exception as exc:
                # Never echo request bodies, cookies, redirect URLs, or secret values.
                raise SourceError(f'{method}: {type(exc).__name__}') from None
            finally:
                self.last_request = time.monotonic()
            if result.get('IsOk'):
                return result
            message = str(result.get('ErrorMessage') or '')
            if 'CSRF' in message and attempt==0:
                continue
            if allow_error:
                return result
            reason = ('login_required' if 'logged in' in message.lower() else
                      'csrf_validation_failed' if 'CSRF' in message else 'source_rejected_request')
            raise SourceError(f'{method}: {reason}')
        raise SourceError(f'{method}: source_rejected_request')

    def login(self):
        username = os.environ.get('BCER_USERNAME', '')
        password = os.environ.get('BCER_PASSWORD', '')
        if not username or not password:
            raise SourceError('missing_BCER_USERNAME_or_BCER_PASSWORD_secret')
        # The first API request establishes the normal anti-CSRF cookie.
        self.post('IsLoggedIn', {}, allow_error=True)
        result = self.post('AuthUser', {'user':username, 'pass':password, 'rememberMe':False, 'lang':'en-US'})
        del username, password
        if result.get('IsCredentialsExpired'):
            raise SourceError('source_password_expired_manual_login_required')
        return self.list_folder(None)

    @staticmethod
    def full_path(record):
        return str(record['RelativePath']).rstrip('/')+'/'+record['Name']

    def list_folder(self, path):
        data = self.post('GetFileTree' if path is None else 'GetFileSubTree',
                         {} if path is None else {'subFolderPath':path})
        records = data.get('FilesList')
        if not isinstance(records, list):
            raise SourceError('file_listing_missing')
        for record in records:
            record['full_path'] = self.full_path(record)
        return records

    @staticmethod
    def iso_date(value):
        match = re.fullmatch(r'/Date\((\d+)\)/', str(value))
        if not match:
            raise SourceError('unexpected_source_date_format')
        return dt.datetime.fromtimestamp(int(match[1])/1000,dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')

    def download(self, record, target):
        expected = int(record['Size'])
        if expected>shutil.disk_usage(Path(target).parent).free-64*1024*1024:
            raise SourceError('file_exceeds_cloud_temporary_file_budget')
        info = {k:record.get(k) for k in ['Name','Size','FileType','IsFolder','RelativePath','SessionStatGuid']}
        info.update(FullPath=record['full_path'],Created=self.iso_date(record['Created']),LastModified=self.iso_date(record['LastModified']))
        reply = self.post('GetDownloadingFileInfo', {'fileInfo':info})
        url = urllib.parse.urljoin(HOST+'/ThinClient/',reply['DownloadLink'])
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='files.bc-er.ca':
            raise SourceError('unexpected_download_host')
        digest = hashlib.sha256(); size=0
        with self.opener.open(url, timeout=90) as source, Path(target).open('wb') as out:
            while block:=source.read(512*1024):
                size+=len(block)
                if size>expected:
                    raise SourceError('download_exceeds_directory_size')
                digest.update(block);out.write(block)
        if size!=expected:
            raise SourceError('download_size_mismatch')
        return {'bytes':size,'sha256':digest.hexdigest()}

def check_access(output):
    target=Path(output);target.mkdir(parents=True,exist_ok=True)
    result={'status':'pending','source':HOST,'scope':'all wells; no 100-well limit'}
    try:
        client=BCERClient();root=client.login()
        folders=[r for r in root if r.get('IsFolder')]
        well_data=[r for r in folders if r['Name'].lower()=='welldata']
        result.update(status='access_verified',root_entries=len(root),
                      root_folder_names=[r['Name'] for r in folders],
                      well_data_root=well_data[0]['full_path'] if len(well_data)==1 else None)
        if len(well_data)==1:
            children=client.list_folder(well_data[0]['full_path'])
            result['well_data_subfolders']=[{'path':r['full_path'],'name':r['Name']} for r in children if r.get('IsFolder')]
    except SourceError as exc:
        result.update(status='blocked',reason=str(exc))
    except Exception as exc:
        result.update(status='blocked',reason=type(exc).__name__)
    (target/'access.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result['status']=='access_verified'

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check-access',action='store_true');parser.add_argument('--output',default='results')
    args=parser.parse_args()
    raise SystemExit(0 if check_access(args.output) else 2)
