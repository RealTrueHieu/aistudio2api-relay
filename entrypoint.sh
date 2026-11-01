#!/bin/bash
set -e

echo "=== [1/4] Decrypting Authentication Bundle ==="
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

echo "=== [2/4] Resetting Stale Account Cooldowns ==="
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

echo "=== [3/4] Starting Proxy Rotator on 127.0.0.1:10800 ==="
python3 -u /app/rotator.py > /app/rotator.log 2>&1 &
sleep 2

PORT="${PORT:-10000}"
export LISTEN_ADDR="0.0.0.0:${PORT}"
export PREVIEW_LISTEN_ADDR="127.0.0.1:2050"
export PROXY="http://127.0.0.1:10800"
export AISTUDIO_AUTH_STATES="/app/auth"
export AUTO_START="true"
export WAA_BACKEND="spidermonkey"
export FIRST_EVENT_HEDGE="off"
export CONTINUATION_REWRITE="true"
export INIT_TIMEOUT="2m0s"
export REQUEST_TIMEOUT="30m0s"

echo "=== [4/4] Starting aistudio2api on ${LISTEN_ADDR} ==="
exec /app/aistudio2api
