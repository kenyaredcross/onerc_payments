"""Expose ONLY KCB's callback to the internet, for a live test from a dev machine.

A tunnel pointed straight at the bench would publish the whole site (desk login, data).
Point the tunnel at this relay instead: it forwards exactly ``POST /api/method/kcb`` to the
local bench for one site, and answers 404 to everything else. Standard library only.

    python3 kcb_callback_relay.py --site kenyaredcross.localhost
    # then point the tunnel at http://127.0.0.1:8099

Logs one line per request (time, sender, method, path, result) - never bodies or headers.
"""

import argparse
import http.server
import time
import urllib.error
import urllib.request

PATH = "/api/method/kcb"
MAX_BODY = 64 * 1024


def main():
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--site", required=True, help="the bench site, e.g. kenyaredcross.localhost")
	parser.add_argument("--port", type=int, default=8099, help="where the tunnel connects (default 8099)")
	parser.add_argument("--upstream", default="http://127.0.0.1:8000", help="the bench web server")
	parser.add_argument(
		"--bind", default="127.0.0.1", help="0.0.0.0 when the tunnel runs on Windows and this in WSL"
	)
	args = parser.parse_args()

	class Relay(http.server.BaseHTTPRequestHandler):
		server_version = "relay"
		sys_version = ""

		def log_message(self, *a):
			pass

		def _say(self, result):
			sender = self.headers.get("X-Forwarded-For") or self.client_address[0]
			print(
				f"{time.strftime('%H:%M:%S')}  {sender:<40}  {self.command:<6} {self.path[:80]}  -> {result}",
				flush=True,
			)

		def _refuse(self, code=404):
			self.send_response(code)
			self.send_header("Content-Length", "0")
			self.end_headers()
			self._say(code)

		def do_POST(self):
			if self.path != PATH:
				return self._refuse()
			try:
				length = int(self.headers.get("Content-Length") or 0)
			except ValueError:
				return self._refuse(400)
			if length < 0 or length > MAX_BODY:
				return self._refuse(413)
			body = self.rfile.read(length)
			forwarded = ", ".join(
				part for part in (self.headers.get("X-Forwarded-For"), self.client_address[0]) if part
			)
			request = urllib.request.Request(
				args.upstream.rstrip("/") + PATH,
				data=body,
				method="POST",
				headers={
					"Host": args.site,
					"Content-Type": self.headers.get("Content-Type") or "application/json",
					"X-Forwarded-For": forwarded,
				},
			)
			try:
				with urllib.request.urlopen(request, timeout=30) as response:
					status, reply = response.status, response.read()
			except urllib.error.HTTPError as e:
				status, reply = e.code, e.read()
			except Exception as e:
				status, reply = 502, b""
				print(f"   upstream failed: {type(e).__name__}", flush=True)
			self.send_response(status)
			self.send_header("Content-Type", "application/json")
			self.send_header("Content-Length", str(len(reply)))
			self.end_headers()
			self.wfile.write(reply)
			self._say(status)

		def do_GET(self):
			self._refuse()

		do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_GET

	server = http.server.ThreadingHTTPServer((args.bind, args.port), Relay)
	print(f"Relaying POST {PATH} on {args.bind}:{args.port} to {args.upstream} as {args.site}. Ctrl+C stops.")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	main()
