"""Collect current report text, a small set of original pages and three official CSV slices."""
import csv
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


# Pages are the stage parameter pages in the verified current catalog.
CASES = {
    "34230": ("34230_COMP_2022JAN15_D9-14 FRAC UPDATED VERSION.PDF", "de81b15a51b86ac92a7c0f2a7734236d0ac3b90d9c790fca4610797be320545b", 2, 61),
    "34233": ("34233_COMP_2022JAN18_A9-14 FRAC UPDATED VERSION.PDF", "49f62dbe227314c624b24a7306331cb8de12b6f1232694ef67e4bc98736c86f3", 4, 71),
    "34234": ("34234_COMP_2022JAN26_09-14 FRAC UPDATED VERSION.PDF", "23da532a39d4447468f542b8ed6aecaba90757df8a3f959950c141f2fbc0c331", 3, 42),
}
NATIVE = "https://iris.bcogc.ca/download/hydraulic_fracture_csv.zip"


def report_family(name):
    return re.sub(r"[^A-Z0-9]", "", re.sub(r"\b(?:UPDATED(?:\s+VERSION)?|REVISED?\s*\d*|REV\s*\d+)\b", "", name.upper().replace("_", " ")))


def main():
    out = Path("well-sources")
    out.mkdir(exist_ok=True)
    catalog = {}
    client = BCERClient()
    client.login()
    for wa, (expected, digest, stage, summary) in CASES.items():
        folder = f"/WellData/34000-34999/34200-34299/{wa}"
        records = client.list_folder(folder)
        versions = [r for r in records if report_family(r["Name"]) == report_family(expected)]
        stamp = max(client.iso_date(r["LastModified"]) for r in versions)
        current = [r for r in versions if client.iso_date(r["LastModified"]) == stamp]
        if len(current) != 1 or current[0]["Name"].upper() != expected:
            raise ValueError(f"Source revision changed for WA{wa}.")
        with tempfile.TemporaryDirectory() as scratch:
            pdf = Path(scratch) / "report.pdf"
            receipt = client.download(current[0], pdf)
            if receipt["sha256"] != digest:
                raise ValueError(f"New report contents for WA{wa}.")
            pages = subprocess.check_output(["pdftotext", "-enc", "UTF-8", "-layout", str(pdf), "-"]).decode().split("\f")
            if not pages[-1].strip():
                pages.pop()
            (out / f"{wa}-text.json").write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
            for page in range(summary - 4, summary + 3):
                subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1700", "-png", str(pdf), str(out / f"{wa}-{page}")], check=True)
        catalog[wa] = dict(file=expected, path=current[0]["full_path"], modified=stamp, sha256=digest, pages=len(pages), selected_stage=stage, summary_page=summary,
                           versions=[[r["Name"], client.iso_date(r["LastModified"])] for r in versions])
    with urllib.request.urlopen(NATIVE, timeout=90) as response:
        data = response.read()
        last = response.headers["Last-Modified"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        with archive.open("hydraulic_fracture.csv") as part:
            items = [[line, row] for line,row in enumerate(csv.DictReader(io.TextIOWrapper(part, encoding="cp1252")),2) if row["WA NUM"] in CASES]
    (out / "native.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    meta = dict(reports=catalog, native_url=NATIVE, native_modified=last, native_sha256=hashlib.sha256(data).hexdigest(), native_rows=len(items))
    (out / "source-index.json").write_text(json.dumps(meta, ensure_ascii=False,indent=2), encoding="utf-8")
    print("Verified current report hashes for", ", ".join(catalog), "and", len(items), "regulator records.")


if __name__ == "__main__":
    main()
