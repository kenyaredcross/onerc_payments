// Copyright (c) 2026, OneRC and contributors
// For license information, please see license.txt

frappe.query_reports["Payment Reconciliation"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "gateway",
			label: __("Gateway"),
			fieldtype: "Link",
			options: "OneRC Payment Gateway",
		},
		{
			fieldname: "source_app",
			label: __("App"),
			fieldtype: "Data",
		},
	],
};
