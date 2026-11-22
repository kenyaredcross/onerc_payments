// Copyright (c) 2026, OneRC and contributors
// For license information, please see license.txt

/**
 * A saved gateway message. "Process again" runs it through exactly the same code as
 * when it first arrived - useful after an Error, or for one still Awaiting
 * confirmation. Safe to repeat: a payment is never completed or notified twice.
 */
frappe.ui.form.on("OneRC Payment Callback", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) {
			return;
		}
		frm.add_custom_button(__("Process again"), () => {
			frappe.confirm(
				__("Process this message again? A payment is never completed or notified twice."),
				() =>
					frappe
						.call({
							method: "onerc_payments.api.v1.payment.reprocess_callback",
							args: { name: frm.doc.name },
							freeze: true,
						})
						.then((r) => {
							frappe.show_alert(
								{ message: __("Processed: {0}", [r?.message?.status || "?"]), indicator: "blue" },
								6,
							);
							frm.reload_doc();
						}),
			);
		});
	},
});
