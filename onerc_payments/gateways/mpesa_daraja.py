# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
M-Pesa Daraja API gateway driver.
Supports STK Push (inbound) and B2C (outbound).

Daraja API documentation:
https://developer.safaricom.co.ke/Documentation
"""

import base64
import ipaddress
from datetime import datetime

import frappe
import requests
from frappe.utils.password import get_decrypted_password

from .base import BaseGateway

SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE = "https://api.safaricom.co.ke"

# Safaricom Daraja publishes a fixed set of source IPs for callbacks.
# Reference: https://developer.safaricom.co.ke/Documentation
# Operators can extend this via frappe.conf.mpesa_extra_allowed_ips
# (comma-separated string or list, supports CIDR) for staging proxies.
SAFARICOM_PRODUCTION_IPS = frozenset([
	"196.201.212.69",
	"196.201.212.74",
	"196.201.212.127",
	"196.201.212.129",
	"196.201.212.136",
	"196.201.212.138",
	"196.201.213.44",
	"196.201.213.114",
	"196.201.214.200",
	"196.201.214.206",
	"196.201.214.207",
	"196.201.214.208",
])


def client_ip():
	"""Return the request's originating IP. Frappe populates
	frappe.local.request_ip per request, resolving X-Forwarded-For when behind
	a trusted reverse proxy (configured via the host's proxy settings)."""
	return getattr(frappe.local, "request_ip", None) or ""


def _as_int(value, default=-1):
	"""Daraja is inconsistent about whether a result code is an int or a string."""
	try:
		return int(value)
	except (TypeError, ValueError):
		return default


class MpesaDarajaGateway(BaseGateway):

	def _base_url(self):
		if self.settings.environment == "Production":
			return PRODUCTION_BASE
		return SANDBOX_BASE

	def _get_token(self):
		cache_key = "onerc_payments:daraja_token"
		cached = frappe.cache().get_value(cache_key)
		if cached:
			return cached

		key = get_decrypted_password(
			"OneRC Payment Settings",
			"OneRC Payment Settings",
			"mpesa_consumer_key",
			raise_exception=False,
		)
		secret = get_decrypted_password(
			"OneRC Payment Settings",
			"OneRC Payment Settings",
			"mpesa_consumer_secret",
			raise_exception=False,
		)

		if not key or not secret:
			frappe.throw(
				"M-Pesa consumer key or secret is not configured. "
				"Go to OneRC Payment Settings and enter your credentials."
			)

		url = f"{self._base_url()}/oauth/v1/generate?grant_type=client_credentials"
		response = requests.get(
			url,
			auth=(key, secret),
			timeout=30,
		)
		response.raise_for_status()
		token = response.json().get("access_token")

		frappe.cache().set_value(cache_key, token, expires_in_sec=3300)
		return token

	def _generate_password(self, timestamp):
		passkey = get_decrypted_password(
			"OneRC Payment Settings",
			"OneRC Payment Settings",
			"mpesa_passkey",
			raise_exception=False,
		)

		if not passkey:
			frappe.throw(
				"M-Pesa passkey is not configured. "
				"Go to OneRC Payment Settings and enter your passkey."
			)

		raw = (
			self.settings.mpesa_shortcode
			+ passkey
			+ timestamp
		)
		return base64.b64encode(raw.encode()).decode()

	def _timestamp(self):
		return datetime.now().strftime("%Y%m%d%H%M%S")

	def _callback_url(self, transaction):
		"""The URL Safaricom posts the receipt to. Everything depends on it.

		The receipt only ever arrives on this callback, so a base URL that Safaricom
		cannot reach means no M-Pesa code, ever - while the payment itself still
		succeeds and the status poll still marks it Completed. That failure is silent,
		so refuse to push at all rather than take a payment we can never receipt.
		"""
		base = (self.settings.mpesa_callback_base_url or "").strip().rstrip("/")
		if not base:
			base = (frappe.utils.get_url() or "").strip().rstrip("/")
		if not base.startswith("https://") or "localhost" in base or "127.0.0.1" in base:
			frappe.throw(
				"M-Pesa cannot deliver a payment receipt to "
				f"{base or '(no callback URL)'}. Set OneRC Payment Settings > "
				"M-Pesa Callback Base URL to a public https address for this site."
			)
		return (
			f"{base}/api/method/onerc_payments.api.v1.payment"
			f".payment_callback?gateway_name=Mpesa+Daraja"
		)

	@staticmethod
	def _redacted(payload):
		"""Return a JSON string of the request payload with secrets removed,
		safe to persist for audit/debugging."""
		safe = {
			k: v for k, v in payload.items()
			if k not in ("Password", "SecurityCredential")
		}
		return frappe.as_json(safe)

	# ── CALLBACK SOURCE VERIFICATION ─────────────────────────────────

	def _candidate_source_ips(self):
		"""Every plausible source IP for the current callback, most-trusted first.

		In production a callback usually passes through one or more reverse proxies
		(nginx, a load balancer, a CDN), so Safaricom's real IP may sit in
		request_ip, anywhere along the X-Forwarded-For chain, or in a CDN header
		rather than in the socket peer address. We gather every candidate and accept
		the callback if any one of them is a known Safaricom IP - a genuine callback
		only needs its real IP to appear somewhere in the forwarded chain.
		"""
		candidates = []

		def add(value):
			value = (value or "").strip()
			if value and value not in candidates:
				candidates.append(value)

		add(client_ip())
		xff = frappe.get_request_header("X-Forwarded-For") or ""
		for part in xff.split(","):
			add(part)
		for header in ("CF-Connecting-IP", "True-Client-IP", "X-Real-IP"):
			add(frappe.get_request_header(header))

		return candidates

	@staticmethod
	def _ip_in_allowlist(src, extra):
		"""True if one source IP is a Safaricom IP or matches an operator allowlist
		entry (a plain IP or a CIDR block)."""
		if src in SAFARICOM_PRODUCTION_IPS:
			return True
		try:
			src_addr = ipaddress.ip_address(src)
		except ValueError:
			return False
		for entry in extra:
			entry = entry.strip()
			if not entry:
				continue
			try:
				if "/" in entry:
					if src_addr in ipaddress.ip_network(entry, strict=False):
						return True
				elif src_addr == ipaddress.ip_address(entry):
					return True
			except ValueError:
				continue
		return False

	def verify_callback_source(self):
		"""Only accept callbacks from Safaricom's published IPs in production.

		In Sandbox, allow any source so the Daraja STK simulator can drive the flow.
		In production we check *every* candidate source IP (request_ip, the full
		X-Forwarded-For chain, and common CDN headers) so a genuine callback isn't
		rejected just because a proxy hop rewrote the peer address - the previous
		single-IP check was the most likely reason receipts weren't landing behind a
		reverse proxy. Operators can widen the allowlist via
		frappe.conf.mpesa_extra_allowed_ips (comma-separated, CIDR supported), or -
		for proxy topologies where the origin IP genuinely can't be recovered - set
		frappe.conf.mpesa_verify_callback_ip = 0 to fall back to the unguessable
		per-transaction CheckoutRequestID match (which is the real security gate).
		"""
		if self.settings.environment != "Production":
			return True

		if not frappe.conf.get("mpesa_verify_callback_ip", True):
			frappe.logger().warning(
				"onerc_payments: M-Pesa callback IP verification is disabled "
				"(mpesa_verify_callback_ip=0); relying on the CheckoutRequestID match."
			)
			return True

		extra = frappe.conf.get("mpesa_extra_allowed_ips") or []
		if isinstance(extra, str):
			extra = [x.strip() for x in extra.split(",") if x.strip()]

		return any(
			self._ip_in_allowlist(src, extra)
			for src in self._candidate_source_ips()
		)

	# ── INBOUND — STK Push ───────────────────────────────────────────

	def initiate(self, transaction):
		if transaction.direction == "Outbound":
			return self._initiate_b2c(transaction)

		timestamp = self._timestamp()
		payload = {
			"BusinessShortCode": self.settings.mpesa_shortcode,
			"Password": self._generate_password(timestamp),
			"Timestamp": timestamp,
			"TransactionType": "CustomerPayBillOnline",
			"Amount": int(transaction.amount),
			"PartyA": transaction.phone_number,
			"PartyB": self.settings.mpesa_shortcode,
			"PhoneNumber": transaction.phone_number,
			"CallBackURL": self._callback_url(transaction),
			"AccountReference": transaction.name,
			"TransactionDesc": (
				f"{transaction.source_app} — {transaction.source_document}"
			),
		}

		raw_request = self._redacted(payload)

		try:
			response = requests.post(
				f"{self._base_url()}/mpesa/stkpush/v1/processrequest",
				json=payload,
				headers={"Authorization": f"Bearer {self._get_token()}"},
				timeout=30,
			)
			data = response.json()
		except Exception as e:
			return {
				"success": False,
				"raw_request": raw_request,
				"message": str(e),
			}

		if data.get("ResponseCode") == "0":
			return {
				"success": True,
				"gateway_reference": data.get("CheckoutRequestID"),
				"merchant_request_id": data.get("MerchantRequestID"),
				"raw_request": raw_request,
				"raw_response": frappe.as_json(data),
				"message": (
					"Check your phone and enter your M-Pesa PIN "
					"to complete the payment."
				),
			}

		return {
			"success": False,
			"gateway_reference": data.get("CheckoutRequestID"),
			"merchant_request_id": data.get("MerchantRequestID"),
			"raw_request": raw_request,
			"raw_response": frappe.as_json(data),
			"message": data.get("CustomerMessage") or data.get("errorMessage"),
		}

	def check_status(self, transaction):
		timestamp = self._timestamp()
		payload = {
			"BusinessShortCode": self.settings.mpesa_shortcode,
			"Password": self._generate_password(timestamp),
			"Timestamp": timestamp,
			"CheckoutRequestID": transaction.gateway_reference,
		}

		try:
			response = requests.post(
				f"{self._base_url()}/mpesa/stkpushquery/v1/query",
				json=payload,
				headers={"Authorization": f"Bearer {self._get_token()}"},
				timeout=30,
			)
			data = response.json()
		except Exception as e:
			frappe.logger().error(f"Daraja status query failed: {e}")
			return {"status": "Pending"}

		result_code = str(data.get("ResultCode", ""))
		result_desc = data.get("ResultDesc")

		if result_code == "0":
			return {
				"status": "Completed",
				"gateway_receipt": data.get("MpesaReceiptNumber"),
				"result_code": result_code,
				"result_description": result_desc,
			}

		# 1032 = cancelled by user, 1037 = timeout (PIN not entered).
		if result_code in ("1032", "1037"):
			return {
				"status": "Failed",
				"failure_reason": result_desc,
				"result_code": result_code,
				"result_description": result_desc,
			}

		return {
			"status": "Pending",
			"result_code": result_code or None,
			"result_description": result_desc,
		}

	def handle_callback(self, data, transaction):
		try:
			callback = data["Body"]["stkCallback"]
		except (KeyError, TypeError):
			return {
				"status": "Failed",
				"failure_reason": "Invalid callback structure",
			}

		# Daraja sends ResultCode as an int on the STK callback and as a string on the
		# query response. Comparing it strictly to 0 read a successful "0" as a failure
		# and dropped the receipt with it, so coerce before deciding.
		result_code = _as_int(callback.get("ResultCode"))
		result_desc = callback.get("ResultDesc")

		if result_code == 0:
			meta = {
				item.get("Name"): item.get("Value")
				for item in (callback.get("CallbackMetadata") or {}).get("Item", [])
			}
			receipt = meta.get("MpesaReceiptNumber")

			# Cross-check the amount the customer actually paid against what we
			# requested. A mismatch shouldn't silently pass — flag it loudly.
			paid = meta.get("Amount")
			if paid is not None and float(paid) != float(transaction.amount):
				frappe.logger().warning(
					f"onerc_payments: M-Pesa amount mismatch on {transaction.name} "
					f"— requested {transaction.amount}, paid {paid}"
				)

			return {
				"status": "Completed",
				"gateway_receipt": receipt,
				"result_code": str(result_code),
				"result_description": result_desc,
				"transaction_date": self._parse_mpesa_date(
					meta.get("TransactionDate")
				),
			}

		return {
			"status": "Failed",
			"failure_reason": result_desc,
			"result_code": str(result_code),
			"result_description": result_desc,
		}

	def record_payment_details(self, data, transaction):
		"""Capture the full STK callback into an ``Mpesa Payment`` record and link it
		to the transaction.

		Idempotent by CheckoutRequestID (one detail record per STK request), so a
		Safaricom retry updates the existing record rather than duplicating it. Runs
		only after the callback source + reference have been verified upstream; it
		does not widen trust. The record is a desk-only audit doctype (no web
		exposure) and all fields are read-only, so it can't be tampered with via the
		form. Amount is cross-checked against the requested amount and flagged.
		"""
		try:
			callback = data["Body"]["stkCallback"]
		except (KeyError, TypeError):
			return None

		checkout_id = callback.get("CheckoutRequestID")
		if not checkout_id:
			return None

		result_code = callback.get("ResultCode")
		meta = {
			item.get("Name"): item.get("Value")
			for item in (callback.get("CallbackMetadata", {}) or {}).get("Item", [])
		}

		paid = meta.get("Amount")
		expected = float(transaction.amount or 0)
		amount_matched = 1
		if paid is not None:
			try:
				amount_matched = 1 if float(paid) == expected else 0
			except (TypeError, ValueError):
				amount_matched = 0

		phone = meta.get("PhoneNumber")
		balance = meta.get("Balance")
		values = {
			"payment_transaction": transaction.name,
			"merchant_request_id": callback.get("MerchantRequestID"),
			"checkout_request_id": checkout_id,
			"status": "Completed" if result_code == 0 else "Failed",
			"result_code": str(result_code) if result_code is not None else None,
			"result_description": callback.get("ResultDesc"),
			"mpesa_receipt_number": meta.get("MpesaReceiptNumber"),
			"amount": paid,
			"expected_amount": expected,
			"amount_matched": amount_matched,
			"phone_number": str(phone) if phone is not None else None,
			"balance": str(balance) if balance is not None else None,
			"transaction_date": self._parse_mpesa_date(meta.get("TransactionDate")),
			"callback_ip": client_ip(),
			"raw_callback": frappe.as_json(data),
		}

		doc = self._upsert_mpesa_payment(checkout_id, values)
		transaction.gateway_detail_doctype = "Mpesa Payment"
		transaction.gateway_detail = doc.name
		return doc.name

	def record_status_update(self, transaction, result):
		"""Update the linked ``Mpesa Payment`` record from an STK status query.

		The STK *query* API does not return the M-Pesa receipt number - only the
		callback carries that - so this mainly moves the detail record off
		"Initiated"/"Pending" onto its resolved status. If a receipt is somehow
		present it is stored too. Keyed by CheckoutRequestID, so it updates the
		existing detail record rather than creating a duplicate.
		"""
		checkout_id = transaction.gateway_reference
		if not checkout_id:
			return None

		values = {
			"payment_transaction": transaction.name,
			"checkout_request_id": checkout_id,
			"status": result.get("status"),
			"result_code": result.get("result_code"),
			"result_description": (
				result.get("result_description") or result.get("failure_reason")
			),
			"mpesa_receipt_number": result.get("gateway_receipt"),
		}
		return self._upsert_mpesa_payment(checkout_id, values).name

	def record_initiation_details(self, transaction, result):
		"""Open the ``Mpesa Payment`` detail record at STK initiation.

		Stores the CheckoutRequestID/MerchantRequestID and the raw STK request/
		response so those gateway-specific fields live on the detail doctype, not the
		generic transaction. The callback later updates the same record (keyed by
		CheckoutRequestID) with the receipt and metadata.
		"""
		checkout_id = result.get("gateway_reference") or transaction.gateway_reference
		if not checkout_id:
			return None

		values = {
			"payment_transaction": transaction.name,
			"checkout_request_id": checkout_id,
			"merchant_request_id": result.get("merchant_request_id"),
			# A successful STK push means the prompt reached the customer's phone and
			# we're waiting on their PIN ("Pending"); a failed push is "Failed". Either
			# is truer than a stuck "Initiated" that never advances.
			"status": "Pending" if result.get("success") else "Failed",
			"expected_amount": float(transaction.amount or 0),
			"raw_request": result.get("raw_request"),
			"raw_response": result.get("raw_response"),
		}
		doc = self._upsert_mpesa_payment(checkout_id, values)
		transaction.gateway_detail_doctype = "Mpesa Payment"
		transaction.gateway_detail = doc.name
		return doc.name

	@staticmethod
	def _upsert_mpesa_payment(checkout_id, values):
		"""Insert or update the Mpesa Payment keyed by CheckoutRequestID.

		None values are dropped so a later stage never blanks a field an earlier
		stage set (e.g. the callback must not wipe the initiation's raw_request).
		"""
		values = {k: v for k, v in values.items() if v is not None}
		values["checkout_request_id"] = checkout_id
		existing = frappe.db.get_value("Mpesa Payment", {"checkout_request_id": checkout_id}, "name")
		if existing:
			doc = frappe.get_doc("Mpesa Payment", existing)
			doc.update(values)
			doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({"doctype": "Mpesa Payment", **values})
			doc.insert(ignore_permissions=True)
		return doc

	@staticmethod
	def _parse_mpesa_date(value):
		"""M-Pesa reports TransactionDate as an int like 20191219102115
		(YYYYMMDDHHMMSS). Convert to a datetime string, or None on failure."""
		if not value:
			return None
		try:
			return datetime.strptime(str(value), "%Y%m%d%H%M%S")
		except (ValueError, TypeError):
			return None

	def generate_receipt(self, transaction):
		return (
			f"M-PESA PAYMENT RECEIPT\n"
			f"Receipt No:  {transaction.gateway_receipt}\n"
			f"Amount:      {transaction.currency} {transaction.amount}\n"
			f"Phone:       {transaction.phone_number}\n"
			f"Date:        {transaction.transaction_date}\n"
			f"Reference:   {transaction.name}\n"
		)

	# ── OUTBOUND — B2C ───────────────────────────────────────────────

	def _initiate_b2c(self, transaction):
		payload = {
			"InitiatorName": self.settings.mpesa_shortcode,
			"SecurityCredential": get_decrypted_password(
				"OneRC Payment Settings",
				"OneRC Payment Settings",
				"mpesa_passkey",
				raise_exception=False,
			),
			"CommandID": "BusinessPayment",
			"Amount": int(transaction.amount),
			"PartyA": self.settings.mpesa_shortcode,
			"PartyB": transaction.recipient_phone,
			"Remarks": (
				f"{transaction.source_app} — {transaction.source_document}"
			),
			"QueueTimeOutURL": self._callback_url(transaction),
			"ResultURL": self._callback_url(transaction),
			"Occasion": transaction.name,
		}

		raw_request = self._redacted(payload)

		try:
			response = requests.post(
				f"{self._base_url()}/mpesa/b2c/v1/paymentrequest",
				json=payload,
				headers={"Authorization": f"Bearer {self._get_token()}"},
				timeout=30,
			)
			data = response.json()
		except Exception as e:
			return {
				"success": False,
				"raw_request": raw_request,
				"message": str(e),
			}

		if data.get("ResponseCode") == "0":
			return {
				"success": True,
				"gateway_reference": data.get("ConversationID"),
				"merchant_request_id": data.get("OriginatorConversationID"),
				"raw_request": raw_request,
				"raw_response": frappe.as_json(data),
				"message": "Payment initiated to recipient.",
			}

		return {
			"success": False,
			"raw_request": raw_request,
			"raw_response": frappe.as_json(data),
			"message": data.get("ResponseDescription"),
		}