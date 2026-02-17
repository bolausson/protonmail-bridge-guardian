#!/usr/bin/env python3
#
#
# Check logs with:
# docker logs protonmail-guardian
# docker logs protonmail-bridge
# and
# sudo journalctl -u docker.service | grep protonmail-guardian
# sudo journalctl -u docker.service | grep protonmail-bridge

import os
import socket
import threading
import time
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Configuration (env overrides) -----------------------------------------

BRIDGE = os.getenv("BRIDGE_NAME", "protonmail-bridge")
HOST = os.getenv("IMAP_HOST", "protonmail-bridge")
PORT = int(os.getenv("IMAP_PORT", "143"))
USER = os.getenv("IMAP_USER", "CHANGE_ME")
PASS = os.getenv("IMAP_PASS", "CHANGE_ME")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "20"))
RESTART_COOLDOWN = int(os.getenv("RESTART_COOLDOWN", "30"))
MAX_RESTARTS_PER_HOUR = int(os.getenv("MAX_RESTARTS_PER_HOUR", "5"))
STARTUP_DELAY = int(os.getenv("STARTUP_DELAY", "30"))

METRICS_PORT = int(os.getenv("METRICS_PORT", "8008"))

# --- Metrics state ---------------------------------------------------------

lock = threading.Lock()

imap_checks_total = 0
imap_failures_total = 0
restarts_total = 0
recent_restarts = []  # list of timestamps
last_restart_ts = 0
bridge_status = 0  # 1 = healthy, 0 = unhealthy


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"{ts} [GUARDIAN] {msg}", flush=True)


# --- IMAP health check -----------------------------------------------------

def check_imap() -> bool:
    global imap_checks_total, imap_failures_total, bridge_status

    with lock:
        imap_checks_total += 1

    try:
        s = socket.create_connection((HOST, PORT), timeout=5)
        s.settimeout(5)

        def send(cmd: str) -> None:
            s.sendall(cmd.encode("utf-8"))

        send(f'a LOGIN {USER} {PASS}\r\n')
        send('a LIST "" "*"\r\n')
        send('a LOGOUT\r\n')

        data = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
        s.close()

        text = data.decode("utf-8", errors="ignore")

        login_ok = "a OK" in text
        inbox_ok = "INBOX" in text

        healthy = login_ok and inbox_ok

    except Exception as e:
        log(f"IMAP check error: {e}")
        healthy = False

    with lock:
        if not healthy:
            imap_failures_total += 1
            bridge_status = 0
        else:
            bridge_status = 1

    return healthy


# --- Restart logic ---------------------------------------------------------

def count_recent_restarts(now: float) -> int:
    global recent_restarts
    one_hour_ago = now - 3600
    recent_restarts = [t for t in recent_restarts if t >= one_hour_ago]
    return len(recent_restarts)


def record_restart(now: float) -> None:
    global recent_restarts, last_restart_ts, restarts_total
    recent_restarts.append(now)
    last_restart_ts = now
    restarts_total += 1


def restart_bridge() -> None:
    log(f"Restarting {BRIDGE}...")
    try:
        subprocess.run(["docker", "restart", BRIDGE], check=True)
        log(f"Restarted {BRIDGE}.")
    except subprocess.CalledProcessError as e:
        log(f"Failed to restart {BRIDGE}: {e}")


# --- Guardian loop ---------------------------------------------------------

def guardian_loop() -> None:
    log(f"Startup delay: {STARTUP_DELAY}s")
    time.sleep(STARTUP_DELAY)
    log("Starting health checks.")

    while True:
        healthy = check_imap()

        now = time.time()

        if not healthy:
            log("Bridge unhealthy.")

            with lock:
                recent_count = count_recent_restarts(now)
                limit = MAX_RESTARTS_PER_HOUR

            if recent_count >= limit:
                log(f"Restart limit reached ({recent_count}/{limit} in last hour). Waiting 300s.")
                time.sleep(300)
            else:
                with lock:
                    record_restart(now)
                    current_count = count_recent_restarts(now)
                log(f"Restart #{current_count} in last hour.")
                restart_bridge()
                log(f"Cooldown {RESTART_COOLDOWN}s after restart.")
                time.sleep(RESTART_COOLDOWN)
        else:
            log("Bridge healthy.")
            time.sleep(CHECK_INTERVAL)


# --- Metrics HTTP handler --------------------------------------------------

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        with lock:
            body = []
            body.append("# HELP proton_guardian_imap_checks_total Total IMAP health checks performed")
            body.append("# TYPE proton_guardian_imap_checks_total counter")
            body.append(f"proton_guardian_imap_checks_total {imap_checks_total}")

            body.append("# HELP proton_guardian_imap_failures_total Number of failed IMAP checks")
            body.append("# TYPE proton_guardian_imap_failures_total counter")
            body.append(f"proton_guardian_imap_failures_total {imap_failures_total}")

            body.append("# HELP proton_guardian_restarts_total Number of bridge restarts triggered")
            body.append("# TYPE proton_guardian_restarts_total counter")
            body.append(f"proton_guardian_restarts_total {restarts_total}")

            body.append("# HELP proton_guardian_recent_restarts Number of restarts in the last hour")
            body.append("# TYPE proton_guardian_recent_restarts gauge")
            body.append(f"proton_guardian_recent_restarts {count_recent_restarts(time.time())}")

            body.append("# HELP proton_guardian_restart_limit Maximum allowed restarts per hour")
            body.append("# TYPE proton_guardian_restart_limit gauge")
            body.append(f"proton_guardian_restart_limit {MAX_RESTARTS_PER_HOUR}")

            body.append("# HELP proton_guardian_last_restart_timestamp Unix timestamp of last restart")
            body.append("# TYPE proton_guardian_last_restart_timestamp gauge")
            body.append(f"proton_guardian_last_restart_timestamp {last_restart_ts}")

            body.append("# HELP proton_guardian_bridge_status Current IMAP health (1=healthy, 0=unhealthy)")
            body.append("# TYPE proton_guardian_bridge_status gauge")
            body.append(f"proton_guardian_bridge_status {bridge_status}")

            payload = "\n".join(body) + "\n"

        payload_bytes = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload_bytes)))
        self.end_headers()
        self.wfile.write(payload_bytes)

    def log_message(self, format, *args):
        # Silence default HTTP logs
        return


def run_http_server():
    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    log(f"Metrics server listening on :{METRICS_PORT}/metrics")
    server.serve_forever()


if __name__ == "__main__":
    t = threading.Thread(target=guardian_loop, daemon=True)
    t.start()
    run_http_server()
