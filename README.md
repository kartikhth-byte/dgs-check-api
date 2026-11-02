# DGS Check API

Internal, single-process API for checking Indian seafarer CV claims against the public DG Shipping checkers. The server binds to `127.0.0.1` by default and uses only the Python standard library.

## Run

```bash
python3 dgs_check.py --capture-dir ./captures
```

Submit a check:

```bash
curl -sS http://127.0.0.1:8080/checks \
  -H 'Content-Type: application/json' \
  -d '{"indos_number":"AB1234","date_of_birth":"01/02/1990","consent_id":"consent-123","cv":{"name":"Asha Rao","cdc":"CDC123","passport":"P123","certificate_number":"CERT123"}}'
```

For the `qwen_output` resume JSON structure, send `{"resume": <the JSON object>, "consent_id": "..."}` instead of the flat `cv` object. The API reads `sections[].fields` and `sections[].tables` for name, date of birth, INDoS, passport, Indian seaman book number, and certificate numbers. If the resume omits INDoS or DOB, supply `indos_number` or `date_of_birth` at the top level. If a CoC is explicitly marked with a foreign issuing authority, its CoC check is skipped.

The response is `202` with a `poll_url`. `GET /checks/{id}` returns the job and each check's `confirmed`, `not_found`, `skipped`, or `error` status, DGS fields, check time, and any CV differences. It checks identity, passport, CDC, CoC, STCW courses, GMDSS, DC, CoP Basic, IGF, watchkeeping, polar water, and SMY. Certificate of Endorsement is excluded. `GET /health` reports whether requests are halted. A confirmed item carries the badge `Issued per DG Shipping record`. Foreign CoC and sea service are listed as unable to be checked with DG Shipping.

## Important setup before live use

The supplied spec did not include successful response HTML or a runnable `dgs_capture.py`. On 2 November 2026, the public checker forms exposed the request fields used here. An end-to-end API job against a user-authorized resume completed: identity, passport, CDC, CoC, STCW (9 rows), GMDSS (4 rows), and DC (8 rows) were confirmed. CoP Basic, IGF, watchkeeping, polar water, and SMY returned `not_found`. All returned result rows were checked against private raw captures. The private validation report and captures are excluded from Git. The endpoint may change, so an unfamiliar response halts all future requests instead of claiming a match.

The API persists each raw response in `captures/{job_id}/{check}.html` before parsing it, including `not_found` responses. Those files can contain personal data and are owner-only (`0700` directory, `0600` files). Poll results expose capture paths, so keep this service on a trusted internal network. Jobs are held in memory and are lost on restart; captures remain on disk. Run **one server process** to preserve the 5-second spacing across all upstream requests. The code never calls `/IndosApplication/Indos/CDCMissing.jsp`.

Collect consent using language that states: **INDoS, date of birth, passport number, and CDC number may be transmitted over plain HTTP to a bare IP address for DG Shipping verification.** `consent_id` is an audit reference supplied by your system; this API cannot itself verify that consent was obtained. Review automated access and data handling with counsel before production use. DGS may change its site, and `403`, `429`, CAPTCHA, login, or unrecognized layouts halt checks until a person reviews the response and restarts the process.

Run tests with `python3 -m unittest -v`.
