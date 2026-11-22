# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
The single entry point for all OneRC payment operations.

Every OneRC app that needs payments calls initiate_payment().
Nothing else. The gateway, credentials, and flow are handled here.
"""

import json

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import add_to_date, cint, flt, now_datetime

from onerc_payments.gateways import get_gateway
from onerc_payments.gateways.base import CONFIRMED, CONTRADICTED, UNAVAILABLE

TRANSACTION = "OneRC Payment Transaction"
CALLBACK_LOG = "OneRC Payment Callback"
ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}

# A payment in one of these has been decided. The browser poll answers them from our
# own record instead of asking the gateway again.
FINAL_STATUSES = ("Completed", "Failed", "Cancelled", "Refunded")
# Decided against the payer, but the gateway may yet confirm the money arrived.
RECOVERABLE_STATUSES = ("Failed", "Cancelled")

# The browser asks for a payment's status every few seconds while the payer types their
# PIN. Asking Safaricom every time adds nothing - the callback usually lands first - and
# Safaricom rate-limits the query. So the gateway is asked at most once per interval per
# payment; in between, the answer is our own record. The first poll always asks.
STATUS_QUERY_INTERVAL_SECONDS = 15

# A second request for the same thing, to the same phone, for the same amount, this soon
# after the first, is a double click or a second tab: it gets the first prompt back
# rather than a second prompt the payer could also pay.
REPEAT_PROMPT_WINDOW_SECONDS = 120

# Kept with each raw callback so its origin can be checked later. Never cookies or auth.
LOGGED_HEADERS = (
	"X-Forwarded-For",
	"X-Real-IP",
	"CF-Connecting-IP",
	"True-Client-IP",
	"User-Agent",
	"Content-Type",
)


def normalize_phone(phone):
	"""Normalize a Kenyan mobile number to E.164 (254XXXXXXXXX, 12 chars).

	Accepts '0712345678', '+254712345678', '254712345678', '712345678'.
	Returns the canonical form, or None for empty input. Other formats are
	returned digits-only so callers still get something to store.
	"""
	if not phone:
		return None
	digits = "".join(ch for ch in str(phone) if ch.isdigit())
	if not digits:
		return None
	if digits.startswith("254") and len(digits) == 12:
		return digits
	if digits.startswith("0") and len(digits) == 10:
		return "254" + digits[1:]
	if len(digits) == 9:
		return "254" + digits
	return digits


def _client_ip():
	"""Originating IP of the current request, for callback audit logging."""
	return getattr(frappe.local, "request_ip", None) or ""


def _settings_value(fieldname, default=None):
	"""A payment setting - or ``default`` while the field does not exist yet.

	New code can be running for a few minutes before ``migrate`` adds its fields (a deploy
	pulls the code first). Reading a missing field throws, and on the callback path that
	would lose the message's receipt; the default keeps the old behaviour instead.
	"""
	if not frappe.get_meta("OneRC Payment Settings").has_field(fieldname):
		return default
	value = frappe.db.get_single_value("OneRC Payment Settings", fieldname)
	return default if value is None else value


# ── STARTING A PAYMENT ─────────────────────────────────────────────────────


@frappe.whitelist()
def initiate_payment(
	amount,
	currency,
	direction,
	source_app,
	source_doctype,
	source_document,
	payer_name=None,
	payer_phone=None,
	payer_email=None,
	recipient_name=None,
	recipient_phone=None,
	recipient_account=None,
	metadata=None,
	gateway=None,
):
	"""
	Initiate a payment. Called by any OneRC app.

	`gateway` names which gateway to collect through. Omitted, the one set as
	active in OneRC Payment Settings is used, so every existing caller behaves
	exactly as it did. It exists because a site may keep several gateways live
	at once and let the payer pick: an organisation taking both M-Pesa and bank
	transfer offers both, and a single site-wide setting can only answer for one
	of them — which meant a payer with no mobile money account had no way to say
	so. A named gateway that is not active is refused; see `get_gateway`.

	A currency the gateway cannot collect is refused before anything is written:
	M-Pesa takes KES only, and used to send any other currency's amount as that many
	shillings. An inbound payment gets an account reference (see
	``onerc_payments.references``), which is what the payer sees as the account number.

	Returns:
	{
	    "transaction_id": "PAY-2026-00001",
	    "status": "Pending",
	    "gateway_reference": "...",
	    "message": "Check your phone and enter your M-Pesa PIN",
	}
	"""
	settings = frappe.get_single("OneRC Payment Settings")

	# Resolved before the transaction is written, so a bad gateway name is
	# refused without leaving an Initiated row nobody will ever resolve.
	driver = get_gateway(gateway)
	gateway_name = (gateway or "").strip() or settings.active_gateway

	currency = (currency or settings.default_currency or "KES").strip().upper()
	driver.check_currency(currency)

	phone = normalize_phone(payer_phone)

	if direction == "Inbound" and driver.prompts_payer:
		earlier = _recent_prompt(gateway_name, source_doctype, source_document, phone, amount)
		if earlier:
			return {
				"transaction_id": earlier.name,
				"status": earlier.status,
				"gateway_reference": earlier.gateway_reference,
				"message": (
					"A payment request was sent to this phone less than two minutes ago. "
					"Complete that prompt, or wait two minutes and try again."
				),
			}

	transaction = frappe.get_doc(
		{
			"doctype": TRANSACTION,
			"gateway": gateway_name,
			"direction": direction,
			"status": "Initiated",
			"amount": float(amount),
			"currency": currency,
			"source_app": source_app,
			"source_doctype": source_doctype,
			"source_document": source_document,
			"payer_name": payer_name,
			"phone_number": phone,
			"email": payer_email,
			"recipient_name": recipient_name,
			"recipient_phone": normalize_phone(recipient_phone),
			"recipient_account": recipient_account,
			"metadata": metadata,
		}
	)
	if direction == "Inbound":
		from onerc_payments.references import next_reference

		reference, description = next_reference(source_app, settings)
		transaction.account_reference = reference
		transaction.flags.reference_description = description
	transaction.insert(ignore_permissions=True)

	result = driver.initiate(transaction)

	if result.get("success"):
		transaction.status = "Pending"
		transaction.gateway_reference = result.get("gateway_reference")
	else:
		transaction.status = "Failed"
		transaction.failure_reason = result.get("message")
		if result.get("unconfirmed"):
			# The gateway may have prompted the payer even though we never heard back.
			# A callback for it can still be matched (``match_unconfirmed``).
			transaction.initiation_unconfirmed = 1

	# Gateway-specific initiation data (merchant ids, raw request/response) lives on
	# the per-gateway detail doctype, not the generic transaction. Best-effort: a
	# detail-record failure must never break initiating the payment.
	try:
		driver.record_initiation_details(transaction, result)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to record initiation details for {transaction.name}: {e}"
		)

	transaction.save(ignore_permissions=True)

	return {
		"transaction_id": transaction.name,
		"status": transaction.status,
		"gateway_reference": transaction.gateway_reference,
		"message": result.get("message"),
	}


def _recent_prompt(gateway_name, source_doctype, source_document, phone, amount):
	"""A Pending prompt for the same thing, phone and amount, sent moments ago."""
	rows = frappe.get_all(
		TRANSACTION,
		filters={
			"gateway": gateway_name,
			"direction": "Inbound",
			"status": "Pending",
			"source_doctype": source_doctype,
			"source_document": source_document,
			"creation": [">=", add_to_date(now_datetime(), seconds=-REPEAT_PROMPT_WINDOW_SECONDS)],
		},
		fields=["name", "status", "gateway_reference", "phone_number", "amount"],
		order_by="creation desc",
		limit=1,
	)
	if rows and rows[0].phone_number == phone and flt(rows[0].amount) == flt(amount):
		return rows[0]
	return None


@frappe.whitelist(allow_guest=True)
def get_gateway_capabilities(gateway=None):
	"""What a gateway can take, so a payment page can ask before it offers it.

	E.g. a donation page: if the donor picked USD, M-Pesa answers ["KES"], and the page
	can offer to switch to KES rather than fail at the prompt.
	"""
	driver = get_gateway(gateway)
	return {
		"label": driver.label,
		"currencies": list(driver.supported_currencies or []),
		"prompts_payer": bool(driver.prompts_payer),
	}


# ── CHECKING A PAYMENT'S STATUS ────────────────────────────────────────────


def _status_reply(transaction):
	return {
		"transaction_id": transaction.name,
		"status": transaction.status,
		"gateway_receipt": transaction.gateway_receipt,
		"failure_reason": transaction.failure_reason,
	}


def _status_query_key(transaction):
	# The creation time too: a name can come round again where transactions are rolled
	# back (tests), and must not inherit the last one's throttle.
	return f"onerc_payments:status_query:{transaction.name}:{transaction.creation}"


def _due_for_gateway_query(transaction):
	"""Whether the browser poll should ask the gateway now, or answer from our record."""
	key = _status_query_key(transaction)
	cache = frappe.cache()
	if cache.get_value(key, use_local_cache=False):
		return False
	cache.set_value(key, 1, expires_in_sec=STATUS_QUERY_INTERVAL_SECONDS)
	return True


@frappe.whitelist()
@rate_limit(limit=60, seconds=60)
def check_payment_status(transaction_id):
	"""
	Check the current status of a payment.
	Called by frontend polling loops (rate-limited per IP).

	A decided payment is answered from our own record. A Pending one is checked with
	the gateway at most every STATUS_QUERY_INTERVAL_SECONDS (see the constants above);
	in between, the callback has usually already updated our record.
	"""
	transaction = frappe.get_doc(TRANSACTION, transaction_id)
	if transaction.status != "Pending" or not _due_for_gateway_query(transaction):
		return _status_reply(transaction)
	return refresh_status(transaction)


def _reload_locked(transaction):
	"""Lock the transaction's row and reload it as committed now (SELECT ... FOR UPDATE).

	A plain reload after taking the lock is not enough: it can return this request's
	earlier snapshot, without what a concurrent callback has just committed.
	"""
	transaction.flags.for_update = True
	try:
		transaction.reload()
	finally:
		transaction.flags.for_update = False


def refresh_status(transaction, force=False):
	"""Ask the gateway for a transaction's status and apply the answer.

	Used by the browser poll (through check_payment_status, throttled), the hourly
	job and the desk re-query. ``force`` lets the desk re-query a payment marked
	Failed or Cancelled: if the gateway now says it was paid, it is recovered exactly
	as a confirmed late callback would recover it.
	"""
	previous = transaction.status
	if previous in FINAL_STATUSES and not (force and previous in RECOVERABLE_STATUSES):
		return _status_reply(transaction)

	# Snapshot the doc before the gateway sees it. check_status is free to enrich the
	# transaction in place (gateway reference, receipt, failure detail) even when the
	# status itself has not moved, and those writes must not be dropped.
	#
	# Compared by value rather than through a framework dirty-check: this line used to
	# call Document.is_dirty(), which upstream removed, and every poll then died with
	# AttributeError - a 500 on the endpoint the vendor portal polls to learn that its
	# payment cleared. A local snapshot cannot be taken away from us.
	before = transaction.as_dict(no_default_fields=True)
	# The gateway that *collected* this, not whichever one the site has active
	# now. Those were the same thing until a caller could name one, and they stop
	# being the same the moment an organisation takes both M-Pesa and bank
	# transfer: polling an M-Pesa payment with the manual driver would report it
	# as unresolved for ever. Falls back to the active gateway for a transaction
	# written before the column carried anything.
	gateway = get_gateway(transaction.gateway)
	result = gateway.check_status(transaction)
	new_status = result["status"]

	if new_status == previous:
		if transaction.as_dict(no_default_fields=True) != before:
			transaction.save(ignore_permissions=True)
		return _status_reply(transaction)

	# The status moves. Lock the row, and if a callback moved it while we were asking
	# the gateway, its answer stands - applying ours too would notify the source twice.
	_reload_locked(transaction)
	if transaction.status != previous:
		return _status_reply(transaction)

	if previous in RECOVERABLE_STATUSES:
		if new_status == "Completed":
			# The gateway's own status query is the confirmation.
			_recover_paid(gateway, transaction, result, data=None)
		return _status_reply(transaction)

	transaction.status = new_status
	if result.get("gateway_receipt"):
		_take_receipt(transaction, result["gateway_receipt"], now_datetime())
	if result.get("failure_reason"):
		transaction.failure_reason = result["failure_reason"]

	# Keep the per-gateway detail record (e.g. Mpesa Payment) in step with the
	# transaction. Without this, a payment resolved by polling rather than by the
	# callback would leave its detail record stuck on "Initiated". Best-effort:
	# a detail-sync failure must never block resolving the payment itself.
	try:
		gateway.record_status_update(transaction, result)
	except Exception as e:
		frappe.logger().error(f"onerc_payments: failed to sync gateway detail for {transaction.name}: {e}")

	transaction.save(ignore_permissions=True)

	if new_status == "Completed":
		_flag_if_paid_twice(transaction)
		_notify_source_app(transaction)

	return _status_reply(transaction)


@frappe.whitelist()
def reconcile_mpesa_payment(mpesa_payment):
	"""Desk action: re-query the gateway for a single Mpesa Payment and sync it.

	Lets an admin resolve a record that is still showing "Initiated"/"Pending"
	(e.g. because the STK callback never reached us) straight from the form - or a
	payment marked Failed that the payer says went through: if Safaricom now confirms
	it, it is recovered (see ``_recover_paid``).
	Note: the STK status query returns the payment's status but not the M-Pesa
	receipt number - only the callback carries that - so this confirms whether a
	payment went through, but the receipt itself still depends on the callback.
	"""
	frappe.has_permission("Mpesa Payment", "read", doc=mpesa_payment, throw=True)
	transaction_id = frappe.db.get_value("Mpesa Payment", mpesa_payment, "payment_transaction")
	if not transaction_id:
		frappe.throw("This Mpesa Payment is not linked to a payment transaction.")
	return refresh_status(frappe.get_doc(TRANSACTION, transaction_id), force=True)


# ── CALLBACKS: EVERY MESSAGE IS KEPT, THEN PROCESSED ───────────────────────
#
# Each message a gateway posts is saved to OneRC Payment Callback and committed before
# anything else happens. Whatever goes wrong afterwards - a crash, a bug, a payment it
# matches nothing - the message itself is never lost, and can be processed again from
# its form ("Process again") or by the retry job.


def _read_body():
	"""(raw text, parsed dict) of the current request."""
	raw = getattr(frappe.request, "data", None) if frappe.request else None
	if isinstance(raw, bytes):
		text = raw.decode("utf-8", "replace")
	else:
		text = raw or ""

	try:
		data = json.loads(text) if text else {}
	except Exception:
		data = {}

	if not data:
		data = {k: v for k, v in (frappe.form_dict or {}).items() if k != "cmd"}
		if data and not text:
			text = frappe.as_json(data)

	return text, data if isinstance(data, dict) else {}


def _header(name):
	"""A request header, or None - also when there is no request or no headers."""
	try:
		headers = getattr(frappe.request, "headers", None) if frappe.request else None
		return headers.get(name) if headers else None
	except Exception:
		return None


# A gateway's message is a few kilobytes. The endpoint is open to anyone, so a message far
# larger than that is kept only in part.
MAX_LOGGED_PAYLOAD = 64 * 1024

# "Not asked yet", as opposed to an answer of None ("this gateway cannot be asked").
_NOT_ASKED = object()


def _open_log(kind, raw_text, gateway_name, reference=None, trusted=False):
	"""Save the message as received and commit, before it is processed.

	The reference, and whether the sender's address looked like the gateway's, are
	stored now rather than during processing: a message whose processing crashes (and is
	rolled back) can still be found, and is judged the same way when processed again.

	Best-effort by design: if the log itself cannot be written (say, during a deploy
	before ``migrate`` has created it), the message is still processed.
	"""
	try:
		headers = {h: _header(h) for h in LOGGED_HEADERS}
		payload = raw_text or ""
		if len(payload) > MAX_LOGGED_PAYLOAD:
			payload = f"{payload[:MAX_LOGGED_PAYLOAD]}\n[cut: {len(raw_text)} characters received]"
		log = frappe.get_doc(
			{
				"doctype": CALLBACK_LOG,
				"kind": kind,
				"status": "Received",
				"received_on": now_datetime(),
				"gateway": gateway_name,
				"gateway_reference": reference,
				"source_ip": _client_ip(),
				"source_trusted": 1 if trusted else 0,
				"headers": frappe.as_json({k: v for k, v in headers.items() if v}),
				"payload": payload,
				"attempts": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return log.name
	except Exception:
		frappe.db.rollback()
		frappe.logger().error(f"onerc_payments: could not save a {kind} callback before processing it")
		return None


def _update_log(log, **values):
	if not log or not values:
		return
	try:
		frappe.db.set_value(CALLBACK_LOG, log, values, update_modified=False)
	except Exception:
		frappe.logger().error(f"onerc_payments: could not update callback log {log}")


def _run_callback(kind, data, gateway_name, log, stored_trust=None):
	"""Process a saved message; on any error, roll back and keep the error with it.

	``stored_trust`` is set when a saved message is processed again: the request it came
	in on is gone, so whether its sender looked like the gateway is read from the log.
	"""
	try:
		if kind == "Receipt lookup":
			_process_receipt_result(data, log)
		else:
			_process_payment_callback(data, gateway_name, log, stored_trust)
		frappe.db.commit()
	except Exception:
		error = frappe.get_traceback()
		frappe.db.rollback()
		# The rollback also undid the links processing had put on the log. Put back what
		# can be worked out without processing, so the failed message is easy to find.
		values = {"status": "Error", "error": error}
		if kind == "Payment":
			reference = _reference_or_none(gateway_name, data)
			if reference:
				values["gateway_reference"] = reference
				transaction_name = frappe.db.get_value(TRANSACTION, {"gateway_reference": reference}, "name")
				if transaction_name:
					values["payment_transaction"] = transaction_name
		_update_log(log, **values)
		frappe.db.commit()
		frappe.log_error(
			title=f"Payment callback could not be processed ({kind})",
			reference_doctype=CALLBACK_LOG if log else None,
			reference_name=log,
		)
	return frappe.db.get_value(CALLBACK_LOG, log, "status") if log else None


@frappe.whitelist(allow_guest=True)
def payment_callback(gateway_name=None, **kwargs):
	"""Where a gateway tells us how a payment ended (M-Pesa: the STK callback).

	Always answers Accepted: the message is saved first, so there is nothing for the
	gateway to retry, and a failure on our side is ours to process again.
	"""
	raw, data = _read_body()
	if not gateway_name:
		gateway_name = data.get("gateway_name", "") if isinstance(data, dict) else ""
	log = _open_log(
		"Payment",
		raw,
		gateway_name,
		_reference_or_none(gateway_name, data),
		_sender_trusted(gateway_name),
	)
	_run_callback("Payment", data, gateway_name, log)
	return ACCEPTED


def _reference_or_none(gateway_name, data):
	try:
		return _extract_gateway_reference(gateway_name, data) or None
	except Exception:
		return None


def _sender_trusted(gateway_name):
	"""Whether the request's sender is at an address the named gateway publishes."""
	try:
		return _source_trusted(get_gateway(gateway_name or None))
	except Exception:
		return False


def _process_payment_callback(data, gateway_name, log, stored_trust=None):
	live = stored_trust is None  # False when a saved message is processed again
	gateway_reference = _extract_gateway_reference(gateway_name, data)

	if not gateway_reference:
		_update_log(
			log,
			status="Ignored",
			note="No gateway reference in the message, so it cannot belong to a payment.",
		)
		if live:
			frappe.log_error(
				title="M-Pesa callback: no reference found",
				message=(
					f"A callback was received but no gateway reference could be "
					f"extracted, so it could not be matched to a payment.\n"
					f"gateway={gateway_name}\ndata={data}"
				),
			)
		return

	_update_log(log, gateway_reference=gateway_reference)

	transaction_name = frappe.db.get_value(TRANSACTION, {"gateway_reference": gateway_reference}, "name")
	adopting = False
	if not transaction_name:
		# Our request to start this payment got no reply, so we never learned the
		# reference Safaricom gave it. Match it by what the callback does carry.
		transaction_name = _match_unconfirmed(data)
		adopting = bool(transaction_name)

	if not transaction_name:
		_update_log(
			log,
			status="Unmatched",
			note=(
				f"No payment has reference {gateway_reference}, and it fitted no payment whose start "
				"went unanswered. Looked for again every few minutes for half an hour, in case the "
				"payment had not been saved yet when this arrived."
			),
		)
		if live:
			frappe.log_error(
				title="M-Pesa callback: no matching transaction",
				message=(
					f"A callback with reference {gateway_reference} could not be linked "
					f"to any initiated payment. It is kept in {CALLBACK_LOG} {log or ''}."
				),
			)
		return

	# First an unlocked look. If the gateway has to be asked whether this "paid" is true,
	# it is asked now, before the row is locked: the answer can take seconds, and the
	# payer's own status poll needs the same lock to record its result.
	snapshot = frappe.get_doc(TRANSACTION, transaction_name)
	# The gateway that collected this payment, never one named by the request.
	gateway = get_gateway(snapshot.gateway)
	trusted = _source_trusted(gateway) if live else bool(cint(stored_trust))

	confirmation = _NOT_ASKED
	if snapshot.status not in ("Completed", "Refunded") and (
		gateway.handle_callback(data, snapshot).get("status") == "Completed"
	):
		confirmation = gateway.confirm_callback(snapshot, {}, data)
		_update_log(log, gateway_confirmation=confirmation or "Not needed")

	if adopting and confirmation != CONFIRMED:
		# Only a message the gateway confirms may claim a payment: the claim is permanent
		# (the reference is written onto it), and a wrong one would turn away the real one.
		if confirmation == UNAVAILABLE:
			_update_log(
				log,
				status="Awaiting confirmation",
				note=f"Fits {transaction_name} by phone and amount; waiting for {gateway.label} to confirm it.",
			)
		else:
			_update_log(
				log,
				status="Unmatched",
				note=f"Fits {transaction_name} by phone and amount, but {gateway.label} does not confirm it was paid.",
			)
		return

	# Idempotency: Safaricom retries callbacks until it gets a 200, and a retry
	# may race the original. Lock the row (SELECT ... FOR UPDATE) so concurrent
	# duplicates serialize; if the transaction is already resolved we must not run
	# the status transition again, or the source-app hook (receipt / GL posting)
	# fires twice. A locking read also returns the row as committed now - a plain
	# read could return this request's earlier snapshot, missing a receipt a
	# concurrent duplicate has just stored.
	#
	# But we cannot simply drop the callback either. The STK *query* used by the
	# status poll resolves a payment to Completed while carrying no receipt - only
	# this callback ever carries MpesaReceiptNumber. The poll (browser, every few
	# seconds; scheduler, every few minutes) usually wins that race, so returning
	# here threw the receipt away for good, and every payment showed a blank M-Pesa
	# code. Absorb what only the callback knows, then stop.
	transaction = frappe.get_doc(TRANSACTION, transaction_name, for_update=True)
	current_status = transaction.status
	_update_log(log, payment_transaction=transaction.name, source_trusted=1 if trusted else 0)

	if adopting:
		if transaction.gateway_reference:
			_update_log(
				log,
				status="Unmatched",
				note=f"{transaction.name} was matched to {transaction.gateway_reference} meanwhile.",
			)
			return
		transaction.db_set("gateway_reference", gateway_reference, update_modified=False)
		transaction.add_comment(
			"Info",
			f"Matched to {gateway.label} request {gateway_reference} by phone and amount, and confirmed "
			"by the gateway: its reply to the request that started this payment never arrived.",
		)

	result = gateway.handle_callback(data, transaction)
	_update_log(
		log,
		status=_apply_callback(
			gateway, transaction, current_status, result, data, log, confirmation, trusted
		),
	)


def _source_trusted(gateway):
	try:
		return bool(gateway.verify_callback_source())
	except Exception:
		return False


def _match_unconfirmed(data):
	"""Ask each active driver whether the callback fits a payment that got no reply."""
	by_driver = {}
	for row in frappe.get_all(
		"OneRC Payment Gateway", filters={"is_active": 1}, fields=["name", "driver_class"]
	):
		by_driver.setdefault(row.driver_class, []).append(row.name)

	for names in by_driver.values():
		try:
			match = get_gateway(names[0]).match_unconfirmed(data, names)
		except Exception:
			frappe.logger().error("onerc_payments: matching an unconfirmed payment failed")
			continue
		if match:
			return match
	return None


def _apply_callback(
	gateway, transaction, current_status, result, data, log, confirmation=_NOT_ASKED, trusted=True
):
	"""Decide what a callback does to a transaction. Returns the callback log status.

	A "paid" callback moves money-relevant state only once the gateway confirms it
	(``confirm_callback``), with one exception kept for a safe rollout: in "Watch only"
	mode a Pending payment completes as it always did - from a sender at the gateway's
	published addresses - and anything the gateway did not confirm is flagged for review.
	"""
	paid = result.get("status") == "Completed"
	if not paid and result.get("status") not in ("Failed", "Cancelled"):
		# A message that decides nothing (a gateway that takes no callbacks, a malformed
		# body) changes nothing - and is not a failure to put on the review list.
		_update_log(
			log,
			note=f"The message decides nothing about the payment ({result.get('failure_reason') or result.get('status')}).",
		)
		return "Ignored"

	if paid and result.get("amount_mismatch"):
		_flag_review(transaction, result["amount_mismatch"])

	if current_status == "Completed":
		if paid:
			_absorb_late_receipt(gateway, data, transaction, result)
			return "Processed"
		_flag_review(
			transaction,
			f"{gateway.label} sent a failure ({result.get('failure_reason') or result.get('result_code')}) for a "
			"payment already confirmed as paid. Check it against the statement.",
		)
		return "Needs review"

	if current_status == "Refunded":
		_absorb_late_receipt(gateway, data, transaction, result)
		return "Processed"

	if not paid:
		if current_status in ("Pending", "Initiated"):
			transaction.status = result["status"]
			if result.get("failure_reason"):
				transaction.failure_reason = result["failure_reason"]
			_record_details(gateway, data, transaction)
			transaction.save(ignore_permissions=True)
		else:
			# Already Failed/Cancelled: the message is evidence, nothing more.
			_record_details(gateway, data, transaction)
			_persist_detail_link(transaction)
		return "Processed"

	if confirmation is _NOT_ASKED:
		confirmation = gateway.confirm_callback(transaction, result, data)
		_update_log(log, gateway_confirmation=confirmation or "Not needed")

	if current_status in RECOVERABLE_STATUSES:
		return _apply_late_payment(gateway, transaction, current_status, result, data, confirmation)
	return _apply_payment(gateway, transaction, result, data, confirmation, trusted)


def _apply_payment(gateway, transaction, result, data, confirmation, trusted=True):
	"""A "paid" callback for a payment still Pending."""
	confirmed = confirmation in (None, CONFIRMED)
	enforce = _settings_value("callback_confirmation") == "Enforce"

	# Watch only keeps the gate that came before confirmation, too: an unconfirmed message
	# completes a payment only if it came from the gateway's published addresses. So
	# Watch only is never weaker than the old behaviour, only better informed.
	if confirmed or (not enforce and trusted):
		if not confirmed:
			_flag_review(
				transaction,
				f"A callback said this was paid, but {gateway.label} did not confirm it when asked "
				f"({confirmation}). It was completed anyway because payment confirmation checks are "
				"set to Watch only. Check it against the statement.",
			)
		transaction.status = "Completed"
		_take_receipt(transaction, result.get("gateway_receipt"), result.get("transaction_date"))
		# Capture the full gateway payload (e.g. the M-Pesa receipt + metadata) into a
		# dedicated detail record and link it here. Best-effort: a failure to record the
		# detail must never block confirming the payment itself.
		_record_details(gateway, data, transaction)
		transaction.save(ignore_permissions=True)
		_flag_if_paid_twice(transaction)
		frappe.db.commit()
		_notify_source_app(transaction)
		return "Processed" if confirmed else "Needs review"

	# Not confirmed, and either Enforce or a sender we do not recognise. Nothing is
	# written from the message itself - the detail record included - since its content
	# is unproven. It is kept on the callback log.
	if confirmation == UNAVAILABLE:
		return "Awaiting confirmation"

	_flag_review(
		transaction,
		f"A callback said this was paid, but {gateway.label} says it was not, so it was not marked "
		"paid. If the payer has an M-Pesa message for it, check the statement and confirm it here.",
	)
	return "Needs review"


def _apply_late_payment(gateway, transaction, current_status, result, data, confirmation):
	"""A "paid" callback for a payment we had marked Failed or Cancelled."""
	if confirmation is None:
		# A gateway that cannot be asked: as before, take the evidence, change nothing.
		_absorb_late_receipt(gateway, data, transaction, result)
		return "Processed"

	if confirmation == UNAVAILABLE:
		return "Awaiting confirmation"

	if confirmation == CONTRADICTED:
		_flag_review(
			transaction,
			f"A callback said this was paid after it was marked {current_status}, but {gateway.label} "
			"says it was not paid. Nothing was changed.",
		)
		return "Needs review"

	if not cint(_settings_value("recover_failed_payments")):
		_record_details(gateway, data, transaction)
		_persist_detail_link(transaction)
		_flag_review(
			transaction,
			f"{gateway.label} confirms this was paid after it was marked {current_status}. Automatic "
			"recovery is switched off, so it was left as it was: confirm it as paid from this form.",
		)
		return "Needs review"

	_recover_paid(gateway, transaction, result, data)
	return "Processed"


def _recover_paid(gateway, transaction, result, data=None):
	"""Complete a payment that was marked Failed/Cancelled but the gateway confirms was paid.

	The money arrived, so the record must say Completed. The source app is told -
	unless its record was already paid by another payment (the payer retried after
	the "failure" and paid again). Then it is not told twice: the payment is flagged as
	paid twice so one can be refunded.
	"""
	previous = transaction.status
	earlier_reason = transaction.failure_reason
	other = _other_completed_payment(transaction)

	transaction.status = "Completed"
	transaction.failure_reason = None
	_take_receipt(transaction, result.get("gateway_receipt"), result.get("transaction_date"))
	if data is not None:
		_record_details(gateway, data, transaction)
	else:
		try:
			gateway.record_status_update(transaction, result)
		except Exception as e:
			frappe.logger().error(
				f"onerc_payments: failed to sync gateway detail for {transaction.name}: {e}"
			)
	transaction.save(ignore_permissions=True)
	transaction.add_comment(
		"Info",
		f"{gateway.label} confirmed this payment went through after it was recorded as {previous}"
		f"{f' ({earlier_reason})' if earlier_reason else ''}. Marked Completed.",
	)

	if other:
		_flag_review(
			transaction,
			f"{gateway.label} confirmed this payment after it was marked {previous}, but "
			f"{transaction.source_doctype} {transaction.source_document} had already been paid by {other}. "
			"The payer paid twice: refund one. The app was not told about this payment, so nothing "
			"was activated twice.",
		)
		frappe.db.commit()
		return

	# Told it failed, a payer usually tries again - and an app may start the retry on a
	# new record (a new order, a new batch), which the check above cannot see.
	similar = _similar_completed_payment(transaction)
	if similar:
		_flag_review(
			transaction,
			f"{gateway.label} confirmed this payment after it was marked {previous}, and the same phone "
			f"also paid {similar}, the same amount to the same app, within a day. If both were for the "
			"same thing, the payer paid twice: refund one.",
		)

	frappe.db.commit()
	_notify_source_app(transaction)


def _similar_completed_payment(transaction):
	"""Another completed payment from the same phone, to the same app, for the same amount,
	created within a day of this one."""
	if not transaction.phone_number:
		return None
	return frappe.db.get_value(
		TRANSACTION,
		{
			"source_app": transaction.source_app,
			"direction": transaction.direction,
			"phone_number": transaction.phone_number,
			"amount": transaction.amount,
			"status": "Completed",
			"name": ["!=", transaction.name],
			"creation": [
				"between",
				[add_to_date(transaction.creation, hours=-24), add_to_date(transaction.creation, hours=24)],
			],
		},
		"name",
	)


def _record_details(gateway, data, transaction):
	try:
		gateway.record_payment_details(data, transaction)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to record gateway payment details for {transaction.name}: {e}"
		)


def _persist_detail_link(transaction):
	"""Save the detail link ``record_payment_details`` set, on a transaction not otherwise saved."""
	if transaction.get("gateway_detail") and transaction.gateway_detail != frappe.db.get_value(
		TRANSACTION, transaction.name, "gateway_detail"
	):
		transaction.db_set(
			{
				"gateway_detail_doctype": transaction.gateway_detail_doctype,
				"gateway_detail": transaction.gateway_detail,
			},
			update_modified=False,
		)


def _absorb_late_receipt(gateway, data, transaction, result):
	"""Take the receipt off a callback for a payment the poll already resolved.

	The status transition and ``on_payment_confirmed`` have already happened, so we
	deliberately do NOT re-run them - that would post a receipt or a GL entry twice.
	What we do take is the one thing the callback alone can tell us: the M-Pesa code
	(and the full payload, onto the gateway detail record). The source app is then
	given the receipt it never got, via the optional ``on_payment_receipt`` hook.
	"""
	receipt = result.get("gateway_receipt")
	stored = False

	if receipt and not transaction.gateway_receipt:
		other = _receipt_used_by(receipt, transaction.name)
		if other:
			_flag_review(
				transaction, f"Receipt {receipt} is already recorded on {other}, so it was not stored here."
			)
		else:
			_store_receipt(transaction, receipt, result.get("transaction_date") or now_datetime())
			stored = True

	# Best-effort: the raw payload / result codes belong on the per-gateway detail
	# record whether or not a receipt came with them (a failed callback is evidence too).
	_record_details(gateway, data, transaction)
	_persist_detail_link(transaction)

	frappe.db.commit()

	if stored:
		_notify_source_receipt(transaction, receipt)


def _take_receipt(transaction, receipt, transaction_date=None):
	"""Put a receipt on a transaction about to be saved - unless it already has one, or
	another transaction already carries this code (then it is flagged instead)."""
	if not receipt or transaction.gateway_receipt:
		return False
	other = _receipt_used_by(receipt, transaction.name)
	if other:
		_flag_review(
			transaction, f"Receipt {receipt} is already recorded on {other}, so it was not stored here."
		)
		return False
	transaction.gateway_receipt = receipt
	transaction.transaction_date = transaction_date or now_datetime()
	return True


def _store_receipt(transaction, receipt, transaction_date=None):
	"""Write a receipt onto a transaction that is already resolved.

	``db_set`` without touching ``modified``: the status transition happened earlier
	and the record should still say when. ``transaction_date`` is set only when the
	source knows when the money moved; a code typed in by staff does not, and the
	source app then falls back to the date it recorded the payment.
	"""
	transaction.db_set("gateway_receipt", receipt, update_modified=False)
	if transaction_date:
		transaction.db_set("transaction_date", transaction_date, update_modified=False)
	transaction.gateway_receipt = receipt


# ── THE REVIEW LIST ────────────────────────────────────────────────────────
#
# Anything a person should look at - a payment confirmed twice, a callback the gateway
# would not confirm, a payment with no final answer after a day, a source app that
# failed to record a payment - sets ``needs_review`` with the reason. Filter the
# transaction list on Needs Review to work through them.


def _flag_review(transaction, reason):
	"""Put a payment on the review list, keeping every reason it was put there for.

	Never raises: a payment that cannot be flagged (say, new code running before
	``migrate`` has added the review fields) is still completed, and the reason logged.
	"""
	name = transaction if isinstance(transaction, str) else transaction.name
	try:
		_write_review_flag(transaction, name, reason)
	except Exception:
		frappe.logger().error(f"onerc_payments: could not flag {name} for review: {reason}")


def _write_review_flag(transaction, name, reason):
	if not frappe.get_meta(TRANSACTION).has_field("needs_review"):
		frappe.logger().error(f"onerc_payments: {name} needs review (fields not migrated yet): {reason}")
		return
	current = frappe.db.get_value(TRANSACTION, name, ["needs_review", "review_reason"], as_dict=True) or {}
	history = current.get("review_reason") or ""
	if current.get("needs_review") and reason in history:
		return  # the same reason again (e.g. a retried callback)

	stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	values = {
		"needs_review": 1,
		"review_reason": f"{history}\n{stamp}: {reason}".strip(),
		"reviewed_by": None,
		"reviewed_on": None,
	}
	frappe.db.set_value(TRANSACTION, name, values, update_modified=False)
	if not isinstance(transaction, str):
		# Keep the in-memory doc in step, or a later save() would write the flag back off.
		transaction.update(values)


def _other_completed_payment(transaction):
	"""Another completed payment for the same source record, if there is one."""
	return frappe.db.get_value(
		TRANSACTION,
		{
			"source_doctype": transaction.source_doctype,
			"source_document": transaction.source_document,
			"direction": transaction.direction,
			"status": "Completed",
			"name": ["!=", transaction.name],
		},
		"name",
	)


def _flag_if_paid_twice(transaction):
	other = _other_completed_payment(transaction)
	if other:
		_flag_review(
			transaction,
			f"{transaction.source_doctype} {transaction.source_document} was also paid by {other}. Check "
			"whether the payer was charged twice and refund one.",
		)
	return other


@frappe.whitelist(methods=["POST"])
def close_review(transaction_names, note):
	"""Take payments off the review list once a person has dealt with them."""
	names = (
		frappe.parse_json(transaction_names)
		if str(transaction_names).startswith("[")
		else [transaction_names]
	)
	note = (note or "").strip()
	if not note:
		frappe.throw("Say what was checked or done, so the next person knows.")

	closed = 0
	for name in names:
		transaction = frappe.get_doc(TRANSACTION, name)
		frappe.has_permission(TRANSACTION, ptype="write", doc=transaction, throw=True)
		if not transaction.needs_review:
			continue
		transaction.db_set(
			{
				"needs_review": 0,
				"reviewed_by": frappe.session.user,
				"reviewed_on": now_datetime(),
				"review_note": note,
			},
			update_modified=False,
		)
		transaction.add_comment(
			"Info", f"Review closed by {frappe.utils.get_fullname(frappe.session.user)}: {note}"
		)
		closed += 1
	return {"closed": closed}


@frappe.whitelist()
def review_options(transaction_name):
	"""What the form may offer for a payment on the review list."""
	transaction = frappe.get_doc(TRANSACTION, transaction_name)
	frappe.has_permission(TRANSACTION, ptype="read", doc=transaction, throw=True)
	can_write = frappe.has_permission(TRANSACTION, ptype="write", doc=transaction)
	try:
		takes_receipt = bool(getattr(get_gateway(transaction.gateway), "accepts_entered_receipt", False))
	except Exception:
		takes_receipt = False
	return {
		"can_close": bool(can_write and transaction.needs_review),
		"can_confirm_paid": bool(
			can_write
			and takes_receipt
			and transaction.status in ("Initiated", "Pending", "Failed", "Cancelled")
		),
		"can_mark_refunded": bool(
			can_write and transaction.needs_review and transaction.status == "Completed"
		),
	}


@frappe.whitelist(methods=["POST"])
def mark_refunded(transaction_name, note):
	"""Record that a completed payment was paid back - say one of a double payment.

	Only the record changes: no money moves from here, and the source app is not told
	(no app has a refund hook). The status field is read-only on the form, so this is
	the one way to set Refunded.
	"""
	transaction = frappe.get_doc(TRANSACTION, transaction_name)
	frappe.has_permission(TRANSACTION, ptype="write", doc=transaction, throw=True)
	note = (note or "").strip()
	if not note:
		frappe.throw("Say how it was refunded, e.g. the reversal's M-Pesa code.")

	_reload_locked(transaction)
	if transaction.status != "Completed":
		frappe.throw(f"{transaction.name} is {transaction.status}; only a completed payment can be refunded.")

	user = frappe.session.user
	transaction.status = "Refunded"
	transaction.needs_review = 0
	transaction.reviewed_by = user
	transaction.reviewed_on = now_datetime()
	transaction.review_note = note
	transaction.save(ignore_permissions=True)
	transaction.add_comment("Info", f"Marked refunded by {frappe.utils.get_fullname(user)}: {note}")
	return {"status": transaction.status}


@frappe.whitelist(methods=["POST"])
def confirm_paid_after_review(transaction_name, receipt, note):
	"""A person confirms, with the payer's receipt, that a payment went through.

	For what the automatic paths could not settle - say a payment marked Failed that
	the statement shows was paid. Replaces editing the status by hand, which skipped the
	source app entirely. Refused if the source record was already paid by another
	payment: then this one is a double payment to refund, not to confirm.
	"""
	transaction = frappe.get_doc(TRANSACTION, transaction_name)
	frappe.has_permission(TRANSACTION, ptype="write", doc=transaction, throw=True)
	note = (note or "").strip()
	if not note:
		frappe.throw("Say how the payment was checked, e.g. which statement line.")

	_reload_locked(transaction)
	if transaction.status not in ("Initiated", "Pending", "Failed", "Cancelled"):
		frappe.throw(f"{transaction.name} is {transaction.status}; only an unpaid payment can be confirmed.")

	gateway = get_gateway(transaction.gateway)
	code = gateway.normalise_receipt(receipt)
	used = _receipt_used_by(code, transaction.name)
	if used:
		frappe.throw(f"Receipt {code} is already recorded on {used}. Check the code with the payer.")
	paid_by = _other_completed_payment(transaction)
	if paid_by:
		frappe.throw(
			f"{transaction.source_doctype} {transaction.source_document} was already paid by {paid_by}. "
			"Refund this payment rather than confirming it."
		)

	previous = transaction.status
	user = frappe.session.user
	transaction.status = "Completed"
	transaction.gateway_receipt = code
	transaction.failure_reason = None
	transaction.needs_review = 0
	transaction.reviewed_by = user
	transaction.reviewed_on = now_datetime()
	transaction.review_note = note
	try:
		gateway.record_entered_receipt(transaction, code)
	except Exception as e:
		frappe.logger().error(f"onerc_payments: failed to note entered receipt on {transaction.name}: {e}")
	transaction.save(ignore_permissions=True)
	transaction.add_comment(
		"Info",
		f"Confirmed as paid by {frappe.utils.get_fullname(user)} with receipt {code} (was {previous}): {note}",
	)
	frappe.db.commit()

	_notify_source_app(transaction)
	return {"status": transaction.status, "receipt": code}


@frappe.whitelist(methods=["POST"])
def reprocess_callback(name):
	"""Desk action: process a saved callback again (after a fix, or a gateway outage)."""
	frappe.only_for("System Manager")
	return {"status": reprocess_saved_callback(name)}


def reprocess_saved_callback(name):
	log = frappe.db.get_value(
		CALLBACK_LOG, name, ["name", "kind", "payload", "gateway", "attempts", "source_trusted"], as_dict=True
	)
	if not log:
		frappe.throw(f"Callback {name} not found.")
	try:
		data = json.loads(log.payload) if log.payload else {}
	except ValueError:
		data = {}
	_update_log(log.name, attempts=cint(log.attempts) + 1, error=None)
	# Count the attempt even if it fails: a failure rolls back everything after this.
	frappe.db.commit()
	return _run_callback(
		log.kind, data if isinstance(data, dict) else {}, log.gateway, log.name, cint(log.source_trusted)
	)


# ── RECOVERING A RECEIPT THE CALLBACK NEVER DELIVERED ──────────────────────
#
# A payment confirmed by the status poll has no receipt until the callback comes.
# If the callback is lost, staff can type the code from the payer's message, or
# ask the gateway to look it up. Either way the receipt only ever fills a blank:
# a receipt already on a transaction is never replaced, and the source app hears
# about it through ``on_payment_receipt`` exactly as for a late callback.


def _assert_receipt_missing(transaction):
	if transaction.status != "Completed":
		frappe.throw(
			f"{transaction.name} is {transaction.status}, not Completed. Only a "
			"completed payment can be given its receipt."
		)
	if transaction.gateway_receipt:
		frappe.throw(f"{transaction.name} already has receipt {transaction.gateway_receipt}.")


def _receipt_used_by(receipt, transaction_name):
	"""The other transaction that already carries this receipt, if any."""
	return frappe.db.get_value(
		TRANSACTION,
		{"gateway_receipt": receipt, "name": ("!=", transaction_name)},
		"name",
	)


@frappe.whitelist()
def receipt_options(transaction_name):
	"""What the desk may offer for a transaction with no receipt."""
	transaction = frappe.get_doc(TRANSACTION, transaction_name)
	frappe.has_permission(TRANSACTION, ptype="read", doc=transaction, throw=True)

	missing = transaction.status == "Completed" and not transaction.gateway_receipt
	if not missing or not transaction.gateway:
		return {"missing": False, "can_enter": False, "can_request": False}

	try:
		gateway = get_gateway(transaction.gateway)
	except Exception:
		return {"missing": True, "can_enter": False, "can_request": False}

	return {
		"missing": True,
		"can_enter": bool(getattr(gateway, "accepts_entered_receipt", False)),
		"can_request": bool(gateway.can_request_receipt()),
		"last_requested_on": frappe.db.get_value(
			"Mpesa Payment",
			{"payment_transaction": transaction.name},
			"status_query_requested_on",
		)
		if transaction.gateway_detail_doctype == "Mpesa Payment"
		else None,
	}


@frappe.whitelist(methods=["POST"])
def record_gateway_receipt(transaction_name, receipt):
	"""Staff enter the receipt from the payer's message, e.g. the M-Pesa SMS.

	Write permission on the transaction is the gate, as for ``confirm_payment``.
	The code is checked for shape by the gateway and refused if another
	transaction already carries it, which is the one mistake that matters: the
	same SMS used to receipt two payments.
	"""
	transaction = frappe.get_doc(TRANSACTION, transaction_name)
	frappe.has_permission(TRANSACTION, ptype="write", doc=transaction, throw=True)

	# Serialise against a late callback or status result landing at the same time.
	frappe.db.get_value(TRANSACTION, transaction.name, "status", for_update=True)
	transaction.reload()
	_assert_receipt_missing(transaction)

	gateway = get_gateway(transaction.gateway)
	code = gateway.normalise_receipt(receipt)

	other = _receipt_used_by(code, transaction.name)
	if other:
		frappe.throw(f"Receipt {code} is already recorded on {other}. Check the code with the payer.")

	_store_receipt(transaction, code)

	try:
		gateway.record_entered_receipt(transaction, code)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to note entered receipt on the detail record of {transaction.name}: {e}"
		)

	transaction.add_comment(
		"Info",
		f"Receipt {code} entered by {frappe.utils.get_fullname(frappe.session.user)} from the payer's message.",
	)

	_notify_source_receipt(transaction, code)
	return {"receipt": code}


@frappe.whitelist(methods=["POST"])
def request_gateway_receipt(transaction_name):
	"""Ask the gateway to look up the receipt. The answer arrives later, on
	``receipt_query_result``; the form is reloaded by the person to see it."""
	transaction = frappe.get_doc(TRANSACTION, transaction_name)
	frappe.has_permission(TRANSACTION, ptype="write", doc=transaction, throw=True)
	_assert_receipt_missing(transaction)

	gateway = get_gateway(transaction.gateway)
	if not gateway.can_request_receipt():
		frappe.throw("This payment method is not set up to look up receipts.")

	gateway.request_receipt(transaction)
	return {"requested": True}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receipt_query_result(**kwargs):
	"""Daraja posts the answer to a Transaction Status query here (also its queue
	timeout).

	The result is matched by its ConversationID, which Safaricom issued to us when
	we asked and which nobody else has seen. A receipt is taken only if the lookup is
	unambiguous: the amount matches what we asked for and no other transaction
	already carries the code. Anything else is logged for a person to look at,
	because a wrong code on a receipt is worse than a blank one. Like every callback,
	the message is saved before it is processed.
	"""
	raw, data = _read_body()
	log = _open_log("Receipt lookup", raw, None)
	_run_callback("Receipt lookup", data, None, log)
	return ACCEPTED


def _process_receipt_result(data, log):
	conversation_id = ((data.get("Result") or {}).get("ConversationID")) if isinstance(data, dict) else None
	_update_log(log, gateway_reference=conversation_id)

	detail = (
		frappe.db.get_value(
			"Mpesa Payment",
			{"status_query_conversation_id": conversation_id},
			["name", "payment_transaction"],
			as_dict=True,
		)
		if conversation_id
		else None
	)

	if not detail or not detail.payment_transaction:
		_update_log(log, status="Unmatched", note="It answers no receipt lookup we sent.")
		frappe.log_error(
			title="M-Pesa receipt lookup: no matching request",
			message=f"A Transaction Status result for ConversationID {conversation_id} matched no lookup we sent.",
		)
		return

	transaction = frappe.get_doc(TRANSACTION, detail.payment_transaction)
	gateway = get_gateway(transaction.gateway)
	_update_log(
		log, payment_transaction=transaction.name, source_trusted=1 if _source_trusted(gateway) else 0
	)

	parser = getattr(gateway, "parse_receipt_result", None)
	parsed = parser(data) if parser else {}

	frappe.db.set_value(
		"Mpesa Payment", detail.name, "raw_status_result", frappe.as_json(data), update_modified=False
	)

	status = frappe.db.get_value(TRANSACTION, transaction.name, "status", for_update=True)
	transaction.reload()
	receipt = parsed.get("receipt")

	if receipt and transaction.gateway_receipt == receipt:
		# The callback or a person got there first, with the same code.
		_update_log(log, status="Processed", note="The payment already had this receipt.")
		return

	problem = None
	if not receipt:
		problem = (
			f"Safaricom did not return a completed transaction for {transaction.name}: "
			f"{parsed.get('result_description') or 'no detail'}."
		)
	elif status != "Completed" or transaction.gateway_receipt:
		problem = (
			f"{transaction.name} is {status} with receipt {transaction.gateway_receipt or 'none'}; "
			f"the lookup returned {receipt}, which was not applied."
		)
	elif parsed.get("amount") is not None and _amount(parsed["amount"]) != _amount(transaction.amount):
		problem = (
			f"The lookup for {transaction.name} returned {receipt} for {parsed['amount']}, but the "
			f"payment was for {transaction.amount}. The receipt was not applied."
		)
	elif _receipt_used_by(receipt, transaction.name):
		problem = f"The lookup for {transaction.name} returned {receipt}, which another transaction already carries."

	if problem:
		_update_log(log, status="Processed", note=problem)
		frappe.log_error(title="M-Pesa receipt lookup not applied", message=problem)
		return

	_store_receipt(transaction, receipt, parsed.get("transaction_date"))
	frappe.db.set_value(
		"Mpesa Payment",
		detail.name,
		{
			"mpesa_receipt_number": receipt,
			"receipt_source": "Status Query",
			"transaction_date": parsed.get("transaction_date"),
		},
		update_modified=False,
	)
	_update_log(log, status="Processed")
	frappe.db.commit()

	_notify_source_receipt(transaction, receipt)


def _amount(value):
	try:
		return round(float(value), 2)
	except TypeError, ValueError:
		return None


# ── TELLING THE SOURCE APP ─────────────────────────────────────────────────


def _notify_source_receipt(transaction, receipt):
	"""Hand a late-arriving receipt to the source app, if it wants one.

	Separate from ``on_payment_confirmed``: the payment was already confirmed, so
	only the receipt is new. A source doc that does not implement the hook is fine -
	it just keeps whatever reference it recorded at confirmation.
	"""
	try:
		doc = frappe.get_doc(transaction.source_doctype, transaction.source_document)
		if hasattr(doc, "on_payment_receipt"):
			doc.on_payment_receipt(receipt=receipt, transaction_id=transaction.name)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: could not hand receipt {receipt} to "
			f"{transaction.source_doctype} {transaction.source_document}: {e}"
		)


def _notify_source_app(transaction):
	"""
	Tell the source app the payment is complete.
	Calls on_payment_confirmed() on the source document if it exists.

	If the source app fails, the payment stays Completed - the money did arrive - and
	is put on the review list, because otherwise nobody would know the app never
	recorded it (the payer paid, and nothing was activated).
	"""
	try:
		doc = frappe.get_doc(transaction.source_doctype, transaction.source_document)
		if hasattr(doc, "on_payment_confirmed"):
			doc.on_payment_confirmed(
				amount=transaction.amount,
				receipt=transaction.gateway_receipt,
				transaction_id=transaction.name,
			)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: could not notify {transaction.source_doctype} {transaction.source_document}: {e}"
		)
		try:
			_flag_review(
				transaction,
				f"{transaction.source_doctype} {transaction.source_document} could not record this payment "
				f"({e}). The money was received: make sure the record shows it as paid.",
			)
		except Exception:
			frappe.logger().error(f"onerc_payments: could not flag {transaction.name} for review")


def _extract_gateway_reference(gateway_name, data):
	"""
	Extract the gateway reference from a callback payload.
	Each gateway puts it in a different place.

	``gateway_name`` reaches us as a query parameter on the CallBackURL, which is the
	one part of the URL we cannot count on: Daraja is fussy about query strings, and a
	proxy or rewrite in front of the site can drop them. So we also recognise a payload
	by its shape - an M-Pesa STK callback is the only one carrying Body.stkCallback -
	rather than silently failing to match the transaction and binning the receipt.
	"""
	gateway_name = (gateway_name or "").lower()

	stk = (data.get("Body") or {}).get("stkCallback") or {}
	if stk or "mpesa" in gateway_name:
		return stk.get("CheckoutRequestID")
	if "mtn" in gateway_name:
		return data.get("financialTransactionId") or data.get("externalId")
	if "stripe" in gateway_name:
		return data.get("id")
	return data.get("reference") or data.get("transaction_id")
