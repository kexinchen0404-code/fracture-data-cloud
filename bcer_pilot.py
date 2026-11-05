"""Small BCER -> Zenodo draft pilot. No local bulk downloads or publication."""
import argparse
import csv
import ftplib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import ssl
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOST = "files.bc-er.ca"
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
    expected = ftp.size(remote)
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
        ftp.retrbinary("RETR " + remote, write, blocksize=CHUNK)
    if count != expected:
        raise ValueError("Incomplete download; partial file retained in cloud staging.")
    if target.suffix.lower() == ".pdf":
        with part.open("rb") as stream:
            if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                raise ValueError("Source is not a PDF; no upload performed.")
    part.replace(target)
    return {"bytes": count, "sha256": sha256.hexdigest(), "md5": md5.hexdigest()}


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
