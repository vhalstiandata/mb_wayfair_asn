"""
PATCH FOR shared/netsuite.py

Add this function to the end of shared/netsuite.py (just before EOF).
It fetches SO line items (item name + quantity) for the email body.

Wraps a SuiteQL query against transactionline. mainline='F' filters out the
summary row; itemtype='InvtPart' excludes the line for the Discount Values item
we add (otherwise the email would show a "-630.12" line as if it were a product).
"""

def get_so_items(so_internal_id: str):
    """Return list of {wayfair_sku, oracle_sku, ordered_qty} for an SO."""
    rows = ns_suiteql(f"""
        SELECT
            BUILTIN.DF(tl.item)  AS oracle_sku,
            tl.quantity          AS qty
        FROM transactionline tl
        WHERE tl.transaction = {so_internal_id}
          AND tl.mainline = 'F'
          AND tl.itemtype IN ('InvtPart', 'NonInvtPart', 'Kit', 'Assembly')
          AND tl.quantity > 0
        ORDER BY tl.linesequencenumber ASC
    """) or []

    return [
        {
            "wayfair_sku": "",  # not needed for email body
            "oracle_sku":  r.get("oracle_sku") or "?",
            "ordered_qty": int(float(r.get("qty") or 0)),
        }
        for r in rows
    ]
