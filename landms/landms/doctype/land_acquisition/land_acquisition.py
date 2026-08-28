import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, create_batch, flt, today


class LandAcquisition(Document):
	def validate(self):
		self._convert_payment_period_to_days()
		self._validate_area()
		self._validate_coordinates()
		self._validate_sales_defaults()
		self._validate_plot_type_rates()
		self._validate_and_set_tcb_patterns()

	def before_submit(self):
		if self.status != "Approved":
			frappe.throw(_("Land Acquisition must be Approved through the workflow before submission."))
		self.approval_date = today()
		self.approved_by = frappe.session.user

	def on_submit(self):
		sync_land_acquisition_cost_summary(self.name)
		sync_land_acquisition_plot_summary(self.name)

	def before_cancel(self):
		self._block_if_active_plots()
		self._block_or_cancel_purchase_documents()

	def _block_if_active_plots(self):
		if not frappe.db.exists("DocType", "Plot Master"):
			return
		active_plots = frappe.db.count("Plot Master", {"land_acquisition": self.name, "docstatus": 1})
		if not active_plots:
			return
		sample = frappe.db.get_all(
			"Plot Master",
			filters={"land_acquisition": self.name, "docstatus": 1},
			fields=["name"],
			limit_page_length=3,
		)
		names = ", ".join(r.name for r in sample)
		extra = f" and {active_plots - len(sample)} more" if active_plots > len(sample) else ""
		frappe.throw(
			_(
				"Cannot cancel — submitted plots still exist: {0}{1}. Cancel those Plot Master records first."
			).format(names, extra)
		)

	def _block_or_cancel_purchase_documents(self):
		"""Block cancellation if any PI or PO has payments against it.
		Auto-cancel unpaid submitted PIs and POs so the LA can be cancelled cleanly.
		"""
		for doctype, item_doctype in (
			("Purchase Invoice", "Purchase Invoice Item"),
			("Purchase Order", "Purchase Order Item"),
		):
			linked = frappe.db.sql(
				f"""
                SELECT DISTINCT p.name
                FROM `tab{doctype}` p
                INNER JOIN `tab{item_doctype}` pi ON pi.parent = p.name
                WHERE pi.land_acquisition = %s AND p.docstatus = 1
                """,
				self.name,
				as_dict=True,
			)

			for row in linked:
				has_payment = frappe.db.sql(
					"""
                    SELECT COUNT(*) FROM `tabPayment Entry Reference` per
                    INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
                    WHERE per.reference_doctype = %s
                      AND per.reference_name = %s
                      AND pe.docstatus = 1
                    """,
					(doctype, row.name),
				)[0][0]

				if has_payment:
					frappe.throw(
						f"Cannot cancel — {doctype} {row.name} has payments posted. "
						"Cancel the Payment Entry first."
					)

			for row in linked:
				doc = frappe.get_doc(doctype, row.name)
				doc.flags.ignore_permissions = True
				doc.cancel()

	def on_cancel(self):
		self.db_set("status", "Cancelled")
		sync_land_acquisition_cost_summary(self.name)
		sync_land_acquisition_plot_summary(self.name)
		self._clear_land_acquisition_references()

	def _clear_land_acquisition_references(self):
		"""Clear the land_acquisition dimension from all linked doctypes after cancel.

		Allows the cancelled LA to be deleted without ERPNext's link-check blocking it.
		land_acquisition is a reporting dimension — clearing it does not affect
		debit/credit amounts or financial totals.
		"""
		doctypes = [
			"GL Entry",
			"Payment Ledger Entry",
			"Purchase Invoice",
			"Purchase Invoice Item",
			"Purchase Order",
			"Purchase Order Item",
			"Payment Entry",
			"Journal Entry Account",
			"Stock Entry",
			"Stock Entry Detail",
			"Purchase Receipt",
			"Purchase Receipt Item",
			"Sales Invoice",
			"Sales Invoice Item",
			"Sales Order",
			"Sales Order Item",
			"Delivery Note",
			"Delivery Note Item",
		]
		for doctype in doctypes:
			try:
				names = frappe.get_all(
					doctype,
					filters={"land_acquisition": self.name},
					pluck="name",
				)
				for name in names:
					frappe.db.set_value(doctype, name, "land_acquisition", None, update_modified=False)
			except Exception:
				pass

	# ── Private validators ───────────────────────────────────────────────────

	def _validate_area(self):
		if not self.total_area_sqm or flt(self.total_area_sqm) <= 0:
			frappe.throw(_("Total Area must be greater than zero."))

	def _validate_coordinates(self):
		has_lat = self.latitude not in (None, "")
		has_lng = self.longitude not in (None, "")
		if has_lat != has_lng:
			frappe.throw(_("Enter both Latitude and Longitude, or leave both blank."))
		if has_lat:
			lat = flt(self.latitude)
			lng = flt(self.longitude)
			if lat < -90 or lat > 90:
				frappe.throw(_("Latitude must be between -90 and 90."))
			if lng < -180 or lng > 180:
				frappe.throw(_("Longitude must be between -180 and 180."))

	def _convert_payment_period_to_days(self):
		total = (
			cint(self.payment_years or 0) * 365
			+ cint(self.payment_months or 0) * 30
			+ cint(self.payment_days_input or 0)
		)
		if total <= 0:
			frappe.throw(_("Payment period must be greater than zero."))
		self.payment_completion_days = total
		self.payment_period_summary = _build_period_summary(
			cint(self.payment_years or 0),
			cint(self.payment_months or 0),
			cint(self.payment_days_input or 0),
			total,
		)

	def _validate_sales_defaults(self):
		if not (0 <= flt(self.booking_fee_percent) <= 100):
			frappe.throw(_("Booking Fee % must be between 0 and 100."))
		if not (0 <= flt(self.government_share_percent) <= 100):
			frappe.throw(_("Government Share % must be between 0 and 100."))
		if cint(self.payment_completion_days) <= 0:
			frappe.throw(_("Payment Completion Days must be greater than zero."))

	def _validate_and_set_tcb_patterns(self):
		if not self.partner_code:
			frappe.throw(_("Partner Code is required."))

		if "-" not in self.partner_code:
			frappe.throw(
				_(
					"Partner Code must contain a hyphen (e.g., 'PART-GVA') to generate the TCB Control Number pattern."
				)
			)

		suffix = self.partner_code.split("-")[-1].strip()
		if not suffix:
			frappe.throw(_("Partner Code suffix cannot be empty."))

		self.control_number_pattern = f"99910#{suffix}####"
		self.related_control_number_pattern = f"9992#R{suffix}####"

	def _validate_plot_type_rates(self):
		seen = set()
		for row in self.get("plot_type_rates") or []:
			if not row.plot_type:
				frappe.throw(f"Row {row.idx} in Plot Type Rates is missing a Plot Type.")
			if row.plot_type in seen:
				frappe.throw(
					f"Plot Type '{row.plot_type}' appears more than once in the rates table. "
					"Each plot type can only have one rate per Land Acquisition."
				)
			seen.add(row.plot_type)


# =============================================================================
# Cost summary — single source of truth for PO/PI/PE rollups per supplier
# =============================================================================


@frappe.whitelist()
def sync_land_acquisition_cost_summary(land_acquisition):
	"""Recompute the cost summary and persist totals on the Land Acquisition."""
	if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
		return {}

	summary = get_land_acquisition_cost_summary(land_acquisition)
	t = summary["totals"]
	frappe.db.set_value(
		"Land Acquisition",
		land_acquisition,
		{
			"acquisition_cost_tzs": t["acquisition_cost_tzs"],
			"cost_per_sqm_tzs": t["cost_per_sqm_tzs"],
			"total_committed_tzs": t["committed"],
			"total_paid_tzs": t["paid"],
			"total_outstanding_tzs": t["outstanding"],
			"total_unbilled_po_tzs": t["unbilled_po"],
			"je_billed_tzs": t["je_billed"],
		},
		update_modified=False,
	)
	return summary


@frappe.whitelist()
def get_land_acquisition_cost_summary(land_acquisition):
	"""Return the consolidated PO/PI/PE rollup for one Land Acquisition.

	Shape:
	    {
	      "land_acquisition": str,
	      "total_area_sqm":   float,
	      "lud_account":      str,
	      "sellers":          [supplier_row, ...],
	      "others":           [supplier_row, ...],
	      "totals":           { ...numbers... },
	    }

	supplier_row:
	    {
	      "supplier": str, "supplier_name": str, "is_land_seller": int,
	      "committed":   float,  # PO total tagged to this LA
	      "billed":      float,  # PI total tagged to this LA hitting LUD
	      "paid":        float,  # PE allocations against those PIs
	      "outstanding": float,  # billed - paid
	      "unbilled_po": float,  # max(committed - billed, 0)
	      "po_docs": [...], "pi_docs": [...], "pe_docs": [...],
	    }
	"""
	if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
		return _empty_summary(land_acquisition)

	la = frappe.db.get_value(
		"Land Acquisition",
		land_acquisition,
		["company", "total_area_sqm"],
		as_dict=True,
	)
	lud_account = _get_lud_account(la.company)

	po_rows = _fetch_committed(land_acquisition)
	pi_rows = _fetch_billed(land_acquisition, lud_account)
	pe_rows, paid_per_pi = _fetch_paid([r["pi"] for r in pi_rows])
	advance_rows = _fetch_paid_advance(land_acquisition)

	# Annotate per-PI paid/outstanding for the drill-down view
	for row in pi_rows:
		row["paid"] = flt(paid_per_pi.get(row["pi"], 0))
		row["outstanding"] = flt(row["amount"]) - row["paid"]

	suppliers = _build_supplier_rollup(po_rows, pi_rows, pe_rows, advance_rows)
	sellers = [s for s in suppliers if s["is_land_seller"]]
	others = [s for s in suppliers if not s["is_land_seller"]]
	totals = _compute_totals(sellers, others, flt(la.total_area_sqm))

	plot_inv_account = frappe.db.get_single_value("LandMS Settings", "plot_inventory_account")
	je_billed = _fetch_billed_from_je(land_acquisition, lud_account, plot_inv_account)
	je_rows = _fetch_je_rows(land_acquisition, lud_account, plot_inv_account)
	totals["je_billed"] = je_billed
	if je_billed:
		area = flt(la.total_area_sqm)
		totals["acquisition_cost_tzs"] = flt(totals["acquisition_cost_tzs"]) + je_billed
		totals["billed"] = flt(totals["billed"]) + je_billed
		totals["cost_per_sqm_tzs"] = (totals["acquisition_cost_tzs"] / area) if area else 0.0

	return {
		"land_acquisition": land_acquisition,
		"total_area_sqm": flt(la.total_area_sqm),
		"lud_account": lud_account,
		"sellers": sellers,
		"others": others,
		"totals": totals,
		"je_rows": je_rows,
	}


def _get_lud_account(company):
	"""Resolve the Land Under Development account for the given company."""
	account = frappe.db.get_single_value("LandMS Settings", "land_under_development_account")
	if account:
		return account
	# Fallback by account number on the company
	return frappe.db.get_value(
		"Account",
		{"company": company, "account_number": "1411"},
		"name",
	)


def _fetch_committed(la_name):
	"""Submitted Purchase Order items tagged to this Land Acquisition."""
	if not frappe.db.has_column("Purchase Order Item", "land_acquisition"):
		return []
	return frappe.db.sql(
		"""
        SELECT
            po.name             AS po,
            po.supplier         AS supplier,
            po.transaction_date AS date,
            po.status           AS status,
            SUM(poi.base_net_amount) AS amount
        FROM `tabPurchase Order Item` poi
        INNER JOIN `tabPurchase Order` po ON po.name = poi.parent
        WHERE poi.land_acquisition = %(la)s
          AND po.docstatus = 1
        GROUP BY po.name
        HAVING ABS(SUM(poi.base_net_amount)) > 0.0001
        ORDER BY po.transaction_date DESC, po.creation DESC
        """,
		{"la": la_name},
		as_dict=True,
	)


def _fetch_billed(la_name, lud_account):
	"""Submitted Purchase Invoice items tagged to this LA hitting the LUD account."""
	if not frappe.db.has_column("Purchase Invoice Item", "land_acquisition"):
		return []
	if not lud_account:
		return []
	return frappe.db.sql(
		"""
        SELECT
            pi.name           AS pi,
            pi.supplier       AS supplier,
            pi.posting_date   AS date,
            pi.status         AS status,
            SUM(pii.base_net_amount) AS amount
        FROM `tabPurchase Invoice Item` pii
        INNER JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
        WHERE pii.land_acquisition = %(la)s
          AND pi.docstatus = 1
          AND pii.expense_account = %(lud)s
        GROUP BY pi.name
        HAVING ABS(SUM(pii.base_net_amount)) > 0.0001
        ORDER BY pi.posting_date DESC, pi.creation DESC
        """,
		{"la": la_name, "lud": lud_account},
		as_dict=True,
	)


def _fetch_je_rows(la_name, lud_account, plot_inv_account):
	"""Return individual submitted JE rows tagged to this LA on land cost accounts."""
	accounts = tuple(a for a in [lud_account, plot_inv_account] if a)
	if not accounts:
		return []
	return frappe.db.sql(
		"""
        SELECT
            je.name          AS je_name,
            je.posting_date,
            je.user_remark,
            jea.debit_in_account_currency AS amount
        FROM `tabJournal Entry Account` jea
        INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
        WHERE jea.land_acquisition = %(la)s
          AND je.docstatus = 1
          AND jea.account IN %(accounts)s
          AND jea.debit_in_account_currency > 0
        ORDER BY je.posting_date DESC, je.name
    """,
		{"la": la_name, "accounts": accounts},
		as_dict=True,
	)


def _fetch_billed_from_je(la_name, lud_account, plot_inv_account):
	"""Sum debit amounts from submitted JEs tagged with this LA on land cost accounts."""
	accounts = tuple(a for a in [lud_account, plot_inv_account] if a)
	if not accounts:
		return 0.0
	result = frappe.db.sql(
		"""
        SELECT COALESCE(SUM(jea.debit_in_account_currency), 0)
        FROM `tabJournal Entry Account` jea
        INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
        WHERE jea.land_acquisition = %(la)s
          AND je.docstatus = 1
          AND jea.account IN %(accounts)s
          AND jea.debit_in_account_currency > 0
    """,
		{"la": la_name, "accounts": accounts},
	)
	return flt(result[0][0]) if result else 0.0


def _fetch_paid(pi_names):
	"""Payment Entry allocations against the given PI names.

	Returns (pe_rows, {pi_name: total_paid}).
	"""
	if not pi_names:
		return [], {}
	rows = frappe.db.sql(
		"""
        SELECT
            pe.name            AS pe,
            pe.party           AS supplier,
            pe.posting_date    AS date,
            per.reference_name AS pi,
            SUM(per.allocated_amount) AS amount
        FROM `tabPayment Entry Reference` per
        INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE per.reference_doctype = 'Purchase Invoice'
          AND per.reference_name IN %(pis)s
          AND pe.docstatus = 1
        GROUP BY pe.name, per.reference_name
        ORDER BY pe.posting_date DESC, pe.creation DESC
        """,
		{"pis": tuple(pi_names)},
		as_dict=True,
	)
	paid_per_pi = {}
	for r in rows:
		paid_per_pi[r["pi"]] = paid_per_pi.get(r["pi"], 0) + flt(r["amount"])
	return rows, paid_per_pi


def _fetch_paid_advance(la_name):
	"""Payment Entry allocations made directly against Purchase Orders tagged
	to this Land Acquisition (i.e. supplier advances paid before any PI exists).

	A single PO can hold items for multiple Land Acquisitions, so each
	allocation is pro-rated by this LA's share of the PO total. The result
	looks the same shape as `_fetch_paid` rows so the rollup can consume both.
	"""
	if not frappe.db.has_column("Purchase Order Item", "land_acquisition"):
		return []
	rows = frappe.db.sql(
		"""
        SELECT
            pe.name             AS pe,
            pe.party            AS supplier,
            pe.posting_date     AS date,
            per.reference_name  AS po,
            per.allocated_amount AS allocated,
            (
                SELECT SUM(poi.base_net_amount)
                FROM `tabPurchase Order Item` poi
                WHERE poi.parent = per.reference_name
                  AND poi.land_acquisition = %(la)s
            ) AS la_share,
            (
                SELECT SUM(poi.base_net_amount)
                FROM `tabPurchase Order Item` poi
                WHERE poi.parent = per.reference_name
            ) AS po_total
        FROM `tabPayment Entry Reference` per
        INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE per.reference_doctype = 'Purchase Order'
          AND pe.docstatus = 1
          AND EXISTS (
              SELECT 1 FROM `tabPurchase Order Item` poi
              WHERE poi.parent = per.reference_name
                AND poi.land_acquisition = %(la)s
          )
        ORDER BY pe.posting_date DESC, pe.creation DESC
        """,
		{"la": la_name},
		as_dict=True,
	)

	result = []
	for r in rows:
		po_total = flt(r["po_total"])
		ratio = (flt(r["la_share"]) / po_total) if po_total else 0.0
		amount = flt(r["allocated"]) * ratio
		if abs(amount) <= 0.0001:
			continue
		result.append(
			{
				"pe": r["pe"],
				"supplier": r["supplier"],
				"date": r["date"],
				"po": r["po"],
				"amount": amount,
			}
		)
	return result


def _build_supplier_rollup(po_rows, pi_rows, pe_rows, advance_rows=None):
	"""Aggregate per-supplier metrics from the three doc-type row sets."""
	advance_rows = advance_rows or []
	supplier_names = (
		{r["supplier"] for r in po_rows}
		| {r["supplier"] for r in pi_rows}
		| {r["supplier"] for r in pe_rows}
		| {r["supplier"] for r in advance_rows}
	)
	supplier_meta = {}
	if supplier_names:
		meta_rows = frappe.db.get_all(
			"Supplier",
			filters={"name": ("in", list(supplier_names))},
			fields=["name", "supplier_name", "is_land_seller"],
		)
		supplier_meta = {r.name: r for r in meta_rows}

	suppliers = {}

	def _row(name):
		if name not in suppliers:
			meta = supplier_meta.get(name) or frappe._dict()
			suppliers[name] = {
				"supplier": name,
				"supplier_name": meta.get("supplier_name") or name,
				"is_land_seller": int(meta.get("is_land_seller") or 0),
				"committed": 0.0,
				"billed": 0.0,
				"paid": 0.0,
				"outstanding": 0.0,
				"unbilled_po": 0.0,
				"po_docs": [],
				"pi_docs": [],
				"pe_docs": [],
			}
		return suppliers[name]

	for r in po_rows:
		s = _row(r["supplier"])
		s["committed"] += flt(r["amount"])
		s["po_docs"].append(
			{
				"name": r["po"],
				"amount": flt(r["amount"]),
				"date": str(r["date"]) if r["date"] else None,
				"status": r["status"],
			}
		)

	for r in pi_rows:
		s = _row(r["supplier"])
		s["billed"] += flt(r["amount"])
		s["paid"] += flt(r["paid"])
		s["pi_docs"].append(
			{
				"name": r["pi"],
				"amount": flt(r["amount"]),
				"paid": flt(r["paid"]),
				"outstanding": flt(r["outstanding"]),
				"date": str(r["date"]) if r["date"] else None,
				"status": r["status"],
			}
		)

	for r in pe_rows:
		s = _row(r["supplier"])
		s["pe_docs"].append(
			{
				"name": r["pe"],
				"amount": flt(r["amount"]),
				"date": str(r["date"]) if r["date"] else None,
				"pi": r["pi"],
				"against": "pi",
			}
		)

	for r in advance_rows:
		s = _row(r["supplier"])
		s["paid"] += flt(r["amount"])
		s["pe_docs"].append(
			{
				"name": r["pe"],
				"amount": flt(r["amount"]),
				"date": str(r["date"]) if r["date"] else None,
				"po": r["po"],
				"against": "po",
			}
		)

	for s in suppliers.values():
		s["outstanding"] = max(s["billed"] - s["paid"], 0.0)
		s["unbilled_po"] = max(s["committed"] - s["billed"], 0.0)

	return sorted(
		suppliers.values(),
		key=lambda s: (-s["is_land_seller"], -(s["committed"] + s["billed"])),
	)


def _compute_totals(sellers, others, total_area_sqm):
	def _sum(rows, key):
		return sum(flt(r[key]) for r in rows)

	seller_billed = _sum(sellers, "billed")
	other_billed = _sum(others, "billed")
	acquisition_cost = seller_billed + other_billed
	cost_per_sqm = (acquisition_cost / total_area_sqm) if total_area_sqm else 0.0

	return {
		# split by supplier type
		"seller_committed": _sum(sellers, "committed"),
		"seller_billed": seller_billed,
		"seller_paid": _sum(sellers, "paid"),
		"seller_outstanding": _sum(sellers, "outstanding"),
		"seller_unbilled_po": _sum(sellers, "unbilled_po"),
		"other_committed": _sum(others, "committed"),
		"other_billed": other_billed,
		"other_paid": _sum(others, "paid"),
		"other_outstanding": _sum(others, "outstanding"),
		"other_unbilled_po": _sum(others, "unbilled_po"),
		# grand totals
		"committed": _sum(sellers, "committed") + _sum(others, "committed"),
		"billed": acquisition_cost,
		"paid": _sum(sellers, "paid") + _sum(others, "paid"),
		"outstanding": _sum(sellers, "outstanding") + _sum(others, "outstanding"),
		"unbilled_po": _sum(sellers, "unbilled_po") + _sum(others, "unbilled_po"),
		# cost-basis used by Plot Master allocation
		"acquisition_cost_tzs": acquisition_cost,
		"cost_per_sqm_tzs": cost_per_sqm,
	}


def _empty_summary(la_name):
	return {
		"land_acquisition": la_name,
		"total_area_sqm": 0.0,
		"lud_account": None,
		"sellers": [],
		"others": [],
		"totals": {
			k: 0.0
			for k in (
				"seller_committed",
				"seller_billed",
				"seller_paid",
				"seller_outstanding",
				"seller_unbilled_po",
				"other_committed",
				"other_billed",
				"other_paid",
				"other_outstanding",
				"other_unbilled_po",
				"committed",
				"billed",
				"paid",
				"outstanding",
				"unbilled_po",
				"acquisition_cost_tzs",
				"cost_per_sqm_tzs",
				"je_billed",
			)
		},
	}


# =============================================================================
# Plot summary — counts of submitted Plot Masters per status
# =============================================================================


@frappe.whitelist()
def sync_land_acquisition_plot_summary(land_acquisition):
	if not land_acquisition or not frappe.db.exists("Land Acquisition", land_acquisition):
		return {}

	# Plot Master is created in Phase 3 — skip until then
	if not frappe.db.exists("DocType", "Plot Master"):
		return {
			"total_plots": 0,
			"available_plots": 0,
			"reserved_plots": 0,
			"delivered_plots": 0,
		}

	counts = frappe.db.sql(
		"""
        SELECT
            COUNT(*) AS total,
            SUM(status = 'Available') AS available,
            SUM(status IN ('Pending Fee', 'Pending Advance', 'Reserved', 'Ready for Handover')) AS reserved,
            SUM(status IN ('Delivered', 'Title Closed')) AS delivered
        FROM `tabPlot Master`
        WHERE land_acquisition = %s AND docstatus = 1
    """,
		land_acquisition,
		as_dict=True,
	)[0]

	total = int(counts.total or 0)
	available = int(counts.available or 0)
	reserved = int(counts.reserved or 0)
	delivered = int(counts.delivered or 0)

	frappe.db.set_value(
		"Land Acquisition",
		land_acquisition,
		{
			"total_plots": total,
			"available_plots": available,
			"reserved_plots": reserved,
			"delivered_plots": delivered,
		},
		update_modified=False,
	)

	current_status = frappe.db.get_value("Land Acquisition", land_acquisition, "status")
	if current_status in ("Approved", "Subdivided"):
		new_status = "Subdivided" if total > 0 else "Approved"
		if new_status != current_status:
			frappe.db.set_value(
				"Land Acquisition", land_acquisition, "status", new_status, update_modified=False
			)

	return {
		"total_plots": total,
		"available_plots": available,
		"reserved_plots": reserved,
		"delivered_plots": delivered,
	}


# =============================================================================
# Doc event handlers — wire PO/PI/PE submits to the LA cost summary
# =============================================================================


def _sync_many(la_names):
	for name in {n for n in (la_names or []) if n}:
		sync_land_acquisition_cost_summary(name)


def sync_costs_from_purchase_order(doc, method=None):
	_sync_many({row.land_acquisition for row in (doc.get("items") or []) if row.get("land_acquisition")})


def sync_costs_from_purchase_invoice(doc, method=None):
	_sync_many({row.land_acquisition for row in (doc.get("items") or []) if row.get("land_acquisition")})


def sync_costs_from_journal_entry(doc, method=None):
	_sync_many({row.land_acquisition for row in (doc.get("accounts") or []) if row.get("land_acquisition")})


def autoset_land_acquisition_on_payment_entry(doc, method=None):
	"""Tag a Payment Entry with the LA dimension by following its references.

	Walks both Purchase Invoice and Purchase Order references — the latter
	catches advance payments made before any invoice exists. Only sets the
	dimension on the PE header when all referenced docs resolve to the same
	LA. Mixed-LA payments are left untagged so the operator reviews them.
	"""
	if doc.get("payment_type") != "Pay" or doc.get("party_type") != "Supplier":
		return
	if doc.get("land_acquisition"):
		return
	la_names = set()
	for r in doc.get("references") or []:
		if r.reference_doctype == "Purchase Invoice":
			child_dt = "Purchase Invoice Item"
		elif r.reference_doctype == "Purchase Order":
			child_dt = "Purchase Order Item"
		else:
			continue
		item_rows = frappe.db.get_all(
			child_dt,
			filters={"parent": r.reference_name},
			fields=["land_acquisition"],
		)
		for row in item_rows:
			if row.land_acquisition:
				la_names.add(row.land_acquisition)
	if len(la_names) == 1:
		doc.land_acquisition = la_names.pop()


def sync_costs_from_payment_entry(doc, method=None):
	"""Refresh LA cost summaries when a PE is submitted or cancelled.

	A single PE can settle PIs and/or pay advances against POs across multiple
	Land Acquisitions, so we walk every reference (both kinds) and resync each
	affected LA.
	"""
	la_names = set()
	if doc.get("land_acquisition"):
		la_names.add(doc.land_acquisition)
	for r in doc.get("references") or []:
		if r.reference_doctype == "Purchase Invoice":
			child_dt = "Purchase Invoice Item"
		elif r.reference_doctype == "Purchase Order":
			child_dt = "Purchase Order Item"
		else:
			continue
		item_rows = frappe.db.get_all(
			child_dt,
			filters={"parent": r.reference_name},
			fields=["land_acquisition"],
		)
		for row in item_rows:
			if row.land_acquisition:
				la_names.add(row.land_acquisition)
	_sync_many(la_names)


def set_land_acquisition_expense_account(doc, method=None):
	"""Auto-fill land_acquisition on item rows from the header, then set
	expense_account to Land Under Development on all tagged items.

	ERPNext's accounting dimension already copies land_acquisition from header
	to items. This hook is a safety net — it ensures the expense_account is
	always Land Under Development regardless of the item's default, so survey
	fees, legal fees, and land price all capitalise correctly.
	"""
	header_la = doc.get("land_acquisition")

	# Propagate header land_acquisition to any item rows that missed it
	if header_la:
		for item in doc.get("items") or []:
			if not item.get("land_acquisition"):
				item.land_acquisition = header_la

	has_la_items = any(row.get("land_acquisition") for row in (doc.get("items") or []))
	if not has_la_items:
		return

	land_account = frappe.db.get_single_value("LandMS Settings", "land_under_development_account")
	if not land_account:
		return

	cost_center = frappe.db.get_single_value("LandMS Settings", "cost_center")

	for item in doc.get("items") or []:
		if item.get("land_acquisition"):
			item.expense_account = land_account
			if cost_center:
				item.cost_center = cost_center


def _build_period_summary(years, months, days, total_days):
	parts = []
	if years:
		parts.append(f"{years} year{'s' if years > 1 else ''}")
	if months:
		parts.append(f"{months} month{'s' if months > 1 else ''}")
	if days:
		parts.append(f"{days} day{'s' if days > 1 else ''}")
	label = " + ".join(parts) if parts else "0 days"
	return f"{label} = {total_days} days total"


# =============================================================================
# Recalculate Plot Costs
# =============================================================================


@frappe.whitelist()
def recalculate_plot_costs(land_acquisition):
	"""Enqueue a background job to update plot stock valuations to the current LA cost."""
	la = frappe.db.get_value(
		"Land Acquisition",
		land_acquisition,
		["docstatus", "recalculation_in_progress"],
		as_dict=True,
	)
	if not la or la.docstatus != 1:
		frappe.throw("Land Acquisition must be submitted.")
	if la.recalculation_in_progress:
		frappe.throw("A recalculation is already running. Please wait for it to complete.")

	frappe.db.set_value(
		"Land Acquisition",
		land_acquisition,
		"recalculation_in_progress",
		1,
		update_modified=False,
	)
	frappe.db.commit()

	frappe.enqueue(
		"landms.landms.doctype.land_acquisition.land_acquisition._run_plot_cost_recalculation",
		land_acquisition=land_acquisition,
		queue="long",
		timeout=3600,
		job_name=f"recalc_plot_costs_{land_acquisition}",
		now=frappe.conf.get("developer_mode"),
	)
	return {"status": "queued"}


def _run_plot_cost_recalculation(land_acquisition):
	"""Background job: create Stock Reconciliations to adjust un-delivered
	plot valuations to match the current Land Acquisition cost.

	Uses ERPNext's Stock Reconciliation to properly create SLEs and GL
	entries rather than manipulating ledger records directly.  Stock
	Reconciliation has a known limitation where serialised items are
	temporarily marked as 'Delivered' during the outward SLE — we fix
	the Serial No status/warehouse metadata after each batch.
	"""
	from landms.landms.doctype.plot_master.plot_master import get_plot_item_code

	def _finish(log, cost_to_save=None):
		update = {
			"recalculation_in_progress": 0,
			"last_recalculation_date": frappe.utils.now(),
			"last_recalculation_log": log,
		}
		if cost_to_save is not None:
			update["last_recalculation_cost"] = cost_to_save
		frappe.db.set_value("Land Acquisition", land_acquisition, update, update_modified=False)
		frappe.db.commit()

	try:
		settings = frappe.get_single("LandMS Settings")
		lud_account = settings.land_under_development_account
		warehouse = settings.plot_inventory_warehouse

		if not lud_account:
			_finish("FAILED: Land Under Development Account not set in LandMS Settings.")
			return
		if not warehouse:
			_finish("FAILED: Plot Inventory Warehouse not set in LandMS Settings.")
			return

		la = frappe.db.get_value(
			"Land Acquisition",
			land_acquisition,
			[
				"company",
				"total_area_sqm",
				"acquisition_cost_tzs",
				"last_recalculation_cost",
				"last_recalculation_log",
			],
			as_dict=True,
		)

		if not flt(la.total_area_sqm):
			_finish("FAILED: Total Area (sqm) is not set on this Land Acquisition.")
			return

		new_total_cost = flt(la.acquisition_cost_tzs)
		if not new_total_cost:
			_finish("FAILED: Acquisition cost is zero. Submit Purchase Invoices first.")
			return

		# Guard — only skip if last run was clean (no failures recorded)
		prev_log = la.last_recalculation_log or ""
		prev_had_failures = "FAILED" in prev_log or "✗" in prev_log
		if flt(la.last_recalculation_cost) == new_total_cost and not prev_had_failures:
			_finish("Nothing to do — plot costs are already up to date.", cost_to_save=new_total_cost)
			return

		cost_per_sqm = new_total_cost / flt(la.total_area_sqm)

		plots = frappe.get_all(
			"Plot Master",
			filters={"land_acquisition": land_acquisition, "docstatus": 1},
			fields=[
				"name",
				"plot_type",
				"plot_size_sqm",
				"allocated_cost",
				"serial_no",
				"stock_entry",
				"status",
			],
		)

		updated = []
		skipped = []
		failures = []
		sr_items = []

		for plot in plots:
			if plot.status in ("Delivered", "Title Closed"):
				skipped.append(f"{plot.name} — already delivered (manual JE adjustment needed)")
				continue
			if not plot.stock_entry:
				skipped.append(f"{plot.name} — not in inventory (no stock entry)")
				continue
			if not plot.serial_no:
				skipped.append(f"{plot.name} — missing serial number")
				continue
			if not flt(plot.plot_size_sqm):
				skipped.append(f"{plot.name} — plot area is 0, cannot calculate cost")
				continue

			new_plot_cost = flt(cost_per_sqm) * flt(plot.plot_size_sqm)
			old_plot_cost = flt(plot.allocated_cost)

			if abs(new_plot_cost - old_plot_cost) < 0.01:
				# Allocated cost is already correct — but sync cost_per_sqm if stale
				if abs(flt(plot.cost_per_sqm) - flt(cost_per_sqm)) > 0.01:
					frappe.db.set_value(
						"Plot Master", plot.name, "cost_per_sqm", flt(cost_per_sqm), update_modified=False
					)
				skipped.append(f"{plot.name} — cost unchanged ({old_plot_cost:.2f})")
				continue

			try:
				item_code = get_plot_item_code(plot.plot_type)
			except Exception as e:
				failures.append(f"{plot.name} — item code error: {e}")
				continue

			sr_items.append(
				{
					"plot": plot,
					"item_code": item_code,
					"new_cost": new_plot_cost,
					"old_cost": old_plot_cost,
				}
			)

		if not sr_items:
			_finish(
				"Nothing to do — no plots need cost update.",
				cost_to_save=new_total_cost,
			)
			return

		# ── Adjust cost on each Stock Entry ────────────────────────────
		# Neither Stock Reconciliation (SerialNoWarehouseError) nor Landed
		# Cost Voucher (doesn't support Stock Entry as receipt type) works
		# for serialised items from Stock Entries.
		#
		# Instead, we replicate exactly what LCV's update_landed_cost()
		# does internally (lines 244-259 of landed_cost_voucher.py):
		#   1. Update item rates on the Stock Entry
		#   2. Cancel SLEs with via_landed_cost_voucher=True  (bypasses
		#      Serial and Batch Bundle validation)
		#   3. Resubmit SLEs with via_landed_cost_voucher=True
		#   4. Redo GL entries
		#   5. Repost future SLE/GLE
		#   6. Update Serial No purchase_rate
		BATCH_SIZE = 50
		adjusted_se = []

		for batch in create_batch(sr_items, BATCH_SIZE):
			for item in batch:
				try:
					doc = frappe.get_doc("Stock Entry", item["plot"].stock_entry)
					new_rate = flt(item["new_cost"])

					# ── Step 1: Update item rates on the Stock Entry ─────
					for se_item in doc.get("items"):
						if se_item.serial_no == item["plot"].serial_no or len(doc.items) == 1:
							se_item.basic_rate = new_rate
							se_item.basic_amount = flt(new_rate * se_item.qty)
							se_item.amount = se_item.basic_amount
							se_item.valuation_rate = new_rate
							se_item.db_update()
							break

					doc.total_incoming_value = sum(flt(d.amount) for d in doc.get("items") if d.t_warehouse)
					doc.value_difference = doc.total_incoming_value - doc.total_outgoing_value
					doc.total_amount = doc.total_incoming_value
					doc.db_update()

					# ── Step 2: Update Serial No purchase_rate ────────────
					frappe.db.set_value(
						"Serial No",
						item["plot"].serial_no,
						"purchase_rate",
						new_rate,
						update_modified=False,
					)

					# ── Step 3: Cancel SLEs (same as LCV line 247-249) ───
					# Stock Entry.update_stock_ledger() does NOT accept
					# via_landed_cost_voucher, so we inline its logic and
					# call make_sl_entries() directly with the flag.
					doc.docstatus = 2
					sl_entries = []
					finished_item_row = doc.get_finished_item_row()
					doc.get_sle_for_source_warehouse(sl_entries, finished_item_row)
					doc.get_sle_for_target_warehouse(sl_entries, finished_item_row)
					sl_entries.reverse()
					doc.make_sl_entries(
						sl_entries,
						allow_negative_stock=True,
						via_landed_cost_voucher=True,
					)
					doc.make_gl_entries_on_cancel()

					# ── Step 4: Resubmit SLEs (same as LCV line 252-259) ─
					doc.docstatus = 1
					doc.make_bundle_using_old_serial_batch_fields(
						via_landed_cost_voucher=True,
					)
					sl_entries = []
					doc.get_sle_for_source_warehouse(sl_entries, finished_item_row)
					doc.get_sle_for_target_warehouse(sl_entries, finished_item_row)
					doc.make_sl_entries(
						sl_entries,
						allow_negative_stock=True,
						via_landed_cost_voucher=True,
					)
					doc.make_gl_entries()
					doc.repost_future_sle_and_gle(via_landed_cost_voucher=True)

					# ── Step 5: Update Plot Master ────────────────────────
					frappe.db.set_value(
						"Plot Master",
						item["plot"].name,
						{
							"allocated_cost": item["new_cost"],
							"cost_per_sqm": flt(cost_per_sqm),
						},
						update_modified=False,
					)

					adjusted_se.append(item["plot"].stock_entry)
					updated.append(f"{item['plot'].name} ({item['old_cost']:.0f} → {item['new_cost']:.0f})")

				except Exception:
					frappe.log_error(
						title=f"Plot cost adjust failed: {item['plot'].name}",
						message=frappe.get_traceback(),
						reference_doctype="Land Acquisition",
						reference_name=land_acquisition,
					)
					failures.append(f"{item['plot'].name} — cost adjustment failed (see Error Log)")

			frappe.db.commit()

		# Build result log
		lines = [
			f"Run: {frappe.utils.now()}",
			f"Total cost: {new_total_cost:,.0f} TZS  |  Cost/sqm: {cost_per_sqm:,.2f} TZS",
			"",
			f"✓ Updated:  {len(updated)}",
		]
		if adjusted_se:
			lines.append(f"   Stock Entries adjusted: {len(set(adjusted_se))}")
		for item in updated:
			lines.append(f"   {item}")
		lines += ["", f"→ Skipped:  {len(skipped)}"]
		for item in skipped:
			lines.append(f"   {item}")
		if failures:
			lines += ["", f"✗ Failed:   {len(failures)}  ← re-run to retry"]
			for item in failures:
				lines.append(f"   {item}")

		log = "\n".join(lines)
		cost_to_save = new_total_cost if not failures else None
		_finish(log, cost_to_save=cost_to_save)

	except Exception:
		frappe.log_error(
			title=f"Plot cost recalculation crashed: {land_acquisition}",
			message=frappe.get_traceback(),
			reference_doctype="Land Acquisition",
			reference_name=land_acquisition,
		)
		_finish("FAILED (crash) — see Error Log for details.")
