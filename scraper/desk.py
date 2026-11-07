"""Local page for reading a URL or a spreadsheet into the Dublin Core CSV.

The page stays on this computer. It writes a new file under output/desk and
does not replace output/black_history_metadata.csv.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from scraper.from_csv import load_prefilled
from scraper.import_page import harvest_url, prepare_url
from scraper.schema import COLUMNS

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ICONS = ROOT / "design" / "assets" / "icons"
OUT = ROOT / "output" / "desk"
HOST = "127.0.0.1"
PORT = 8765
PREVIEW = ("dc_title", "dc_creator", "dc_date", "dc_type", "dc_subject", "dcterms_spatial", "dc_identifier")

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), DeskHandler)
    print(f"Metadata desk: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


class DeskHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._file(WEB / "index.html", "text/html; charset=utf-8")
            return
        if path == "/desk.css":
            self._file(WEB / "desk.css", "text/css; charset=utf-8")
            return
        if path == "/desk.js":
            self._file(WEB / "desk.js", "text/javascript; charset=utf-8")
            return
        if path.startswith("/icons/"):
            name = Path(path).name
            if name in {"link.svg", "sheet.svg", "download.svg", "download-on.svg", "alert.svg"}:
                self._file(ICONS / name, "image/svg+xml")
                return
        if path.startswith("/api/jobs/") and path.endswith(".csv"):
            self._download(path.removeprefix("/api/jobs/").removesuffix(".csv"))
            return
        if path.startswith("/api/jobs/"):
            self._status(path.removeprefix("/api/jobs/"))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/jobs":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 8_000_000:
            self._json(400, {"error": "Send a page link or a spreadsheet."})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._json(400, {"error": "The request was not readable."})
            return
        kind = body.get("kind")
        if kind == "url":
            self._start_url(str(body.get("url") or ""))
            return
        if kind == "csv":
            self._start_csv(str(body.get("filename") or "spreadsheet.csv"), str(body.get("csv") or ""))
            return
        self._json(400, {"error": "Send a page link or a spreadsheet."})

    def log_message(self, format: str, *args) -> None:
        return

    def _start_url(self, raw: str) -> None:
        try:
            url = prepare_url(raw)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        job_id = _open_job("Reading the page and the resources it lists.")
        threading.Thread(target=_read_url, args=(job_id, url), daemon=True).start()
        self._json(202, {"id": job_id})

    def _start_csv(self, filename: str, text: str) -> None:
        if not text.strip():
            self._json(400, {"error": "That file is empty."})
            return
        if not filename.lower().endswith(".csv"):
            self._json(400, {"error": "Use a .csv file."})
            return
        job_id = _open_job("Reading the spreadsheet.")
        inbox = OUT / "inbox" / job_id
        inbox.mkdir(parents=True, exist_ok=True)
        source = inbox / f"{_stem(filename)}.csv"
        source.write_text(text, encoding="utf-8")
        threading.Thread(target=_read_csv, args=(job_id, source), daemon=True).start()
        self._json(202, {"id": job_id})

    def _status(self, job_id: str) -> None:
        with _lock:
            job = _jobs.get(job_id)
            if job is None:
                self._json(404, {"error": "That read is no longer on this desk."})
                return
            payload = {
                "id": job_id,
                "status": job["status"],
                "message": job["message"],
                "count": len(job["records"]) if job["records"] else 0,
                "preview": [_preview(row) for row in (job["records"] or [])[:12]],
                "download": f"/api/jobs/{job_id}.csv" if job["status"] == "done" else "",
            }
        self._json(200, payload)

    def _download(self, job_id: str) -> None:
        with _lock:
            job = _jobs.get(job_id)
            path = job["path"] if job else None
            name = job["download_name"] if job else "dublin.csv"
        if path is None or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _open_job(message: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "status": "running",
            "message": message,
            "records": None,
            "path": None,
            "download_name": "dublin.csv",
        }
    return job_id


def _read_url(job_id: str, url: str) -> None:
    try:
        records = asyncio.run(harvest_url(url))
        if not records:
            _fail(job_id, "No records were found on that page.")
            return
        host = (urlsplit(url).hostname or "page").removeprefix("www.")
        name = f"{_stem(host)}-dublin.csv"
        _finish(job_id, records, name, f"{len(records)} records from {url}.")
    except (ValueError, RuntimeError) as exc:
        _fail(job_id, str(exc))
    except Exception:
        traceback.print_exc()
        _fail(job_id, "The page could not be read.")


def _read_csv(job_id: str, source: Path) -> None:
    try:
        def progress(done: int, total: int) -> None:
            _message(job_id, f"Reading batch {done} of {total}.")

        records, report = load_prefilled(source, on_progress=progress)
        if not records:
            _fail(job_id, "That spreadsheet did not produce any records.")
            return
        name = f"{source.stem}-dublin.csv"
        _finish(job_id, records, name, report.text())
    except (OSError, ValueError, csv.Error, RuntimeError) as exc:
        _fail(job_id, str(exc))
    except Exception:
        traceback.print_exc()
        _fail(job_id, "The spreadsheet could not be read.")


def _finish(job_id: str, records: list[dict[str, str]], name: str, message: str) -> None:
    path = OUT / f"{job_id}.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({column: record.get(column, "") for column in COLUMNS})
    with _lock:
        job = _jobs[job_id]
        job["status"] = "done"
        job["message"] = message
        job["records"] = records
        job["path"] = path
        job["download_name"] = name


def _fail(job_id: str, message: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["status"] = "error"
        job["message"] = message


def _message(job_id: str, message: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "running":
            job["message"] = message


def _preview(row: dict[str, str]) -> dict[str, str]:
    return {column: row.get(column, "") for column in PREVIEW}


def _stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(value).stem).strip("-._")
    return (cleaned or "sheet")[:80]


if __name__ == "__main__":
    main()
