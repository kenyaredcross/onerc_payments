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
		base = self.settings.mpesa_callback_base_url.rstrip("/")
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

	def verify_callback_source(self):
		"""Only accept callbacks from Safaricom's published IPs in production.
		In Sandbox, allow any source so the Daraja STK simulator can drive
		the flow. Operators may widen the allowlist (e.g. for a staging
		proxy) via frappe.conf.mpesa_extra_allowed_ips."""
		if self.settings.environment != "Production":
			return True

		src = (client_ip() or "").strip()
		if not src:
			return False

		if src in SAFARICOM_PRODUCTION_IPS:
			return True

		extra = frappe.conf.get("mpesa_extra_allowed_ips") or []
		if isinstance(extra, str):
			extra = [x.strip() for x in extra.split(",") if x.strip()]

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

		result_code = callback.get("ResultCode")
		result_desc = callback.get("ResultDesc")

		if result_code == 0:
			meta = {
				item.get("Name"): item.get("Value")
				for item in callback.get("CallbackMetadata", {}).get("Item", [])
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
			"status": "Initiated",
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