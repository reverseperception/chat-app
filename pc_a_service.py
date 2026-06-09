"""
PC-A Microservice - Serwis inicjujący
Port: 5001
Rola: Wysyła żądania do PC-B, obsługuje odpowiedzi, timeout i błędy

Ulepszenia v2.0:
- Konfiguracja przez zmienne środowiskowe (PC_B_URL, TIMEOUT_SECONDS)
- Retry z exponential backoff (naprawiony problem z ReadTimeout)
- Poprawna obsługa HTTPError
- Health-check endpoint /health
"""

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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
logger = logging.getLogger("PC-A")

# ─── Konfiguracja (env > defaults) ──────────────────────────────────────────
PC_B_URL = os.environ.get("PC_B_URL", "http://localhost:5002")
TIMEOUT_SECONDS = int(os.environ.get("TIMEOUT_SECONDS", "5"))
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Serwis-A")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "100"))

# Thread-safe lista historii
call_history: list = []
history_lock = threading.Lock()


def get_session(retries: int = 2, backoff: float = 0.3) -> requests.Session:
    """Tworzy sesję HTTP z retry i backoff."""
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        connect=retries,
        read=False,  # KLUCZOWE: Nie ponawiaj żądań, gdy wystąpi ReadTimeout
        backoff_factor=backoff,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def log_call(endpoint: str, status: str, response_time: float, message: str, request_id: str = "") -> dict:
    entry = {
        "id": request_id or str(uuid.uuid4())[:8],
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "endpoint": endpoint,
        "status": status,
        "response_time_ms": round(response_time * 1000, 1),
        "message": message,
    }
    with history_lock:
        call_history.insert(0, entry)
        if len(call_history) > MAX_HISTORY:
            call_history.pop()
    return entry


# ─── Endpointy własne PC-A ───────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """Serwuje interfejs graficzny PC-A."""
    gui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_pc_a.html")
    if not os.path.exists(gui_path):
        return "<h2>Brak pliku gui_pc_a.html w katalogu serwisu</h2>", 404
    return send_file(gui_path)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "service": SERVICE_NAME,
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "pc_b_url": PC_B_URL,
        "timeout_seconds": TIMEOUT_SECONDS,
    })


@app.route("/history", methods=["GET"])
def history():
    with history_lock:
        return jsonify(list(call_history))


@app.route("/history/clear", methods=["POST"])
def clear_history():
    with history_lock:
        call_history.clear()
    return jsonify({"message": "Historia wyczyszczona"})


@app.route("/config", methods=["GET", "POST"])
def config():
    global PC_B_URL, TIMEOUT_SECONDS
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "pc_b_url" in body:
            new_url = str(body["pc_b_url"]).rstrip("/")
            if not new_url.startswith(("http://", "https://")):
                return jsonify({"error": "pc_b_url musi zaczynać się od http:// lub https://"}), 400
            PC_B_URL = new_url
            logger.info(f"PC_B_URL zmieniony na: {PC_B_URL}")
        if "timeout" in body:
            t = int(body["timeout"])
            if not (1 <= t <= 60):
                return jsonify({"error": "timeout musi być w zakresie 1–60 s"}), 400
            TIMEOUT_SECONDS = t
    return jsonify({"pc_b_url": PC_B_URL, "timeout": TIMEOUT_SECONDS})


# ─── Wywołania do PC-B ───────────────────────────────────────────────────────

def _call_pc_b(method: str, path: str, payload: dict | None = None, use_retry: bool = True) -> tuple[dict, int]:
    """Wspólna logika wywołania do PC-B."""
    req_id = str(uuid.uuid4())[:8]
    start = time.time()
    session = get_session() if use_retry else requests.Session()

    try:
        url = f"{PC_B_URL}{path}"
        if method == "GET":
            resp = session.get(url, timeout=TIMEOUT_SECONDS)
        else:
            resp = session.post(url, json=payload, timeout=TIMEOUT_SECONDS)

        elapsed = time.time() - start
        resp.raise_for_status()
        data = resp.json()
        entry = log_call(path, "success", elapsed, data.get("message", "OK"), req_id)
        logger.info(f"[{req_id}] {path} → 200 ({round(elapsed*1000)}ms)")
        return {"result": "success", "response": data, "log": entry}, 200

    except requests.exceptions.Timeout:
        elapsed = time.time() - start
        msg = f"Timeout po {TIMEOUT_SECONDS}s"
        entry = log_call(path, "timeout", elapsed, msg, req_id)
        logger.warning(f"[{req_id}] {path} → TIMEOUT")
        return {"result": "timeout", "error": f"PC-B nie odpowiedział w {TIMEOUT_SECONDS}s", "log": entry}, 504

    except requests.exceptions.ConnectionError as exc:
        elapsed = time.time() - start
        
        # Zabezpieczenie: jeśli urllib3 jakimś cudem "opakuje" MaxRetryError z ReadTimeout
        if "ReadTimeoutError" in str(exc) or "Timeout" in str(exc):
            msg = f"Timeout po {TIMEOUT_SECONDS}s (MaxRetryError)"
            entry = log_call(path, "timeout", elapsed, msg, req_id)
            logger.warning(f"[{req_id}] {path} → TIMEOUT (ukryty w ConnectionError)")
            return {"result": "timeout", "error": f"PC-B nie odpowiedział w {TIMEOUT_SECONDS}s", "log": entry}, 504
            
        msg = f"Brak połączenia z PC-B: {exc}"
        entry = log_call(path, "error", elapsed, "Brak połączenia z PC-B", req_id)
        logger.error(f"[{req_id}] {path} → CONNECTION ERROR: {exc}")
        return {"result": "error", "error": "Nie można połączyć się z PC-B", "log": entry}, 503

    except requests.exceptions.HTTPError as exc:
        elapsed = time.time() - start
        code = exc.response.status_code if exc.response is not None else "?"
        try:
            err_body = exc.response.json() if exc.response is not None else {}
        except Exception:
            err_body = {}
        msg = err_body.get("error", f"HTTP {code} od PC-B")
        entry = log_call(path, f"http_{code}", elapsed, msg, req_id)
        logger.error(f"[{req_id}] {path} → HTTP {code}: {msg}")
        return {"result": "error", "error": msg, "http_status": code, "log": entry}, 502

    except Exception as exc:
        elapsed = time.time() - start
        entry = log_call(path, "error", elapsed, str(exc), req_id)
        logger.exception(f"[{req_id}] {path} → NIEOCZEKIWANY BŁĄD")
        return {"result": "error", "error": str(exc), "log": entry}, 500

    finally:
        session.close()


@app.route("/call/hello", methods=["POST"])
def call_hello():
    payload = {"from": SERVICE_NAME, "message": "Cześć od PC-A!"}
    result, code = _call_pc_b("POST", "/receive/hello", payload)
    return jsonify(result), code


@app.route("/call/data", methods=["POST"])
def call_data():
    body = request.get_json(silent=True) or {}
    payload = {
        "from": SERVICE_NAME,
        "data": body.get("data", "Przykładowe dane z PC-A"),
        "timestamp": datetime.now().isoformat(),
    }
    result, code = _call_pc_b("POST", "/receive/data", payload)
    return jsonify(result), code


@app.route("/call/ping", methods=["POST"])
def call_ping():
    result, code = _call_pc_b("GET", "/status", use_retry=False)
    return jsonify(result), code


@app.route("/call/simulate-timeout", methods=["POST"])
def call_simulate_timeout():
    payload = {"from": SERVICE_NAME}
    result, code = _call_pc_b("POST", "/receive/slow", payload, use_retry=False)
    return jsonify(result), code


if __name__ == "__main__":
    PORT = 5001
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
        "║     PC-A Microservice v2.0                       ║",
        f"║     Port     : {PORT:<34}║",
        f"║     Cel (PC-B): {PC_B_URL:<33}║",
        f"║     Timeout  : {TIMEOUT_SECONDS}s{' '*33}║",
        "║     ─────────────────────────────────────────    ║",
        "║     Dostępny pod adresami:                       ║",
    ]
    for ip in ip_list:
        url = f"http://{ip}:{PORT}"
        lines.append(f"║       {url:<43}║")
    lines.append("╚══════════════════════════════════════════════════╝")
    logger.info("\n" + "\n".join(lines))
    app.run(host="0.0.0.0", port=PORT, debug=False)