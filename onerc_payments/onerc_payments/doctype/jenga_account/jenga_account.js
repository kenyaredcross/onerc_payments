// Copyright (c) 2026, OneRC and contributors
// For license information, please see license.txt

frappe.ui.form.on("Jenga Account", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Test Connection"), () => {
			frm.call("test_connection").then((r) => {
				if (r.message) frappe.msgprint(r.message.message);
			});
		});
		if (frm.doc.gateway) {
			frm.add_custom_button(__("Payment Gateway"), () =>
				frappe.set_route("Form", "OneRC Payment Gateway", frm.doc.gateway)
			);
		}
	},
});
