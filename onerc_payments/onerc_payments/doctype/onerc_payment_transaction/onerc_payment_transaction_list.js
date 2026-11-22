// Copyright (c) 2026, OneRC and contributors
// For license information, please see license.txt

/**
 * Working through the review list: filter on Needs Review, tick the payments that
 * have been dealt with, and close them together with one note (for example, a batch
 * of abandoned prompts that never appear on the statement).
 */
frappe.listview_settings["OneRC Payment Transaction"] = {
	add_fields: ["needs_review"],

	onload(listview) {
		listview.page.add_action_item(__("Close review"), () => {
			const names = listview.get_checked_items(true);
			if (!names.length) {
				frappe.msgprint(__("Tick the payments to close first."));
				return;
			}
			close_selected_reviews(names, () => listview.refresh());
		});
	},
};

function close_selected_reviews(names, after) {
	const dialog = new frappe.ui.Dialog({
		title: __("Close review for {0} payment(s)", [names.length]),
		fields: [
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("What was checked or done"),
				reqd: 1,
				description: __("Kept on each payment. Payments not on the review list are skipped."),
			},
		],
		primary_action_label: __("Close review"),
		primary_action(values) {
			frappe
				.call({
					method: "onerc_payments.api.v1.payment.close_review",
					args: { transaction_names: JSON.stringify(names), note: values.note },
					freeze: true,
				})
				.then((r) => {
					dialog.hide();
					frappe.show_alert(
						{ message: __("{0} review(s) closed.", [r?.message?.closed || 0]), indicator: "green" },
						5,
					);
					after && after();
				});
		},
	});
	dialog.show();
}
