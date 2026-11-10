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
