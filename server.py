from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path
import itertools
import json
import random
import secrets
import threading
import time
import urllib.request
import requests

HOST = "0.0.0.0"
PORT = 8000
VALIDATE_URL = "https://auth.roblox.com/v2/usernames/validate"
DEFAULT_DELAY = 0.1
MAX_BACKOFF = 60
MAX_QUEUE_DISPLAY = 100
MAX_RECENT_RESULTS = 100
ALPHABET = "abcdefghijklmnopqrstuvwxyz"
CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789_"
WORDS_URL = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
WORDS_FILE = "words_alpha.txt"

FALLBACK_WORDS = set("""
able acid after agile alert alpha amber angel animal apex apple arcade arrow artist atomic audio aurora
beautiful champion celestial crystal digital electric fantasy galaxy legendary lightning midnight mystery
nebula phantom quantum sanctuary shadow starlight velocity victory warrior wisdom wonder adventure paradise
radiant secret sunshine thunder whisper wildflower transformation
""".split())

# Popular slang / internet / gaming lingo included in Words mode.
SLANG_WORDS = set("""
rizz sigma gyatt skibidi mog mogged bussin sus cap bet frfr ong ngl idc tbh rn asap afk brb btw imo lmao lol rofl xd uwu simp stan drip flex fire lit slay ate aura based basedaf cringe cooked cooking clutch cracked sweats sweatlord noob nub pro gamer gamers gg ez ezpz op goated goat vibe vibin lowkey highkey finna tryna irl iykyk idk wya wyd hmu wsg wassup bro bruh fam homie gang squad main alt legit meta grind ghost peek aim build edit reset buff nerf carry carried toxic sweaty
""".split())

users = {}
users_lock = threading.Lock()


def load_words():
    path = Path(__file__).resolve().parent / WORDS_FILE
    words = set()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    w = line.strip().lower()
                    if w.isalpha():
                        words.add(w)
        except OSError:
            pass
    if len(words) < 100000:
        try:
            print("Downloading large English word database...")
            urllib.request.urlretrieve(WORDS_URL, path)
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    w = line.strip().lower()
                    if w.isalpha():
                        words.add(w)
            print(f"Loaded {len(words):,} alphabetic words.")
        except Exception as exc:
            print(f"Large word database unavailable: {exc}")
            words.update(FALLBACK_WORDS)
    return sorted(words or FALLBACK_WORDS)


WORDS = load_words()


def new_user():
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "KOBYUsernameFinder/3.0",
        "Accept": "application/json",
    })
    return {
        "lock": threading.Lock(),
        "running": False,
        "mode": "random",
        "platform": "roblox",
        "max_length": 4,
        "delay": DEFAULT_DELAY,
        "checked": 0,
        "available": [],
        "valid_candidates": [],
        "watch": {},
        "queue": [],
        "recent": [],
        "error": "",
        "worker": None,
        "session": sess,
        "seed": secrets.randbits(64),
        "hunt": True,
        "stop_on_available": False,
        "last_candidate": "",
    }


def get_user(user_id):
    with users_lock:
        if user_id not in users:
            users[user_id] = new_user()
        return users[user_id]


def user_id_from_cookie(handler):
    for part in handler.headers.get("Cookie", "").split(";"):
        part = part.strip()
        if part.startswith("koby_user="):
            value = part.split("=", 1)[1].strip()
            if 20 <= len(value) <= 100:
                return value, False
    return secrets.token_urlsafe(32), True


def clean_letter(value):
    value = str(value or "").strip().lower()
    return value[0] if value and value[0] in ALPHABET else ""


def clamp_length(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 4
    return max(1, min(10, value))


def clamp_delay(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_DELAY
    return max(0.1, min(5.0, value))


def exact_length(mode, maximum):
    try:
        n = int(mode.split("_")[0])
    except (ValueError, IndexError):
        n = maximum
    return max(1, min(n, maximum))


def number_to_candidate(number, chars, length):
    out = [""] * length
    base = len(chars)
    for i in range(length - 1, -1, -1):
        number, rem = divmod(number, base)
        out[i] = chars[rem]
    return "".join(out)


def infinite_exhaustive(chars, length, starting_char="", seed=0):
    """Walk every combination, then start another randomized cycle forever."""
    fixed = clean_letter(starting_char)
    if fixed and fixed in chars:
        suffix_len = length - 1
        if suffix_len == 0:
            while True:
                yield fixed
        total = len(chars) ** suffix_len
        cycle = 0
        while True:
            offset = (seed + cycle * 0x9E3779B97F4A7C15) % total
            step_order = list(range(total))
            rng = random.Random(seed ^ cycle)
            rng.shuffle(step_order)
            for idx in step_order:
                yield fixed + number_to_candidate((offset + idx) % total, chars, suffix_len)
            cycle += 1
        return

    total = len(chars) ** length
    cycle = 0
    while True:
        offset = (seed + cycle * 0x9E3779B97F4A7C15) % total
        order = list(range(total))
        rng = random.Random(seed ^ cycle)
        rng.shuffle(order)
        for idx in order:
            yield number_to_candidate((offset + idx) % total, chars, length)
        cycle += 1


def infinite_random(chars, min_length, max_length, seed):
    rng = random.Random(seed)
    while True:
        length = rng.randint(min_length, max_length)
        yield "".join(rng.choice(chars) for _ in range(length))


def pattern_stream(maximum, seed):
    rng = random.Random(seed)
    patterns = []
    for a in ALPHABET:
        for b in ALPHABET:
            if a == b:
                continue
            patterns.append(a + b)
            if maximum >= 3:
                patterns.extend((a + a + b, a + b + b, a + b + a))
            if maximum >= 4:
                patterns.extend((a + b + a + b, a + a + b + b, a + b + b + a))
    while True:
        rng.shuffle(patterns)
        for p in patterns:
            if len(p) <= maximum:
                yield p


def repeater_stream(repeat_mode, repeat_char, maximum, seed):
    fixed = clean_letter(repeat_char)
    letters = [fixed] if fixed else list(ALPHABET)
    rng = random.Random(seed)
    while True:
        batch = []
        for a in letters:
            if maximum >= 2:
                batch.append(a + a)
            if maximum >= 3:
                for b in ALPHABET:
                    if b != a:
                        batch.extend((a + a + b, a + b + b))
            if maximum >= 4:
                for b in ALPHABET:
                    if b != a:
                        batch.append(a + a + b + b)
            if repeat_mode == "triple" and maximum >= 3:
                batch.append(a * 3)
                if maximum >= 4:
                    for b in ALPHABET:
                        if b != a:
                            batch.extend((a * 3 + b, b + a * 3))
            if repeat_mode == "mirror":
                if maximum >= 3:
                    for b in ALPHABET:
                        if b != a:
                            batch.append(a + b + a)
                if maximum >= 4:
                    for b in ALPHABET:
                        if b != a:
                            batch.append(a + b + b + a)
        if not batch:
            batch = list(infinite_random(ALPHABET, 1, maximum, rng.getrandbits(64)))
        rng.shuffle(batch)
        for candidate in batch:
            yield candidate


def word_stream(maximum, seed):
    eligible = sorted(set(w for w in WORDS if 1 <= len(w) <= maximum and w.isalpha()) | {w for w in SLANG_WORDS if 1 <= len(w) <= maximum and w.isalpha()})
    if not eligible:
        eligible = sorted(set(FALLBACK_WORDS) | SLANG_WORDS)
    rng = random.Random(seed)
    while True:
        rng.shuffle(eligible)
        for word in eligible:
            yield word


def mixed_stream(maximum, seed):
    """Infinite mixed generator: letters, digits and underscore, varied patterns."""
    rng = random.Random(seed)
    while True:
        length = rng.randint(1, maximum)
        style = rng.randrange(8)
        if style == 0 and length >= 2:
            a = rng.choice(ALPHABET); b = rng.choice(ALPHABET + "0123456789_")
            tail = "".join(rng.choice(CHARACTERS) for _ in range(length - 2))
            yield a + b + tail
        elif style == 1:
            yield "".join(rng.choice(ALPHABET) for _ in range(length))
        elif style == 2:
            yield "".join(rng.choice("0123456789") for _ in range(length))
        elif style == 3 and length >= 3:
            a = rng.choice(ALPHABET)
            yield a + "".join(rng.choice(CHARACTERS) for _ in range(length - 1))
        elif style == 4 and length >= 2:
            yield "".join(rng.choice(ALPHABET) for _ in range(length - 1)) + rng.choice("0123456789_")
        elif style == 5:
            yield "".join(rng.choice(CHARACTERS) for _ in range(length))
        elif style == 6 and length >= 3:
            a = rng.choice(ALPHABET); b = rng.choice(CHARACTERS)
            yield a + b + a + "".join(rng.choice(CHARACTERS) for _ in range(length - 3))
        else:
            yield "".join(rng.choice(CHARACTERS) for _ in range(length))


def hunter_stream(mode, maximum, patterns, repeat_mode, repeat_char, seed):
    """Infinite rare-looking pool. It does not invent word+number hybrids in Words mode."""
    rng = random.Random(seed)
    if mode == "pattern_hunter":
        yield from pattern_stream(maximum, seed)
        return
    if mode == "og_hunter":
        pools = [
            infinite_exhaustive(ALPHABET, min(4, maximum), seed=seed ^ 11),
            pattern_stream(maximum, seed ^ 22),
            repeater_stream(repeat_mode, repeat_char, maximum, seed ^ 33),
        ]
    elif mode == "premium_hunter":
        pools = [
            word_stream(maximum, seed ^ 44),
            pattern_stream(maximum, seed ^ 55),
            mixed_stream(maximum, seed ^ 66),
        ]
    else:  # One Above All
        pools = [
            pattern_stream(maximum, seed ^ 77),
            repeater_stream(repeat_mode, repeat_char, maximum, seed ^ 88),
            word_stream(maximum, seed ^ 99),
            mixed_stream(maximum, seed ^ 111),
        ]
    while True:
        order = list(range(len(pools)))
        rng.shuffle(order)
        for idx in order:
            # Take a varied number from each pool before rotating.
            for _ in range(rng.randint(4, 16)):
                yield next(pools[idx])


def generate_mode(mode, maximum, repeat_mode="none", repeat_char="", patterns=None, starting_char="", seed=0):
    if mode.endswith("_letters"):
        yield from infinite_exhaustive(ALPHABET, exact_length(mode, maximum), starting_char, seed)
        return
    if mode.endswith("_characters"):
        yield from infinite_exhaustive(CHARACTERS, exact_length(mode, maximum), starting_char, seed)
        return
    if mode == "words":
        yield from word_stream(maximum, seed)
        return
    if mode == "repeater":
        yield from repeater_stream(repeat_mode, repeat_char, maximum, seed)
        return
    if mode in {"patterns", "pattern_hunter"}:
        yield from pattern_stream(maximum, seed)
        return
    if mode in {"og_hunter", "premium_hunter", "one_above_all"}:
        yield from hunter_stream(mode, maximum, patterns, repeat_mode, repeat_char, seed)
        return
    if mode == "best":
        yield from hunter_stream("one_above_all", maximum, patterns, repeat_mode, repeat_char, seed)
        return
    yield from mixed_stream(maximum, seed)


def platform_candidate(user, platform, username):
    import re
    n = str(username or "").strip().lower()
    if platform == "discord":
        ok = 2 <= len(n) <= 32 and re.fullmatch(r"[a-z0-9_.]+", n) is not None and not n.startswith(".") and not n.endswith(".")
        return ("VALID", "Discord username format passes") if ok else ("INVALID", "Does not match Discord username format")
    if platform == "tiktok":
        ok = 2 <= len(n) <= 24 and re.fullmatch(r"[a-zA-Z0-9_.]+", n) is not None and not n.endswith(".")
        return ("VALID", "TikTok username format passes") if ok else ("INVALID", "Does not match TikTok username format")
    return validate(user, username)

def validate(user, username):
    try:
        response = user["session"].get(
            VALIDATE_URL,
            params={
                "request.username": username,
                "request.birthday": "2000-01-01T00:00:00.000Z",
                "request.context": "Signup",
            },
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
    return "INVALID", data.get("message", "Invalid username")


def add_queue(user, username):
    with user["lock"]:
        user["queue"].append({"username": username, "status": "CHECKING"})
        user["queue"] = user["queue"][-MAX_QUEUE_DISPLAY:]


def remove_queue(user, username):
    with user["lock"]:
        user["queue"] = [x for x in user["queue"] if x["username"] != username]


def add_result(user, username, status, message=""):
    with user["lock"]:
        user["recent"].append({
            "username": username,
            "status": status,
            "message": message,
            "time": time.time(),
        })
        user["recent"] = user["recent"][-MAX_RECENT_RESULTS:]


def worker(user, platform, mode, maximum, delay, repeat_mode, repeat_char, patterns, starting_char, seed, stop_on_available):
    backoff = delay
    seen = set()
    try:
        stream = generate_mode(mode, maximum, repeat_mode, repeat_char, patterns, starting_char, seed)
        for username in stream:
            with user["lock"]:
                if not user["running"]:
                    return
            if not username or len(username) > maximum or username in seen:
                continue
            seen.add(username)
            # Keep memory bounded during an infinite hunt. After a large pass,
            # allow a fresh randomized cycle rather than growing forever.
            if len(seen) >= 100000:
                seen.clear()
                seen.add(username)
            with user["lock"]:
                user["last_candidate"] = username
            add_queue(user, username)
            status, message = platform_candidate(user, platform, username)
            if platform == "roblox" and status == "AVAILABLE":
                with user["lock"]:
                    if username not in user["available"]:
                        user["available"].append(username)
                    user["checked"] += 1
                    user["error"] = ""
                    should_stop = stop_on_available
                add_result(user, username, status)
                remove_queue(user, username)
                backoff = delay
                if should_stop:
                    return
            elif platform != "roblox" and status == "VALID":
                with user["lock"]:
                    if username not in user["valid_candidates"]:
                        user["valid_candidates"].append(username)
                    user["checked"] += 1
                    user["error"] = ""
                add_result(user, username, "VALID", message)
                remove_queue(user, username)
                backoff = delay
            elif status in ("TAKEN", "INVALID"):
                with user["lock"]:
                    user["checked"] += 1
                    user["error"] = ""
                add_result(user, username, status, message)
                remove_queue(user, username)
                backoff = delay
            elif status == "THROTTLED":
                add_result(user, username, "WAITING", message)
                with user["lock"]:
                    user["error"] = f"Roblox is throttling requests. Waiting {backoff:.1f}s."
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                remove_queue(user, username)
                continue
            else:
                add_result(user, username, "ERROR", message)
                with user["lock"]:
                    user["error"] = message
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                remove_queue(user, username)
                continue
            time.sleep(backoff)
    finally:
        with user["lock"]:
            user["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, data, status=200, cookie=None):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if cookie:
            self.send_header("Set-Cookie", f"koby_user={cookie}; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        uid, fresh = user_id_from_cookie(self)
        user = get_user(uid)
        if parsed.path == "/":
            return self.serve_file("index.html", uid if fresh else None)
        if parsed.path == "/api/status":
            with user["lock"]:
                data = {
                    "running": user["running"],
                    "mode": user["mode"],
                    "platform": user["platform"],
                    "maxLength": user["max_length"],
                    "delay": user["delay"],
                    "checked": user["checked"],
                    "available": list(user["available"]),
                    "validCandidates": list(user["valid_candidates"]),
                    "watch": dict(user["watch"]),
                    "queue": list(user["queue"]),
                    "queueCount": len(user["queue"]),
                    "recent": list(user["recent"]),
                    "error": user["error"],
                    "lastCandidate": user["last_candidate"],
                }
            return self.send_json(data, cookie=uid if fresh else None)
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        uid, fresh = user_id_from_cookie(self)
        user = get_user(uid)
        if parsed.path == "/api/start":
            return self.start(user, uid if fresh else None)
        if parsed.path == "/api/watch":
            return self.watch(user, uid if fresh else None)
        if parsed.path == "/api/stop":
            with user["lock"]:
                user["running"] = False
            return self.send_json({"ok": True}, cookie=uid if fresh else None)
        self.send_error(404)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def start(self, user, cookie):
        data = self.read_json()
        platform = data.get("platform", "roblox")
        if platform not in {"roblox", "discord", "tiktok"}: platform = "roblox"
        mode = data.get("mode", "random")
        allowed = {
            "one_above_all", "og_hunter", "premium_hunter", "pattern_hunter",
            "best", "random",
            "1_letters", "2_letters", "3_letters", "4_letters",
            "1_characters", "2_characters", "3_characters", "4_characters",
            "repeater", "patterns", "words",
        }
        if mode not in allowed:
            mode = "random"
        maximum = clamp_length(data.get("maxLength", 4))
        delay = clamp_delay(data.get("delay", DEFAULT_DELAY))
        repeat_mode = data.get("repeatMode", "none")
        if repeat_mode not in {"none", "double", "triple", "mirror"}:
            repeat_mode = "none"
        repeat_char = clean_letter(data.get("repeatChar", ""))
        starting_char = clean_letter(data.get("startingChar", ""))
        patterns = data.get("patterns") if isinstance(data.get("patterns"), dict) else {}
        stop_on_available = bool(data.get("stopOnAvailable", False))
        hunt = bool(data.get("hunt", True))

        with user["lock"]:
            if user["running"]:
                return self.send_json({"ok": False, "message": "Already running."}, 409, cookie)
            user.update({
                "running": True,
                "platform": platform,
                "mode": mode,
                "max_length": maximum,
                "delay": delay,
                "checked": 0,
                "available": [],
        "valid_candidates": [],
        "watch": {},
                "queue": [],
                "recent": [],
                "error": "",
                "seed": secrets.randbits(64),
                "hunt": hunt,
                "stop_on_available": stop_on_available,
                "last_candidate": "",
            })
            seed = user["seed"]

        t = threading.Thread(
            target=worker,
            args=(user, platform, mode, maximum, delay, repeat_mode, repeat_char, patterns, starting_char, seed, stop_on_available),
            daemon=True,
        )
        with user["lock"]:
            user["worker"] = t
        t.start()
        return self.send_json({"ok": True}, cookie=cookie)

    def watch(self, user, cookie):
        data = self.read_json()
        names = data.get("names", [])
        if not isinstance(names, list): names = []
        names = [str(n).strip().lower() for n in names if isinstance(n, str) and n.strip()]
        names = list(dict.fromkeys(names))[:25]
        results = {}
        for name in names:
            status, message = validate(user, name)
            results[name] = {"status": status, "message": message, "time": time.time()}
            time.sleep(0.25)
        with user["lock"]:
            user["watch"] = results
        return self.send_json({"ok": True, "watch": results}, cookie=cookie)

    def serve_file(self, filename, cookie=None):
        path = Path(__file__).resolve().parent / filename
        if not path.exists():
            return self.send_error(404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", f"koby_user={cookie}; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("KOBY USERNAME FINDER 3.0")
    print("Website: http://127.0.0.1:8000")
    print("Per-browser checker sessions: ENABLED")
    print("Modes: Letters / Characters / Words + Slang / Repeater / Patterns / Best Mix / Random")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with users_lock:
            for user in users.values():
                with user["lock"]:
                    user["running"] = False
        server.server_close()
