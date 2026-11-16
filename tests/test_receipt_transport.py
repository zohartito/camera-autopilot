"""CAM-10: synthetic local endpoints must not forward receipt credentials."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/receipts"))
import _common as common
import night_session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import plan_night_session


@contextmanager
def endpoint(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("method", ["get", "post", "get_bytes", "night_plan", "plan_submit"])
def test_authenticated_receipts_do_not_follow_cross_origin_redirects(method):
    received = []
    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            received.append(bool(self.headers.get(common.TOKEN_HEADER)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
        do_POST = do_GET
    with endpoint(Sink) as other:
        class Redirect(Sink):
            def do_GET(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(302)
                self.send_header("Location", other + "/collect")
                self.end_headers()
            do_POST = do_GET
        with endpoint(Redirect) as origin:
            client = common.Client(origin, "synthetic-receipt-test-only")
            if method == "plan_submit":
                with pytest.raises(OSError, match="302"):
                    plan_night_session._submit({"plan": "fixture"}, origin, "synthetic-receipt-test-only")
            elif method == "night_plan":
                request, _, error = night_session._night_plan(
                    client, image="meter.jpg", model="fixture", port="usb:fixture",
                    serial="fixture-serial", max_frames=2)
                assert request is None and "302" in error
            else:
                status, _ = getattr(client, method)("/receipt")
                assert status == 302
    assert received == [], "a second origin received the authenticated request"


@pytest.mark.parametrize("url", ["http://camera.example.test:8000", "http://192.0.2.1:8000",
                                 "http://[2001:db8::1]:8000", "ftp://camera.example.test",
                                 "https://user:password@camera.example.test"])
def test_receipt_client_rejects_unsafe_base_url_before_request(url):
    with pytest.raises(SystemExit):
        common.Client(url, "synthetic-receipt-test-only")
    with pytest.raises(SystemExit):
        plan_night_session._submit({"plan": "fixture"}, url, "synthetic-receipt-test-only")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000",
                                 "http://[::1]:8000", "https://camera.example.test"])
def test_receipt_client_accepts_loopback_http_or_remote_https_without_network(url):
    assert common.Client(url, "synthetic-receipt-test-only").base_url == url
