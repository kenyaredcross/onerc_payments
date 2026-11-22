# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Equity Jenga Payment Gateway (JengaPGW v3): card payments on Jenga's hosted checkout.

Each Jenga merchant is a Jenga Account, and each account is a gateway of its own.

How a card payment runs:

1. ``initiate`` gives the payment an order reference and returns a ``checkout_url`` on this
   site. The app sends the payer there (a popup or a redirect).
2. That page (``onerc_payments.api.v1.card.checkout``) signs in to Jenga, signs the order
   and posts the payer's browser to Jenga's checkout, where they pay.
3. Jenga redirects the payer's **browser** back to ``card.jenga_return``. Anyone can fake
   that request, and Jenga does not document its ``hash``, so it completes nothing: it is
   logged, and at most makes us ask Jenga (when the status query is switched on).
4. Jenga's **server** posts an Instant Payment Notification to ``card.jenga_notification``
   with HTTP Basic credentials we chose and registered in the JengaHQ portal. That message,
   from an authenticated sender, is what completes or fails the payment
   (``sender_proves_payment``). One without the right credentials is ignored entirely.

Jenga's documentation leaves open which notification field carries our order reference
and whether its Transaction Details query covers card checkouts. So the reference is
looked for in each likely field (an order reference is 20 random characters, so a match
cannot be an accident), and the status query stays off per account until a sandbox
payment proves it answers for card payments.

A checkout the payer abandons sends nothing at all. Jenga's page expires after
``paymentTimeLimit`` (15 minutes); a payment with no answer after CHECKOUT_EXPIRY_MINUTES
is Cancelled, and a notification that still arrives later recovers it as for M-Pesa.
"""

import base64
import hmac
import re
import secrets
import string
from datetime import UTC, datetime

import frappe
import requests
from frappe.utils import cint, flt, get_datetime, now_datetime, time_diff_in_seconds

from .accounts import public_base, require_https, sign_in_request, token_cache_key, upsert_detail
from .base import CONFIRMED, CONTRADICTED, MANUAL_CHECK, UNAVAILABLE, BaseGateway

ACCOUNT = "Jenga Account"
DRIVER = "onerc_payments.gateways.jenga.JengaGateway"
DETAIL = "Jenga Payment"

AUTH_URLS = {
	"Sandbox": "https://uat.finserve.africa/authentication/api/v3/authenticate/merchant",
	"Production": "https://api.finserve.africa/authentication/api/v3/authenticate/merchant",
}
CHECKOUT_URLS = {
	"Sandbox": "https://v3-uat.jengapgw.io/processPayment",
	"Production": "https://v3.jengapgw.io/processPayment",
}
QUERY_BASES = {
	"Sandbox": "https://uat.finserve.africa",
	"Production": "https://api.finserve.africa",
}
QUERY_PATH = "/v3-apis/transaction-api/v3.0/transactions/details/"

PAYMENT_TIME_LIMIT = "15mins"
CHECKOUT_EXPIRY_MINUTES = 60
ORDER_REFERENCE_LENGTH = 20
DESCRIPTION_MAX = 200

TOKEN_CACHE_PREFIX = "onerc_payments:jenga_token"

PAID = frozenset(["SUCCESS", "SUCCESSFUL", "COMPLETED", "COMPLETE", "PAID"])
NOT_PAID = frozenset(["FAILED", "FAILURE", "DECLINED", "REJECTED", "CANCELLED", "CANCELED", "REVERSED"])

RECEIPT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{3,39}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def clear_token_cache():
	frappe.cache().delete_keys(TOKEN_CACHE_PREFIX)


def new_order_reference():
	alphabet = string.ascii_uppercase + string.digits
	return "".join(secrets.choice(alphabet) for _ in range(ORDER_REFERENCE_LENGTH))


def parse_currencies(value):
	"""'KES, USD' or one per line -> ("KES", "USD")."""
	codes = [c.strip().upper() for c in re.split(r"[\s,;]+", value or "") if c.strip()]
	return tuple(dict.fromkeys(codes))


def amount_text(amount):
	"""The one way an amount is written: in the form and in the signed string alike."""
	return f"{flt(amount, 2):.2f}"


def _notification(data):
	"""(transaction part, whole message) of a Jenga notification."""
	data = data if isinstance(data, dict) else {}
	txn = data.get("transaction") if isinstance(data.get("transaction"), dict) else {}
	return txn, data


def _ordered(txn):
	"""(amount, currency) of the order a notification reports. ``orderAmount`` and
	``orderCurrency`` are the order as placed; ``amount``/``currency`` what the card was
	charged, which can be another currency (and include a service charge)."""
	amount = txn.get("orderAmount") if txn.get("orderAmount") is not None else txn.get("amount")
	currency = str(txn.get("orderCurrency") or txn.get("currency") or "").strip().upper()
	return amount, currency


def candidate_references(data):
	"""Every value in a notification that could be our order reference."""
	txn, data = _notification(data)
	customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
	values = [
		txn.get("orderReference"),
		txn.get("billNumber"),
		txn.get("reference"),
		txn.get("additionalInfo"),
		customer.get("reference"),
		data.get("orderReference"),
		data.get("billNumber"),
		data.get("extraData"),
	]
	seen = []
	for value in values:
		text = str(value or "").strip()
		if len(text) == ORDER_REFERENCE_LENGTH and text.isalnum() and text not in seen:
			seen.append(text)
	return seen


class JengaGateway(BaseGateway):
	label = "Card"
	opens_checkout = True
	resolves_automatically = True
	callback_kind = "Card notification"
	sender_proves_payment = True
	accepts_entered_receipt = True
	detail_is_required = True

	def __init__(self, settings, gateway=None):
		super().__init__(settings, gateway)
		if gateway is not None and (gateway.get("label") or "").strip():
			self.label = gateway.label.strip()
		if self.account is not None:
			self.supported_currencies = parse_currencies(self.account.get("currencies")) or ("KES",)
		else:
			self.supported_currencies = ("KES",)

	# ── configuration ────────────────────────────────────────────────

	def _require_account(self):
		if not self.account:
			frappe.throw(f"{self.gateway_name or 'This gateway'} has no Jenga Account behind it.")
		return self.account

	def _environment(self):
		return self._require_account().environment or "Sandbox"

	def _auth_url(self):
		account = self._require_account()
		if account.get("auth_url"):
			return require_https(account.auth_url, f"{account.name}: Authentication URL")
		return AUTH_URLS[self._environment()]

	def checkout_action_url(self):
		account = self._require_account()
		if account.get("checkout_url"):
			return require_https(account.checkout_url, f"{account.name}: Checkout URL")
		return CHECKOUT_URLS[self._environment()]

	def _credentials(self):
		account = self._require_account()
		return (
			(account.merchant_code or "").strip(),
			account.get_password("api_key", raise_exception=False),
			account.get_password("consumer_secret", raise_exception=False),
		)

	def _base(self):
		account = self._require_account()
		return public_base(account, account.name)

	def checkout_page_url(self, order_reference):
		return f"{self._base()}/api/method/onerc_payments.api.v1.card.checkout?ref={order_reference}"

	def return_url(self):
		return f"{self._base()}/api/method/onerc_payments.api.v1.card.jenga_return"

	def notification_url(self):
		return f"{self._base()}/api/method/onerc_payments.api.v1.card.jenga_notification"

	def validate_account(self):
		account = self._require_account()
		merchant, api_key, secret = self._credentials()
		if not (merchant and api_key and secret):
			frappe.throw(f"{account.name}: enter the Jenga merchant code, API key and consumer secret first.")
		if not cint(account.get("query_status")) and not (
			(account.get("ipn_username") or "").strip()
			and account.get_password("ipn_password", raise_exception=False)
		):
			frappe.throw(
				f"{account.name}: set the notification (IPN) username and password, and register "
				f"{self.notification_url()} with them in the JengaHQ portal. Without the notification "
				"no card payment can ever be confirmed."
			)
		currencies = parse_currencies(account.get("currencies"))
		if not currencies or any(not _CURRENCY.match(c) for c in currencies):
			frappe.throw(f"{account.name}: list the currencies this merchant takes, e.g. KES, USD.")
		if account.get_password("private_key", raise_exception=False):
			self._private_key()
		self._auth_url()
		self.checkout_action_url()
		self._base()

	# ── talking to Jenga ─────────────────────────────────────────────

	def get_token(self, refresh=False):
		merchant, api_key, secret = self._credentials()
		if not (merchant and api_key and secret):
			frappe.throw(
				f"{self._require_account().name}: the Jenga merchant code, API key or secret is not set."
			)
		url = self._auth_url()
		cache_key = token_cache_key(TOKEN_CACHE_PREFIX, self._environment(), url, merchant, api_key)
		if not refresh:
			cached = frappe.cache().get_value(cache_key)
			if cached:
				return cached

		status, body, failure = sign_in_request(
			url,
			headers={"Api-Key": api_key, "Content-Type": "application/json"},
			json={"merchantCode": merchant, "consumerSecret": secret},
			timeout=20,
			allow_redirects=False,
		)
		if failure:
			frappe.throw(f"Jenga could not be reached to sign in {self.account.name} ({failure}).")
		if status != 200:
			frappe.throw(f"Jenga did not accept the credentials of {self.account.name} (HTTP {status}).")
		token = body.get("accessToken") or body.get("access_token")
		if not isinstance(token, str) or not token:
			frappe.throw("Jenga did not return an access token.")
		frappe.cache().set_value(cache_key, token, expires_in_sec=self._token_lifetime(body))
		return token

	@staticmethod
	def _token_lifetime(body):
		"""Seconds to keep a token: until a minute before Jenga's ``expiresIn`` (a timestamp
		about 15 minutes ahead), or 10 minutes when it cannot be read."""
		try:
			expires = datetime.fromisoformat(str(body.get("expiresIn")).replace("Z", "+00:00"))
			if expires.tzinfo is None:
				expires = expires.replace(tzinfo=UTC)
			remaining = (expires - datetime.now(UTC)).total_seconds()
		except TypeError, ValueError:
			remaining = 600
		return int(max(30, min(remaining, 3600) - 60))

	def _private_key(self):
		from cryptography.hazmat.primitives.serialization import load_pem_private_key

		pem = self._require_account().get_password("private_key", raise_exception=False)
		if not pem:
			return None
		key = None
		try:
			key = load_pem_private_key(pem.strip().encode(), password=None)
		except Exception:
			pass
		# Raised here, outside the except block and with the key text gone: a logged
		# traceback shows every frame's variables, and "pem" is not a name Frappe masks.
		del pem
		if key is None:
			frappe.throw(f"{self.account.name}: the signing private key is not a readable PEM private key.")
		return key

	def sign(self, message):
		"""RSA SHA-256 (PKCS#1 v1.5) of ``message`` with the merchant key, base64. None when
		no key is set (Jenga needs a signature only from merchants in secure mode)."""
		from cryptography.hazmat.primitives import hashes
		from cryptography.hazmat.primitives.asymmetric import padding

		key = self._private_key()
		if key is None:
			return None
		return base64.b64encode(key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()

	def test_connection(self):
		self.get_token(refresh=True)
		return True

	# ── starting a payment ───────────────────────────────────────────

	def initiate(self, transaction):
		if transaction.direction == "Outbound":
			return {"success": False, "message": "Card payments can only be collected."}
		try:
			# Sign in now so a wrong key fails here, not on the payer's checkout page.
			self.get_token()
		except Exception as e:
			return {"success": False, "message": f"The card payment could not be started: {e}"}
		order_reference = new_order_reference()
		return {
			"success": True,
			"gateway_reference": order_reference,
			"checkout_url": self.checkout_page_url(order_reference),
			"message": "Continue to the secure card page to pay.",
		}

	def checkout_url(self, transaction):
		return (
			self.checkout_page_url(transaction.gateway_reference) if transaction.gateway_reference else None
		)

	def checkout_form(self, transaction):
		"""(action URL, fields) of the form that takes the payer's browser to Jenga."""
		account = self._require_account()
		merchant, _api_key, _secret = self._credentials()
		order_reference = transaction.gateway_reference
		currency = (transaction.currency or "KES").upper()
		amount = amount_text(transaction.amount)
		callback = self.return_url()

		first, _space, last = (transaction.payer_name or "").strip().partition(" ")
		description = (
			f"{account.get('product_description') or 'Payment'} {transaction.account_reference or ''}"
		)
		fields = {
			# A fresh token: the payer can spend paymentTimeLimit on Jenga's page, and a cached
			# one may be minutes from expiring.
			"token": self.get_token(refresh=True),
			"merchantCode": merchant,
			"currency": currency,
			"orderAmount": amount,
			"orderReference": order_reference,
			"productType": account.get("product_type") or "Payment",
			"productDescription": description.strip()[:DESCRIPTION_MAX],
			"paymentTimeLimit": PAYMENT_TIME_LIMIT,
			"customerFirstName": first or "Payer",
			"customerLastName": last.strip() or "-",
			"customerPostalCodeZip": account.get("default_postal_code") or "00100",
			"customerAddress": account.get("default_address") or "Nairobi, Kenya",
			"customerEmail": transaction.email or account.get("default_email") or "",
			"customerPhone": transaction.phone_number or "",
			"countryCode": (account.get("country_code") or "KE").upper(),
			"callbackUrl": callback,
			"secondaryReference": transaction.account_reference or "",
		}
		signature = self.sign(merchant + order_reference + currency + amount + callback)
		if signature:
			fields["signature"] = signature
		return self.checkout_action_url(), fields

	# ── status ───────────────────────────────────────────────────────

	def check_status(self, transaction, timeout=30):
		account = self._require_account()
		if cint(account.get("query_status")) and transaction.gateway_reference:
			answer = self.query(transaction, timeout=timeout)
			if answer["status"] != "Pending" or answer.get("unreachable") or answer.get("review_reason"):
				return answer
		return self._expired(transaction) or {"status": transaction.status or "Pending"}

	@staticmethod
	def _expired(transaction):
		if transaction.status not in ("Pending", "Initiated") or not transaction.creation:
			return None
		age = time_diff_in_seconds(now_datetime(), get_datetime(transaction.creation))
		if age < CHECKOUT_EXPIRY_MINUTES * 60:
			return None
		return {
			"status": "Cancelled",
			"failure_reason": f"The card checkout was not completed within {CHECKOUT_EXPIRY_MINUTES} minutes.",
		}

	def query(self, transaction, timeout=30):
		"""Jenga's Transaction Details query for this order. Used only when the account says
		it answers for card payments (proved in the sandbox first)."""
		reference = transaction.gateway_reference
		headers = {"Authorization": f"Bearer {self.get_token()}"}
		signature = self.sign(reference)
		if signature:
			headers["Signature"] = signature
		try:
			response = requests.get(
				QUERY_BASES[self._environment()] + QUERY_PATH + reference,
				headers=headers,
				timeout=timeout,
				allow_redirects=False,
			)
			body = response.json()
		except Exception as e:
			frappe.logger().error(f"onerc_payments: Jenga status query for {transaction.name} failed: {e}")
			return {"status": "Pending", "unreachable": True}

		data = (body or {}).get("data") if isinstance(body, dict) else None
		data = data if isinstance(data, dict) else {}
		state = str(data.get("state") or "").upper()
		code = str(data.get("stateCode") if data.get("stateCode") is not None else "")
		raw = frappe.as_json(body)
		if state in PAID or code == "2":
			# The card form passes through the payer's browser (see confirm_callback): an
			# answer that does not state the same amount AND currency completes nothing.
			amount = data.get("orderAmount") if data.get("orderAmount") is not None else data.get("amount")
			currency = str(data.get("orderCurrency") or data.get("currency") or "").strip().upper()
			if (
				amount is None
				or flt(amount, 2) != flt(transaction.amount, 2)
				or currency != (transaction.currency or "").upper()
			):
				return {
					"status": "Pending",
					"raw": raw,
					"review_reason": (
						f"Jenga's status query says this was paid, but for {amount} {currency or '(no currency)'}, "
						f"not {transaction.amount} {transaction.currency}. It was not completed. Check it in "
						"the JengaHQ portal; if it was paid in full, confirm it as paid here."
					),
				}
			return {
				"status": "Completed",
				"gateway_receipt": data.get("transactionReference") or None,
				"result_description": state,
				"raw": raw,
			}
		if state in NOT_PAID or code == "1":
			return {"status": "Failed", "failure_reason": f"Jenga: {state or 'failed'}", "raw": raw}
		return {"status": "Pending", "raw": raw}

	def confirm_callback(self, transaction, result, data):
		"""Only reached for a notification from an authenticated sender, which is Jenga's own
		word; the query, when switched on, is asked as well.

		A different amount or currency is never taken as paid, even on Jenga's word: the
		checkout form passes through the payer's browser, and unless the merchant signs its
		orders (secure mode) the amount in it can be edited there before it reaches Jenga."""
		self.manual_check_reason = None
		mismatch = self.handle_callback(data, transaction).get("amount_mismatch")
		if mismatch:
			self.manual_check_reason = (
				f"{mismatch} The card form passes through the payer's browser, so a payment for a "
				"different amount is never completed automatically"
			)
			return MANUAL_CHECK
		if not cint(self._require_account().get("query_status")):
			return CONFIRMED
		answer = self.query(transaction, timeout=10)
		if answer.get("review_reason"):
			self.manual_check_reason = answer["review_reason"]
			return MANUAL_CHECK
		if answer["status"] == "Completed":
			return CONFIRMED
		if answer["status"] == "Failed":
			return CONTRADICTED
		return UNAVAILABLE

	# ── the notification ─────────────────────────────────────────────

	@staticmethod
	def callback_reference(data):
		"""Our order reference, from whichever field of the notification carries it."""
		candidates = candidate_references(data)
		if not candidates:
			return None
		found = frappe.get_all(
			DETAIL, filters={"order_reference": ["in", candidates]}, pluck="order_reference", limit=2
		)
		return found[0] if len(found) == 1 else None

	@staticmethod
	def presented_credentials():
		"""(username, password) from the request's HTTP Basic header, or (None, None)."""
		# On a real request the header was set aside before Frappe could read it as an API-key
		# login (onerc_payments.request_hooks); a direct call still carries it.
		header = frappe.local.flags.get("jenga_authorization")
		if not header:
			try:
				header = frappe.request.headers.get("Authorization") if frappe.request else None
			except Exception:
				header = None
		if not header or not header.lower().startswith("basic "):
			return None, None
		try:
			username, _colon, password = base64.b64decode(header[6:].strip()).decode().partition(":")
		except Exception:
			return None, None
		return username, password

	@classmethod
	def authenticated_account(cls):
		"""The Jenga Account whose notification credentials the request presents, or None."""
		username, password = cls.presented_credentials()
		if not username or not password:
			return None
		for name in frappe.get_all(ACCOUNT, filters={"ipn_username": username}, pluck="name"):
			account = frappe.get_doc(ACCOUNT, name)
			expected = account.get_password("ipn_password", raise_exception=False) or ""
			if expected and hmac.compare_digest(expected.encode(), password.encode()):
				return name
		return None

	def verify_callback_source(self):
		return bool(self.account) and self.authenticated_account() == self.account.name

	def handle_callback(self, data, transaction):
		txn, whole = _notification(data)
		status = str(txn.get("status") or whole.get("status") or "").strip().upper()
		remarks = txn.get("remarks") or whole.get("remarks")
		if status in NOT_PAID:
			return {
				"status": "Failed",
				"failure_reason": f"Card payment {status.lower()}" + (f": {remarks}" if remarks else ""),
				"result_code": status,
				"result_description": remarks,
			}
		if status not in PAID:
			return {"status": "Pending", "failure_reason": f"Not a payment result ({status or 'no status'})"}

		paid, currency = _ordered(txn)
		problems = []
		if paid is None or flt(paid, 2) != flt(transaction.amount, 2):
			problems.append(f"Jenga says an order of {paid} was paid, but {transaction.amount} was asked for")
		if currency != (transaction.currency or "").upper():
			problems.append(f"in {currency or 'no currency'}, not {transaction.currency}")
		return {
			"status": "Completed",
			"gateway_receipt": self._receipt_of(data, transaction),
			"result_code": status,
			"result_description": remarks,
			"transaction_date": self._date(txn.get("date")),
			"amount_mismatch": ("; ".join(problems) + ".") if problems else None,
		}

	@staticmethod
	def _receipt_of(data, transaction):
		"""Jenga's own reference for the payment (never our order reference)."""
		txn, whole = _notification(data)
		bank = whole.get("bank") if isinstance(whole.get("bank"), dict) else {}
		for value in (txn.get("transactionId"), txn.get("reference"), bank.get("reference")):
			text = str(value or "").strip()
			if text and text != transaction.gateway_reference:
				return text
		return None

	@staticmethod
	def _date(value):
		try:
			return get_datetime(str(value).replace("T", " ")[:19]) if value else None
		except Exception:
			return None

	# ── the Jenga Payment record ─────────────────────────────────────

	def _upsert(self, transaction, values):
		name = upsert_detail(DETAIL, transaction.name, {"gateway": self.gateway_name, **values})
		transaction.gateway_detail_doctype = DETAIL
		transaction.gateway_detail = name
		return name

	def record_initiation_details(self, transaction, result):
		return self._upsert(
			transaction,
			{
				"order_reference": result.get("gateway_reference"),
				"status": "Pending" if result.get("success") else "Failed",
				"amount": transaction.amount,
				"currency": transaction.currency,
				"return_url": transaction.flags.get("return_url"),
			},
		)

	def record_payment_details(self, data, transaction):
		txn, whole = _notification(data)
		receipt = self._receipt_of(data, transaction)
		existing = frappe.db.get_value(DETAIL, {"payment_transaction": transaction.name}, "jenga_reference")
		if existing and receipt and receipt != existing:
			return None
		bank = whole.get("bank") if isinstance(whole.get("bank"), dict) else {}
		paid, currency = _ordered(txn)
		status = str(txn.get("status") or whole.get("status") or "").upper()
		return self._upsert(
			transaction,
			{
				"status": "Completed" if status in PAID else "Failed" if status in NOT_PAID else None,
				"jenga_reference": receipt,
				"receipt_source": "Notification" if receipt else None,
				"payment_mode": txn.get("paymentMode") or whole.get("desc"),
				"paid_amount": paid,
				"paid_currency": currency or None,
				"amount_matched": 1
				if paid is not None
				and flt(paid, 2) == flt(transaction.amount, 2)
				and currency == (transaction.currency or "").upper()
				else 0,
				"bank_reference": bank.get("reference"),
				"remarks": txn.get("remarks"),
				"raw_notification": frappe.as_json(data),
			},
		)

	def record_status_update(self, transaction, result):
		return self._upsert(
			transaction, {"status": result.get("status"), "raw_status_result": result.get("raw")}
		)

	# ── receipts ─────────────────────────────────────────────────────

	def normalise_receipt(self, value):
		code = "".join(str(value or "").split())
		if not RECEIPT_PATTERN.match(code):
			frappe.throw(
				f"{value or 'That'} is not a Jenga transaction reference: 4 to 40 letters, numbers "
				"or hyphens, as shown for the payment in the JengaHQ portal."
			)
		return code

	def record_entered_receipt(self, transaction, receipt):
		return self._upsert(
			transaction,
			{"jenga_reference": receipt, "receipt_source": "Entered by Staff", "status": "Completed"},
		)

	def generate_receipt(self, transaction):
		return (
			f"CARD PAYMENT RECEIPT\n"
			f"Reference:   {transaction.gateway_receipt}\n"
			f"Amount:      {transaction.currency} {transaction.amount}\n"
			f"Order:       {transaction.gateway_reference}\n"
		)
