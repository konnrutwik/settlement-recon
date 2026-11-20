"""Reconciliation engine. Integer minor units only. Item level first, then batch aggregates."""
import math
from collections import defaultdict
from datetime import date

SEVERITY = {"MATCHED": "none", "SETTLED_NEXT_MONTH": "info", "ROUNDING_VARIANCE": "low",
            "DUPLICATE_ENTRY": "high", "ORPHAN_REFUND": "high", "UNSETTLED": "high",
            "BANK_ONLY": "high", "AMOUNT_MISMATCH": "high", "FEE_BREAK": "high"}


def reconcile(platform, bank, month="2026-10"):
    F = []

    def add(gap, source, ref, batch="", p=None, b=None, variance=0, impact=0, detail="", prow=(), brow=()):
        F.append(dict(id="F-%03d" % (len(F) + 1), gap_type=gap, severity=SEVERITY[gap], source=source,
                      ref=ref, batch_id=batch, platform_amount_minor=p, bank_amount_minor=b,
                      variance_minor=variance, impact_minor=impact, detail=detail,
                      rows=dict(platform=list(prow), bank=list(brow))))

    # 1. Dedupe each dataset first so a duplicate can never steal a match or inflate totals
    first_p, Pu = {}, []
    for p in platform:
        if p["txn_id"] in first_p:
            add("DUPLICATE_ENTRY", "platform", p["txn_id"], p=p["gross_minor"], impact=p["gross_minor"],
                detail="txn_id appears twice in the platform ledger; copy excluded from matching and totals",
                prow=[first_p[p["txn_id"]], p])
        else:
            first_p[p["txn_id"]] = p; Pu.append(p)
    first_ref, first_key, Bu = {}, {}, []
    for b in bank:
        lk = (b["transaction_id"], b["line_type"])
        if b["settlement_ref"] in first_ref:
            add("DUPLICATE_ENTRY", "bank", b["transaction_id"] or b["settlement_ref"], b["batch_id"], b=b["gross_minor"],
                impact=b["gross_minor"], detail="settlement_ref delivered twice (file re-delivery); copy excluded",
                brow=[first_ref[b["settlement_ref"]], b])
        elif b["line_type"] != "FEE" and lk in first_key:
            add("DUPLICATE_ENTRY", "bank", b["transaction_id"], b["batch_id"], b=b["gross_minor"],
                impact=b["gross_minor"], detail="same transaction_id settled again under a new settlement_ref; copy excluded",
                brow=[first_key[lk], b])
        else:
            first_ref[b["settlement_ref"]] = b
            if b["line_type"] != "FEE":
                first_key[lk] = b
            Bu.append(b)

    # 2. Item matching on transaction_id <-> bank transaction_id
    pay_ids = {p["txn_id"] for p in Pu if p["txn_type"] == "PAYMENT"}
    all_ids = {p["txn_id"] for p in Pu}
    scope = [p for p in Pu if p["created_at"][:7] == month and p["status"] == "CAPTURED"]
    items = {b["transaction_id"]: b for b in Bu if b["line_type"] != "FEE"}
    pairs = {}
    for p in scope:
        t, g = p["txn_id"], p["gross_minor"]
        b = items.get(t)
        if b is None:
            add("UNSETTLED", "platform", t, p=g, impact=g, detail="No bank settlement line found (look-ahead data loaded)", prow=[p])
            continue
        pairs[t] = (p, b)
        bg, vm = b["gross_minor"], b["value_date"][:7]
        if p["txn_type"] == "REFUND" and (not p["parent_txn_id"] or p["parent_txn_id"] not in pay_ids):
            why = "no parent_txn_id" if not p["parent_txn_id"] else "parent %s not in ledger" % p["parent_txn_id"]
            add("ORPHAN_REFUND", "platform", t, b["batch_id"], g, bg, bg - g, g, "Refund has no original charge (%s)" % why, [p], [b])
        elif bg != g:
            add("AMOUNT_MISMATCH", "platform", t, b["batch_id"], g, bg, bg - g, bg - g, "Platform and bank gross differ", [p], [b])
        elif vm != month:
            lag = (date.fromisoformat(b["value_date"]) - date.fromisoformat(p["created_at"][:10])).days
            add("SETTLED_NEXT_MONTH", "platform", t, b["batch_id"], g, bg, 0, g,
                "Captured %s, settled %s (T+%d). Timing only; clears next month" % (p["created_at"][:10], b["value_date"], lag), [p], [b])
        else:
            add("MATCHED", "platform", t, b["batch_id"], g, bg, 0, 0, "Gross amounts tie; settled in month", [p], [b])
    for t, b in items.items():
        if t not in pairs and t not in all_ids and b["value_date"][:7] == month:
            add("BANK_ONLY", "bank", t, b["batch_id"], b=b["gross_minor"], impact=b["gross_minor"],
                detail="Bank line with no platform transaction", brow=[b])

    # 3. Batch fee check: bank fee is posted once per batch, platform fee is per row
    by_batch = defaultdict(list)
    for b in Bu:
        by_batch[b["batch_id"]].append(b)
    for bid, lines in sorted(by_batch.items()):
        if lines[0]["value_date"][:7] != month:
            continue
        its = [l for l in lines if l["line_type"] == "SETTLEMENT" and l["transaction_id"] in pairs]
        if not its:
            continue
        fees = [l for l in lines if l["line_type"] == "FEE"]
        exp = sum(pairs[l["transaction_id"]][0]["fee_minor"] for l in its)
        act = -sum(f["gross_minor"] for f in fees)
        res = act - exp
        if res:
            tol = math.ceil(len(its) / 2)
            gap = "ROUNDING_VARIANCE" if abs(res) <= tol else "FEE_BREAK"
            add(gap, "batch", bid, bid, exp, act, res, res,
                "%d items tie individually. Summed item fees %d vs bank batch fee %d; residual %d (tolerance %d)"
                % (len(its), exp, act, res, tol), [pairs[l["transaction_id"]][0] for l in its], fees)

    F.sort(key=lambda f: (f["gap_type"] == "MATCHED", f["id"]))
    by = defaultdict(lambda: dict(count=0, impact_minor=0))
    for f in F:
        by[f["gap_type"]]["count"] += 1
        by[f["gap_type"]]["impact_minor"] += f["impact_minor"]
    S = lambda gap, k: sum(f[k] or 0 for f in F if f["gap_type"] == gap)
    pc = sum(p["gross_minor"] for p in scope)
    bank_in = [b for b in Bu if b["line_type"] != "FEE" and b["value_date"][:7] == month]
    bank_actual = sum(b["gross_minor"] for b in bank_in)
    expected = pc - S("SETTLED_NEXT_MONTH", "platform_amount_minor") - S("UNSETTLED", "platform_amount_minor") \
        + S("BANK_ONLY", "bank_amount_minor") + S("AMOUNT_MISMATCH", "variance_minor")
    raw_p = sum(p["gross_minor"] for p in platform if p["created_at"][:7] == month and p["status"] == "CAPTURED")
    raw_b = sum(b["gross_minor"] for b in bank if b["line_type"] != "FEE" and b["value_date"][:7] == month)
    return dict(month=month, findings=F, by_gap=dict(by),
                summary=dict(platform_rows=len(platform), bank_rows=len(bank), in_scope_transactions=len(scope),
                             duplicates_removed=by["DUPLICATE_ENTRY"]["count"],
                             platform_gross_raw=raw_p, platform_gross_clean=pc,
                             bank_item_gross_raw=raw_b, bank_item_gross_clean=bank_actual),
                tie_out=dict(platform_clean=pc, less_settled_next_month=S("SETTLED_NEXT_MONTH", "platform_amount_minor"),
                             less_unsettled=S("UNSETTLED", "platform_amount_minor"),
                             plus_bank_only=S("BANK_ONLY", "bank_amount_minor"),
                             plus_amount_mismatch=S("AMOUNT_MISMATCH", "variance_minor"),
                             expected_bank=expected, actual_bank=bank_actual, difference=bank_actual - expected))
