# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""KCB Buni "Mpesa Express": an M-Pesa prompt that pays into a KCB / Vooma till.

Each till is a KCB Buni Account, and each account is a gateway of its own, so one site
can collect for several appeals into several tills. The prompt goes out through KCB's
paybill 522533; ``invoiceNumber`` ("TILL-REFERENCE") routes the money to the till and is
what the payer sees in the PIN prompt.

Contract: KCB's MpesaExpressAPIService 1.0.0 (OpenAPI) and "KCB MPESA STK PUSH API
SPECIFICATION DOCUMENT" v1.0. What they do NOT offer shapes this driver:

- **No status query.** We cannot ask KCB whether a payment went through, so the browser
  poll and the hourly job answer from our own record, and a payment whose callback never
  arrives stays Pending until the stale check puts it on the review list.
- **The callback is not signed, and KCB publishes no source addresses.** What proves a
  callback genuine is the MerchantRequestID KCB gave us when the prompt was sent: it is
  kept only on the KCB Buni Payment record (System Managers), never on the transaction,
  which apps read and show to payers. A callback must carry it, and the exact amount, and
  the phone we prompted. Anything else, or an account set not to complete on callbacks,
  goes to a person with the till statement (``MANUAL_CHECK``).
- **callbackUrl is at most 50 characters**, so it is the short alias ``/api/method/kcb``
  (see hooks.py) on the account's public address, checked before anything is sent.
- **invoiceNumber is at most 20 characters** here (the spec says 12 in one place and 30
  in another; agreed to work with 20), so a till is at most 7 digits.
- At acceptance ``CheckoutRequestID`` may be ``false``; MerchantRequestID is required.
"""

import re
import secrets

import frappe
import requests
from frappe.utils import add_to_date, cint, flt, now_datetime

from .accounts import public_base, require_https, sign_in_request, token_cache_key
from .base import MANUAL_CHECK, BaseGateway
from .mpesa_daraja import MPESA_RECEIPT_PATTERN, _as_int, charged_amount

ACCOUNT = "KCB Buni Account"
DRIVER = "onerc_payments.gateways.kcb_buni.KcbBuniGateway"
DETAIL = "KCB Buni Payment"
TRANSACTION = "OneRC Payment Transaction"

SANDBOX_TOKEN_URL = "https://uat.buni.kcbgroup.com/token"
SANDBOX_STK_URL = "https://uat.buni.kcbgroup.com/mm/api/request/1.0.0/stkpush"

# The alias hooks.py maps to ``onerc_payments.api.v1.kcb.callback``.
CALLBACK_PATH = "/api/method/kcb"
CALLBACK_URL_MAX = 50
INVOICE_MAX = 20
DESCRIPTION_MAX = 13
# An account reference is at most 12 characters (references.REFERENCE_MAX), plus "-".
TILL_MAX_DIGITS = INVOICE_MAX - 1 - 12

TOKEN_CACHE_PREFIX = "onerc_payments:kcb_token"
# KCB's API gateway answers these when it refuses the token rather than the request.
TOKEN_REJECTED_FAULTS = frozenset(["900901", "900902"])

# How far back a prompt whose reply never reached us can be matched by phone and amount.
UNCONFIRMED_MATCH_MINUTES = 20

# A genuine KCB callback matches its payment. Many that match nothing, in a short time, is
# what guessing a request ID looks like. Guessing is only plausible for a payment whose
# proof is a sequential-style MerchantRequestID alone (KCB's older format, e.g.
# "17684-56147665-1", with CheckoutRequestID false); while the alarm lasts, such payments
# do not complete by themselves. Payments with a random-looking ID or a CheckoutRequestID
# are not affected, so junk posted to the callback cannot hold up every KCB payment.
GUESSING_ALARM_UNMATCHED = 20
GUESSING_ALARM_MINUTES = 60
_SEQUENTIAL_ID = re.compile(r"^[\d-]+$")


def clear_token_cache():
	frappe.cache().delete_keys(TOKEN_CACHE_PREFIX)


def _stk(data):
	"""Body.stkCallback, or {} for anything else - a message here can be any shape."""
	body = data.get("Body") if isinstance(data, dict) else None
	stk = body.get("stkCallback") if isinstance(body, dict) else None
	return stk if isinstance(stk, dict) else {}


def _guessable(detail):
	"""Whether a payment's proof could be guessed: no CheckoutRequestID, and a short or
	sequential-style MerchantRequestID."""
	merchant = str(detail.get("merchant_request_id") or "")
	return not detail.get("checkout_request_id") and (
		bool(_SEQUENTIAL_ID.match(merchant)) or len(merchant) < 20
	)


def _metadata(stk):
	items = (stk.get("CallbackMetadata") or {}).get("Item") or []
	if isinstance(items, dict):
		items = [items]
	return {item.get("Name"): item.get("Value") for item in items if isinstance(item, dict)}


def _phone_text(value):
	text = str(value or "").strip()
	return text[:-2] if text.endswith(".0") else text


def _phone(value):
	"""A callback phone as digits, or None when masked (2547*****678) or absent."""
	text = _phone_text(value)
	return text if text.isdigit() else None


def _masked_phone_fits(value, prompted):
	"""Whether a masked callback phone (2547*****678) agrees with the phone we prompted in
	every digit it shows. A phone that is neither digits nor a mask does not fit."""
	text = _phone_text(value)
	prompted = str(prompted or "")
	if not text or "*" not in text or len(text) != len(prompted):
		return False
	return all(shown == "*" or shown == digit for shown, digit in zip(text, prompted, strict=True))


def _recent_unmatched_callbacks():
	from onerc_payments.api.v1.payment import CALLBACK_LOG

	return frappe.db.count(
		CALLBACK_LOG,
		{
			"kind": "KCB payment",
			"status": "Unmatched",
			"received_on": [">=", add_to_date(now_datetime(), minutes=-GUESSING_ALARM_MINUTES)],
		},
	)


def _kcb_date(value):
	from datetime import datetime

	try:
		return datetime.strptime(str(value).split(".")[0], "%Y%m%d%H%M%S")
	except TypeError, ValueError:
		return None


class KcbBuniGateway(BaseGateway):
	label = "M-Pesa (KCB till)"
	supported_currencies = ("KES",)
	prompts_payer = True
	resolves_automatically = True
	callback_kind = "KCB payment"
	accepts_entered_receipt = True
	detail_is_required = True

	def __init__(self, settings, gateway=None):
		super().__init__(settings, gateway)
		if gateway is not None and (gateway.get("label") or "").strip():
			self.label = gateway.label.strip()

	# ── configuration ────────────────────────────────────────────────

	def _require_account(self):
		if not self.account:
			frappe.throw(f"{self.gateway_name or 'This gateway'} has no KCB Buni Account behind it.")
		return self.account

	def _production(self):
		return (self._require_account().environment or "Sandbox") == "Production"

	def _token_url(self):
		account = self._require_account()
		if self._production():
			return require_https(account.production_token_url, f"{account.name}: Production Token URL")
		return SANDBOX_TOKEN_URL

	def _stk_url(self):
		account = self._require_account()
		if self._production():
			return require_https(account.production_stk_url, f"{account.name}: Production STK URL")
		return SANDBOX_STK_URL

	def _credentials(self):
		account = self._require_account()
		return (
			account.get_password("consumer_key", raise_exception=False),
			account.get_password("consumer_secret", raise_exception=False),
		)

	def _till(self):
		till = str(self._require_account().till_number or "").strip()
		if not (till.isascii() and till.isdigit() and 1 <= len(till) <= TILL_MAX_DIGITS):
			frappe.throw(
				f"{self.account.name}: the till number must be 1 to {TILL_MAX_DIGITS} digits. "
				f"KCB shows the till and the payment reference together, in at most {INVOICE_MAX} characters."
			)
		return till

	def callback_url(self):
		"""KCB's callbackUrl: the short alias on the account's public address, 50 characters
		at most. A longer one is refused before anything is sent - a payment whose result
		can never reach us would succeed silently and stay Pending."""
		account = self._require_account()
		url = public_base(account, account.name) + CALLBACK_PATH
		if len(url) > CALLBACK_URL_MAX:
			frappe.throw(
				f"KCB accepts a callback address of at most {CALLBACK_URL_MAX} characters, and {url} has "
				f"{len(url)}. Set a shorter Public Site Address on {account.name} (a short domain that "
				"points at this site)."
			)
		return url

	def invoice_number(self, transaction):
		"""invoiceNumber: TILL-REFERENCE, the reference being the payment's account reference."""
		import re

		reference = re.sub(
			r"[^A-Za-z0-9]", "", transaction.get("account_reference") or transaction.name or ""
		)
		value = f"{self._till()}-{reference.upper()}"
		if len(value) > INVOICE_MAX:
			value = value[:INVOICE_MAX]
		return value

	@staticmethod
	def transaction_description(transaction):
		from onerc_payments.references import FALLBACK_DESCRIPTION, clean_description

		text = clean_description(transaction.flags.get("reference_description")) or FALLBACK_DESCRIPTION
		return text[:DESCRIPTION_MAX]

	def validate_account(self):
		account = self._require_account()
		key, secret = self._credentials()
		if not key or not secret:
			frappe.throw(f"{account.name}: enter the KCB consumer key and secret first.")
		self._till()
		self._token_url()
		self._stk_url()
		self.callback_url()

	# ── talking to KCB ───────────────────────────────────────────────

	def _get_token(self, refresh=False):
		key, secret = self._credentials()
		if not key or not secret:
			frappe.throw(f"{self.account.name}: the KCB consumer key or secret is not set.")

		url = self._token_url()
		cache_key = token_cache_key(TOKEN_CACHE_PREFIX, self.account.environment, url, key)
		if not refresh:
			cached = frappe.cache().get_value(cache_key)
			if cached:
				return cached

		status, body, failure = sign_in_request(
			url,
			params={"grant_type": "client_credentials"},
			data={"grant_type": "client_credentials"},
			auth=(key, secret),
			timeout=20,
			allow_redirects=False,
		)
		if failure:
			frappe.throw(f"KCB could not be reached to sign in {self.account.name} ({failure}).")
		if status != 200:
			frappe.throw(
				f"KCB did not accept the credentials of {self.account.name} (HTTP {status}). "
				"Check the consumer key and secret, and that the app is subscribed to Mpesa Express."
			)
		token = body.get("access_token")
		if not isinstance(token, str) or not token:
			frappe.throw("KCB did not return an access token.")
		lifetime = cint(body.get("expires_in")) or 3600
		frappe.cache().set_value(cache_key, token, expires_in_sec=max(60, lifetime - 120))
		return token

	@staticmethod
	def _token_rejected(response):
		if getattr(response, "status_code", None) == 401:
			return True
		try:
			body = response.json()
		except Exception:
			return False
		fault = body.get("fault") if isinstance(body, dict) else None
		return isinstance(fault, dict) and str(fault.get("code") or "") in TOKEN_REJECTED_FAULTS

	def _send(self, payload):
		"""POST the prompt. A refused token means KCB processed nothing, so it is sent once
		more with a fresh one; nothing else is ever sent twice."""

		def post(token):
			return requests.post(
				self._stk_url(),
				json=payload,
				headers={
					"Authorization": f"Bearer {token}",
					"Content-Type": "application/json",
					# From KCB's Postman collection; not in the OpenAPI document.
					"routeCode": "207",
					"operation": "STKPush",
					"messageId": secrets.token_hex(16),
				},
				timeout=30,
				allow_redirects=False,
			)

		response = post(self._get_token())
		if self._token_rejected(response):
			response = post(self._get_token(refresh=True))
		return response

	def test_connection(self):
		"""Sign in only. No prompt is sent."""
		self._get_token(refresh=True)
		return True

	# ── starting a payment ───────────────────────────────────────────

	def initiate(self, transaction):
		if transaction.direction == "Outbound":
			return {"success": False, "message": "A KCB till here only collects payments."}

		phone = str(transaction.phone_number or "")
		if not (len(phone) == 12 and phone.isdigit() and phone.startswith("254")):
			return {
				"success": False,
				"message": "Enter the M-Pesa phone number to prompt, e.g. 0712345678.",
			}

		invoice = self.invoice_number(transaction)
		payload = {
			"phoneNumber": phone,
			"amount": str(charged_amount(transaction.amount)),
			"invoiceNumber": invoice,
			"sharedShortCode": True,
			"orgShortCode": "",
			"orgPassKey": "",
			"callbackUrl": self.callback_url(),
			"transactionDescription": self.transaction_description(transaction),
		}
		raw_request = frappe.as_json(payload)
		base = {"invoice_number": invoice, "raw_request": raw_request}

		# Nothing can have reached the payer before the prompt is sent.
		try:
			self._get_token()
		except Exception as e:
			return {**base, "success": False, "message": str(e)}

		try:
			response = self._send(payload)
		except requests.exceptions.ConnectTimeout as e:
			return {**base, "success": False, "message": f"KCB could not be reached. ({e})"}
		except requests.exceptions.RequestException as e:
			# Sent, but the reply was lost: KCB may well have prompted the payer.
			return {
				**base,
				"success": False,
				"unconfirmed": True,
				"message": (
					"KCB did not reply in time. If an M-Pesa prompt reached the phone and was paid, "
					f"it will be checked against the till statement. ({e})"
				),
			}

		status_code = getattr(response, "status_code", 0) or 0
		try:
			body = response.json()
		except ValueError:
			body = None
		if not isinstance(body, dict):
			return {
				**base,
				"success": False,
				"unconfirmed": status_code >= 500,
				"raw_response": (getattr(response, "text", "") or "")[:2000],
				"message": f"KCB returned an unreadable reply (HTTP {status_code}).",
			}

		header = body.get("header") or {}
		result = body.get("response") or {}
		merchant = result.get("MerchantRequestID")
		checkout = result.get("CheckoutRequestID")
		accepted = (
			status_code == 200
			and str(header.get("statusCode")) == "0"
			and _as_int(result.get("ResponseCode")) == 0
			and isinstance(merchant, str)
			and merchant.strip()
		)
		if accepted:
			return {
				**base,
				"success": True,
				# Public: the transaction's reference is what apps show. The request IDs stay
				# on the KCB Buni Payment record.
				"gateway_reference": invoice,
				"merchant_request_id": merchant.strip(),
				"checkout_request_id": checkout.strip()
				if isinstance(checkout, str) and checkout.strip()
				else None,
				"raw_response": frappe.as_json(body),
				"message": "Check your phone and enter your M-Pesa PIN to complete the payment.",
			}

		fault = body.get("fault") if isinstance(body.get("fault"), dict) else {}
		return {
			**base,
			"success": False,
			"unconfirmed": status_code >= 500,
			"raw_response": frappe.as_json(body),
			"message": (
				result.get("CustomerMessage")
				or header.get("statusDescription")
				or fault.get("description")
				or f"KCB did not accept the payment request (HTTP {status_code})."
			),
		}

	def check_status(self, transaction, timeout=30):
		# KCB publishes no status query: only the callback decides. Never changes anything.
		return {"status": transaction.status or "Pending"}

	# ── the callback ─────────────────────────────────────────────────

	@staticmethod
	def callback_reference(data):
		merchant = _stk(data).get("MerchantRequestID")
		return merchant.strip() if isinstance(merchant, str) and merchant.strip() else None

	@staticmethod
	def find_transaction(reference):
		if not reference:
			return None
		return frappe.db.get_value(DETAIL, {"merchant_request_id": reference}, "payment_transaction")

	def verify_callback_source(self):
		# KCB publishes no addresses and signs nothing: a sender is never "trusted".
		return False

	def handle_callback(self, data, transaction):
		stk = _stk(data)
		if not stk or "ResultCode" not in stk:
			return {"status": "Pending", "failure_reason": "Not an M-Pesa STK callback"}

		result_code = _as_int(stk.get("ResultCode"))
		result_desc = stk.get("ResultDesc")
		if result_code != 0:
			return {
				"status": "Failed",
				"failure_reason": result_desc,
				"result_code": str(stk.get("ResultCode")),
				"result_description": result_desc,
			}

		meta = _metadata(stk)
		paid = meta.get("Amount")
		expected = charged_amount(transaction.amount)
		mismatch = None
		if paid is None or flt(paid) != expected:
			mismatch = f"The KCB callback says {paid} was paid, but the prompt asked for {expected}."
		return {
			"status": "Completed",
			"gateway_receipt": meta.get("MpesaReceiptNumber"),
			"result_code": "0",
			"result_description": result_desc,
			"transaction_date": _kcb_date(meta.get("TransactionDate")),
			"amount_mismatch": mismatch,
		}

	def confirm_callback(self, transaction, result, data):
		"""KCB cannot be asked. A "paid" completes on what only KCB could know - the
		MerchantRequestID it gave us (this callback was found by it) and the CheckoutRequestID
		when we have it - together with the exact amount and the phone we prompted. Anything
		short of that is for a person with the till statement."""
		self.manual_check_reason = None
		account = self._require_account()
		stk = _stk(data)
		meta = _metadata(stk)
		detail = (
			frappe.db.get_value(
				DETAIL,
				{"payment_transaction": transaction.name},
				["merchant_request_id", "checkout_request_id", "phone_number"],
				as_dict=True,
			)
			or frappe._dict()
		)

		reasons = []
		if transaction.status in ("Failed", "Cancelled"):
			reasons.append(f"it arrived after the payment was marked {transaction.status}")
		if not cint(account.get("complete_on_callback")):
			reasons.append(f"{account.name} is set to have a person check every KCB payment")
		if not detail.merchant_request_id or detail.merchant_request_id != self.callback_reference(data):
			reasons.append("it does not carry the request ID KCB gave this payment")
		checkout = stk.get("CheckoutRequestID")
		if detail.checkout_request_id and checkout != detail.checkout_request_id:
			reasons.append("its checkout ID is not the one KCB gave this payment")
		paid = meta.get("Amount")
		if paid is None or flt(paid) != charged_amount(transaction.amount):
			reasons.append(f"it says {paid} was paid, not {charged_amount(transaction.amount)}")
		shown = meta.get("PhoneNumber")
		phone = _phone(shown)
		prompted = str(detail.phone_number or transaction.phone_number or "")
		if phone:
			if phone != prompted:
				reasons.append("it names a different phone from the one prompted")
		elif shown not in (None, "") and not _masked_phone_fits(shown, prompted):
			reasons.append("its phone does not fit the one prompted")
		if not meta.get("MpesaReceiptNumber"):
			reasons.append("it carries no M-Pesa code")
		if not reasons and _guessable(detail):
			unmatched = _recent_unmatched_callbacks()
			if unmatched >= GUESSING_ALARM_UNMATCHED:
				reasons.append(
					f"{unmatched} KCB callbacks in the last {GUESSING_ALARM_MINUTES} minutes matched no "
					"payment, which looks like someone guessing request IDs"
				)

		if reasons:
			self.manual_check_reason = "; ".join(reasons)
			return MANUAL_CHECK
		return None

	def match_unconfirmed(self, data, gateway_names):
		"""A paid callback for a prompt whose reply never reached us: one payment from the
		same phone, for the same amount, on one of these tills, in the last few minutes. KCB
		cannot confirm it, so it is never claimed automatically - the payment it fits is put
		in front of a person instead."""
		stk = _stk(data)
		if _as_int(stk.get("ResultCode")) != 0 or not gateway_names:
			return None
		meta = _metadata(stk)
		phone = _phone(meta.get("PhoneNumber"))
		paid = meta.get("Amount")
		if not phone or paid is None:
			return None
		candidates = frappe.get_all(
			TRANSACTION,
			filters={
				"initiation_unconfirmed": 1,
				"gateway": ["in", gateway_names],
				"direction": "Inbound",
				"phone_number": phone,
				"status": ["in", ["Failed", "Pending", "Initiated"]],
				"creation": [">=", add_to_date(now_datetime(), minutes=-UNCONFIRMED_MATCH_MINUTES)],
			},
			fields=["name", "amount"],
		)
		fits = [c.name for c in candidates if charged_amount(c.amount) == flt(paid)]
		return fits[0] if len(fits) == 1 else None

	# ── the KCB Buni Payment record ──────────────────────────────────

	def _upsert(self, transaction, values):
		from .accounts import upsert_detail

		values = {
			"gateway": self.gateway_name,
			"till_number": self.account.till_number if self.account else None,
			**values,
		}
		name = upsert_detail(DETAIL, transaction.name, values)
		transaction.gateway_detail_doctype = DETAIL
		transaction.gateway_detail = name
		return name

	def record_initiation_details(self, transaction, result):
		return self._upsert(
			transaction,
			{
				"invoice_number": result.get("invoice_number"),
				"merchant_request_id": result.get("merchant_request_id"),
				"checkout_request_id": result.get("checkout_request_id"),
				"status": "Pending" if result.get("success") else "Failed",
				"phone_number": transaction.phone_number,
				"expected_amount": charged_amount(transaction.amount),
				"raw_request": result.get("raw_request"),
				"raw_response": result.get("raw_response"),
			},
		)

	def record_payment_details(self, data, transaction):
		stk = _stk(data)
		if not stk:
			return None
		meta = _metadata(stk)
		incoming = meta.get("MpesaReceiptNumber")
		existing = frappe.db.get_value(
			DETAIL, {"payment_transaction": transaction.name}, "mpesa_receipt_number"
		)
		if existing and incoming and incoming != existing:
			# A second "paid" with another code is not a retry; the first record stands.
			return None
		paid = meta.get("Amount")
		phone = meta.get("PhoneNumber")
		checkout = stk.get("CheckoutRequestID")
		return self._upsert(
			transaction,
			{
				"checkout_request_id": checkout if isinstance(checkout, str) and checkout else None,
				"status": "Completed" if _as_int(stk.get("ResultCode")) == 0 else "Failed",
				"result_code": str(stk.get("ResultCode")) if stk.get("ResultCode") is not None else None,
				"result_description": stk.get("ResultDesc"),
				"mpesa_receipt_number": incoming,
				"receipt_source": "Callback" if incoming else None,
				"amount": paid,
				"amount_matched": 1
				if paid is not None and flt(paid) == charged_amount(transaction.amount)
				else 0,
				"callback_phone": str(phone) if phone is not None else None,
				"transaction_date": _kcb_date(meta.get("TransactionDate")),
				"raw_callback": frappe.as_json(data),
			},
		)

	def record_status_update(self, transaction, result):
		return self._upsert(transaction, {"status": result.get("status")})

	# ── receipts ─────────────────────────────────────────────────────

	def normalise_receipt(self, value):
		code = "".join(str(value or "").split()).upper()
		if not MPESA_RECEIPT_PATTERN.match(code):
			frappe.throw(
				f"{value or 'That'} is not an M-Pesa code. An M-Pesa code is 10 letters and numbers, "
				"like TKB1A2C3D4, at the start of the payer's M-Pesa message."
			)
		return code

	def record_entered_receipt(self, transaction, receipt):
		return self._upsert(
			transaction,
			{"mpesa_receipt_number": receipt, "receipt_source": "Entered by Staff", "status": "Completed"},
		)

	def generate_receipt(self, transaction):
		return (
			f"M-PESA PAYMENT RECEIPT (KCB till {self.account.till_number if self.account else ''})\n"
			f"Receipt No:  {transaction.gateway_receipt}\n"
			f"Amount:      {transaction.currency} {transaction.amount}\n"
			f"Reference:   {transaction.gateway_reference}\n"
		)
