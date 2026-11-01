#!/bin/bash
set -e

echo "=== [1/5] Decrypting Authentication Bundle ==="
python3 -c "
import os, tarfile, io
from cryptography.fernet import Fernet

key = os.environ.get('AUTH_KEY', '').strip()
if not key:
    print('[!] ERROR: Missing AUTH_KEY environment variable!')
    exit(1)

f = Fernet(key.encode('ascii'))
with open('/app/bundle.enc', 'rb') as ef:
    enc_bytes = ef.read()

dec_bytes = f.decrypt(enc_bytes)
with tarfile.open(fileobj=io.BytesIO(dec_bytes), mode='r:gz') as tar:
    tar.extractall('/app')

print('[+] Successfully unpacked auth and proxies!')
"

echo "=== [2/5] Resetting Stale Account Cooldowns ==="
python3 -c "
import glob, json
for p in glob.glob('/app/auth/*/runtime-state.json'):
    try:
        with open(p, 'r') as f: data = json.load(f)
        data['cooldowns'] = {}
        with open(p, 'w') as f: json.dump(data, f, indent=2)
        print(f'[+] Cleared cooldowns: {p}')
    except Exception as e:
        print(f'[!] Error updating {p}: {e}')
"

echo "=== [3/5] Starting Proxy Rotator on 127.0.0.1:10800 ==="
python3 -u /app/rotator.py > /app/rotator.log 2>&1 &
sleep 2

echo "=== [4/5] Testing Outbound Connectivity ==="
PROXY_CONFIG="PROXY=http://127.0.0.1:10800"
PROXY_OK=$(python3 -c "
import urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': 'http://127.0.0.1:10800', 'https': 'http://127.0.0.1:10800'}))
req = urllib.request.Request('https://api.ipify.org?format=json', headers={'User-Agent': 'Mozilla/5.0'})
try:
    with opener.open(req, timeout=8) as r:
        print('SUCCESS')
except Exception as e:
    print(f'FAIL: {e}')
")

echo "Proxy test result: $PROXY_OK"
if [[ "$PROXY_OK" != *"SUCCESS"* ]]; then
    echo "[!] Warning: Residential proxy not reachable from this network. Falling back to direct egress."
    PROXY_CONFIG="PROXY="
else
    echo "[+] Residential proxy confirmed working!"
fi

PORT="${PORT:-10000}"

echo "=== [5/5] Generating /app/.env Configuration ==="
cat << EOF > /app/.env
AISTUDIO_AUTH_STATES=/app/auth
LISTEN_ADDR=0.0.0.0:${PORT}
PREVIEW_LISTEN_ADDR=127.0.0.1:2050
${PROXY_CONFIG}
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
EOF

echo "[+] Generated /app/.env successfully:"
cat /app/.env

echo "=== Starting aistudio2api on 0.0.0.0:${PORT} ==="
exec /app/aistudio2api
