"""Bounded BCER pilot in Codespaces, with private staging over HTTPS."""
import argparse
import csv
import ftplib
import getpass
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import ssl
import sys
import warnings
from datetime import datetime, timezone
from urllib.error import HTTPError
from http.cookiejar import CookieJar
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

HOST = "files.bc-er.ca"
BCER = "https://" + HOST
ZENODO = "https://zenodo.org"
CHUNK = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Zenodo redirected the request; token was not forwarded.")


def require_cloud():
    if os.environ.get("CODESPACES") != "true":
        raise RuntimeError("Run data transfers in GitHub Codespaces, not on this computer.")


def read_sources(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or len(rows) > 98:
        raise ValueError("A pilot requires 1-98 source files.")
    names = set()
    for row in rows:
        remote = row.get("remote_path", "")
        name = PurePosixPath(remote).name
        if (not remote.startswith("/WellData/") or ".." in remote.split("/")
                or any(c in remote for c in "\r\n\0\\")
                or not name or name in names or name == "pilot_manifest.json"):
            raise ValueError("Invalid or duplicate BCER source path.")
        names.add(name)
    return rows


def connect_ftps():
    context = ssl.create_default_context()
    # BCER currently omits R13 on its FTPS chain. Supply the official intermediate,
    # while still requiring a trusted root, hostname verification and valid dates.
    context.load_verify_locations(cafile=str(Path(__file__).parent / "certs/letsencrypt-r13.pem"))
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    ftp = ftplib.FTP_TLS(context=context, timeout=60)
    ftp.connect(HOST, 21)
    ftp.auth()
    return ftp


def download(ftp, remote, target, limit):
    ftp.voidcmd("TYPE I")
    return save_download(target, ftp.size(remote), limit,
                         lambda write: ftp.retrbinary("RETR " + remote, write, blocksize=CHUNK))


def save_download(target, expected, limit, transfer):
    if expected is None or expected <= 0 or expected > limit:
        raise ValueError("Source size is unknown, empty, or exceeds the remaining pilot limit.")
    part = target.with_name(target.name + ".part")
    sha256, md5, count = hashlib.sha256(), hashlib.md5(), 0
    with part.open("wb") as stream:
        def write(block):
            nonlocal count
            if count + len(block) > limit or count + len(block) > expected:
                raise ValueError("Transfer exceeded its declared size or pilot limit.")
            stream.write(block)
            sha256.update(block)
            md5.update(block)
            count += len(block)
        transfer(write)
    if count != expected:
        raise ValueError("Incomplete download; partial file retained in cloud staging.")
    with part.open("rb") as stream:
        header = stream.read(65536)
    if target.suffix.lower() == ".pdf" and not header.lstrip().startswith(b"%PDF-"):
        raise ValueError("Source is not a PDF; partial file retained.")
    if target.suffix.lower() == ".las" and not re.search(rb"(?mi)^[ \t]*~V", header.lstrip(b"\xef\xbb\xbf")):
        raise ValueError("LAS version section not found; partial file retained.")
    part.replace(target)
    return {"bytes": count, "sha256": sha256.hexdigest(), "md5": md5.hexdigest()}


class BCERRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.netloc != HOST:
            raise RuntimeError("BCER redirected outside its HTTPS host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BCERWeb:
    """Use the eLibrary Web Transfer Client's normal authenticated requests."""

    def __init__(self):
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), BCERRedirect())

    def open(self, url, data=None, headers=None):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != HOST:
            raise ValueError("Unexpected BCER URL.")
        try:
            return self.opener.open(Request(url, data=data, headers=headers or {}), timeout=60)
        except HTTPError as exc:
            raise RuntimeError("BCER returned HTTP %d." % exc.code) from None

    def post(self, method, value):
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Origin": BCER, "Referer": BCER + "/ThinClient/"}
        for cookie in self.cookies:
            if cookie.name == "XSRF-TOKEN":
                headers["X-XSRF-TOKEN"] = unquote(cookie.value)
        data = json.dumps(value).encode("utf-8")
        with self.open(BCER + "/ThinClient/WtmApiService.asmx/" + method, data, headers) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("BCER metadata response exceeded the pilot limit.")
        result = json.loads(body)["d"]
        if not result.get("IsOk"):
            raise RuntimeError("BCER rejected %s. Check login or source availability." % method)
        return result

    def login(self, username, password):
        with self.open(BCER + "/ThinClient/"):
            pass
        result = self.post("AuthUser", {"user": username, "pass": password,
                                       "rememberMe": False, "lang": "en-US"})
        if result.get("IsCredentialsExpired") or result.get("IsTOTPConfigured") or result.get("TOTPSetupMode") == 2:
            raise RuntimeError("BCER requires an account action in its web login.")

    def files(self, folder):
        return {f["Name"]: f for f in self.post("GetFileSubTree", {"subFolderPath": folder})["FilesList"]
                if not f.get("IsFolder")}

    def download(self, remote, info, target, limit):
        expected = int(info["Size"])

        def transfer(write):
            file_info = {key: info[key] for key in
                         ("Name", "Size", "FileType", "IsFolder", "RelativePath", "SessionStatGuid")
                         if key in info}
            file_info["FullPath"] = remote
            for key in ("Created", "LastModified"):
                timestamp = re.fullmatch(r"/Date\((\d+)\)/", info[key])
                if not timestamp:
                    raise ValueError("Unexpected BCER file timestamp.")
                file_info[key] = datetime.fromtimestamp(int(timestamp[1]) / 1000, timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z")
            link = self.post("GetDownloadingFileInfo", {"fileInfo": file_info})["DownloadLink"]
            url = urljoin(BCER + "/ThinClient/", link)
            with self.open(url) as response:
                if response.headers.get_content_type() in ("text/html", "application/json"):
                    raise ValueError("BCER returned a page instead of the requested file.")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != expected:
                    raise ValueError("BCER download size differed from its directory entry.")
                for block in iter(lambda: response.read(CHUNK), b""):
                    write(block)

        return save_download(target, expected, limit, transfer)


def stage_https(rows, limit):
    if not sys.stdin.isatty():
        raise RuntimeError("Use the interactive terminal in the private Codespace.")
    client = BCERWeb()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        username = getpass.getpass("BCER username (hidden): ")
        password = getpass.getpass("BCER password (hidden): ")
    if not username or not password:
        raise ValueError("Both BCER credentials are required.")
    client.login(username, password)
    del username, password
    stage = Path(".cloud-state") / datetime.now(timezone.utc).strftime("https-%Y%m%dT%H%M%S%fZ")
    stage.mkdir(mode=0o700, parents=True)
    manifest = stage / "pilot_manifest.json"
    folders, records = {}, []
    for row in rows:
        remote = row["remote_path"]
        source = PurePosixPath(remote)
        folder = str(source.parent)
        if folder not in folders:
            folders[folder] = client.files(folder)
        info = folders[folder].get(source.name)
        if info is None:
            raise ValueError("BCER source file not found: " + source.name)
        check = client.download(remote, info, stage / source.name, limit)
        limit -= check["bytes"]
        records.append(dict(row, filename=source.name, source_host=HOST,
                            downloaded_at=datetime.now(timezone.utc).isoformat(), **check))
        temporary = manifest.with_suffix(".json.part")
        temporary.write_text(json.dumps({"transport": "HTTPS", "files": records}, ensure_ascii=False,
                                        indent=2), encoding="utf-8")
        temporary.replace(manifest)
        print("Staged and checked:", source.name, check["bytes"], "bytes", flush=True)
    print("Cloud manifest:", manifest, flush=True)


class Zenodo:
    def __init__(self, token):
        self.token = token
        self.opener = build_opener(NoRedirect())

    def call(self, method, url, data=None, length=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "zenodo.org"
                or parsed.query or parsed.fragment):
            raise ValueError("Refusing to send the Zenodo token to this URL.")
        headers = {"Authorization": "Bearer " + self.token,
                   "Content-Type": "application/octet-stream"}
        if isinstance(data, dict):
            data = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if length is not None:
            headers["Content-Length"] = str(length)
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=180) as response:
                body = response.read(1024 * 1024 + 1)
        except HTTPError as exc:
            raise RuntimeError("Zenodo HTTP error %s; check account, scope and quota."
                               % exc.code) from None
        if len(body) > 1024 * 1024:
            raise ValueError("Unexpectedly large Zenodo API response.")
        return json.loads(body) if body else {}

    def draft(self, draft_id):
        result = self.call("GET", ZENODO + "/api/deposit/depositions/" + str(draft_id))
        if result.get("submitted"):
            raise ValueError("Use an unpublished Zenodo draft.")
        return result

    def upload(self, draft_id, path, replace_manifest=False):
        draft = self.draft(draft_id)
        size = path.stat().st_size
        digest = hashlib.md5()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
        wanted = digest.hexdigest()
        existing = next((f for f in draft.get("files", [])
                         if f.get("filename") == path.name), None)
        if existing and not replace_manifest:
            if (existing.get("checksum", "").removeprefix("md5:") != wanted
                    or existing.get("filesize") != size):
                raise ValueError("A different file exists in the draft; refusing to overwrite.")
        else:
            bucket = draft["links"]["bucket"].rstrip("/")
            with path.open("rb") as stream:
                self.call("PUT", bucket + "/" + quote(path.name, safe=""),
                          data=stream, length=size)
        actual = next((f for f in self.draft(draft_id).get("files", [])
                       if f.get("filename") == path.name), None)
        if (not actual or actual.get("checksum", "").removeprefix("md5:") != wanted
                or actual.get("filesize") != size):
            raise ValueError("Zenodo size/checksum verification failed; cloud copy retained.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true", help="TLS handshake only; no credentials/data")
    mode.add_argument("--https-stage", action="store_true", help="Privately stage three BCER files over HTTPS")
    mode.add_argument("--create-draft", action="store_true")
    mode.add_argument("--draft-id", type=int)
    parser.add_argument("--sources", default="sources.csv")
    parser.add_argument("--max-files", type=int, default=3)
    parser.add_argument("--max-bytes", type=int, default=100_000_000)
    args = parser.parse_args()
    if args.probe:
        ftp = connect_ftps()
        print("BCER explicit FTPS: verified TLS handshake succeeded.")
        ftp.close()
        return
    require_cloud()
    if args.https_stage:
        if not (1 <= args.max_files <= 3 and 1 <= args.max_bytes <= 100_000_000):
            raise ValueError("HTTPS pilot bounds: 1-3 files and at most 100 MB.")
        stage_https(read_sources(args.sources)[:args.max_files], args.max_bytes)
        return
    token = os.environ.get("ZENODO_TOKEN")
    if not token:
        raise ValueError("Set ZENODO_TOKEN in Codespaces Secrets; do not paste it in code.")
    zenodo = Zenodo(token)
    if args.create_draft:
        draft = zenodo.call("POST", ZENODO + "/api/deposit/depositions", {})
        print("Unpublished Zenodo draft ID:", draft["id"])
        return
    if not args.draft_id or args.draft_id < 1:
        raise ValueError("A positive draft ID is required.")
    if not (1 <= args.max_files <= 98 and 1 <= args.max_bytes <= 5_000_000_000):
        raise ValueError("Pilot bounds: 1-98 files and at most 5 GB.")
    rows = read_sources(args.sources)[:args.max_files]
    zenodo.draft(args.draft_id)
    username, password = os.environ.get("BCER_USERNAME"), os.environ.get("BCER_PASSWORD")
    if not username or not password:
        raise ValueError("Set BCER_USERNAME and BCER_PASSWORD in Codespaces Secrets.")
    stage = Path(".cloud-state") / str(args.draft_id)
    stage.mkdir(parents=True, exist_ok=True)
    ftp = connect_ftps()
    records, total = [], 0
    try:
        ftp.login(username, password)
        ftp.prot_p()
        for row in rows:
            remote = row["remote_path"]
            target = stage / PurePosixPath(remote).name
            checks = download(ftp, remote, target, args.max_bytes - total)
            total += checks["bytes"]
            entry = dict(row, filename=target.name, source_host=HOST,
                         downloaded_at=datetime.now(timezone.utc).isoformat(), **checks)
            records.append(entry)
            manifest = stage / "pilot_manifest.json"
            manifest.write_text(json.dumps({"draft_id": args.draft_id, "files": records},
                                           ensure_ascii=False, indent=2), encoding="utf-8")
            zenodo.upload(args.draft_id, target)
            # Remove only the verified cloud staging copy; raw source is untouched.
            target.unlink()
            print("Verified in Zenodo:", entry["filename"], checks["bytes"], "bytes")
        zenodo.upload(args.draft_id, manifest, replace_manifest=True)
        print("Pilot complete:", len(records), "files,", total, "bytes; draft remains unpublished.")
        print("Draft:", ZENODO + "/uploads/" + str(args.draft_id))
    finally:
        ftp.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("STOP:", str(exc), file=sys.stderr)
        sys.exit(1)
