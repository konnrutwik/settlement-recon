"""Scenario tests for the reconciliation engine using small hand-built fixtures.

No randomness and no generator: every amount is written out so each expected gap code and
variance can be read straight from the test. Amounts are integer cents.
Conventions asserted here: variance = bank - platform; impact is the money at stake for the gap.
"""
import pytest
from app.engine import reconcile

MONTH = "2026-10"


def P(tid, gross, created="2026-10-10T12:00:00+00:00", fee=0, typ="PAYMENT", parent=""):
    return dict(txn_id=tid, order_id="ORD-" + tid, txn_type=typ, status="CAPTURED", parent_txn_id=parent,
                currency="USD", gross_minor=gross, fee_minor=fee, net_minor=gross - fee,
                created_at=created, source_row_id=0)


def B(tid, gross, value="2026-10-11", batch=None, ref=None, typ="SETTLEMENT", parent=""):
    return dict(settlement_ref=ref or "SR-" + tid, batch_id=batch or "B-" + value, line_type=typ,
                transaction_id=tid, original_ref=parent, currency="USD", gross_minor=gross, fee_minor=0,
                net_minor=gross, value_date=value, source_row_id=0)


def FEE(batch, fee, value):
    return dict(settlement_ref="FEE-" + batch, batch_id=batch, line_type="FEE", transaction_id="",
                original_ref="", currency="USD", gross_minor=-fee, fee_minor=0, net_minor=-fee,
                value_date=value, source_row_id=0)


def run(platform, bank):
    R = reconcile(platform, bank, MONTH)
    assert R["tie_out"]["difference"] == 0, R["tie_out"]  # every scenario must tie out
    return R


def codes(R):
    return sorted(f["gap_type"] for f in R["findings"])


def only(R, code):
    hits = [f for f in R["findings"] if f["gap_type"] == code]
    assert len(hits) == 1, "expected exactly one %s, got %d" % (code, len(hits))
    return hits[0]


def by_ref(R, ref):
    return next(f for f in R["findings"] if f["ref"] == ref and f["source"] != "batch")


# ---------------------------------------------------------------- 1. normal matches
def test_normal_matches_with_one_and_two_day_lag_and_valid_refund():
    plat = [P("A", 5000, "2026-10-10T09:00:00+00:00", fee=145),
            P("B", 2000, "2026-10-20T09:00:00+00:00", fee=58),
            P("R1", -1000, "2026-10-12T09:00:00+00:00", typ="REFUND", parent="A")]
    bank = [B("A", 5000, "2026-10-11"), FEE("B-2026-10-11", 145, "2026-10-11"),   # T+1
            B("B", 2000, "2026-10-22"), FEE("B-2026-10-22", 58, "2026-10-22"),   # T+2
            B("R1", -1000, "2026-10-13", typ="REFUND", parent="A")]
    R = run(plat, bank)
    assert codes(R) == ["MATCHED"] * 3
    assert all(f["variance_minor"] == 0 and f["impact_minor"] == 0 for f in R["findings"])
    assert R["tie_out"]["platform_clean"] == R["tie_out"]["actual_bank"] == 6000
    assert R["by_gap"] == {"MATCHED": {"count": 3, "impact_minor": 0}}


def test_settlement_on_last_day_of_month_is_not_a_lag():
    R = run([P("A", 3000, "2026-10-30T10:00:00+00:00")], [B("A", 3000, "2026-10-31")])
    assert codes(R) == ["MATCHED"]


# ---------------------------------------------------------------- 2. cross-month lag
def test_settles_next_month_is_timing_not_missing():
    plat = [P("LATE", 7500, "2026-10-31T23:30:00+00:00", fee=218)]
    bank = [B("LATE", 7500, "2026-11-02"), FEE("B-2026-11-02", 218, "2026-11-02")]
    R = run(plat, bank)
    f = only(R, "SETTLED_NEXT_MONTH")
    assert (f["ref"], f["platform_amount_minor"], f["bank_amount_minor"]) == ("LATE", 7500, 7500)
    assert f["variance_minor"] == 0 and f["impact_minor"] == 7500
    assert "T+2" in f["detail"]
    assert codes(R) == ["SETTLED_NEXT_MONTH"]  # no UNSETTLED, no fee finding for the November batch
    t = R["tie_out"]
    assert (t["platform_clean"], t["less_settled_next_month"], t["actual_bank"]) == (7500, 7500, 0)


def test_no_bank_line_at_all_is_unsettled_not_lagged():
    f = only(run([P("GONE", 4200)], []), "UNSETTLED")
    assert (f["ref"], f["impact_minor"]) == ("GONE", 4200)


def test_transactions_outside_target_month_are_out_of_scope():
    plat = [P("SEPT", 1000, "2026-09-30T23:59:00+00:00"), P("NOV", 1000, "2026-11-01T00:00:00+00:00")]
    assert run(plat, [])["findings"] == []


# ---------------------------------------------------------------- 3. rounding (batch level)
def rounding_case(n, bank_fee):
    """n payments of 10.10, platform fee 29 each (29.29 rounded per row); bank posts one batch fee."""
    plat = [P("T%d" % i, 1010, "2026-10-14T10:00:00+00:00", fee=29) for i in range(n)]
    bank = [B("T%d" % i, 1010, "2026-10-15", batch="B-RND") for i in range(n)] + [FEE("B-RND", bank_fee, "2026-10-15")]
    return run(plat, bank)


def test_rounding_invisible_per_row_but_visible_in_batch_sum():
    R = rounding_case(4, 117)  # 4 x 29 = 116 per row; bank 4040 * 2.9% = 117.16 -> 117
    assert codes(R) == ["MATCHED"] * 4 + ["ROUNDING_VARIANCE"]
    assert all(f["variance_minor"] == 0 for f in R["findings"] if f["gap_type"] == "MATCHED")
    f = only(R, "ROUNDING_VARIANCE")
    assert (f["ref"], f["source"]) == ("B-RND", "batch")
    assert (f["platform_amount_minor"], f["bank_amount_minor"]) == (116, 117)
    assert f["variance_minor"] == 1 and f["impact_minor"] == 1


@pytest.mark.parametrize("n,bank_fee,expected,variance", [
    (4, 116, None, 0),                    # exact tie: no finding
    (4, 117, "ROUNDING_VARIANCE", 1),
    (4, 115, "ROUNDING_VARIANCE", -1),    # bank lower than platform
    (4, 118, "ROUNDING_VARIANCE", 2),     # exactly ceil(4/2) = tolerance
    (4, 119, "FEE_BREAK", 3),             # beyond tolerance: not rounding
    (4, 113, "FEE_BREAK", -3),
    (1, 30, "ROUNDING_VARIANCE", 1),      # ceil(1/2) = 1
    (1, 31, "FEE_BREAK", 2),
])
def test_rounding_tolerance_boundaries(n, bank_fee, expected, variance):
    R = rounding_case(n, bank_fee)
    batch = [f for f in R["findings"] if f["source"] == "batch"]
    if expected is None:
        assert batch == []
    else:
        assert [(f["gap_type"], f["variance_minor"]) for f in batch] == [(expected, variance)]


def test_item_gross_difference_is_not_rounding():
    R = reconcile([P("X", 1000)], [B("X", 995)], MONTH)
    f = only(R, "AMOUNT_MISMATCH")
    assert f["variance_minor"] == -5 and f["impact_minor"] == -5
    assert R["tie_out"]["difference"] == 0


# ---------------------------------------------------------------- 4. duplicates, one dataset only
def test_duplicate_in_platform_only():
    pay = P("A", 5000, fee=145)
    R = run([pay, dict(pay, source_row_id=1), P("B", 2000, fee=58)],
            [B("A", 5000, batch="B1"), B("B", 2000, batch="B1"), FEE("B1", 203, "2026-10-11")])
    f = only(R, "DUPLICATE_ENTRY")
    assert (f["source"], f["ref"], f["impact_minor"]) == ("platform", "A", 5000)
    assert codes(R) == ["DUPLICATE_ENTRY", "MATCHED", "MATCHED"]  # original still matches; no false UNSETTLED
    s = R["summary"]
    assert s["platform_gross_raw"] - s["platform_gross_clean"] == 5000 and s["platform_gross_clean"] == 7000


def test_duplicate_in_bank_only_redelivered_line():
    line = B("A", 5000, batch="B1")
    R = run([P("A", 5000, fee=145)], [line, dict(line, source_row_id=1), FEE("B1", 145, "2026-10-11")])
    f = only(R, "DUPLICATE_ENTRY")
    assert (f["source"], f["ref"], f["impact_minor"]) == ("bank", "A", 5000)
    assert codes(R) == ["DUPLICATE_ENTRY", "MATCHED"]
    s = R["summary"]
    assert s["bank_item_gross_raw"] - s["bank_item_gross_clean"] == 5000


def test_duplicate_in_bank_only_same_transaction_new_settlement_ref():
    R = run([P("A", 5000, fee=145)],
            [B("A", 5000, batch="B1"), B("A", 5000, batch="B1", ref="SR-OTHER"), FEE("B1", 145, "2026-10-11")])
    f = only(R, "DUPLICATE_ENTRY")
    assert f["source"] == "bank" and f["impact_minor"] == 5000 and "new settlement_ref" in f["detail"]


def test_two_legitimate_payments_with_same_amount_are_not_duplicates():
    R = run([P("A", 5000, fee=145), P("B", 5000, fee=145)],
            [B("A", 5000, batch="B1"), B("B", 5000, batch="B1"), FEE("B1", 290, "2026-10-11")])
    assert codes(R) == ["MATCHED", "MATCHED"]


# ---------------------------------------------------------------- 5. orphan refunds
@pytest.mark.parametrize("parent,why", [("GHOST-1", "GHOST-1"), ("", "no parent_txn_id")])
def test_refund_without_original_is_orphan(parent, why):
    R = run([P("R1", -4500, typ="REFUND", parent=parent)], [B("R1", -4500, typ="REFUND", parent=parent)])
    f = only(R, "ORPHAN_REFUND")
    assert (f["ref"], f["platform_amount_minor"], f["bank_amount_minor"]) == ("R1", -4500, -4500)
    assert f["variance_minor"] == 0 and f["impact_minor"] == -4500  # full refund amount at risk
    assert why in f["detail"]


def test_refund_of_a_refund_is_orphan_but_partial_refunds_of_a_real_charge_match():
    plat = [P("A", 9000, fee=261),
            P("R1", -2000, typ="REFUND", parent="A"), P("R2", -3000, typ="REFUND", parent="A"),
            P("R3", -500, typ="REFUND", parent="R1")]
    bank = [B("A", 9000, batch="B1"), FEE("B1", 261, "2026-10-11"),
            B("R1", -2000, typ="REFUND"), B("R2", -3000, typ="REFUND"), B("R3", -500, typ="REFUND")]
    R = run(plat, bank)
    assert by_ref(R, "R1")["gap_type"] == by_ref(R, "R2")["gap_type"] == "MATCHED"
    assert only(R, "ORPHAN_REFUND")["ref"] == "R3"
