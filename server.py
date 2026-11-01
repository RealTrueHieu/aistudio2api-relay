import os
import sys
import time
import glob
import json
import io
import tarfile
import subprocess
import threading
import collections
import urllib.request
import urllib.error
import http.server
import http.client
from cryptography.fernet import Fernet

recent_logs = collections.deque(maxlen=2000)

def log(msg):
    t = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{t}] {msg}"
    print(entry, flush=True)
    recent_logs.append(entry)

def log_reader(pipe, name):
    for line in iter(pipe.readline, b""):
        text = line.decode("utf-8", errors="replace").rstrip()
        entry = f"[{name}] {text}"
        print(entry, flush=True)
        recent_logs.append(entry)
    pipe.close()

# 1. Unpack auth bundle
def setup_auth():
    log("=== [1/5] Decrypting Authentication Bundle ===")
    key = os.environ.get("AUTH_KEY", "").strip()
    if not key:
        log("[!] ERROR: Missing AUTH_KEY environment variable!")
        sys.exit(1)

    try:
        f = Fernet(key.encode("ascii"))
        with open("/app/bundle.enc", "rb") as ef:
            enc_bytes = ef.read()
        dec_bytes = f.decrypt(enc_bytes)
        with tarfile.open(fileobj=io.BytesIO(dec_bytes), mode="r:gz") as tar:
            tar.extractall("/app")
        log("[+] Successfully unpacked auth and proxies into /app!")
    except Exception as e:
        log(f"[!] Decrypt error: {e}")
        sys.exit(1)

    log("=== [2/5] Resetting Stale Account Cooldowns ===")
    for p in glob.glob("/app/auth/*/runtime-state.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["cooldowns"] = {}
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log(f"[+] Cleared cooldowns in: {p}")
        except Exception as e:
            log(f"[!] Error updating {p}: {e}")

# 2. Start rotator and test connectivity
def setup_proxies():
    log("=== [3/5] Starting Sticky Residential Proxy on 127.0.0.1:10800 ===")
    p = subprocess.Popen(
        ["python3", "-u", "/app/rotator.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1
    )
    threading.Thread(target=log_reader, args=(p.stdout, "rotator"), daemon=True).start()
    time.sleep(2)

    log("=== [4/5] Testing Outbound Connectivity ===")
    proxy_ok = False
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({
                "http": "http://127.0.0.1:10800",
                "https": "http://127.0.0.1:10800"
            })
        )
        req = urllib.request.Request("https://api.ipify.org?format=json", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=8) as r:
            res = r.read().decode("utf-8")
            log(f"[+] Sticky proxy confirmed working! Outbound IP: {res.strip()}")
            proxy_ok = True
    except Exception as e:
        log(f"[!] Sticky residential proxy test failed: {e}. Falling back to direct egress.")

    return "http://127.0.0.1:10800" if proxy_ok else ""

# 3. Write .env configuration
def write_env(proxy_url):
    log("=== [5/5] Generating /app/.env Configuration ===")
    proxy_line = f"PROXY={proxy_url}" if proxy_url else "PROXY="
    content = f"""AISTUDIO_AUTH_STATES=auth
LISTEN_ADDR=127.0.0.1:2048
PREVIEW_LISTEN_ADDR=127.0.0.1:2050
{proxy_line}
INIT_TIMEOUT=2m0s
REQUEST_TIMEOUT=30m0s
WARM_WORKER_LIMIT=5
MAX_ACTIVE_WORKERS=10
WARM_STARTUP_CONCURRENCY=5
PER_ACCOUNT_CONCURRENCY=2
ROUTING_STRATEGY=round-robin
UPSTREAM_CHANNELS=playground
WAA_BACKEND=spidermonkey
AUTO_START=true
ADMIN_AUTH_ENABLED=false
BUILD_NATIVE_NONSTREAM=true
NONSTREAM_KEEPALIVE=true
FAKE_STREAM=false
SEARCH_MODEL_ALIASES=true
FIRST_EVENT_HEDGE=off
CONTINUATION_REWRITE=true
"""
    with open("/app/.env", "w", encoding="utf-8") as f:
        f.write(content)
    log("[+] Generated /app/.env successfully.")

# 4. Start AIStudio2API and supervise
def start_aistudio_process():
    log("=== Starting /app/aistudio2api on 127.0.0.1:2048 ===")
    p = subprocess.Popen(
        ["/app/aistudio2api"],
        cwd="/app",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1
    )
    threading.Thread(target=log_reader, args=(p.stdout, "aistudio2api"), daemon=True).start()
    return p

def trigger_start():
    try:
        req = urllib.request.Request("http://127.0.0.1:2048/api/control/start", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            log(f"[Supervisor] Triggered /api/control/start -> {data}")
            return data
    except Exception as e:
        log(f"[Supervisor] Error triggering start: {e}")
        return str(e)

def clear_all_cooldowns():
    try:
        req = urllib.request.Request("http://127.0.0.1:2048/api/cooldowns")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        count = 0
        for item in data.get("cooldowns", []):
            acc_id = item.get("account_id")
            scope = item.get("scope")
            if acc_id and scope:
                del_url = f"http://127.0.0.1:2048/api/accounts/{acc_id}/cooldowns/{scope}"
                del_req = urllib.request.Request(del_url, method="DELETE")
                try:
                    with urllib.request.urlopen(del_req, timeout=2):
                        count += 1
                except Exception:
                    pass
        if count > 0:
            log(f"[Supervisor] Auto-cleared {count} cooldowns from accounts!")
        return count
    except Exception as e:
        return 0

def supervisor_loop():
    log("[Supervisor] Waiting for aistudio2api HTTP listener on 127.0.0.1:2048...")
    started = False
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:2048/api/status", timeout=2) as r:
                log(f"[Supervisor] aistudio2api is reachable! Response HTTP {r.status}")
                started = True
                break
        except Exception:
            pass

    if started:
        time.sleep(1)
        trigger_start()

    loop_count = 0
    while True:
        time.sleep(15)
        loop_count += 1
        # Every 60s, clear false-positive / stale cooldowns
        if loop_count % 4 == 0:
            clear_all_cooldowns()

        try:
            with urllib.request.urlopen("http://127.0.0.1:2048/api/status", timeout=5) as r:
                status_data = json.loads(r.read().decode("utf-8"))
                svc_status = status_data.get("status") or status_data.get("service", {}).get("status")
                if svc_status in ["stopped", "error"]:
                    log(f"[Supervisor] Service is {svc_status}! Restarting generation...")
                    trigger_start()
        except Exception:
            pass

# 5. Reverse Proxy Server
class ReverseProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_body(self, status, content_type, body_bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def do_GET(self):
        if self.path == "/healthz":
            self.send_body(200, "application/json", b'{"status":"healthy"}')
            return
        elif self.path == "/debug/logs":
            self.send_body(200, "text/plain; charset=utf-8", "\n".join(recent_logs).encode("utf-8"))
            return
        elif self.path == "/debug/status":
            try:
                with urllib.request.urlopen("http://127.0.0.1:2048/api/status", timeout=5) as r:
                    data = r.read()
                    self.send_body(r.status, "application/json", data)
            except Exception as e:
                self.send_body(502, "application/json", json.dumps({"error": str(e)}).encode("utf-8"))
            return
        elif self.path == "/debug/start":
            res = trigger_start()
            self.send_body(200, "application/json", json.dumps({"result": res}).encode("utf-8"))
            return
        elif self.path == "/debug/clear-cooldowns":
            cnt = clear_all_cooldowns()
            self.send_body(200, "application/json", json.dumps({"cleared_count": cnt}).encode("utf-8"))
            return
        elif self.path == "/debug/test-google":
            result = {}
            try:
                req = urllib.request.Request("https://aistudio.google.com/", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    result["direct"] = f"HTTP {r.status} (url={r.url})"
            except Exception as e:
                result["direct"] = f"FAIL: {e}"

            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": "http://127.0.0.1:10800", "https": "http://127.0.0.1:10800"})
                )
                req = urllib.request.Request("https://aistudio.google.com/", headers={"User-Agent": "Mozilla/5.0"})
                with opener.open(req, timeout=8) as r:
                    result["proxy"] = f"HTTP {r.status} (url={r.url})"
            except Exception as e:
                result["proxy"] = f"FAIL: {e}"

            self.send_body(200, "application/json", json.dumps(result, indent=2).encode("utf-8"))
            return

        self.forward_request("GET")

    def do_POST(self):
        if self.path == "/debug/start":
            res = trigger_start()
            self.send_body(200, "application/json", json.dumps({"result": res}).encode("utf-8"))
            return
        elif self.path == "/debug/clear-cooldowns":
            cnt = clear_all_cooldowns()
            self.send_body(200, "application/json", json.dumps({"cleared_count": cnt}).encode("utf-8"))
            return
        self.forward_request("POST")

    def do_PUT(self):
        self.forward_request("PUT")

    def do_DELETE(self):
        self.forward_request("DELETE")

    def forward_request(self, method):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        hop_by_hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                      "te", "trailers", "transfer-encoding", "upgrade", "host"}
        upstream_headers = {k: v for k, v in self.headers.items() if k.lower() not in hop_by_hop}
        upstream_headers["Host"] = "127.0.0.1:2048"

        try:
            conn = http.client.HTTPConnection("127.0.0.1", 2048, timeout=180)
            conn.request(method, self.path, body=body, headers=upstream_headers)
            resp = conn.getresponse()

            if resp.status == 503:
                threading.Thread(target=trigger_start, daemon=True).start()
            elif resp.status == 429:
                threading.Thread(target=clear_all_cooldowns, daemon=True).start()

            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ["transfer-encoding", "connection"]:
                    self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()

            while True:
                chunk = resp.read(2048)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break

            conn.close()
        except Exception as e:
            log(f"[Proxy] Forward error for {method} {self.path}: {e}")
            try:
                self.send_body(502, "application/json", json.dumps({"error": f"Upstream error: {e}"}).encode("utf-8"))
            except Exception:
                pass

def run():
    setup_auth()
    proxy_url = setup_proxies()
    write_env(proxy_url)
    start_aistudio_process()

    threading.Thread(target=supervisor_loop, daemon=True).start()

    port = int(os.environ.get("PORT", "10000"))
    log(f"=== Reverse Proxy Server listening on 0.0.0.0:{port} ===")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), ReverseProxyHandler)
    server.serve_forever()

if __name__ == "__main__":
    run()
