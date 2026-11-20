import copy
from fastapi.testclient import TestClient
from app.generator import generate
from app.engine import reconcile
from app.main import app

D = generate(42)
R = reconcile(D["platform"], D["bank"], D["month"])
F = R["findings"]
of = lambda g: [f for f in F if f["gap_type"] == g]


def labelled(g):
    return {(l["source"], l["ref"]): l["impact_minor"] for l in D["labels"] if l["gap_type"] == g}


def test_normal_items_match_with_zero_variance():
    m = of("MATCHED")
    assert len(m) >= 60 + 3 + 4  # normals, valid partial refunds, rounding-batch items
    assert all(f["variance_minor"] == 0 and f["impact_minor"] == 0 for f in m)
    assert {"TXN-R00", "TXN-R01", "TXN-R02"} <= {f["ref"] for f in m}


def test_cross_month_settlement():
    got = {("platform", f["ref"]): f["impact_minor"] for f in of("SETTLED_NEXT_MONTH")}
    assert got == labelled("SETTLED_NEXT_MONTH") and len(got) == 3
    assert not of("UNSETTLED")


def test_rounding_variance_only_at_batch_level():
    f = of("ROUNDING_VARIANCE")
    assert [(x["ref"], x["impact_minor"]) for x in f] == [("B-20261015-R", 1)]
    assert not of("FEE_BREAK") and not of("AMOUNT_MISMATCH")


def test_duplicates_flagged_and_not_double_counted():
    got = {(f["source"], f["ref"]): f["impact_minor"] for f in of("DUPLICATE_ENTRY")}
    assert got == labelled("DUPLICATE_ENTRY") and len(got) == 3
    s = R["summary"]
    assert s["platform_gross_raw"] - s["platform_gross_clean"] == sum(
        f["impact_minor"] for f in of("DUPLICATE_ENTRY") if f["source"] == "platform")


def test_orphan_refunds():
    got = {("platform", f["ref"]): f["impact_minor"] for f in of("ORPHAN_REFUND")}
    assert got == labelled("ORPHAN_REFUND") == {("platform", "TXN-ORPH-1"): -4500, ("platform", "TXN-ORPH-2"): -2250}


def test_tie_out_is_zero_and_no_unexpected_gaps():
    assert R["tie_out"]["difference"] == 0
    assert {f["gap_type"] for f in F} == {"MATCHED", "SETTLED_NEXT_MONTH", "ROUNDING_VARIANCE", "DUPLICATE_ENTRY", "ORPHAN_REFUND"}


def test_exact_integers_and_determinism():
    assert all(isinstance(r["gross_minor"], int) for r in D["platform"] + D["bank"])
    assert generate(7) == generate(7) and generate(7) != generate(8)


def test_missing_settlement_and_large_fee_error_not_hidden_as_rounding():
    bank = [b for b in D["bank"] if b["transaction_id"] != "TXN-0001"]
    assert [f["ref"] for f in reconcile(D["platform"], bank)["findings"] if f["gap_type"] == "UNSETTLED"] == ["TXN-0001"]
    bank = copy.deepcopy(D["bank"])
    next(b for b in bank if b["settlement_ref"] == "FEE-B-20261015-R")["gross_minor"] -= 50
    assert any(f["gap_type"] == "FEE_BREAK" for f in reconcile(D["platform"], bank)["findings"])


def test_api():
    c = TestClient(app)
    assert c.post("/api/generate?seed=5").status_code == 200
    j = c.get("/api/reconcile?gap_type=ORPHAN_REFUND").json()
    assert len(j["findings"]) == 2 and j["tie_out"]["difference"] == 0
