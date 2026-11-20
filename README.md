# Settlement reconciliation

FastAPI + a single-page UI. No database, no build step.

    pip install -r requirements.txt
    uvicorn app.main:app --reload      # http://localhost:8000
    pytest -q                          # tests

Deploy: `docker build -t recon . && docker run -p 8000:8000 recon` (or any host that runs `uvicorn app.main:app`).

API: `POST /api/generate?seed=42` · `GET /api/reconcile?gap_type=ORPHAN_REFUND` · `GET /api/assumptions` · `GET /api/csv/{platform|bank}`

Money is integer minor units (USD cents) end to end; the UI formats with integer maths only.
