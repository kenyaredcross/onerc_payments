// Copyright (c) 2026, Kelvin Njenga and contributors
// For license information, please see license.txt

/**
 * Confirming a manually collected payment — cash at a branch, or a bank
 * transfer — from the form rather than from a console.
 *
 * `confirm_payment()` has existed in `gateways/manual.py` since the driver was
 * written, and its own docstring said it "can be wired to a custom button on
 * the OneRC Payment Transaction form". Nothing ever wired it, so the only way
 * to settle a manual payment was `bench execute` or a `frappe.call` typed into
 * devtools. That is not a thing to ask a branch clerk to do, and a payment
 * method nobody can complete is a payment method that does not work.
 *
 * **Driver-specific behaviour, decided by the driver rather than by this file.**
 * The button is drawn only when the transaction's own gateway resolves to the
 * manual driver — read from the linked `OneRC Payment Gateway`, not from a
 * gateway name matched here. The app's first design commitment is that adding
 * or switching a driver is a settings change, and a form that hardcoded
 * "Manual" would break the moment a society named its gateway "Cash at Branch".
 *
 * The server re-checks everything this file decides: `confirm_payment()`
 * requires write permission on the transaction and refuses any status other
 * than Pending. The button is a convenience, never the authorisation.
 */

frappe.ui.form.on("OneRC Payment Transaction", {
	refresh(frm) {
		if (!frm.is_new() && frm.doc.needs_review) {
			offer_review_actions(frm);
		}

		if (!frm.is_new() && frm.doc.status === "Completed" && !frm.doc.gateway_receipt) {
			offer_receipt_recovery(frm, frm.doc.name);
		}

		if (frm.is_new() || frm.doc.status !== "Pending" || !frm.doc.gateway) {
			return;
		}

		// Only somebody who could perform the write should be offered it. The
		// server asks the same question again on the call.
		if (!frm.perm[0]?.write) {
			return;
		}

		frappe.db.get_value("OneRC Payment Gateway", frm.doc.gateway, "driver_class").then((r) => {
			const driver = r?.message?.driver_class || "";

			if (!driver.toLowerCase().includes("manual")) {
				return;
			}

			frm.add_custom_button(__("Confirm Payment"), () => confirm(frm)).addClass(
				"btn-primary",
			);
		});
	},
});

/**
 * Ask for the receipt and settle the transaction.
 *
 * The receipt number is required because it is the only thing tying this record
 * back to the paper the payer was given — a confirmation with no reference is
 * a payment nobody can trace or dispute later.
 */
function confirm(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Confirm Payment"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Record that this payment was collected outside the system. The source record is notified and updated, so only confirm what has actually been received.",
				)}</p>`,
			},
			{
				fieldname: "receipt_number",
				fieldtype: "Data",
				label: __("Receipt or Reference Number"),
				reqd: 1,
				description: __("The number on the receipt given to the payer, or the bank reference."),
			},
		],
		primary_action_label: __("Confirm Payment"),
		primary_action(values) {
			dialog.hide();
			frappe.dom.freeze(__("Confirming…"));

			frappe
				.call({
					method: "onerc_payments.gateways.manual.confirm_payment",
					args: {
						transaction_name: frm.doc.name,
						receipt_number: values.receipt_number,
					},
				})
				.then((r) => {
					if (r?.message?.success) {
						frappe.show_alert(
							{ message: r.message.message, indicator: "green" },
							5,
						);
					}
					frm.reload_doc();
				})
				.always(() => frappe.dom.unfreeze());
		},
	});

	dialog.show();
}

/**
 * A completed payment with no receipt: the gateway's callback never reached us.
 *
 * Two ways to fill the blank, each offered only when the server says the
 * gateway supports it: type the code from the payer's message, or ask the
 * gateway to look it up (the answer comes back on its own a little later).
 * The server checks permission, status and that the code is not already used
 * on another payment; nothing here is the authorisation.
 */
function offer_receipt_recovery(frm, transaction_name) {
	frappe
		.call({
			method: "onerc_payments.api.v1.payment.receipt_options",
			args: { transaction_name },
		})
		.then((r) => {
			const options = r?.message || {};
			if (!options.missing) {
				return;
			}

			frm.dashboard.set_headline(
				__("This payment went through, but its receipt number never arrived."),
				"orange",
			);

			const group = __("Receipt");

			if (options.can_request) {
				frm.add_custom_button(
					__("Look up with gateway"),
					() => {
						frappe
							.call({
								method: "onerc_payments.api.v1.payment.request_gateway_receipt",
								args: { transaction_name },
								freeze: true,
								freeze_message: __("Asking the gateway…"),
							})
							.then(() => {
								frappe.show_alert(
									{
										message: __(
											"Lookup sent. The receipt usually arrives within a minute; reload to see it.",
										),
										indicator: "blue",
									},
									8,
								);
								frm.reload_doc();
							});
					},
					group,
				);
			}

			if (options.can_enter) {
				frm.add_custom_button(
					__("Enter from payer's message"),
					() => enter_receipt(frm, transaction_name),
					group,
				);
			}
		});
}

function enter_receipt(frm, transaction_name) {
	const dialog = new frappe.ui.Dialog({
		title: __("Enter receipt number"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Copy the code at the start of the payer's confirmation message, for example TKB1A2C3D4. Check the amount in the message matches this payment.",
				)}</p>`,
			},
			{
				fieldname: "receipt",
				fieldtype: "Data",
				label: __("Receipt number"),
				reqd: 1,
			},
		],
		primary_action_label: __("Save receipt"),
		primary_action(values) {
			frappe
				.call({
					method: "onerc_payments.api.v1.payment.record_gateway_receipt",
					args: { transaction_name, receipt: values.receipt },
					freeze: true,
				})
				.then((r) => {
					dialog.hide();
					frappe.show_alert(
						{ message: __("Receipt {0} saved.", [r.message.receipt]), indicator: "green" },
						5,
					);
					frm.reload_doc();
				});
		},
	});

	dialog.show();
}

/**
 * A payment on the review list: say why, and offer what a person can do about it.
 *
 * The status, amount and receipt are read-only on this form, so these are the
 * ways to settle a payment by hand. "Confirm as paid" runs the same path as an
 * automatic confirmation, so the app the payment belongs to is told; editing the
 * status field used to skip that. The server checks permission and refuses a
 * receipt already used, or a record already paid by another payment.
 */
function offer_review_actions(frm) {
	const reasons = (frm.doc.review_reason || "").trim().split("\n");
	frm.set_intro(
		__("Needs review: {0}", [frappe.utils.escape_html(reasons[reasons.length - 1] || "")]),
		"red",
	);

	frappe
		.call({
			method: "onerc_payments.api.v1.payment.review_options",
			args: { transaction_name: frm.doc.name },
		})
		.then((r) => {
			const options = r?.message || {};
			const group = __("Review");

			if (options.can_confirm_paid) {
				frm.add_custom_button(__("Confirm as paid"), () => confirm_paid(frm), group);
			}
			if (options.can_mark_refunded) {
				frm.add_custom_button(__("Mark as refunded"), () => mark_refunded(frm), group);
			}
			if (options.can_close) {
				frm.add_custom_button(__("Close review"), () => close_review([frm.doc.name], () => frm.reload_doc()), group);
			}
		});
}

function mark_refunded(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Mark as refunded"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Records that this payment was paid back. No money is sent from here, and the app this payment belongs to is not told.",
				)}</p>`,
			},
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("How it was refunded"),
				reqd: 1,
				description: __("For example: M-Pesa reversal SLK4H8N2Q6 on 18 Nov."),
			},
		],
		primary_action_label: __("Mark as refunded"),
		primary_action(values) {
			frappe
				.call({
					method: "onerc_payments.api.v1.payment.mark_refunded",
					args: { transaction_name: frm.doc.name, note: values.note },
					freeze: true,
				})
				.then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Marked as refunded."), indicator: "green" }, 5);
					frm.reload_doc();
				});
		},
	});
	dialog.show();
}

function confirm_paid(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Confirm as paid"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted small">${__(
					"Only confirm a payment you have found on the statement. The app this payment belongs to is told it was paid, exactly as if the gateway had confirmed it.",
				)}</p>`,
			},
			{ fieldname: "receipt", fieldtype: "Data", label: __("Receipt number"), reqd: 1 },
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("How you checked it"),
				reqd: 1,
				description: __("For example: found on the M-Pesa statement of 12 Nov, line for 254712…"),
			},
		],
		primary_action_label: __("Confirm as paid"),
		primary_action(values) {
			frappe
				.call({
					method: "onerc_payments.api.v1.payment.confirm_paid_after_review",
					args: { transaction_name: frm.doc.name, receipt: values.receipt, note: values.note },
					freeze: true,
				})
				.then(() => {
					dialog.hide();
					frappe.show_alert({ message: __("Payment confirmed."), indicator: "green" }, 5);
					frm.reload_doc();
				});
		},
	});
	dialog.show();
}

function close_review(names, after) {
	const dialog = new frappe.ui.Dialog({
		title: __("Close review"),
		fields: [
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("What was checked or done"),
				reqd: 1,
				description: __("Kept on the payment, so the next person knows."),
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
