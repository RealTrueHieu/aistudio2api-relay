import asyncio
import base64
import random
import re
import sys
import os
from urllib.parse import urlparse

PROXIES_FILE = "/app/proxies.txt"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 10800

proxies = []

def load_proxies():
    global proxies
    path = PROXIES_FILE
    if not os.path.exists(path):
        path = "proxies.txt"
    if not os.path.exists(path):
        print(f"[!] Warning: No proxies file found at {path}", flush=True)
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if not line.startswith("http://") and not line.startswith("https://"):
                line = "http://" + line
            p = urlparse(line)
            if p.hostname and p.port:
                auth = None
                if p.username and p.password:
                    auth = base64.b64encode(f"{p.username}:{p.password}".encode()).decode()
                proxies.append({
                    "host": p.hostname,
                    "port": p.port,
                    "auth": auth
                })
    print(f"[*] Loaded {len(proxies)} proxies into pool.", flush=True)

async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass

async def handle_client(client_r, client_w):
    try:
        head = await client_r.readuntil(b"\r\n\r\n")
    except Exception:
        client_w.close()
        return

    first_line = head.split(b"\r\n")[0].decode("utf-8", errors="replace")
    parts = first_line.split()
    if len(parts) < 2:
        client_w.close()
        return

    method, target = parts[0], parts[1]
    
    if not proxies:
        client_w.write(b"HTTP/1.1 502 No Proxies Configured\r\n\r\n")
        await client_w.drain()
        client_w.close()
        return

    # Try up to 4 proxies in case one is dead/slow
    if method.upper() == "CONNECT":
        if ":" in target:
            target_host, target_port = target.split(":", 1)
        else:
            target_host, target_port = target, "443"

        connected = False
        for attempt in range(4):
            proxy = random.choice(proxies)
            try:
                proxy_r, proxy_w = await asyncio.wait_for(
                    asyncio.open_connection(proxy["host"], proxy["port"]),
                    timeout=6
                )
                
                connect_req = f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n"
                if proxy["auth"]:
                    connect_req += f"Proxy-Authorization: Basic {proxy['auth']}\r\n"
                connect_req += "\r\n"

                proxy_w.write(connect_req.encode("utf-8"))
                await proxy_w.drain()

                proxy_resp = await asyncio.wait_for(proxy_r.readuntil(b"\r\n\r\n"), timeout=6)
                if b"200" in proxy_resp.split(b"\r\n")[0]:
                    client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await client_w.drain()
                    await asyncio.gather(pipe(client_r, proxy_w), pipe(proxy_r, client_w))
                    connected = True
                    break
                else:
                    proxy_w.close()
            except Exception:
                continue

        if not connected:
            try:
                client_w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await client_w.drain()
                client_w.close()
            except Exception:
                pass
    else:
        # Standard HTTP forwarding
        connected = False
        for attempt in range(4):
            proxy = random.choice(proxies)
            try:
                proxy_r, proxy_w = await asyncio.wait_for(
                    asyncio.open_connection(proxy["host"], proxy["port"]),
                    timeout=6
                )
                forward_head = head
                if proxy["auth"]:
                    forward_head = re.sub(b"\r\n\r\n", f"\r\nProxy-Authorization: Basic {proxy['auth']}\r\n\r\n".encode(), forward_head, count=1)
                proxy_w.write(forward_head)
                await proxy_w.drain()
                await asyncio.gather(pipe(client_r, proxy_w), pipe(proxy_r, client_w))
                connected = True
                break
            except Exception:
                continue

        if not connected:
            client_w.close()

async def main():
    load_proxies()
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    print(f"[*] Proxy Rotator listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
