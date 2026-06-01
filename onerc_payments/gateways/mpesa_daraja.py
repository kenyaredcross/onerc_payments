# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
M-Pesa Daraja API gateway driver.
Supports STK Push (inbound) and B2C (outbound).

Daraja API documentation:
https://developer.safaricom.co.ke/Documentation
"""

import base64
from datetime import datetime

import frappe
import requests
from frappe.utils.password import get_decrypted_password

from .base import BaseGateway

SANDBOX_BASE = "https://sandbox.safaricom.co.ke"
PRODUCTION_BASE = "https://api.safaricom.co.ke"


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
			"PartyA": transaction.payer_phone,
			"PartyB": self.settings.mpesa_shortcode,
			"PhoneNumber": transaction.payer_phone,
			"CallBackURL": self._callback_url(transaction),
			"AccountReference": transaction.name,
			"TransactionDesc": (
				f"{transaction.source_app} — {transaction.source_document}"
			),
		}

		try:
			response = requests.post(
				f"{self._base_url()}/mpesa/stkpush/v1/processrequest",
				json=payload,
				headers={"Authorization": f"Bearer {self._get_token()}"},
				timeout=30,
			)
			data = response.json()
		except Exception as e:
			return {"success": False, "message": str(e)}

		if data.get("ResponseCode") == "0":
			return {
				"success": True,
				"gateway_reference": data.get("CheckoutRequestID"),
				"message": (
					"Check your phone and enter your M-Pesa PIN "
					"to complete the payment."
				),
			}

		return {
			"success": False,
			"gateway_reference": data.get("CheckoutRequestID"),
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

		if result_code == "0":
			return {
				"status": "Completed",
				"gateway_receipt": data.get("MpesaReceiptNumber"),
			}

		if result_code in ("1032", "1037"):
			return {
				"status": "Failed",
				"failure_reason": data.get("ResultDesc"),
			}

		return {"status": "Pending"}

	def handle_callback(self, data, transaction):
		try:
			callback = data["Body"]["stkCallback"]
		except (KeyError, TypeError):
			return {
				"status": "Failed",
				"failure_reason": "Invalid callback structure",
			}

		result_code = callback.get("ResultCode")

		if result_code == 0:
			receipt = None
			items = (
				callback.get("CallbackMetadata", {}).get("Item", [])
			)
			for item in items:
				if item.get("Name") == "MpesaReceiptNumber":
					receipt = item.get("Value")
					break

			return {
				"status": "Completed",
				"gateway_receipt": receipt,
			}

		return {
			"status": "Failed",
			"failure_reason": callback.get("ResultDesc"),
		}

	def generate_receipt(self, transaction):
		return (
			f"M-PESA PAYMENT RECEIPT\n"
			f"Receipt No:  {transaction.gateway_receipt}\n"
			f"Amount:      {transaction.currency} {transaction.amount}\n"
			f"Phone:       {transaction.payer_phone}\n"
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

		try:
			response = requests.post(
				f"{self._base_url()}/mpesa/b2c/v1/paymentrequest",
				json=payload,
				headers={"Authorization": f"Bearer {self._get_token()}"},
				timeout=30,
			)
			data = response.json()
		except Exception as e:
			return {"success": False, "message": str(e)}

		if data.get("ResponseCode") == "0":
			return {
				"success": True,
				"gateway_reference": data.get("ConversationID"),
				"message": "Payment initiated to recipient.",
			}

		return {
			"success": False,
			"message": data.get("ResponseDescription"),
		}