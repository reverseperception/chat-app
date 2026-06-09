"""
PC-B Microservice - Serwis odbierający
Port: 5002
Rola: Odbiera żądania od PC-A, przetwarza je i odpowiada
"""

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import time
import logging
import os
import uuid
import threading
import socket
from datetime import datetime

app = Flask(__name__)
CORS(app)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("PC-B")

# ─── Konfiguracja ───────────────────────────────────────────────────────────
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Serwis-B")
SLOW_RESPONSE_DELAY = int(os.environ.get("SLOW_DELAY", "8"))
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "100"))

# Thread-safe stan
received_history: list = []
history_lock = threading.Lock()
_state_lock = threading.Lock()

# Nowe flagi konfiguracyjne do symulacji
simulate_error: bool = False
simulated_error_code: int = 500
global_delay_enabled: bool = False


# ─── Helpers ─────────────────────────────────────────────────────────────────

def log_received(endpoint: str, sender: str, payload_size: int, status: str) -> dict:
    entry = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "endpoint": endpoint,
        "sender": sender,
        "payload_size": payload_size,
        "status": status,
    }
    with history_lock:
        received_history.insert(0, entry)
        if len(received_history) > MAX_HISTORY:
            received_history.pop()
    return entry

def _get_json_body() -> tuple[dict, str | None]:
    if not request.is_json:
        return {}, "Content-Type musi być application/json"
    body = request.get_json(silent=True)
    if body is None:
        return {}, "Nieprawidłowy JSON"
    return body, None

def _simulate_error_response(endpoint: str, sender: str, size: int):
    with _state_lock:
        code = simulated_error_code
    log_received(endpoint, sender, size, f"error_{code}")
    logger.warning(f"[SIMULATE_ERROR] {endpoint} od {sender} -> Zwracam HTTP {code}")
    return jsonify({"error": f"Symulowany błąd serwisu B (HTTP {code})", "service": SERVICE_NAME}), code

def _apply_global_delay_if_needed():
    """Aplikuje opóźnienie, jeśli włączono tryb globalny."""
    with _state_lock:
        g_delay = global_delay_enabled
        delay = SLOW_RESPONSE_DELAY
    if g_delay and delay > 0:
        time.sleep(delay)


# ─── Endpointy własne PC-B ───────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    gui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_pc_b.html")
    if not os.path.exists(gui_path):
        return "<h2>Brak pliku gui_pc_b.html w katalogu serwisu</h2>", 404
    return send_file(gui_path)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/status", methods=["GET"])
def status():
    with _state_lock:
        err = simulate_error
        code = simulated_error_code
        delay = SLOW_RESPONSE_DELAY
        g_delay = global_delay_enabled
    return jsonify({
        "service": SERVICE_NAME,
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "simulate_error": err,
        "error_code": code,
        "global_delay": g_delay,
        "slow_delay_seconds": delay,
        "received_count": len(received_history),
    })

@app.route("/history", methods=["GET"])
def history():
    with history_lock:
        return jsonify(list(received_history))

@app.route("/history/clear", methods=["POST"])
def clear_history():
    with history_lock:
        received_history.clear()
    return jsonify({"message": "Historia wyczyszczona"})

@app.route("/config", methods=["GET", "POST"])
def config():
    global simulate_error, SLOW_RESPONSE_DELAY, simulated_error_code, global_delay_enabled
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        with _state_lock:
            if "simulate_error" in body:
                simulate_error = bool(body["simulate_error"])
            if "error_code" in body:
                simulated_error_code = int(body["error_code"])
            if "global_delay" in body:
                global_delay_enabled = bool(body["global_delay"])
            if "slow_delay" in body:
                d = int(body["slow_delay"])
                if 0 <= d <= 120:
                    SLOW_RESPONSE_DELAY = d
        return jsonify({"status": "updated"})
    
    with _state_lock:
        return jsonify({
            "simulate_error": simulate_error,
            "error_code": simulated_error_code,
            "global_delay": global_delay_enabled,
            "slow_delay": SLOW_RESPONSE_DELAY
        })


# ─── Endpointy odbierające od PC-A ──────────────────────────────────────────

@app.route("/receive/hello", methods=["POST"])
def receive_hello():
    body, err = _get_json_body()
    if err: return jsonify({"error": err}), 400
    sender = str(body.get("from", "Nieznany"))[:64]

    with _state_lock:
        is_error = simulate_error

    if is_error:
        return _simulate_error_response("/receive/hello", sender, len(str(body)))

    _apply_global_delay_if_needed()

    log_received("/receive/hello", sender, len(str(body)), "ok")
    return jsonify({
        "service": SERVICE_NAME,
        "message": f"Cześć {sender}! Pozdrawiam z PC-B 👋",
        "timestamp": datetime.now().isoformat()
    })

@app.route("/receive/data", methods=["POST"])
def receive_data():
    body, err = _get_json_body()
    if err: return jsonify({"error": err}), 400
    sender = str(body.get("from", "Nieznany"))[:64]
    data = body.get("data", "")

    if not isinstance(data, str): data = str(data)
    if len(data) > 10_000:
        return jsonify({"error": "Dane przekraczają limit"}), 413

    with _state_lock:
        is_error = simulate_error

    if is_error:
        return _simulate_error_response("/receive/data", sender, len(str(body)))

    _apply_global_delay_if_needed()

    log_received("/receive/data", sender, len(str(body)), "ok")
    processed = {
        "original": data,
        "uppercase": data.upper(),
        "length": len(data),
        "reversed": data[::-1]
    }
    return jsonify({
        "service": SERVICE_NAME,
        "message": "Dane przetworzone pomyślnie",
        "processed": processed
    })

@app.route("/receive/slow", methods=["POST"])
def receive_slow():
    body, err = _get_json_body()
    if err: return jsonify({"error": err}), 400
    sender = str(body.get("from", "Nieznany"))[:64]

    with _state_lock:
        delay = SLOW_RESPONSE_DELAY
    
    log_received("/receive/slow", sender, len(str(body)), "processing_slow")
    
    if delay > 0:
        time.sleep(delay)

    log_received("/receive/slow", sender, len(str(body)), "ok_after_delay")
    return jsonify({
        "service": SERVICE_NAME,
        "message": f"Odpowiedź po {delay}s opóźnienia",
        "delay_seconds": delay
    })


if __name__ == "__main__":
    PORT = 5002
    try:
        hostname = socket.gethostname()
        local_ips = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ip_list = list({addr[4][0] for addr in local_ips if not addr[4][0].startswith("127.")})
    except Exception:
        ip_list = []
    if not ip_list:
        ip_list = ["127.0.0.1"]

    lines = [
        "╔══════════════════════════════════════════════════╗",
        "║     PC-B Microservice v2.1                       ║",
        f"║     Port     : {PORT:<34}║",
        "║     Nasłuchuje na żądania od PC-A                ║",
        "║     ─────────────────────────────────────────    ║",
        "║     Dostępny pod adresami:                       ║",
    ]
    for ip in ip_list:
        url = f"http://{ip}:{PORT}"
        lines.append(f"║       {url:<43}║")
    lines.append("╚══════════════════════════════════════════════════╝")
    logger.info("\n" + "\n".join(lines))
    app.run(host="0.0.0.0", port=PORT, debug=False)