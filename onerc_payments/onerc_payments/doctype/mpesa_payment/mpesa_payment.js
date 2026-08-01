// Copyright (c) 2026, OneRC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Mpesa Payment", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		// Once we have a receipt, surface it prominently at the top of the form
		// instead of leaving it buried among the read-only fields.
		if (frm.doc.mpesa_receipt_number) {
			const receipt = frappe.utils.escape_html(frm.doc.mpesa_receipt_number);
			frm.dashboard.set_headline(
				__("M-Pesa Receipt: {0}", [`<b>${receipt}</b>`]),
				"green"
			);
		}

		// For a payment that hasn't been confirmed yet (usually because the STK
		// callback never reached us), let an admin re-query Safaricom and sync the
		// record straight from the form.
		if (["Initiated", "Pending"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Check M-Pesa Status"), () => {
				frm.dashboard.show_progress(__("Checking with M-Pesa"), 50);
				frappe
					.call({
						method: "onerc_payments.api.v1.payment.reconcile_mpesa_payment",
						args: { mpesa_payment: frm.doc.name },
					})
					.then((r) => {
						frm.dashboard.hide_progress();
						const status = (r.message && r.message.status) || frm.doc.status;
						frappe.show_alert({
							message: __("M-Pesa status: {0}", [status]),
							indicator: status === "Completed" ? "green" : "orange",
						});
						frm.reload_doc();
					})
					.catch(() => frm.dashboard.hide_progress());
			});
		}
	},
});
