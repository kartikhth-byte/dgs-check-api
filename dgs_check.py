"""Capture-first, fail-closed DG Shipping checker client and HTTP API.

Uses only the Python standard library. Run as one process to preserve the
system-wide request spacing guarantee.
"""

from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Full, Queue
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

BASE = "http://220.156.189.33"
PAGE = "/esamudraUI/jsp/examination/checker/"
BLOCKED = re.compile(r"captcha|verify you are human|access denied|sign\s*in|log\s*in|login page|<form[^>]+name=[\"']loginForm", re.I)
NOT_FOUND = re.compile(r"no\s+(?:records?|results?|data|details?)\s+(?:found|available)|(?:records?|details?)\s+not\s+found|not\s+found|no\s+matching\s+records?|database\s+could\s+not\s+find\s+(?:the\s+match|.{0,80}?\s+details)", re.I)
PROOF_FIELD = re.compile(r"(?:^|\b)(?:indos|seafarer\s*name|certificate\s*(?:no|number)|coc\s*(?:no|number)|coe\s*(?:no|number))", re.I)

CHECKS = (
    ("identity", None, None),
    ("passport", None, None),
    ("cdc", None, None),
    ("coc", "COCSearchDetails.jsp", None),
    ("stcw", "StcwCourse.jsp", "CRSE"),
    ("gmdss", "COPDetails.jsp", "GMDSS"),
    ("dc", "COPDetails.jsp", "DC"),
    ("cop", "COPDetails.jsp", "COP"),
    ("igf", "COPDetails.jsp", "IGF"),
    ("wk", "COPDetails.jsp", "WK"),
    ("dc_polar", "COPDetails.jsp", "DCPOLAR"),
    ("smy", "SmyCourse.jsp", "SMY"),
)


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def normalized(value):
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def same_claim(field, expected, actual):
    if field == "date_of_birth":
        parsed = []
        for value in (expected, actual):
            for fmt in ("%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
                try:
                    parsed.append(datetime.strptime(clean(value), fmt).date())
                    break
                except ValueError:
                    continue
        if len(parsed) == 2:
            return parsed[0] == parsed[1]
    return normalized(expected) == normalized(actual)


def extract_resume(resume: dict):
    """Map qwen_output sections to the small set of DGS-comparable claims."""
    if not isinstance(resume, dict) or not isinstance(resume.get("sections"), list):
        raise ValueError("resume must contain a sections array")
    out = {"name": None, "date_of_birth": None, "indos_number": None,
           "passport": None, "cdc": None, "certificate_numbers": {}, "foreign_coc": False}
    names = {}
    coc_authorities = []
    for section in resume["sections"]:
        if not isinstance(section, dict):
            continue
        category = section.get("category")
        for field in section.get("fields", []):
            if not isinstance(field, list) or len(field) < 3:
                continue
            key, _, value = field[:3]
            if not isinstance(value, str) or not clean(value) or normalized(value) in ("NA", "NONE"):
                continue
            if category == "personal" and key in ("first_name", "middle_name", "sur_name"):
                names[key] = value
            elif category == "personal" and key == "date_of_birth":
                out["date_of_birth"] = value
            elif category == "documents" and key == "indos_number":
                out["indos_number"] = value
            elif category == "passport" and key == "passport_no":
                out["passport"] = value
        for table in section.get("tables", []):
            if not isinstance(table, dict):
                continue
            keys = [col[0] for col in table.get("columns", []) if isinstance(col, list) and col]
            for row in table.get("rows", []):
                if not isinstance(row, list):
                    continue
                values = dict(zip(keys, row))
                if category == "seaman_book" and "indian" in clean(values.get("issuing_authority", "")).lower():
                    out["cdc"] = values.get("number") or out["cdc"]
                if category == "coc":
                    label = clean(values.get("certificate_as_printed", ""))
                    authority = re.search(r"Auth:\s*([^,;]+)", label, re.I)
                    if authority:
                        coc_authorities.append(authority.group(1).strip())
                    if values.get("certificate_number"):
                        out["certificate_numbers"].setdefault("coc", []).append(values["certificate_number"])
                if values.get("certificate_number") and category in ("courses", "cop", "stcw"):
                    check = "stcw" if category == "courses" else category
                    out["certificate_numbers"].setdefault(check, []).append(values["certificate_number"])
                    if category == "courses" and "GMDSS" in clean(values.get("course", "")).upper():
                        out["certificate_numbers"].setdefault("gmdss", []).append(values["certificate_number"])
    out["name"] = " ".join(names[k].strip() for k in ("first_name", "middle_name", "sur_name") if k in names) or None
    out["foreign_coc"] = bool(coc_authorities) and all(normalized(a) not in ("INDIA", "INDIAN", "DGS") for a in coc_authorities)
    return out


class ResultTable(HTMLParser):
    """Extract text pairs only from actual result rows, never form controls."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
            self.depth = 1
        elif self.cell is not None:
            self.depth += 1

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(clean("".join(self.cell)))
            self.cell = None
            self.depth = 0
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


class LayoutChanged(Exception):
    pass


class UpstreamBlocked(Exception):
    pass


def parse_response(raw: bytes, content_type: str, kind: str):
    text = raw.decode("utf-8", "replace")
    if BLOCKED.search(text):
        raise UpstreamBlocked("DGS returned a login, CAPTCHA, or access-denied page")
    if NOT_FOUND.search(re.sub(r"<[^>]+>", " ", text)):
        return "not_found", {}
    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LayoutChanged("Invalid upstream JSON") from exc
        if isinstance(data, dict):
            if data.get("found") is False or data.get("success") is False:
                return "not_found", {}
            record = data.get("data", data.get("record", data))
            if isinstance(record, dict) and any(PROOF_FIELD.search(str(k)) and clean(v) for k, v in record.items()):
                return "confirmed", {str(k): v for k, v in record.items() if v is not None}
        raise LayoutChanged("Unrecognized upstream JSON schema")
    table = ResultTable()
    table.feed(text)
    fields = {}
    for row in table.rows:
        if len(row) == 2 and row[0] and row[1]:
            fields[row[0].rstrip(":")] = row[1]
    records = []
    for index, header in enumerate(table.rows[:-1]):
        if len(header) < 3 or not any(re.search(r"grade|certificate|course", h, re.I) for h in header):
            continue
        for row in table.rows[index + 1:]:
            if len(row) < len(header) - 1 or not row or not re.fullmatch(r"\d+[.]?", row[0]):
                break
            if len(row) == len(header) - 1 and header[-2:] == ["Verified ?", "Status"]:
                # DGS sometimes omits the Verified cell while retaining the
                # Status cell. Preserve that missing value instead of shifting
                # "Valid" into the wrong column.
                record = dict(zip(header[:-2], row[:-1]))
                record["Status"] = row[-1]
            else:
                record = dict(zip(header, row))
            distinctive = next((v for k, v in record.items() if "distinctive characters" in k.lower()), None)
            if distinctive:
                record["certificate_number"] = distinctive
            elif "Certificate No." in record:
                record["certificate_number"] = record["Certificate No."]
            records.append(record)
        if records:
            break
    if records:
        fields["records"] = records
    has_name = any(normalized(k) in ("NAME", "SEAFARERNAME", "NAMEOFSEAFARER") for k in fields)
    if kind == "identity" and any("INDOS" in normalized(k) for k in fields) and has_name:
        return "confirmed", fields
    if kind == "passport" and any("PASSPORT" in normalized(k) for k in fields) and has_name:
        return "confirmed", fields
    if kind == "cdc" and any("CDC" in normalized(k) for k in fields) and has_name:
        return "confirmed", fields
    if kind not in ("identity", "passport", "cdc") and records:
        return "confirmed", fields
    raise LayoutChanged(f"Unrecognized {kind} response layout")


def compare(cv: dict, fields: dict, kind: str):
    aliases = {
        "name": ("name", "seafarer name", "name of seafarer", "candidate name"),
        "indos_number": ("indos no", "indos number"),
        "date_of_birth": ("date of birth", "dob"),
        "cdc": ("cdc", "cdc no", "cdc number"),
        "passport": ("passport", "passport no", "passport number"),
    }
    normalized_fields = {normalized(k): v for k, v in fields.items()}
    differences = []
    for key, labels in aliases.items():
        expected = cv.get(key)
        if expected is None:
            continue
        actual = next((normalized_fields[normalized(label)] for label in labels if normalized(label) in normalized_fields), None)
        if actual is not None and not same_claim(key, expected, actual):
            differences.append({"field": key, "cv": expected, "dgs": actual})
    direct_cert = next((normalized_fields[normalized(label)] for label in ("certificate no", "certificate number", "cert no", "cert number") if normalized(label) in normalized_fields), None)
    actual_certs = [str(value) for value in ([direct_cert] if direct_cert else []) + [record.get("certificate_number") for record in fields.get("records", [])] if value]
    expected_cert = cv.get("certificate_numbers", {}).get(kind)
    if not expected_cert and kind == "coc" and cv.get("certificate_number"):
        expected_cert = [cv["certificate_number"]]
    if actual_certs and expected_cert:
        missing = [value for value in expected_cert if normalized(value) not in {normalized(actual) for actual in actual_certs}]
        if missing:
            differences.append({"field": "certificate_number", "cv": missing, "dgs": actual_certs})
    return differences


class DGSClient:
    def __init__(self, capture_dir: Path, base=BASE, opener=None, sleep=time.sleep, monotonic=time.monotonic, interval=5):
        self.capture_dir = capture_dir
        self.base = base.rstrip("/")
        self.opener = opener or build_opener(HTTPCookieProcessor(CookieJar())).open
        self.sleep = sleep
        self.monotonic = monotonic
        self.interval = interval
        self.last_request = None
        self.capture_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.capture_dir, 0o700)

    def request(self, job_id, kind, indos, dob, cv=None):
        cv = cv or {}
        if kind in ("identity", "passport"):
            lookup = indos if kind == "identity" else cv.get("passport")
            if not lookup:
                raise ValueError(f"{kind} lookup number missing from resume")
            params = {"txtNo": lookup, "dob": dob, "processId": "PPIndosCheck", "searchType": "Indos" if kind == "identity" else "PP"}
            url = self.base + "/esamudraUI/checkerajaxservlet?" + urlencode(params)
            req = Request(url, data=b"", method="POST")
        elif kind == "cdc":
            if not cv.get("cdc"):
                raise ValueError("CDC number missing from resume")
            params = {"CDCno": cv["cdc"].upper(), "dob": dob}
            url = self.base + "/IndosApplication/ajaxservlet?" + urlencode(params)
            req = Request(url, data=b"", method="POST")
        else:
            page, category = next((p, c) for k, p, c in CHECKS if k == kind)
            # The source spec does not enumerate course form fields. These are
            # defaults; override them only after inspecting captured forms.
            params = {"hidSystemDate": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d/%m/%Y"), "txtNo": indos, "txtDob": dob}
            if category:
                params["cmbSearch_by"] = category
                params.update({"hidSystemDate1": "", "hidProcessId": "", "certNo": ""})
                url = self.base + PAGE + page + "?hidProcessMode=beforeAdd&" + urlencode(params)
            else:
                url = self.base + PAGE + page + "?" + urlencode(params)
            req = Request(url, method="GET")
        req.add_header("User-Agent", "DGS-Check-API/0.1 (internal verification)")
        req.add_header("Accept", "text/html,application/json")
        if self.last_request is not None:
            self.sleep(max(0, self.interval - (self.monotonic() - self.last_request)))
        self.last_request = self.monotonic()
        try:
            response = self.opener(req, timeout=20)
            try:
                status = response.status
                headers = response.headers
                body = response.read(2_000_001)
            finally:
                response.close()
        except HTTPError as exc:
            status, headers, body = exc.code, exc.headers, exc.read(2_000_001)
        except URLError as exc:
            raise RuntimeError(f"DGS connection failed: {exc.reason}") from exc
        if len(body) > 2_000_000:
            raise LayoutChanged("Upstream response exceeded 2 MB")
        # Persist the response before any classification or parser runs.
        folder = self.capture_dir / job_id
        folder.mkdir(mode=0o700, exist_ok=True)
        capture = folder / f"{kind}.html"
        if capture.exists():
            capture = folder / f"{kind}-{secrets.token_hex(4)}.html"
        with capture.open("wb") as out:
            out.write(body)
        os.chmod(capture, 0o600)
        if status in (403, 429):
            raise UpstreamBlocked(f"DGS returned HTTP {status}")
        if "mainPage.jsp" in headers.get("Content-Location", ""):
            raise UpstreamBlocked("DGS redirected the checker to its login homepage")
        if status >= 400:
            raise RuntimeError(f"DGS returned HTTP {status}")
        content_type = headers.get("Content-Type", "")
        return parse_response(body, content_type, kind), str(capture)


class Service:
    def __init__(self, client: DGSClient):
        self.client = client
        self.jobs = {}
        self.queue = Queue(maxsize=100)
        self.lock = threading.Lock()
        self.halted = None
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def submit(self, payload):
        resume = extract_resume(payload["resume"]) if "resume" in payload else None
        indos = payload.get("indos_number") or (resume or {}).get("indos_number") or ""
        dob = payload.get("date_of_birth") or (resume or {}).get("date_of_birth") or ""
        consent = payload.get("consent_id", "")
        if resume and payload.get("indos_number") and resume["indos_number"] and normalized(payload["indos_number"]) != normalized(resume["indos_number"]):
            raise ValueError("indos_number conflicts with resume")
        if resume and payload.get("date_of_birth") and resume["date_of_birth"] and payload["date_of_birth"] != resume["date_of_birth"]:
            raise ValueError("date_of_birth conflicts with resume")
        if not isinstance(indos, str) or not re.fullmatch(r"[A-Za-z0-9/-]{4,30}", indos):
            raise ValueError("indos_number must be 4-30 letters, digits, / or -")
        if not isinstance(dob, str) or not re.fullmatch(r"\d{2}/\d{2}/\d{4}", dob):
            raise ValueError("date_of_birth must be DD/MM/YYYY")
        try:
            datetime.strptime(dob, "%d/%m/%Y")
        except (TypeError, ValueError) as exc:
            raise ValueError("date_of_birth must be DD/MM/YYYY") from exc
        if not isinstance(consent, str) or not (1 <= len(consent) <= 128):
            raise ValueError("consent_id is required")
        cv = resume or payload.get("cv", {})
        if not isinstance(cv, dict):
            raise ValueError("cv must be an object")
        if not resume and any(not isinstance(v, (str, int)) for v in cv.values()):
            raise ValueError("cv must be a flat object of strings or numbers")
        job_id = secrets.token_urlsafe(18)
        job = {"id": job_id, "state": "queued", "submitted_at": now(), "consent_id": consent,
               "results": {k: {"status": "skipped", "fields": {}, "checked_at": None} for k, _, _ in CHECKS},
               "cannot_be_checked": (["Foreign CoC"] if resume and resume["foreign_coc"] else []) + ["Sea service"]}
        with self.lock:
            if self.halted:
                raise RuntimeError(f"DGS checks are halted: {self.halted}")
            try:
                self.queue.put_nowait((job_id, indos.upper(), dob, cv))
            except Full as exc:
                raise RuntimeError("Check queue is full") from exc
            self.jobs[job_id] = job
        return job

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def _work(self):
        while True:
            job_id, indos, dob, cv = self.queue.get()
            with self.lock:
                if self.halted:
                    self.jobs[job_id]["state"] = "halted"
                    self.jobs[job_id]["error"] = self.halted
                    self.queue.task_done()
                    continue
                self.jobs[job_id]["state"] = "running"
            for kind, _, _ in CHECKS:
                if kind == "coc" and cv.get("foreign_coc"):
                    continue
                if kind in ("passport", "cdc") and not cv.get(kind):
                    continue
                try:
                    (status, fields), capture = self.client.request(job_id, kind, indos, dob, cv)
                    item = {"status": status, "fields": fields, "checked_at": now(), "capture": capture}
                    if status == "confirmed":
                        item["badge"] = "Issued per DG Shipping record"
                        item["differences"] = compare(cv, fields, kind)
                    with self.lock:
                        self.jobs[job_id]["results"][kind] = item
                    if kind == "identity" and status == "not_found":
                        break
                except (UpstreamBlocked, LayoutChanged) as exc:
                    with self.lock:
                        self.halted = str(exc)
                        self.jobs[job_id]["results"][kind] = {"status": "error", "fields": {}, "checked_at": now(), "reason": str(exc)}
                        self.jobs[job_id]["state"] = "halted"
                        self.jobs[job_id]["error"] = str(exc)
                    alert = self.client.capture_dir / "HALTED.txt"
                    alert.write_text(f"{now()}\nJob: {job_id}\nCheck: {kind}\nReason: {exc}\nReview the saved response before restarting.\n")
                    os.chmod(alert, 0o600)
                    print(f"DGS checks halted: {exc}; review {alert}", file=sys.stderr)
                    break
                except Exception as exc:
                    with self.lock:
                        self.jobs[job_id]["results"][kind] = {"status": "error", "fields": {}, "checked_at": now(), "reason": str(exc)}
                    break
            with self.lock:
                if self.jobs[job_id]["state"] == "running":
                    self.jobs[job_id]["state"] = "completed"
                    self.jobs[job_id]["completed_at"] = now()
            self.queue.task_done()


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Avoid logging INDoS and DOB from requests.
            pass

        def send_json(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/checks":
                return self.send_json(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 64_000:
                    raise ValueError("JSON body must be 1-64000 bytes")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON object required")
                job = service.submit(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                return self.send_json(503, {"error": str(exc)})
            self.send_json(202, {"id": job["id"], "state": job["state"], "poll_url": "/checks/" + job["id"]})

        def do_GET(self):
            if self.path == "/health":
                return self.send_json(200, {"status": "halted" if service.halted else "ok", "reason": service.halted})
            if self.path.startswith("/checks/"):
                job = service.get(self.path.removeprefix("/checks/"))
                return self.send_json(200, job) if job else self.send_json(404, {"error": "not_found"})
            self.send_json(404, {"error": "not_found"})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--capture-dir", type=Path, default=Path("captures"))
    args = parser.parse_args()
    service = Service(DGSClient(args.capture_dir))
    ThreadingHTTPServer((args.host, args.port), handler_for(service)).serve_forever()


if __name__ == "__main__":
    main()
