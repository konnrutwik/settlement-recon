"""Deterministic synthetic data (seeded). Money = integer minor units (USD cents)."""
import csv, os
import random
from datetime import datetime, timedelta, date

MONTH = "2026-10"
FEE_BPS = 290  # 2.90%
P_COLS = ["txn_id", "order_id", "txn_type", "status", "parent_txn_id", "currency",
          "gross_minor", "fee_minor", "net_minor", "created_at", "source_row_id"]
B_COLS = ["settlement_ref", "batch_id", "line_type", "transaction_id", "original_ref", "currency",
          "gross_minor", "fee_minor", "net_minor", "value_date", "source_row_id"]


def fee_item(gross):
    """Platform fee: per row, round half up."""
    return (gross * FEE_BPS + 5000) // 10000


def generate(seed=42, n_normal=60):
    r = random.Random(seed)
    P, B, batches, labels = [], [], {}, []

    def add(tid, created, gross, settle, typ="PAYMENT", parent="", batch=None):
        fee = fee_item(gross) if typ == "PAYMENT" else 0
        P.append(dict(txn_id=tid, order_id="ORD-" + tid[4:], txn_type=typ, status="CAPTURED",
                      parent_txn_id=parent, currency="USD", gross_minor=gross, fee_minor=fee,
                      net_minor=gross - fee, created_at=created.isoformat() + "+00:00",
                      source_row_id=len(P) + 1))
        bid = batch or "B-" + settle.strftime("%Y%m%d")
        B.append(dict(settlement_ref="SR-%05d" % (len(B) + 1), batch_id=bid,
                      line_type="SETTLEMENT" if typ == "PAYMENT" else "REFUND", transaction_id=tid,
                      original_ref=parent, currency="USD", gross_minor=gross, fee_minor=0,
                      net_minor=gross, value_date=settle.isoformat(), source_row_id=len(B) + 1))
        b = batches.setdefault(bid, dict(date=settle, gross=0, fee=0))
        if typ == "PAYMENT":
            b["gross"] += gross
            b["fee"] += fee

    normal = []
    for i in range(n_normal):  # normal: created Oct 1-28, settles T+1/T+2 inside October
        created = datetime(2026, 10, 1 + r.randrange(28), r.randrange(8, 20), r.randrange(60))
        gross = r.randrange(500, 20000)
        add("TXN-%04d" % i, created, gross, created.date() + timedelta(r.choice([1, 2])))
        normal.append((created, gross))
    for k, idx in enumerate([3, 7, 11]):  # valid partial refunds of real charges
        created, gross = normal[idx]
        rc = created + timedelta(days=1)
        add("TXN-R%02d" % k, rc, -(gross // 3), rc.date() + timedelta(1), "REFUND", parent="TXN-%04d" % idx)

    # Gap 1: late-month charges settling Nov 1-2
    for k, (c, d) in enumerate([(datetime(2026, 10, 30, 22, 10), date(2026, 11, 1)),
                                (datetime(2026, 10, 31, 9, 0), date(2026, 11, 2)),
                                (datetime(2026, 10, 31, 23, 30), date(2026, 11, 2))]):
        g = 7500 + 1250 * k
        add("TXN-LAG-%d" % (k + 1), c, g, d)
        labels.append(dict(gap_type="SETTLED_NEXT_MONTH", source="platform", ref="TXN-LAG-%d" % (k + 1), impact_minor=g))

    # Gap 2: four 10.10 payments in one batch. Items tie; bank fee is on the batch total.
    for k in range(4):
        add("TXN-RND-%d" % (k + 1), datetime(2026, 10, 14, 10 + k, 5), 1010, date(2026, 10, 15), batch="B-20261015-R")
    labels.append(dict(gap_type="ROUNDING_VARIANCE", source="batch", ref="B-20261015-R",
                       impact_minor=fee_item(4040) - 4 * fee_item(1010)))

    # Gap 3: duplicates (platform exact, bank re-delivery, bank logical)
    p = dict(P[5]); p["source_row_id"] = len(P) + 1; P.append(p)
    labels.append(dict(gap_type="DUPLICATE_ENTRY", source="platform", ref=p["txn_id"], impact_minor=p["gross_minor"]))
    b = dict(B[10]); b["source_row_id"] = len(B) + 1; B.append(b)
    labels.append(dict(gap_type="DUPLICATE_ENTRY", source="bank", ref=b["transaction_id"], impact_minor=b["gross_minor"]))
    b = dict(B[20]); b["settlement_ref"] = "SR-DUP-001"; b["source_row_id"] = len(B) + 1; B.append(b)
    labels.append(dict(gap_type="DUPLICATE_ENTRY", source="bank", ref=b["transaction_id"], impact_minor=b["gross_minor"]))

    # Gap 4: refunds whose original charge does not exist
    add("TXN-ORPH-1", datetime(2026, 10, 20, 11, 0), -4500, date(2026, 10, 21), "REFUND", parent="TXN-GHOST-9")
    add("TXN-ORPH-2", datetime(2026, 10, 22, 15, 0), -2250, date(2026, 10, 23), "REFUND", parent="")
    labels += [dict(gap_type="ORPHAN_REFUND", source="platform", ref="TXN-ORPH-1", impact_minor=-4500),
               dict(gap_type="ORPHAN_REFUND", source="platform", ref="TXN-ORPH-2", impact_minor=-2250)]

    # Bank charges fees once per batch on the batch gross (the source of rounding variance)
    for bid, bt in batches.items():
        fee = fee_item(bt["gross"]) if bid.endswith("-R") else bt["fee"]
        B.append(dict(settlement_ref="FEE-" + bid, batch_id=bid, line_type="FEE", transaction_id="",
                      original_ref="", currency="USD", gross_minor=-fee, fee_minor=0, net_minor=-fee,
                      value_date=bt["date"].isoformat(), source_row_id=len(B) + 1))
    return dict(month=MONTH, seed=seed, platform=P, bank=B, labels=labels)


def write_csv(data, folder="data"):
    os.makedirs(folder, exist_ok=True)
    for name, cols, rows in [("platform_transactions", P_COLS, data["platform"]),
                             ("bank_settlements", B_COLS, data["bank"])]:
        with open(os.path.join(folder, name + ".csv"), "w", newline="") as f:
            w = csv.DictWriter(f, cols); w.writeheader(); w.writerows(rows)
