import io
import tempfile
import time
import unittest
from pathlib import Path

from dgs_check import DGSClient, LayoutChanged, Service, UpstreamBlocked, compare, extract_resume, parse_response, same_claim


class FakeResponse:
    status = 200
    headers = {"Content-Type": "text/html"}

    def __init__(self, body):
        self.body = body

    def read(self, limit):
        return self.body

    def close(self):
        pass


class APITests(unittest.TestCase):
    def test_parser_only_confirms_result_table(self):
        html = b"<table><tr><td>INDoS Number</td><td>AB1234</td></tr><tr><td>Seafarer Name</td><td>Asha Rao</td></tr></table>"
        self.assertEqual(parse_response(html, "text/html", "identity")[0], "confirmed")
        self.assertEqual(parse_response(b"<p>No records found</p>", "text/html", "identity")[0], "not_found")
        self.assertEqual(parse_response(b"<table><tr><td>Database could not find the match of INDoS No. you are looking for.</td></tr></table>", "text/xml", "identity")[0], "not_found")
        with self.assertRaises(LayoutChanged):
            parse_response(b"<form><input name='txtNo'></form>", "text/html", "identity")

    def test_difference_reports_both_values(self):
        result = compare({"name": "Asha Rao", "passport": "P123"}, {"Seafarer Name": "Asha Rao", "Passport No": "P124"}, "identity")
        self.assertEqual(result, [{"field": "passport", "cv": "P123", "dgs": "P124"}])
        self.assertTrue(same_claim("date_of_birth", "01/07/1980", "01-JUL-1980"))
        certs = compare({"certificate_numbers": {"stcw": ["C2", "C3"]}}, {"records": [{"certificate_number": "C1"}, {"certificate_number": "C2"}]}, "stcw")
        self.assertEqual(certs, [{"field": "certificate_number", "cv": ["C3"], "dgs": ["C1", "C2"]}])

    def test_qwen_resume_schema_and_foreign_coc(self):
        resume = {"sections": [
            {"category": "personal", "fields": [["first_name", "First Name", "Asha"], ["sur_name", "Sur Name", "Rao"], ["date_of_birth", "DOB", "01/02/1990"]]},
            {"category": "documents", "fields": [["indos_number", "INDoS", "AB1234"]]},
            {"category": "passport", "fields": [["passport_no", "Passport", "P123"]]},
            {"category": "seaman_book", "tables": [{"columns": [["issuing_authority", "Authority"], ["number", "Number"]], "rows": [["Indian", "KOL123"]]}]},
            {"category": "coc", "tables": [{"columns": [["certificate_number", "Certificate"], ["certificate_as_printed", "Type"]], "rows": [["C123", "Master Auth: UK"]]}]},
        ]}
        extracted = extract_resume(resume)
        self.assertEqual(extracted["name"], "Asha Rao")
        self.assertEqual(extracted["cdc"], "KOL123")
        self.assertEqual(extracted["certificate_numbers"]["coc"], ["C123"])
        self.assertTrue(extracted["foreign_coc"])
        calls = []

        def opener(req, timeout):
            calls.append(req.full_url)
            if len(calls) == 1:
                return FakeResponse(b"<table><tr><td>Name</td><td>Asha Rao</td></tr><tr><td>INDoS Number</td><td>AB1234</td></tr></table>")
            return FakeResponse(b"No records found")

        with tempfile.TemporaryDirectory() as directory:
            service = Service(DGSClient(Path(directory), opener=opener, interval=0))
            job = service.submit({"resume": resume, "consent_id": "consent-1"})
            service.queue.join()
            self.assertEqual(service.get(job["id"])["state"], "completed")
            self.assertIn("Foreign CoC", service.get(job["id"])["cannot_be_checked"])
            self.assertFalse(any("COCSearchDetails.jsp" in url for url in calls))

    def test_login_form_is_blocked(self):
        with self.assertRaises(UpstreamBlocked):
            parse_response(b'<form name="loginForm"></form>', "text/html", "identity")

    def test_live_coc_shape_and_coe_missing_phrase(self):
        html = b"<table><tr><td>Name of Seafarer</td><td>Asha Rao</td></tr><tr><td>INDoS No.</td><td>AB1234</td></tr><tr><th>Sr. No.</th><th>Grade</th><th>Distinctive Characters in the Certificate**</th><th>Verified ?</th></tr><tr><td>1.</td><td>Master</td><td>C123</td><td>Yes</td></tr></table>"
        status, fields = parse_response(html, "text/html", "coc")
        self.assertEqual(status, "confirmed")
        self.assertEqual(fields["records"][0]["certificate_number"], "C123")
        missing_verified = b"<table><tr><th>Sr. No.</th><th>Grade</th><th>Certificate No.</th><th>Verified ?</th><th>Status</th></tr><tr><td>1.</td><td>Master</td><td>C123</td><td>Valid</td></tr></table>"
        _, fields = parse_response(missing_verified, "text/html", "coc")
        self.assertEqual(fields["records"][0]["Status"], "Valid")
        self.assertNotIn("Verified ?", fields["records"][0])
        self.assertEqual(parse_response(b"Database could not find CoE details", "text/html", "coe")[0], "not_found")
        self.assertEqual(parse_response(b"Database could not find COP Basic details of INDoS No. you are looking for.", "text/html", "cop")[0], "not_found")

    def test_capture_precedes_parser_and_identity_gates_other_checks(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse(b"<p>No records found</p>")

        with tempfile.TemporaryDirectory() as directory:
            client = DGSClient(Path(directory), opener=opener, interval=0)
            service = Service(client)
            job = service.submit({"indos_number": "ab1234", "date_of_birth": "01/02/1990", "consent_id": "consent-1"})
            service.queue.join()
            result = service.get(job["id"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].get_method(), "POST")
            self.assertIn("txtNo=AB1234", calls[0].full_url)
            self.assertEqual(result["results"]["identity"]["status"], "not_found")
            self.assertEqual(result["results"]["coc"]["status"], "skipped")
            self.assertTrue((Path(directory) / job["id"] / "identity.html").exists())

    def test_layout_change_halts_future_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            client = DGSClient(Path(directory), opener=lambda req, timeout: FakeResponse(b"<html>unexpected</html>"), interval=0)
            service = Service(client)
            job = service.submit({"indos_number": "ab1234", "date_of_birth": "01/02/1990", "consent_id": "consent-1"})
            service.queue.join()
            self.assertEqual(service.get(job["id"])["state"], "halted")
            with self.assertRaises(RuntimeError):
                service.submit({"indos_number": "ab1234", "date_of_birth": "01/02/1990", "consent_id": "consent-1"})


if __name__ == "__main__":
    unittest.main()
