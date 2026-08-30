from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import itertools
import json
import threading
import time
import requests

import os

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8000))

# Roblox's public username-validation endpoint.
VALIDATE_URL = "https://auth.roblox.com/v2/usernames/validate"

# We deliberately back off when Roblox throttles requests.
REQUEST_DELAY = 0.9
MAX_BACKOFF = 60

state_lock = threading.Lock()

state = {
    "running": False,
    "mode": "3_letters",
    "max_length": 4,
    "checked": 0,
    "available": [],
    "queue": [],
    "error": "",
}

worker = None
session = requests.Session()
session.headers.update({
    "User-Agent": "KOBYUsernameFinder/1.0",
    "Accept": "application/json",
})


# ------------------------------------------------------------
# Candidate generation
# ------------------------------------------------------------

ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Roblox usernames are handled as letters here for the exhaustive
# 1-4-letter search modes. Character modes can additionally use
# digits/underscore where supported.
CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789_"


def valid_length(name, maximum=10):
    return 1 <= len(name) <= maximum


def generate_exhaustive(chars, length):
    """
    Generate every possible string of exactly `length`.
    """
    for combo in itertools.product(chars, repeat=length):
        yield "".join(combo)


def generate_mode(mode, max_length):
    """
    Generate candidates for the selected search mode.

    1-4 letter/character modes are exhaustive.
    Random-style generation is intentionally capped at 4.
    """

    if mode.endswith("_letters"):
        length = int(mode.split("_")[0])
        if 1 <= length <= 4:
            yield from generate_exhaustive(ALPHABET, length)
        return

    if mode.endswith("_characters"):
        length = int(mode.split("_")[0])
        if 1 <= length <= 4:
            yield from generate_exhaustive(CHARACTERS, length)
        return

    if mode == "repeater":
        # Repeated-letter patterns.
        for length in range(2, min(max_length, 4) + 1):
            for letter in ALPHABET:
                yield letter * length

        # AAB / ABB / AABB / ABAB / ABBA style patterns.
        for a in ALPHABET:
            for b in ALPHABET:
                if a == b:
                    continue

                patterns = [
                    a + a + b,
                    a + b + b,
                    a + a + b + b,
                    a + b + a + b,
                    a + b + b + a,
                ]

                for candidate in patterns:
                    if len(candidate) <= max_length:
                        yield candidate
        return

    if mode == "patterns":
        for a in ALPHABET:
            for b in ALPHABET:
                for c in ALPHABET:
                    candidates = [
                        a + b + a,
                        a + b + b,
                        a + a + b,
                    ]

                    for candidate in candidates:
                        if len(candidate) <= min(max_length, 4):
                            yield candidate

        # Four-character patterns.
        if max_length >= 4:
            for a in ALPHABET:
                for b in ALPHABET:
                    if a == b:
                        continue

                    yield a + b + a + b
                    yield a + b + b + a
                    yield a + a + b + b
        return

    # Readable-word mode.
    if mode == "words":
        words = [
            "able", "acid", "aero", "alive", "amber", "angel",
            "apple", "arrow", "atlas", "audio", "aurora",
            "autumn", "baker", "basil", "beach", "beast",
            "berry", "black", "blaze", "bloom", "blue",
            "brave", "brick", "bright", "brook", "candy",
            "charm", "chase", "chill", "cloud", "clover",
            "coral", "cosmo", "crisp", "crown", "dance",
            "dawn", "dream", "drift", "echo", "ember",
            "energy", "fable", "fairy", "fancy", "flame",
            "flash", "flora", "focus", "forest", "frost",
            "ghost", "glow", "gold", "grace", "grain",
            "green", "halo", "happy", "haven", "hazel",
            "heart", "honey", "hope", "ivory", "jewel",
            "karma", "laser", "lemon", "light", "lily",
            "lunar", "magic", "maple", "marble", "meadow",
            "melon", "mercy", "metal", "mint", "mist",
            "moon", "music", "night", "noble", "nova",
            "ocean", "olive", "orbit", "pearl", "pixel",
            "pluto", "polar", "prism", "pulse", "rain",
            "raven", "rebel", "river", "rose", "royal",
            "ruby", "shadow", "shine", "silver", "solar",
            "sonic", "spark", "spice", "spirit", "stone",
            "storm", "summer", "swift", "system", "tempo",
            "terra", "tiger", "token", "topaz", "tower",
            "trail", "tulip", "velvet", "venus", "violet",
            "wave", "whale", "white", "willow", "winter",
            "wisdom", "wonder", "world", "zenith",
            "adventure", "beautiful", "butterfly", "champion",
            "crystal", "diamond", "evergreen", "fantasy",
            "firefly", "harmony", "infinity", "midnight",
            "moonlight", "mountain", "paradise", "rainbow",
            "sunflower", "treasure", "velocity", "whisper",
            "wildflower",
        ]

        for word in words:
            if len(word) <= max_length:
                yield word

        return

    # Best Mix.
    seen = set()

    for submode in (
        "4_letters",
        "repeater",
        "patterns",
        "words",
    ):
        for candidate in generate_mode(submode, max_length):
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


# ------------------------------------------------------------
# Roblox validation
# ------------------------------------------------------------

def validate_username(username):
    params = {
        "request.username": username,
        "request.birthday": "2000-01-01T00:00:00.000Z",
        "request.context": "Signup",
    }

    try:
        response = session.get(
            VALIDATE_URL,
            params=params,
            timeout=15,
        )
    except requests.RequestException as exc:
        return "ERROR", str(exc)

    if response.status_code == 429:
        return "THROTTLED", "Roblox rate limit"

    if response.status_code != 200:
        return "ERROR", f"HTTP {response.status_code}"

    try:
        data = response.json()
    except ValueError:
        return "ERROR", "Invalid response"

    code = data.get("code")

    if code == 0:
        return "AVAILABLE", ""

    if code == 1:
        return "TAKEN", ""

    return "INVALID", data.get("message", "")


# ------------------------------------------------------------
# Worker
# ------------------------------------------------------------

def add_queue_item(username):
    with state_lock:
        state["queue"].append({
            "username": username,
            "status": "WAITING",
        })


def set_queue_status(username, status):
    with state_lock:
        for item in state["queue"]:
            if item["username"] == username:
                item["status"] = status
                return


def remove_queue_item(username):
    with state_lock:
        state["queue"] = [
            item
            for item in state["queue"]
            if item["username"] != username
        ]


def add_available(username):
    with state_lock:
        if username not in state["available"]:
            state["available"].append(username)


def search_worker(mode, max_length):
    backoff = REQUEST_DELAY

    # Keep a local set so this run never intentionally checks a
    # candidate twice.
    seen = set()

    for username in generate_mode(mode, max_length):

        with state_lock:
            if not state["running"]:
                return

        if username in seen:
            continue

        seen.add(username)

        add_queue_item(username)
        set_queue_status(username, "CHECKING")

        result, message = validate_username(username)

        if result == "AVAILABLE":
            add_available(username)
            remove_queue_item(username)

            with state_lock:
                state["checked"] += 1
                state["error"] = ""

            backoff = REQUEST_DELAY

        elif result == "TAKEN":
            set_queue_status(username, "TAKEN")

            with state_lock:
                state["checked"] += 1
                state["error"] = ""

            time.sleep(0.15)
            remove_queue_item(username)
            backoff = REQUEST_DELAY

        elif result == "INVALID":
            set_queue_status(username, "INVALID")

            with state_lock:
                state["checked"] += 1
                state["error"] = ""

            time.sleep(0.1)
            remove_queue_item(username)
            backoff = REQUEST_DELAY

        elif result == "THROTTLED":
            set_queue_status(username, "WAITING")

            with state_lock:
                state["error"] = (
                    f"Roblox is throttling requests. "
                    f"Waiting {backoff:.1f}s."
                )

            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

            # Retry the same username.
            continue

        else:
            set_queue_status(username, "ERROR")

            with state_lock:
                state["error"] = message

            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

            continue

        time.sleep(backoff)

    with state_lock:
        state["running"] = False


# ------------------------------------------------------------
# HTTP server
# ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.send_header(
            "Cache-Control",
            "no-store",
        )
        self.end_headers()

        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self.serve_file("index.html")
            return

        if parsed.path == "/api/status":

            with state_lock:
                response = {
                    "running": state["running"],
                    "mode": state["mode"],
                    "maxLength": state["max_length"],
                    "checked": state["checked"],
                    "available": list(state["available"]),
                    "queue": list(state["queue"]),
                    "queueCount": len(state["queue"]),
                    "error": state["error"],
                }

            self.send_json(response)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/start":
            self.start_search()
            return

        if parsed.path == "/api/stop":
            self.stop_search()
            return

        self.send_error(404)

    def read_json(self):
        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            raw = self.rfile.read(length)

            return json.loads(
                raw.decode("utf-8")
            )
        except Exception:
            return {}

    def start_search(self):

        global worker

        data = self.read_json()

        mode = data.get(
            "mode",
            "3_letters",
        )

        try:
            max_length = int(
                data.get(
                    "maxLength",
                    4,
                )
            )
        except Exception:
            max_length = 4

        max_length = max(
            1,
            min(10, max_length),
        )

        allowed_modes = {
            "1_letters",
            "2_letters",
            "3_letters",
            "4_letters",
            "1_characters",
            "2_characters",
            "3_characters",
            "4_characters",
            "repeater",
            "patterns",
            "words",
            "best",
        }

        if mode not in allowed_modes:
            mode = "3_letters"

        with state_lock:

            if state["running"]:
                self.send_json({
                    "ok": True,
                    "message": "Already running.",
                })
                return

            state["running"] = True
            state["mode"] = mode
            state["max_length"] = max_length
            state["checked"] = 0
            state["available"] = []
            state["queue"] = []
            state["error"] = ""

        worker = threading.Thread(
            target=search_worker,
            args=(mode, max_length),
            daemon=True,
        )

        worker.start()

        self.send_json({
            "ok": True,
        })

    def stop_search(self):

        with state_lock:
            state["running"] = False

        self.send_json({
            "ok": True,
        })

    def serve_file(self, filename):

        from pathlib import Path

        path = (
            Path(__file__).resolve().parent
            / filename
        )

        if not path.exists():
            self.send_error(404)
            return

        content = path.read_bytes()

        content_type = (
            "text/html; charset=utf-8"
            if filename.endswith(".html")
            else "text/plain; charset=utf-8"
        )

        self.send_response(200)
        self.send_header(
            "Content-Type",
            content_type,
        )
        self.send_header(
            "Content-Length",
            str(len(content)),
        )
        self.end_headers()

        self.wfile.write(content)


# ------------------------------------------------------------
# Start
# ------------------------------------------------------------

if __name__ == "__main__":

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    print()
    print("=" * 55)
    print("KOBY USERNAME FINDER")
    print("=" * 55)
    print()
    print(
        f"Website: http://{HOST}:{PORT}"
    )
    print()
    print(
        "Keep this window open while using the website."
    )
    print(
        "Press Ctrl+C to stop the server."
    )
    print()

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print()
        print("Stopping...")

    finally:

        with state_lock:
            state["running"] = False

        server.server_close()