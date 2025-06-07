import frappe
from frappe import _
from frappe.utils import get_datetime, date_diff, fmt_money

def execute(filters=None):
    if not filters:
        filters = {}

    # Validate required filters
    required_filters = ["customer", "from_date", "to_date", "company"]
    for f in required_filters:
        if not filters.get(f):
            frappe.throw(_("Please set filter: {0}").format(f))

    customer = filters["customer"]
    from_date = filters["from_date"]
    to_date = filters["to_date"]
    company = filters["company"]
    aging_date = get_datetime(to_date).date()

    # Get receivable account
    receivable_account = frappe.db.get_value("Account", {
        "account_type": "Receivable",
        "company": company,
        "is_group": 0
    })

    if not receivable_account:
        frappe.throw(_("Receivable account not found for company {0}").format(company))

    # Get currency
    currency = frappe.get_cached_value("Company", company, "default_currency")

    columns = [
        {"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 120},
        {"fieldname": "reference_no", "label": _("Reference No."), "fieldtype": "Data", "width": 200},
        {"fieldname": "voucher_type", "label": _("Voucher Type"), "fieldtype": "Data", "width": 120},
        {"fieldname": "due_date", "label": _("Due Date"), "fieldtype": "Date", "width": 120},
        {"fieldname": "debit", "label": _("Debit"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "credit", "label": _("Credit"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "balance", "label": _("Balance"), "fieldtype": "Currency", "width": 130},
    ]

    data = []

    # Opening Balance
    opening_balance_result = frappe.db.sql("""
        SELECT SUM(debit) - SUM(credit) AS opening_balance
        FROM `tabGL Entry`
        WHERE party = %(customer)s
        AND account = %(account)s
        AND posting_date < %(from_date)s
        AND company = %(company)s
        AND is_cancelled = 0
    """, {
        "customer": customer,
        "account": receivable_account,
        "from_date": from_date,
        "company": company
    }, as_dict=True)[0]

    opening_balance = opening_balance_result.opening_balance or 0.0
    running_balance = opening_balance

    data.append({
        "date": from_date,
        "due_date": None,
        "reference_no": "Opening Balance",
        "voucher_type": "",
        "debit": None,
        "credit": None,
        "balance": fmt_money(opening_balance, currency=currency)
    })

    # Transactions
    transactions = frappe.db.sql("""
        SELECT
            gle.posting_date AS date,
            gle.voucher_no AS reference_no,
            gle.voucher_type,
            gle.debit,
            gle.credit,
            si.due_date,
            si.is_return
        FROM `tabGL Entry` gle
        LEFT JOIN `tabSales Invoice` si
            ON gle.voucher_type = 'Sales Invoice'
            AND gle.voucher_no = si.name
            AND si.docstatus = 1
        WHERE gle.party = %(customer)s
        AND gle.account = %(account)s
        AND gle.posting_date BETWEEN %(from_date)s AND %(to_date)s
        AND gle.company = %(company)s
        AND gle.is_cancelled = 0
        ORDER BY gle.posting_date ASC, gle.creation ASC
    """, {
        "customer": customer,
        "account": receivable_account,
        "from_date": from_date,
        "to_date": to_date,
        "company": company
    }, as_dict=True)

    for txn in transactions:
        running_balance += (txn.debit or 0) - (txn.credit or 0)
        txn["balance"] = running_balance

        # Set Voucher Type according to rules
        if txn.voucher_type == "Sales Invoice" and txn.credit and not txn.get("is_return"):
            txn["voucher_type"] = "Payment"
        elif txn.voucher_type == "Sales Invoice" and txn.debit and not txn.get("is_return"):
            txn["voucher_type"] = "Invoice"
        elif txn.voucher_type == "Payment Entry":
            txn["voucher_type"] = "Payment"
        elif txn.voucher_type == "Sales Invoice" and txn.get("is_return"):
            txn["voucher_type"] = "Credit Note"

        txn["debit"] = fmt_money(txn.debit, currency=currency) if txn.debit else None
        txn["credit"] = fmt_money(txn.credit, currency=currency) if txn.credit else None
        txn["balance"] = fmt_money(txn["balance"], currency=currency)

        data.append(txn)

    # Closing Balance
    data.append({
        "date": to_date,
        "due_date": None,
        "reference_no": "Closing Balance",
        "voucher_type": "",
        "debit": None,
        "credit": None,
        "balance": fmt_money(running_balance, currency=currency)
    })

    # Aging Buckets
    aging_buckets = {
        "0-30 Days": 0.0,
        "31-60 Days": 0.0,
        "61-90 Days": 0.0,
        "91+ Days": 0.0
    }

    # Aging GL Entries
    aging_entries = frappe.db.sql("""
        SELECT
            posting_date,
            debit,
            credit
        FROM `tabGL Entry`
        WHERE party = %(customer)s
        AND account = %(account)s
        AND posting_date <= %(to_date)s
        AND company = %(company)s
        AND is_cancelled = 0
    """, {
        "customer": customer,
        "account": receivable_account,
        "to_date": to_date,
        "company": company
    }, as_dict=True)

    for entry in aging_entries:
        net_amount = (entry.debit or 0.0) - (entry.credit or 0.0)
        if abs(net_amount) < 0.01:
            continue
        days = date_diff(aging_date, entry.posting_date)

        if days <= 30:
            aging_buckets["0-30 Days"] += net_amount
        elif days <= 60:
            aging_buckets["31-60 Days"] += net_amount
        elif days <= 90:
            aging_buckets["61-90 Days"] += net_amount
        else:
            aging_buckets["91+ Days"] += net_amount

    total_outstanding = sum(aging_buckets.values())

    for bucket in aging_buckets:
        aging_buckets[bucket] = fmt_money(aging_buckets[bucket], currency=currency)

    total_outstanding = fmt_money(total_outstanding, currency=currency)

    return columns, data, {
        "aging_buckets": aging_buckets,
        "total_outstanding": total_outstanding
    }