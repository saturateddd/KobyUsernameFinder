KOBY_CHECK_SPEED = 0.05
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path
import itertools
import math
import json
import random
import re
import secrets
import threading
import time
import urllib.request
import requests

# KOBY SMART PRIORITY START
import re as _koby_re

def koby_smart_score(name):
    """Rank candidate quality; never decides platform availability."""
    if not name:
        return -10**9
    s = str(name).lower()
    score = 0
    if _koby_re.fullmatch(r"[a-z]+", s): score += 28
    elif _koby_re.fullmatch(r"[a-z0-9_]+", s): score += 12
    vowels = len(_koby_re.findall(r"[aeiou]", s))
    if vowels and vowels < len(s): score += 8
    if vowels == 1 and len(s) <= 5: score += 4
    if _koby_re.fullmatch(r"(.)(.)\1\2", s): score += 42
    if _koby_re.fullmatch(r"(.)(.)\2\1", s): score += 40
    if _koby_re.fullmatch(r"(.)(.)\1\1", s): score += 34
    if _koby_re.fullmatch(r"(.)(.)\2\2", s): score += 34
    if _koby_re.fullmatch(r"(.)(.)\1", s): score += 30
    if _koby_re.fullmatch(r"(.)(.)\2", s): score += 28
    if _koby_re.fullmatch(r"(.)\1+", s): score += 36
    if len(set(s)) <= 2: score += 12
    if s == s[::-1]: score += 18
    if _koby_re.search(r"[0-9_]", s): score -= 3
    if (_koby_re.fullmatch(r"[a-z0-9_]{3,4}", s)
        and not _koby_re.search(r"[aeiou]", s)
        and not _koby_re.search(r"[0-9_]", s)):
        score -= 8
    return score

def koby_prioritize_candidates(candidates):
    seen = set()
    unique = []
    for candidate in candidates or []:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return sorted(unique, key=koby_smart_score, reverse=True)
# KOBY SMART PRIORITY END

HOST = "0.0.0.0"
PORT = 8000
VALIDATE_URL = "https://auth.roblox.com/v2/usernames/validate"
DEFAULT_DELAY = 0.05
MAX_BACKOFF = 60
MAX_QUEUE_DISPLAY = 100
MAX_RECENT_RESULTS = 100
ALPHABET = "abcdefghijklmnopqrstuvwxyz"
CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789_"
DISCORD_CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789_."

# KOBY EXHAUSTIVE MIXED CHARACTER GENERATOR START
def exhaustive_mixed_candidates(length, alphabet, seed=None):
    """Lazily visit the COMPLETE fixed-length space in randomized order.

    Repetition is allowed. Every unique candidate appears exactly once.
    No candidate pool is stored in RAM. The affine permutation
        index = (offset + step * counter) mod total
    is a bijection whenever gcd(step, total) == 1.
    """
    if length <= 0:
        return

    alphabet = "".join(dict.fromkeys(str(alphabet)))
    if not alphabet:
        return

    base = len(alphabet)
    total = base ** length
    rng = random.Random(seed if seed is not None else secrets.randbits(64))

    if total == 1:
        yield alphabet * length
        return

    offset = rng.randrange(total)

    # Find a stride coprime with the complete space size.
    # This guarantees no duplicate index and complete coverage.
    step = rng.randrange(1, total)
    while math.gcd(step, total) != 1:
        step = rng.randrange(1, total)

    for counter in range(total):
        n = (offset + counter * step) % total
        out = [""] * length
        for pos in range(length - 1, -1, -1):
            n, rem = divmod(n, base)
            out[pos] = alphabet[rem]
        yield "".join(out)

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


# KOBY READABLE WORD VARIANTS START
LEET_SUBSTITUTIONS = {
    "a": ("4",),
    "e": ("3",),
    "i": ("1",),
    "l": ("1",),
    "o": ("0",),
    "s": ("5",),
    "g": ("6",),
    "t": ("7",),
    "b": ("8",),
    "z": ("2",),
}

def readable_word_variants(word):
    """Readable word variants with AT MOST ONE numeric substitution."""
    word = str(word or "").strip().lower()
    if not word.isalpha():
        return

    seen = set()
    for pos, char in enumerate(word):
        replacements = LEET_SUBSTITUTIONS.get(char, ())
        if isinstance(replacements, str):
            replacements = (replacements,)

        for replacement in replacements:
            replacement = str(replacement)
            if len(replacement) != 1 or not replacement.isdigit():
                continue
            candidate = word[:pos] + replacement + word[pos + 1:]
            if candidate not in seen and sum(ch.isdigit() for ch in candidate) <= 1:
                seen.add(candidate)
                yield candidate

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
    return max(0.05, min(5.0, value))


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
    """Randomized, duplicate-free traversal of the complete space."""
    chars = tuple(dict.fromkeys(str(chars)))
    if not chars or length <= 0:
        return

    base = len(chars)
    total = base ** length
    rng = random.Random(seed)

    if total == 1:
        yield chars[0] * length
        return

    offset = rng.randrange(total)
    stride = rng.randrange(1, total)
    while math.gcd(stride, total) != 1:
        stride = rng.randrange(1, total)

    for counter in range(total):
        n = (offset + counter * stride) % total
        out = [""] * length
        for pos in range(length - 1, -1, -1):
            n, rem = divmod(n, base)
            out[pos] = chars[rem]
        yield "".join(out)

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
    eligible = sorted(
        set(w for w in WORDS if 1 <= len(w) <= maximum and w.isalpha())
        | {w for w in SLANG_WORDS if 1 <= len(w) <= maximum and w.isalpha()}
    )
    if not eligible:
        eligible = sorted(set(FALLBACK_WORDS) | SLANG_WORDS)
    rng = random.Random(seed)
    while True:
        rng.shuffle(eligible)
        for word in eligible:
            yield word
            for variant in readable_word_variants(word):
                if variant != word and len(variant) <= maximum:
                    yield variant


def mixed_stream(maximum, seed, platform="roblox"):
    rng = random.Random(seed)
    chars = DISCORD_CHARACTERS if str(platform).lower() == "discord" else CHARACTERS
    while True:
        length = rng.randint(1, maximum)
        yield "".join(rng.choice(chars) for _ in range(length))

def hunter_stream(mode, maximum, patterns, repeat_mode, repeat_char, seed, platform="roblox"):
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
            mixed_stream(maximum, seed ^ 66, platform),
        ]
    else:
        pools = [
            pattern_stream(maximum, seed ^ 77),
            repeater_stream(repeat_mode, repeat_char, maximum, seed ^ 88),
            word_stream(maximum, seed ^ 99),
            mixed_stream(maximum, seed ^ 111, platform),
        ]
    while True:
        order = list(range(len(pools)))
        rng.shuffle(order)
        for idx in order:
            for _ in range(rng.randint(4, 16)):
                yield next(pools[idx])

def generate_mode(mode, maximum, repeat_mode="none", repeat_char="",
                  patterns=None, starting_char="", seed=0, platform="roblox"):
    platform = str(platform or "roblox").lower()
    alphabet = DISCORD_CHARACTERS if platform == "discord" else CHARACTERS

    if mode.endswith("_letters"):
        length = exact_length(mode, maximum)
        yield from infinite_exhaustive(ALPHABET, length, seed=seed)
        return

    if mode.endswith("_characters"):
        length = exact_length(mode, maximum)
        yield from infinite_exhaustive(alphabet, length, seed=seed)
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
        yield from hunter_stream(
            mode, maximum, patterns, repeat_mode, repeat_char, seed, platform
        )
        return

    if mode == "best":
        yield from hunter_stream(
            "one_above_all", maximum, patterns, repeat_mode, repeat_char,
            seed, platform
        )
        return

    yield from mixed_stream(maximum, seed, platform)

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


def worker(user, platform, mode, maximum, delay, repeat_mode, repeat_char,
           patterns, starting_char, seed, stop_on_available):
    backoff = delay
    seen = set()
    try:
        stream = generate_mode(mode, maximum, repeat_mode, repeat_char, patterns,
                               starting_char, seed, platform)
        for username in stream:
            with user["lock"]:
                if not user["running"]:
                    return
            username = str(username or "").strip().lower()
            key = username.casefold()
            if not username or len(username) > maximum or key in seen:
                continue
            seen.add(key)
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
                remove_queue(user, username)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            else:
                add_result(user, username, "ERROR", message)
                with user["lock"]:
                    user["error"] = message
                remove_queue(user, username)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            time.sleep(backoff)
    except Exception as exc:
        with user["lock"]:
            user["error"] = f"Worker error: {type(exc).__name__}: {exc}"
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
        platform = str(data.get("platform", "roblox")).lower()
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
