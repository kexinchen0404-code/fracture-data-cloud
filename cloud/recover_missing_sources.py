"""Read requested current BCER reports in cloud scratch; export text and small evidence slices."""
import csv
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
import zipfile

from bcer_client import BCERClient
from imputation_sources import NATIVE, report_family


REQUEST = Path("cloud/missing_sources_request.json")
OUT = Path("well-missing-sources")


def load_request():
    reports = json.loads(REQUEST.read_text(encoding="utf-8"))["reports"]
    if not isinstance(reports, list) or not reports:
        raise ValueError("invalid_request")
    seen = set()
    for item in reports:
        wa, name, digest = str(item["wa"]), item["file"], item["sha256"]
        pages = item.get("render_pages", [])
        if (not re.fullmatch(r"[0-9]{1,5}", wa) or int(wa) == 0
                or not isinstance(name, str) or "/" in name or "\\" in name
                or not name.upper().endswith(".PDF")
                or not name.startswith(wa + "_")
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(pages, list)
                or any(type(page) is not int or page < 1 for page in pages)):
            raise ValueError("invalid_request")
        key = (wa, report_family(name))
        if key in seen:
            raise ValueError("duplicate_report_family")
        seen.add(key)
        item.update(wa=wa, render_pages=sorted(set(pages)))
    return reports


def well_folder(wa):
    n = int(wa)
    thousand, hundred = n // 1000 * 1000, n // 100 * 100
    return f"/WellData/{thousand:05d}-{thousand+999:05d}/{hundred:05d}-{hundred+99:05d}/{wa}"


def save_index(index):
    index["counts"] = {
        "requested": len(index["reports"]),
        "verified": sum(row["status"] == "verified" for row in index["reports"]),
        "rendered_pages": sum(len(row.get("rendered_pages", [])) for row in index["reports"]),
    }
    (OUT / "source-index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def read_report(client, request, row):
    records = client.list_folder(row["folder"])
    versions = [r for r in records if not r.get("IsFolder")
                and report_family(r["Name"]) == report_family(request["file"])]
    row["versions"] = [dict(file=r["Name"], path=r["full_path"], modified=client.iso_date(r["LastModified"]))
                       for r in versions]
    if not versions:
        row["status"] = "source_family_missing"
        return
    stamp = max(client.iso_date(r["LastModified"]) for r in versions)
    current = [r for r in versions if client.iso_date(r["LastModified"]) == stamp]
    row["modified"] = stamp
    if len(current) != 1:
        row["status"] = "ambiguous_latest"
        return
    record = current[0]
    row.update(file=record["Name"], path=record["full_path"])
    if record["Name"].casefold() != request["file"].casefold():
        row["status"] = "revision_changed"
        return
    with tempfile.TemporaryDirectory() as scratch:
        pdf = Path(scratch) / "report.pdf"
        receipt = client.download(record, pdf)
        row.update(sha256=receipt["sha256"], bytes=receipt["bytes"])
        if receipt["sha256"] != request["sha256"]:
            row["status"] = "hash_changed"
            return
        text = subprocess.check_output(["pdftotext", "-enc", "UTF-8", "-layout", str(pdf), "-"],
                                       stderr=subprocess.DEVNULL).decode("utf-8")
        pages = text.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        row["pages"] = len(pages)
        row["text_file"] = receipt["sha256"] + "-text.json"
        (OUT / row["text_file"]).write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        row["status"] = "verified"
        row["rendered_pages"], row["render_errors"] = [], []
        for page in request["render_pages"]:
            if page > len(pages):
                row["render_errors"].append(dict(page=page, error_class="page_out_of_range"))
                continue
            stem = receipt["sha256"] + f"-page-{page}"
            try:
                subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "2400",
                                "-png", str(pdf), str(OUT / stem)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                row["rendered_pages"].append(dict(page=page, file=stem + ".png"))
            except Exception as exc:
                row["render_errors"].append(dict(page=page, error_class=type(exc).__name__))


def read_native(wells, native):
    with tempfile.TemporaryDirectory() as scratch:
        archive_path = Path(scratch) / "native.zip"
        digest = hashlib.sha256()
        with urllib.request.urlopen(NATIVE, timeout=90) as response, archive_path.open("wb") as target:
            native["modified"] = response.headers.get("Last-Modified")
            while block := response.read(512 * 1024):
                digest.update(block)
                target.write(block)
        native["sha256"] = digest.hexdigest()
        with zipfile.ZipFile(archive_path) as archive, archive.open("hydraulic_fracture.csv") as part:
            rows = [[line, row] for line, row in enumerate(csv.DictReader(io.TextIOWrapper(part, encoding="cp1252")), 2)
                    if row["WA NUM"] in wells]
        (OUT / "native.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        native.update(status="verified", rows=len(rows), file="native.json",
                      rows_by_well={well: sum(row["WA NUM"] == well for _, row in rows) for well in sorted(wells)})


def main():
    OUT.mkdir(exist_ok=True)
    index = dict(schema_version=1, created_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                 reports=[], native=dict(url=NATIVE, status="pending"))
    try:
        requests = load_request()
    except Exception as exc:
        index.update(status="invalid_request", error_class=type(exc).__name__)
        save_index(index)
        print("Request validation failed; see classified source index.")
        return 2
    index["reports"] = [dict(wa=r["wa"], expected_file=r["file"], expected_sha256=r["sha256"],
                             folder=well_folder(r["wa"]), status="pending") for r in requests]
    save_index(index)
    client = BCERClient()
    login_error = None
    try:
        client.login()
    except Exception as exc:
        login_error = type(exc).__name__
    for request, row in zip(requests, index["reports"]):
        if login_error:
            row.update(status="login_failed", error_class=login_error)
        else:
            try:
                read_report(client, request, row)
            except Exception as exc:
                row.update(status="failed", error_class=type(exc).__name__)
        save_index(index)
        print(f"WA{row['wa']}: {row['status']}", flush=True)
    try:
        read_native({r["wa"] for r in requests}, index["native"])
    except Exception as exc:
        index["native"].update(status="failed", error_class=type(exc).__name__)
    complete = (all(r["status"] == "verified" and not r.get("render_errors") for r in index["reports"])
                and index["native"]["status"] == "verified")
    index["status"] = "complete" if complete else "partial"
    save_index(index)
    print(json.dumps(dict(status=index["status"], counts=index["counts"], native_status=index["native"]["status"])))
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
