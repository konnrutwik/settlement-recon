import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from .generator import generate, write_csv, MONTH
from .engine import reconcile

ROOT = os.path.dirname(__file__)
DATA = os.path.join(os.path.dirname(ROOT), "data")
app = FastAPI(title="Settlement reconciliation")
STATE = {}

ASSUMPTIONS = [
    "Period: one calendar month (%s). Platform transactions are in scope when created_at (UTC) falls in that month." % MONTH,
    "Matching key: platform txn_id = bank transaction_id, one to one. Refunds match their own bank refund line, not via the original.",
    "Money is stored as integer minor units (USD cents). No floats; the UI formats with integer maths.",
    "Sign convention: payments positive, refunds and bank fees negative on both files.",
    "SETTLED_NEXT_MONTH: charge in the month, bank value_date in the next month, amounts tie. A timing item that clears next month, not a missing settlement.",
    "UNSETTLED: no bank line even though next-month bank data is loaded (look-ahead). Kept separate from lagged settlement.",
    "ROUNDING_VARIANCE: items tie one by one, but the bank posts one fee per batch on the batch gross, while the platform rounds half-up per row. Allowed residual is ceil(items / 2) minor units; larger is FEE_BREAK.",
    "DUPLICATE_ENTRY: repeated txn_id (platform) or repeated settlement_ref / transaction_id (bank). Removed before matching so duplicates never double-count totals.",
    "ORPHAN_REFUND: refund whose parent_txn_id is blank or not a PAYMENT in the ledger. Reported at full refund amount.",
    "Reconciliation is item level first; batch aggregates are checked afterwards so offsetting errors cannot hide.",
    "Tie-out: clean platform total - next-month - unsettled + bank-only + mismatches must equal bank item gross settled in the month.",
]


def _run(seed):
    d = generate(seed)
    write_csv(d, DATA)
    STATE.update(data=d, result=reconcile(d["platform"], d["bank"], d["month"]))


_run(42)


@app.post("/api/generate")
def gen(seed: int = 42):
    _run(seed)
    return dict(seed=seed, platform_rows=len(STATE["data"]["platform"]), bank_rows=len(STATE["data"]["bank"]))


@app.get("/api/reconcile")
def rec(gap_type: str | None = None):
    res = dict(STATE["result"], seed=STATE["data"]["seed"])
    if gap_type:
        res["findings"] = [f for f in res["findings"] if f["gap_type"] == gap_type]
    return res


@app.get("/api/assumptions")
def assumptions():
    return ASSUMPTIONS


@app.get("/api/csv/{name}")
def csv_file(name: str):
    files = dict(platform="platform_transactions.csv", bank="bank_settlements.csv")
    if name not in files:
        raise HTTPException(404)
    return FileResponse(os.path.join(DATA, files[name]), filename=files[name], media_type="text/csv")


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "static", "index.html"))
