#!/usr/bin/env python3
"""FaceNinja from the command line, for people and AI agents: photos in, MetaHuman head out.

Standard library only, Python 3.9+, Linux, macOS and Windows (`python faceninja.py ...`).

  faceninja.py login [--no-wait]           sign in: approve a code in the browser, a new API key is saved
  faceninja.py whoami | balance            who is signed in, credits left (balance: and auto-reload)
  faceninja.py make PHOTO... [--name N] [--formats glb,zip] [--out DIR] [--no-wait] [--max-wait S]
  faceninja.py status ID [--wait] [--max-wait S]
  faceninja.py download ID [--formats glb,fbx,zip] [--out DIR]
  faceninja.py batch DIR [--per-person-subfolders] [--yes] ...
  faceninja.py list | logout
  faceninja.py update [--check | --rollback]  update the skill now (it also updates itself, at most hourly)
  faceninja.py guidance                     the server's current rules and notices for agents
  faceninja.py attest [--check | --revoke]  the user's promise about the photos, asked once per computer (see below)

Before the first head on a computer the user must confirm, themselves, that they have the right to use the photos and
the consent of each person shown, that everyone shown is 18 or older, and that they follow the Content & Likeness
Policy. In a terminal `attest` (or the first `make`/`batch`) asks; an AI agent shows the user the text, asks, and only
after the user says yes runs `attest --confirmed-by-user`. Never confirm on the user's behalf.

Every command first checks (at most once an hour) whether faceninja.net has a newer skill. If so it downloads the
skill's files, checks their SHA-256, swaps them in (the old ones stay in .previous/), says so, and runs the command
with the new code. FACENINJA_NO_AUTOUPDATE=1 turns that off. It only ever writes inside the skill's own folder.

Add --json for one machine-readable JSON object on stdout (progress goes to stderr).
The API key comes from FACENINJA_API_KEY, else from the credentials file `login` writes
(~/.config/faceninja/credentials.json, %APPDATA%\\FaceNinja\\credentials.json on Windows). Keys are never printed.

Exit codes: 0 ok, 1 error, 2 bad usage, 3 needs the user's confirmation (--yes, or `attest`), 4 not signed in or sign-in refused,
5 not enough credits, 6 the head failed (credits returned), 75 still running or still waiting (run the command again).
"""
import argparse
import calendar
import hashlib
import http.client
import json
import mimetypes
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

__version__ = "1.4.2"   # 1.4.2: MIT licence (LICENSE file), more install routes (skills CLI, Claude Code plugin).
                        # 1.4.0: fictional adults and NPC crowds from a description (make --describe, portrait review/accept/redraw/cancel,
                        #        --no-keep); says "test mode" on the test environment (sandbox.faceninja.net); `samples` lists without signing in
                        # 1.3.0: the user's attestation for the photos, asked once per computer and sent with every head; `samples` (the
                        #        free sample heads); heads take 10 to 20 minutes (hints, waiting text), hints for greyscale and blurry photos;
                        # 1.2.0: updates itself, shows the server's guidance; 401 is exit 4 everywhere, downloads retry and resume,
                        #        no tracebacks, long Retry-After fails fast (1.1.1); 1.1.0: formats made on request are waited for

DEFAULT_BASE = "https://faceninja.net"
MAX_PHOTOS = 15
MAX_PHOTO_BYTES = 25 * 1024 * 1024
BATCH_CONFIRM_ABOVE = 5
FORMATS = {"glb": "glb", "fbx": "fbx", "obj": "obj", "stl": "stl", "ply": "ply", "zip": "metahuman_zip", "metahuman_zip": "metahuman_zip"}
ALL_FORMATS = ["glb", "fbx", "obj", "stl", "ply", "metahuman_zip"]
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif"}
FINAL = ("completed", "failed", "deleted")
STATE_FILE = ".faceninja.json"
DOWNLOAD_TRIES = 3          # a download cut short is retried (and resumed) this many times, with a fresh link each time
LONG_RETRY_AFTER_S = 90     # a server that asks for a longer pause than this is not waited for: the command fails and says how long

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_CONFIRM, EXIT_AUTH, EXIT_CREDITS, EXIT_FAILED, EXIT_PENDING = 0, 1, 2, 3, 4, 5, 6, 75

# The promise the user makes once per computer. A new version asks again.
ATTESTATION_VERSION = "2026-10-25"
ATTESTATION_TEXT = ("For every head I make with FaceNinja on this computer: I have the right to use the photos and the consent of each "
                    "person shown; they are 18 or older; I will follow the Content & Likeness Policy.")
ATTESTATION = {"rights_and_consent": True, "adult": True}


class FNError(Exception):
    def __init__(self, message, code="error", hint=None, exit_code=EXIT_ERROR, status=None, request_id=None, data=None):
        super().__init__(message)
        self.message, self.code, self.hint, self.exit_code = message, code, hint, exit_code
        self.status, self.request_id, self.data = status, request_id, data


# ---- output ----------------------------------------------------------------------------------------------------------

class Out:
    """Human lines on stdout, or (with --json) progress on stderr and one JSON object on stdout."""

    current = None   # the command's Out, for notes that come from the HTTP layer (test mode)

    def __init__(self, as_json):
        self.json = as_json
        self.extra = {}   # added to the JSON result: skill updates, guidance notices, test mode
        Out.current = self

    def say(self, *parts):
        print(" ".join(str(p) for p in parts), file=sys.stderr if self.json else sys.stdout, flush=True)

    def result(self, data, human=None):
        if self.json:
            print(json.dumps(dict(data, **self.extra) if isinstance(data, dict) else data, indent=2), flush=True)
        elif human:
            print(human, flush=True)


def num(n):
    return "{:,}".format(int(n or 0))


def usd(credits, per_usd=100):
    return "${:,.2f}".format((credits or 0) / float(per_usd or 100))


def key_prefix(key):
    return (key or "")[:12] + "..."


# ---- configuration and credentials -----------------------------------------------------------------------------

def config_dir():
    if os.environ.get("FACENINJA_CONFIG_DIR"):
        return Path(os.environ["FACENINJA_CONFIG_DIR"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "FaceNinja"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "faceninja"


def write_private(path, data):
    """JSON readable by the owner only (0600); the folder is made 0700 if we create it."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(str(tmp), 0o600)
    except OSError:
        pass
    os.replace(str(tmp), str(path))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def norm_base(base):
    return (base or DEFAULT_BASE).strip().rstrip("/")


def load_credentials(base):
    """(key, source): the environment first, then the credentials file if it was made for this server."""
    env = (os.environ.get("FACENINJA_API_KEY") or "").strip()
    if env:
        return env, "FACENINJA_API_KEY"
    path = config_dir() / "credentials.json"
    c = read_json(path)
    if c and c.get("api_key") and norm_base(c.get("base_url")) == base:
        return c["api_key"], str(path)
    return None, None


def detect_client():
    e = os.environ
    if e.get("FACENINJA_CLIENT"):
        agent = e["FACENINJA_CLIENT"]
    elif e.get("CLAUDECODE") or e.get("CLAUDE_CODE_ENTRYPOINT"):
        agent = "Claude Code"
    elif any(k.startswith("CODEX_") for k in e):
        agent = "Codex"
    elif e.get("CURSOR_AGENT") or e.get("CURSOR_TRACE_ID"):
        agent = "Cursor"
    elif e.get("GEMINI_CLI"):
        agent = "Gemini CLI"
    elif any(k.startswith("OPENCLAW") for k in e):
        agent = "OpenClaw"
    elif any(k.startswith("AIDER_") for k in e):
        agent = "Aider"
    else:
        agent = "FaceNinja CLI"
    host = (socket.gethostname() or "").split(".")[0][:40] or None
    return {"agent": agent, "host": host, "os": platform.system() or None, "version": __version__}


# ---- HTTP ------------------------------------------------------------------------------------------------------------

class Api:
    def __init__(self, base, key=None, out=None):
        self.base, self.key, self.out = base, key, out
        self.ua = "faceninja-skill/{} (Python {}; {})".format(__version__, platform.python_version(), platform.system())

    def call(self, method, path, body=None, raw=None, content_type=None, headers=None, auth=True, timeout=60, retries=4, ok=(200, 201, 202)):
        """Returns (status, data, headers). Retries rate limits and transient failures; raises FNError otherwise."""
        url = path if path.startswith("http") else self.base + path
        h = {"Accept": "application/json", "User-Agent": self.ua, "FaceNinja-Skill-Version": __version__}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            h["Content-Type"] = content_type or "application/octet-stream"
        if auth:
            if not self.key:
                raise not_signed_in(self.base)
            h["Authorization"] = "Bearer " + self.key
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as res:
                    text = res.read().decode("utf-8", "replace")
                    note_skill_headers(self.base, url, res.headers)
                    return res.status, parse_json(text, res.status, self.base), res.headers
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                note_skill_headers(self.base, url, e.headers)
                try:
                    payload = json.loads(text) if text.strip() else None
                except ValueError:
                    payload = None
                if e.code in ok:
                    return e.code, payload, e.headers
                err = (payload or {}).get("error") if isinstance(payload, dict) else None
                err = err if isinstance(err, dict) else {}
                retry_after = _int(e.headers.get("Retry-After"), 0)
                if e.code == 426 and err.get("code") == "skill_update_required":
                    raise FNError(err.get("message") or "This skill is too old for the API.", code="skill_update_required", status=426,
                                  hint=err.get("hint") or "Run: python faceninja.py update", request_id=err.get("request_id"))
                if e.code == 401 and auth:
                    # a revoked or unknown key is "sign in again" (exit 4) on every command, not only on the first call
                    raise FNError(err.get("message") or "The API key is not valid any more (revoked?).", code=err.get("code") or "unauthorized",
                                  exit_code=EXIT_AUTH,
                                  hint="Run: python faceninja.py login --force" + ("   (first unset FACENINJA_API_KEY: it wins over the saved key)" if os.environ.get("FACENINJA_API_KEY") else ""),
                                  status=401, request_id=err.get("request_id"))
                # a re-download limit lasts hours, and a long Retry-After (the per-network sign-in limit says 10 minutes) would run
                # past an agent's shell timeout in silence: say so instead of retrying
                if (attempt <= retries and (e.code == 429 or e.code in (500, 502, 503, 504)) and err.get("code") != "redownload_limited"
                        and retry_after <= LONG_RETRY_AFTER_S):
                    wait = min(max(retry_after, 2 ** attempt), 60)
                    if self.out:
                        self.out.say("  server answered {} ({}); retrying in {} s".format(e.code, err.get("code") or "busy", wait))
                    time.sleep(wait)
                    continue
                hint = err.get("hint")
                if e.code == 429 and not hint:
                    hint = "Wait {} s, then run the same command again.".format(retry_after) if retry_after else "Wait a minute, then run the same command again."
                raise FNError(err.get("message") or "HTTP {} from {}".format(e.code, path), code=err.get("code") or "http_{}".format(e.code),
                              hint=hint, status=e.code, request_id=err.get("request_id"), data={"retry_after": retry_after})
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:   # URLError, a cut-off answer (IncompleteRead), TLS and socket errors
                if attempt <= retries:
                    wait = min(2 ** attempt, 30)
                    if self.out:
                        self.out.say("  cannot reach {} ({}); retrying in {} s".format(self.base, getattr(e, "reason", e), wait))
                    time.sleep(wait)
                    continue
                raise FNError("Cannot reach {}: {}".format(self.base, getattr(e, "reason", e)), code="network",
                              hint="Check the connection, or FACENINJA_API_BASE if you changed the server.")

    def download(self, url, dest, expect_bytes=None, expect_sha=None):
        """GET a signed link (never with the API key) into dest, checking size and SHA-256. A transfer cut short keeps its
        `.part` file and raises download_failed; the next call resumes it with an HTTP Range request. dest only appears
        once the file is complete and its checksum matches."""
        tmp = dest.with_name(dest.name + ".part")
        have = tmp.stat().st_size if tmp.exists() else 0
        if have and (not expect_bytes or have >= expect_bytes):
            tmp.unlink()    # not the start of this file
            have = 0
        headers = {"User-Agent": self.ua}
        if have:
            headers["Range"] = "bytes={}-".format(have)
        h = hashlib.sha256()
        n = 0
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as res:
                if have and res.status == 206 and (res.headers.get("Content-Range") or "").startswith("bytes {}-".format(have)):
                    with open(str(tmp), "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                    n, mode = have, "ab"
                else:
                    mode = "wb"      # no earlier part, or the server sent the whole file again
                with open(str(tmp), mode) as f:
                    while True:
                        chunk = res.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        n += len(chunk)
        except urllib.error.HTTPError as e:
            if e.code == 416 and tmp.exists():
                tmp.unlink()
            raise FNError("Download failed: HTTP {}".format(e.code), code="download_failed", status=e.code, hint="Run download again; links are fresh each time.")
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise FNError("Download interrupted after {} bytes: {}".format(num(tmp.stat().st_size if tmp.exists() else 0), getattr(e, "reason", e)),
                          code="download_failed", hint="Run download again; it resumes where it stopped.")
        if expect_bytes and n < expect_bytes:
            raise FNError("Download cut short: {} of {} bytes.".format(num(n), num(expect_bytes)), code="download_failed", hint="Run download again; it resumes where it stopped.")
        if (expect_bytes and n != expect_bytes) or (expect_sha and h.hexdigest() != expect_sha):
            tmp.unlink()
            raise FNError("Downloaded file is corrupted ({} bytes, checksum differs).".format(num(n)), code="download_corrupt", hint="Run download again.")
        os.replace(str(tmp), str(dest))
        return n, h.hexdigest()


def parse_json(text, status, base):
    """The JSON of an answer; a page that is not JSON (a captive portal, a proxy's error page) is an error, not a traceback."""
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise FNError("{} answered HTTP {} with something that is not JSON (a proxy, captive portal or outage page?).".format(base, status),
                      code="bad_response", hint="Check the connection or FACENINJA_API_BASE, then run the command again.", status=status)


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def not_signed_in(base):
    return FNError("Not signed in to {}.".format(base), code="not_signed_in", exit_code=EXIT_AUTH,
                   hint="Run: python faceninja.py login   (or set FACENINJA_API_KEY)")


def api_for(args, out, need_key=True):
    key, source = load_credentials(args.base)
    if need_key and not key:
        raise not_signed_in(args.base)
    api = Api(args.base, key, out)
    api.key_source = source
    return api


def account(api):
    try:
        return api.call("GET", "/v1/account")[1]
    except FNError as e:
        if e.status == 401:
            raise FNError("The saved API key is not valid any more (revoked?).", code="unauthorized", exit_code=EXIT_AUTH,
                          hint="Run: python faceninja.py login --force")
        raise


def head_price(acct):
    p = acct.get("prices") or {}
    return int(p.get("head") or 1500), int(p.get("credits_per_usd") or 100)


# ---- login / whoami / balance / logout ------------------------------------------------------------------------------

def cmd_login(args, out):
    api = Api(args.base, None, out)
    cdir = config_dir()
    pending_path = cdir / "pending-login.json"
    if os.environ.get("FACENINJA_API_KEY"):
        out.say("Note: FACENINJA_API_KEY is set and wins over the saved sign-in; unset it to use the key saved here.")
    key, source = load_credentials(args.base)
    if key and source != "FACENINJA_API_KEY" and not args.force:
        try:
            a = account(Api(args.base, key, out))
            return out.result({"ok": True, "status": "signed_in", "email": a.get("email"), "key_prefix": key_prefix(key), "credits": a.get("credits")},
                              "Already signed in to {} as {} (key {}). Use --force to sign in again.".format(args.base, a.get("email"), key_prefix(key)))
        except FNError as e:
            if e.exit_code != EXIT_AUTH:
                raise

    p = read_json(pending_path)
    fresh = False
    if not (p and norm_base(p.get("base_url")) == args.base and p.get("expires_at", 0) > time.time() + 15) or args.force_new:
        client = detect_client()
        name = args.client_name or "{}{}".format(client["agent"], " on " + client["host"] if client.get("host") else "")
        _, d, _ = api.call("POST", "/v1/device/code", body={"client_name": name, "client": client}, auth=False)
        p = {"base_url": args.base, "device_code": d["device_code"], "user_code": d["user_code"], "verification_uri": d["verification_uri"],
             "verification_uri_complete": d["verification_uri_complete"], "interval": d.get("interval", 5),
             "expires_at": time.time() + int(d.get("expires_in", 600)), "client_name": d.get("client_name", name)}
        write_private(pending_path, p)
        fresh = True

    left = int(p["expires_at"] - time.time())
    info = {"ok": True, "status": "pending", "user_code": p["user_code"], "verification_uri": p["verification_uri"],
            "verification_uri_complete": p["verification_uri_complete"], "expires_in": left, "client_name": p["client_name"]}
    msg = ("To sign in, open {}\nand approve the code {} (it expires in {} min). The page asks you to sign in to FaceNinja first if needed.\n"
           "The new API key will be named \"{}\".").format(p["verification_uri_complete"], p["user_code"], max(1, left // 60), p["client_name"])
    if fresh and not args.no_browser:
        try:
            if webbrowser.open(p["verification_uri_complete"]):
                msg += "\n(Opened the page in your browser.)"
        except Exception:
            pass
    if args.no_wait:
        return out.result(dict(info, tell_the_user="Open {} and approve the code {}.".format(p["verification_uri_complete"], p["user_code"]),
                               next="Send tell_the_user to the user as a message first, then run `faceninja.py login --json --max-wait 540` to wait for the approval."),
                          msg + "\nWhen it is approved, run: python faceninja.py login")
    out.say(msg)
    out.say("Waiting for approval...")

    interval = int(p.get("interval") or 5)
    deadline = time.time() + args.max_wait if args.max_wait else p["expires_at"] + 5
    while True:
        if time.time() + interval > deadline:
            raise FNError("Still waiting for the code {} to be approved.".format(p["user_code"]), code="authorization_pending",
                          exit_code=EXIT_PENDING, hint="Approve it at {}, then run login again.".format(p["verification_uri_complete"]))
        time.sleep(interval)
        try:
            _, tok, _ = api.call("POST", "/v1/device/token", body={"device_code": p["device_code"]}, auth=False, retries=2)
            break
        except FNError as e:
            if e.code == "authorization_pending":
                continue
            if e.code == "slow_down":
                interval = max(interval + 5, (e.data or {}).get("retry_after") or 0)
                continue
            if e.code in ("access_denied", "expired_token", "invalid_grant"):
                pending_path.unlink() if pending_path.exists() else None
                e.exit_code = EXIT_AUTH
                e.hint = "Run login again to get a new code." if e.code != "access_denied" else "The sign-in was denied in the browser."
            raise

    cred = {"base_url": args.base, "api_key": tok["access_token"], "key_id": tok.get("key_id"), "key_prefix": tok.get("key_prefix"),
            "key_name": tok.get("key_name"), "email": tok.get("email"), "created_at": tok.get("created_at")}
    path = cdir / "credentials.json"
    write_private(path, cred)
    pending_path.unlink() if pending_path.exists() else None
    out.result({"ok": True, "status": "signed_in", "email": tok.get("email"), "key_prefix": key_prefix(tok["access_token"]),
                "key_name": tok.get("key_name"), "credentials_file": str(path)},
               "Signed in as {}. API key {} (\"{}\") saved to {}".format(tok.get("email"), key_prefix(tok["access_token"]), tok.get("key_name"), path))


def cmd_whoami(args, out):
    api = api_for(args, out)
    a = account(api)
    price, per = head_price(a)
    out.result({"ok": True, "email": a.get("email"), "account_id": a.get("id"), "key_prefix": key_prefix(api.key), "key_source": api.key_source,
                "base_url": args.base, "credits": a.get("credits"), "held_credits": a.get("held_credits"), "head_price": price},
               "Signed in to {} as {}\nKey {} from {}\nCredits: {} ({} heads at {} credits each)".format(
                   args.base, a.get("email"), key_prefix(api.key), api.key_source, num(a.get("credits")), (a.get("credits") or 0) // price, num(price)))


def auto_reload(api):
    """The account's auto-reload settings (None on a server without them)."""
    try:
        _, r, _ = api.call("GET", "/v1/account/auto-reload", retries=1)
        return r
    except FNError as e:
        if e.code in ("not_found", "method_not_allowed"):
            return None
        raise


def auto_reload_line(r, per):
    if not r:
        return None, ""
    on = bool(r.get("enabled"))
    info = {"enabled": on, "state": r.get("state"), "threshold_credits": r.get("threshold_credits"), "amount_usd": r.get("amount"),
            "credits_per_reload": (r.get("credits") or 0) + (r.get("bonus_credits") or 0), "reloads_this_month": (r.get("this_month") or {}).get("reloads"),
            "max_reloads_per_month": r.get("max_reloads_per_month"), "max_usd_per_month": r.get("max_usd_per_month")}
    if not on:
        why = {"declined": " (turned off after a declined payment)", "agreement_canceled": " (the payment method was removed)"}.get(r.get("off_reason") or "", "")
        return info, "\nAuto-reload: off{}. The account holder can turn it on in Factory > Billing.".format(why)
    cap = " Monthly limit reached until {}.".format(((r.get("this_month") or {}).get("resets_at") or "")[:10]) if r.get("state") == "cap_reached" else ""
    return info, ("\nAuto-reload: on. Below {} credits it buys ${} ({} credits); {} of {} top-ups used this month.{}\n"
                  "A head the balance cannot pay waits for the top-up (stage waiting_for_payment).").format(
        num(r.get("threshold_credits")), r.get("amount"), num(info["credits_per_reload"]), info["reloads_this_month"] or 0, r.get("max_reloads_per_month"), cap)


def cmd_balance(args, out):
    api = api_for(args, out)
    a = account(api)
    price, per = head_price(a)
    c = int(a.get("credits") or 0)
    ar, ar_text = auto_reload_line(auto_reload(api), per)
    out.result({"ok": True, "credits": c, "held_credits": a.get("held_credits"), "head_price": price, "head_price_usd": price / per,
                "heads_affordable": c // price, "buy_url": args.base + "/factory#/billing", "auto_reload": ar},
               "{} credits ({}): enough for {} head(s). A head costs {} credits ({}).{}{}\nBuy credits: {}/factory#/billing".format(
                   num(c), usd(c, per), c // price, num(price), usd(price, per),
                   "\n{} credits are held by heads in progress.".format(num(a.get("held_credits"))) if a.get("held_credits") else "", ar_text, args.base))


def cmd_logout(args, out):
    cdir = config_dir()
    path = cdir / "credentials.json"
    c = read_json(path)
    revoked = False
    if c and c.get("api_key") and not args.keep_key:
        try:
            Api(norm_base(c.get("base_url")), c["api_key"], out).call("DELETE", "/v1/keys/current", retries=1)
            revoked = True
        except FNError as e:
            if e.status == 401:
                revoked = True     # the server no longer knows the key: it is revoked already
            else:
                out.say("Could not revoke the key on the server ({}); revoke it in Factory > API keys.".format(e.message))
    for p in (path, cdir / "pending-login.json"):
        if p.exists():
            p.unlink()
    note = " FACENINJA_API_KEY is still set in your environment." if os.environ.get("FACENINJA_API_KEY") else ""
    out.result({"ok": True, "removed": bool(c), "revoked": revoked, "env_key_set": bool(os.environ.get("FACENINJA_API_KEY"))},
               ("Signed out: the saved key was {} and deleted.".format("revoked on the server" if revoked else "NOT revoked (still valid on the server)") if c else "No saved sign-in.") + note)


# ---- heads ------------------------------------------------------------------------------------------------------------

def parse_formats(s):
    out = []
    for f in (s or "").replace(" ", "").lower().split(","):
        if not f:
            continue
        if f == "all":
            out.extend(ALL_FORMATS)
        elif f in FORMATS:
            out.append(FORMATS[f])
        else:
            raise FNError("Unknown format {!r}. Use glb, fbx, obj, stl, ply, zip (MetaHuman ZIP) or all.".format(f), code="invalid_request", exit_code=EXIT_USAGE)
    return list(dict.fromkeys(out))


def looks_like_image(path):
    """True when the first bytes are those of a JPEG, PNG, WebP, or HEIC/HEIF/AVIF file; a web page saved as photo.jpg is not."""
    with open(str(path), "rb") as f:
        b = f.read(16)
    return b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or (b[:4] == b"RIFF" and b[8:12] == b"WEBP") or b[4:8] == b"ftyp"


def check_photos(paths):
    photos = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            raise FNError("{} is a folder, not a photo.".format(p), code="invalid_request", exit_code=EXIT_USAGE,
                          hint="`make` takes photo files (1 to 15 of ONE person). One head per photo in a folder: python faceninja.py batch {}".format(p))
        if not p.is_file():
            raise FNError("No such photo: {}".format(p), code="invalid_request", exit_code=EXIT_USAGE)
        ext = p.suffix.lower()
        if ext not in IMAGE_TYPES:
            raise FNError("{}: use JPEG, PNG, WebP, HEIC/HEIF or AVIF.".format(p.name), code="unsupported_media_type", exit_code=EXIT_USAGE)
        size = p.stat().st_size
        if size < 1000 or size > MAX_PHOTO_BYTES:
            raise FNError("{}: {} bytes; a photo must be between 1 KB and 25 MB.".format(p.name, num(size)), code="invalid_request", exit_code=EXIT_USAGE,
                          hint="Downscale large photos to about 4000 px on the long side.")
        if not looks_like_image(p):
            with open(str(p), "rb") as f:
                start = f.read(24).decode("latin-1")
            raise FNError("{}: this is not an image file (it starts with {!r}), whatever its extension says.".format(p.name, "".join(c if 32 <= ord(c) < 127 else "?" for c in start)),
                          code="unsupported_media_type", exit_code=EXIT_USAGE, hint="A download that failed often leaves a web page or an error text named photo.jpg. Get the real photo and try again.")
        photos.append(p)
    if not 1 <= len(photos) <= MAX_PHOTOS:
        raise FNError("A head takes 1 to {} photos of one person; got {}.".format(MAX_PHOTOS, len(photos)), code="invalid_request", exit_code=EXIT_USAGE)
    return photos


def fingerprint(photos, name):
    h = hashlib.sha256((name or "").encode("utf-8"))
    for p in sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in photos):
        h.update(p.encode())
    return h.hexdigest()[:32]


def load_state(out_dir):
    s = read_json(out_dir / STATE_FILE) or {}
    s.setdefault("heads", {})
    return s


def save_state(out_dir, state):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / STATE_FILE).write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---- the user's attestation -------------------------------------------------------------------------------------

def attestation_file():
    return config_dir() / "attestation.json"


def attestation_record(base):
    """The confirmation stored for this server and the current text, or None."""
    rec = (read_json(attestation_file()) or {}).get(base)
    return rec if isinstance(rec, dict) and rec.get("version") == ATTESTATION_VERSION else None


def policy_url(base):
    return "{}/legal/content-policy".format(base)


def attestation_needed(base, out):
    return FNError(
        "The user has not confirmed the attestation on this computer yet. Show them this text and ask them; do not answer for them: \"{}\" "
        "(policy: {}). Only after they say yes, run: python faceninja.py attest --confirmed-by-user".format(ATTESTATION_TEXT, policy_url(base)),
        code="attestation_required", exit_code=EXIT_CONFIRM,
        hint="In a terminal the user can also run `python faceninja.py attest` and answer the question themselves.",
        data={"attestation": ATTESTATION_TEXT, "version": ATTESTATION_VERSION, "policy": policy_url(base)})


def save_attestation(base, how):
    data = read_json(attestation_file()) or {}
    data[base] = {"version": ATTESTATION_VERSION, "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "how": how,
                  "text": ATTESTATION_TEXT}
    write_private(attestation_file(), data)
    return data[base]


def ask_in_terminal(out, base):
    """A person at a terminal answers the question themselves (never when stdin is not a terminal)."""
    if out.json or not (sys.stdin and sys.stdin.isatty()):
        return False
    print("\n" + ATTESTATION_TEXT + "\nPolicy: " + policy_url(base), file=sys.stderr)
    try:
        answer = input("Type yes to confirm: ").strip().lower()
    except EOFError:
        return False
    return answer in ("yes", "y")


def report_attestation(api, rec):
    """Record the confirmation with the account once per key (evidence of who confirmed it, on which computer)."""
    if rec.get("recorded_key") == key_prefix(api.key):
        return
    c = detect_client()
    try:
        api.call("POST", "/v1/account/attestation", body={"attestation": ATTESTATION, "client": {"agent": c.get("agent"), "host": c.get("host"), "version": __version__}})
    except FNError as e:
        if e.status in (404, 405):   # an older server: nothing to record there
            return
        raise
    data = read_json(attestation_file()) or {}
    if isinstance(data.get(api.base), dict):
        data[api.base]["recorded_key"] = key_prefix(api.key)
        write_private(attestation_file(), data)


def require_attestation(api, out):
    """Before anything is uploaded: the user's confirmation for this computer, asked in a terminal if there is one."""
    rec = attestation_record(api.base)
    if rec is None:
        if not ask_in_terminal(out, api.base):
            raise attestation_needed(api.base, out)
        rec = save_attestation(api.base, "terminal")
        out.say("Thank you: confirmed for this computer.")
    report_attestation(api, rec)


def cmd_attest(args, out):
    rec = attestation_record(args.base)
    if args.revoke:
        data = read_json(attestation_file()) or {}
        data.pop(args.base, None)
        write_private(attestation_file(), data)
        return out.result({"ok": True, "attested": False}, "Forgotten: the next head asks again.")
    if args.check:
        if rec:
            return out.result({"ok": True, "attested": True, "version": rec["version"], "confirmed_at": rec.get("confirmed_at"), "text": ATTESTATION_TEXT},
                              "Confirmed on this computer on {} (version {}).".format(rec.get("confirmed_at"), rec["version"]))
        raise attestation_needed(args.base, out)
    if args.confirmed_by_user:
        rec = save_attestation(args.base, "confirmed_by_user")
    elif ask_in_terminal(out, args.base):
        rec = save_attestation(args.base, "terminal")
    else:
        raise attestation_needed(args.base, out)
    key, _ = load_credentials(args.base)
    if key:
        try:
            report_attestation(api_for(args, out), rec)
        except FNError as e:
            out.say("Saved on this computer; the account record follows with the next head ({}).".format(e.message))
    out.result({"ok": True, "attested": True, "version": rec["version"], "text": ATTESTATION_TEXT},
               "Confirmed for this computer: " + ATTESTATION_TEXT)


def create_head(api, out, photos, name, public_figure=False):
    ids = []
    for p in photos:
        q = urllib.parse.urlencode({"filename": p.name[:120]})
        _, ph, _ = api.call("POST", "/v1/photos?" + q, raw=p.read_bytes(), content_type=IMAGE_TYPES[p.suffix.lower()], timeout=180)
        ids.append(ph["id"])
        out.say("  uploaded {} -> {}".format(p.name, ph["id"]))
    body = {"photo_ids": ids, "attestation": ATTESTATION}   # the user confirmed it for this computer (require_attestation)
    if public_figure:
        body["public_figure"] = True
    if name:
        body["name"] = name[:120]
    try:
        _, head, _ = api.call("POST", "/v1/heads", body=body, headers={"Idempotency-Key": str(uuid.uuid4())})
    except FNError as e:
        if e.code == "insufficient_credits":
            e.exit_code = EXIT_CREDITS
            e.hint = "Buy credits at {}/factory#/billing".format(api.base)
        elif e.code == "terms_not_accepted":
            e.exit_code = EXIT_CONFIRM
            e.hint = "The user accepts the terms once at {}/factory (one checkbox), then run this again.".format(api.base)
        elif e.code == "attestation_required":
            e.exit_code = EXIT_CONFIRM
        raise
    return head


def describe(h):
    q = h.get("queue") or {}
    bits = [h.get("status")]
    if h.get("stage"):
        bits.append(h["stage"])
    if h.get("progress") is not None and h.get("status") == "processing":
        bits.append("{}%".format(h["progress"]))
    if h.get("status") == "queued" and q.get("position"):
        bits.append("position {} in line".format(q["position"]))
    if q.get("eta_s"):
        bits.append("about {} min left".format(max(1, int(q["eta_s"]) // 60)))
    if h.get("message"):
        bits.append(h["message"])
    return ", ".join(str(b) for b in bits if b)


def wait_head(api, out, head_id, max_wait=0):
    """Long-poll until the head is final. Raises EXIT_PENDING when max_wait seconds pass first."""
    start = time.time()
    last = None
    while True:
        left = max_wait - (time.time() - start) if max_wait else 60
        if max_wait and left <= 0:
            raise FNError("Head {} is still running ({}).".format(head_id, last), code="still_running", exit_code=EXIT_PENDING,
                          hint="Run: python faceninja.py status {} --wait".format(head_id), data={"id": head_id})
        _, h, _ = api.call("GET", "/v1/heads/{}?wait={}".format(head_id, int(max(1, min(60, left)))), timeout=95)
        d = describe(h)
        if d != last:
            out.say("  {}  {}".format(time.strftime("%H:%M:%S"), d))
            last = d
        if h.get("status") in ("needs_licence", "needs_review"):
            raise FNError("Head {} is in staff review; credits are held, not charged.".format(head_id),
                          code="licence_review_required", exit_code=EXIT_PENDING,
                          hint="Open {}/factory#/heads/{} to name the person, declare rights and submit evidence. Usually 1–2 business days. Never assert permission on the user's behalf.".format(api.base, head_id),
                          data={"head": h})
        if h.get("status") in FINAL:
            return h


def head_failure(h, base):
    err = h.get("error") or {}
    code = err.get("code") or h.get("status")
    hint = {
        "no_face": "Use a sharp photo where one face is clearly visible.",
        "multiple_people": "Crop the photos to one person.",
        "not_same_person": "Every photo must show the same person.",
        "unreadable_photo": "Re-save the photo as JPEG or PNG.",
        "greyscale_photo": "A photo is black and white or sepia: send colour photos, without a filter.",
        "blurry_photo": "A photo is heavily blurred: send a sharp one.",
        "face_too_small": "The face in a photo is too small: send the original, full-size photo.",
        "content_rejected": "The photo was refused by the content check (people under 18, nudity, or not a real photo).",
    }.get(code, "See the photo guide: {}/guidelines".format(base))
    credits = (h.get("credits") or {}).get("state")
    msg = "Head {} {}: {}{}".format(h.get("id"), h.get("status"), err.get("message") or code,
                                   " Credits returned." if credits == "released" and "returned" not in (err.get("message") or "") else "")
    return FNError(msg, code=code, hint=hint, exit_code=EXIT_FAILED, data={"head": h})


def safe_name(s):
    s = re.sub(r"[^\w.-]+", "_", s or "").strip("._")
    return s[:120] or "file"


PREPARE_MAX_S = 20 * 60   # how long a format made on request is waited for


def file_link(api, out, head_id, body):
    """A signed link to one file. A format the build did not deliver (OBJ, STL, PLY, GLB/FBX without ARKit), or any
    format of a head whose files expired after 30 days, is made on request: the API answers 202 {"status": "preparing",
    "eta_s"} and we ask again after Retry-After until it is ready."""
    start, said = time.time(), False
    while True:
        status, link, headers = api.call("POST", "/v1/heads/{}/files".format(head_id), body=body)
        if status != 202 or (link or {}).get("url"):
            return link
        if not said:
            eta = (link or {}).get("eta_s")
            out.say("  preparing {} (made on request{}) ...".format(body["format"], ", about {} min".format(max(1, int(eta) // 60)) if eta else ""))
            said = True
        if time.time() - start > PREPARE_MAX_S:
            raise FNError("{} is still being prepared.".format(body["format"]), code="still_preparing", exit_code=EXIT_PENDING,
                          hint="Run download again in a few minutes.", data={"id": head_id})
        time.sleep(min(60, max(5, _int(headers.get("Retry-After"), 15))))


def download_files(api, out, head_id, formats, out_dir, arkit=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    saved, skipped = [], []
    for fmt in formats:
        body = {"format": fmt}
        if arkit is not None and fmt in ("glb", "fbx"):
            body["arkit"] = arkit
        try:
            link = file_link(api, out, head_id, body)
        except FNError as e:
            if e.code == "head_not_ready":
                e.exit_code, e.hint = EXIT_PENDING, "Wait first: python faceninja.py status {} --wait".format(head_id)
                raise
            if e.code in ("variant_not_available", "field_not_applicable", "variant_failed"):
                out.say("  skipped {}: {}".format(fmt, e.message))
                skipped.append({"format": fmt, "reason": e.message})
                continue
            raise
        dest = out_dir / safe_name(link.get("filename") or "{}.{}".format(head_id, fmt))
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            try:
                n, sha = api.download(link["url"], dest, link.get("bytes"), link.get("sha256"))
                break
            except FNError as e:
                if e.code not in ("download_failed", "download_corrupt") or attempt == DOWNLOAD_TRIES:
                    e.data = dict(e.data or {}, id=head_id, format=fmt)
                    e.hint = ("Head {} is finished and paid for; nothing is lost. Run the same command again (it resumes and only downloads) or: "
                              "python faceninja.py download {} --formats {} --out {}").format(head_id, head_id, fmt, out_dir)
                    raise
                out.say("  {} interrupted ({}); trying again ({}/{}) ...".format(fmt, e.message, attempt, DOWNLOAD_TRIES - 1))
                time.sleep(2 * attempt)
                link = file_link(api, out, head_id, body)      # links last a minute: ask for a fresh one
        out.say("  saved {} ({:.1f} MB)".format(dest, n / 1e6))
        saved.append({"format": fmt, "arkit": link.get("arkit"), "path": str(dest), "bytes": n, "sha256": sha})
    return saved, skipped


def quote(api, out, count):
    a = account(api)
    price, per = head_price(a)
    credits = int(a.get("credits") or 0)
    total = price * count
    out.say("Cost: {} head(s) x {} credits = {} credits ({}). Balance: {} credits ({}).".format(
        count, num(price), num(total), usd(total, per), num(credits), usd(credits, per)))
    return {"heads": count, "credits_per_head": price, "total_credits": total, "total_usd": total / per, "balance": credits,
            "balance_after": credits - total, "sufficient": credits >= total}


FICTIONAL_ATTESTATION = "These are generated people. I will not present them as real people or use them to impersonate anyone."


def cmd_make_fictional(args, out):
    if args.photos:
        raise FNError("Use --describe without photos.", code="invalid_request", exit_code=EXIT_USAGE)
    try:
        description = json.loads(args.describe) if args.describe.lstrip().startswith("{") else {"age": args.age, "look": args.describe}
        diversity = json.loads(args.diversity) if args.diversity else {}
    except ValueError:
        raise FNError("The structured description or diversity is not valid JSON.", code="invalid_request", exit_code=EXIT_USAGE)
    for key in ("sex", "descent", "skin_tone", "build", "facial_hair", "glasses"):
        value = getattr(args, key, None)
        if value is not None:
            description[key] = value
    hair = {key: getattr(args, "hair_" + key) for key in ("style", "length", "colour", "dye") if getattr(args, "hair_" + key) is not None}
    if hair:
        description["hair"] = dict(description.get("hair") or {}, **hair)
    payload = {"describe": description, "count": args.count, "diversity": diversity,
               "attestation": {"fictional": True}, "keep_for_improvement": not args.no_keep,
               "options": {"formats": parse_formats(args.formats), "arkit": args.arkit is not False}}
    if args.name:
        payload["name"] = args.name
    api = api_for(args, out)
    _, quote, _ = api.call("POST", "/v1/heads?dry_run", body=payload)
    out.say("Fictional heads: {} credits reserved; {} draws per head included. Cancel before building: {} credits per delivered portrait retained; failures refund fully. Review expires after 24 hours.".format(
        quote["credits"]["cost"], quote["draws"], quote["portrait_fee"]))
    out.say("Build data is kept for algorithm improvement, never model training. --no-keep opts out; you may withdraw later.")
    if args.dry_run:
        return out.result({"ok": True, "dry_run": True, "quote": quote}, "Dry run: nothing generated or reserved.")
    if not args.fictional_confirmed:
        confirmed = False
        if sys.stdin.isatty():
            out.say(FICTIONAL_ATTESTATION)
            confirmed = input("Confirm this promise? [y/N] ").strip().lower() in ("y", "yes")
        if not confirmed:
            raise FNError("Ask the user to confirm: " + FICTIONAL_ATTESTATION, code="attestation_required", exit_code=EXIT_CONFIRM,
                          hint="After the user confirms, rerun with --fictional-confirmed. Never answer on their behalf.")
    if args.count > confirm_above(args.base) and not args.yes:
        raise FNError("Confirm the crowd cost with the user, then rerun with --yes.", code="confirmation_required", exit_code=EXIT_CONFIRM, data={"quote": quote})
    out_dir = Path(args.out).expanduser().resolve()
    state = load_state(out_dir)
    fp = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    records = state.setdefault("fictional", {})
    record = records.get(fp) if not args.new else None
    if record and record.get("heads"):
        heads = [api.call("GET", "/v1/heads/" + h["id"])[1] for h in record["heads"]]
    else:
        record = record or {"key": "fictional-" + os.urandom(16).hex()}
        records[fp] = record
        save_state(out_dir, state)      # persist the retry key BEFORE sending a paid request
        _, result, _ = api.call("POST", "/v1/heads", body=payload, headers={"Idempotency-Key": record["key"]})
        heads = result["data"] if result.get("object") == "list" else [result]
        record["heads"] = [{"id": h["id"]} for h in heads]
        save_state(out_dir, state)
    deadline = time.time() + args.max_wait if args.max_wait else None
    while args.wait and any(h.get("fictional", {}).get("state") == "drawing" and h.get("status") not in FINAL for h in heads):
        if deadline and time.time() > deadline:
            return out.result({"ok": True, "heads": heads, "pending": True}, "Portraits are being prepared. Run this same command to resume.")
        time.sleep(2)
        heads = [api.call("GET", "/v1/heads/" + h["id"])[1] for h in heads]
    held = [h for h in heads if h.get("status") in ("needs_licence", "needs_review")]
    if held:
        raise FNError("A fictional portrait requires staff permission review; no 3D build has started.",
                      code="licence_review_required", exit_code=EXIT_PENDING,
                      hint="Open {}/factory#/heads/{} to submit evidence. Never assert permission for the user.".format(api.base, held[0]["id"]),
                      data={"heads": heads})
    portraits = []
    for h in heads:
        if h.get("status") == "awaiting_portrait":
            _, portrait, _ = api.call("GET", "/v1/heads/" + h["id"] + "/portrait")
            portraits.append({"id": h["id"], **portrait})
            out.say("{} draw {}: {}".format(h["id"], portrait["draw"], portrait["url"]))
    return out.result({"ok": True, "heads": heads, "portraits": portraits, "quote": quote},
        "Show each portrait to the user. Accept only their chosen draw with: portrait HEAD --accept --draw N. Redraw: --redraw. Abandon: --cancel.")


def cmd_portrait(args, out):
    api = api_for(args, out)
    if args.cancel:
        _, head, _ = api.call("DELETE", "/v1/heads/" + args.id)
        return out.result({"ok": True, "head": head}, "Cancelled; the disclosed portrait fee applies if a checked portrait was delivered.")
    action = "accept" if args.accept else "redraw" if args.redraw else None
    if action:
        if args.draw is None:
            raise FNError("Give --draw N for the portrait the user reviewed.", code="invalid_request", exit_code=EXIT_USAGE)
        _, head, _ = api.call("POST", "/v1/heads/{}/portrait/{}".format(args.id, action), body={"draw": args.draw})
        return out.result({"ok": True, "head": head}, "Head {}: {}. Follow with status {} --wait.".format(args.id, action, args.id))
    _, portrait, _ = api.call("GET", "/v1/heads/" + args.id + "/portrait")
    out.result({"ok": True, "id": args.id, **portrait}, "Draw {}: {}".format(portrait["draw"], portrait["url"]))


def cmd_make(args, out):
    if args.describe is not None:
        return cmd_make_fictional(args, out)
    if not args.photos:
        raise FNError("Send photos or --describe.", code="invalid_request", exit_code=EXIT_USAGE)
    photos = check_photos(args.photos)
    formats = parse_formats(args.formats)
    out_dir = Path(args.out).expanduser().resolve()
    api = api_for(args, out)
    name = args.name or photos[0].stem
    state = load_state(out_dir)
    fp = fingerprint(photos, name)
    head = None
    prev = state["heads"].get(fp)
    if prev and not args.new:
        try:
            _, head, _ = api.call("GET", "/v1/heads/{}".format(prev["id"]))
            if head.get("status") in ("failed", "deleted"):
                head = None
            else:
                out.say("Resuming head {} made from these photos earlier ({}). Use --new for another head.".format(head["id"], head["status"]))
        except FNError as e:
            if e.status != 404:
                raise
            head = None
    q = None
    if head is None:
        q = quote(api, out, 1)
        if args.dry_run:
            return out.result(dict(ok=True, dry_run=True, quote=q, photos=[str(p) for p in photos], name=name, formats=formats),
                              "Dry run: nothing uploaded or spent." + ("" if q["sufficient"] else
                              "\nThe balance is too low: a real run would stop with exit 5. Buy credits at {}/factory#/billing".format(args.base)))
        if not q["sufficient"]:
            raise FNError("Not enough credits: a head costs {} credits, the balance is {}.".format(num(q["credits_per_head"]), num(q["balance"])),
                          code="insufficient_credits", exit_code=EXIT_CREDITS, hint="Buy credits at {}/factory#/billing".format(args.base))
        require_attestation(api, out)
        head = create_head(api, out, photos, name, args.public_figure)
        state["heads"][fp] = {"id": head["id"], "name": name, "photos": [str(p) for p in photos], "created_at": head.get("created_at")}
        save_state(out_dir, state)
        out.say("Created head {} ({}). {} credits held.".format(head["id"], describe(head), num(q["credits_per_head"])))
    result = {"ok": True, "id": head["id"], "name": name, "status": head.get("status"), "quote": q, "out": str(out_dir)}
    if not args.wait:
        return out.result(dict(result, next="python faceninja.py status {} --wait".format(head["id"])),
                          "Head {} is {}. Follow it: python faceninja.py status {} --wait".format(head["id"], head.get("status"), head["id"]))
    try:
        if head.get("status") not in FINAL:
            out.say("Waiting for {} (usually 10 to 20 minutes, because each head is built carefully; longer when the line is long)...".format(head["id"]))
            head = wait_head(api, out, head["id"], args.max_wait)
        if head.get("status") != "completed":
            raise head_failure(head, args.base)
        saved, skipped = download_files(api, out, head["id"], formats, out_dir, args.arkit)
    except FNError as e:
        if e.exit_code in (EXIT_ERROR, EXIT_AUTH) and "id" not in (e.data or {}):
            # the head exists and is paid: an error from here on (network, server, key) must not read like "nothing happened"
            e.data = dict(e.data or {}, id=head["id"])
            e.hint = "{}{}Head {} exists and its credits are held or spent. Run the same command again: it resumes this head and charges nothing twice.".format(
                e.hint or "", " " if e.hint else "", head["id"])
        raise
    result.update(status="completed", files=saved, skipped=skipped)
    out.result(result, "Done: head {} saved to {}".format(head["id"], out_dir))


def cmd_status(args, out):
    api = api_for(args, out)
    if args.wait:
        h = wait_head(api, out, args.id, args.max_wait)
    else:
        _, h, _ = api.call("GET", "/v1/heads/{}".format(args.id))
    files = [f.get("format") + ("+arkit" if f.get("arkit") else "") for f in (h.get("files") or [])]
    out.result({"ok": True, "head": h},
               "{} {}: {}{}".format(h["id"], "(" + h["name"] + ")" if h.get("name") else "", describe(h),
                                     "\nFiles: " + ", ".join(files) if files else "") +
               ("\nError: {} ({})".format((h.get("error") or {}).get("message"), (h.get("error") or {}).get("code")) if h.get("error") else ""))
    if args.wait and h.get("status") == "failed":
        sys.exit(EXIT_FAILED)


def cmd_download(args, out):
    api = api_for(args, out)
    saved, skipped = download_files(api, out, args.id, parse_formats(args.formats), Path(args.out).expanduser().resolve(), args.arkit)
    out.result({"ok": True, "id": args.id, "files": saved, "skipped": skipped},
               "Saved {} file(s) to {}".format(len(saved), Path(args.out).expanduser().resolve()))


def cmd_samples(args, out):
    """The free sample heads (no credits): list them (no sign-in needed), or download one (or all) with --download ID|all."""
    api = api_for(args, out, need_key=bool(args.download))
    _, page, _ = api.call("GET", "/v1/samples", auth=bool(api.key))
    rows = page.get("data") or []
    lic = page.get("license") or {}
    limits = page.get("limits") or {}
    if not args.download:
        lines = ["{}  {:<6} {}".format(s["id"], s.get("code") or "", s.get("label") or "") for s in rows]
        left = ("{} of {} sample files left today.".format(limits.get("remaining_today"), limits.get("files_per_day")) if api.key
                else "Downloading them needs an account: python faceninja.py login")
        res = {"ok": True, "samples": rows, "license": lic, "limits": limits}
        if not api.key:
            res["signed_in"] = False
        return out.result(res, ("\n".join(lines) if lines else "No samples yet.") + "\nFree, no credits. {} Licence: {}".format(left, lic.get("url")))
    wanted = rows if args.download == "all" else [s for s in rows if s["id"] == args.download or s.get("code") == args.download]
    if not wanted:
        raise FNError("No sample {}.".format(args.download), code="not_found", exit_code=EXIT_USAGE, hint="List them: python faceninja.py samples")
    kinds = parse_formats(args.formats)
    saved = []
    for s in wanted:
        dest_dir = Path(args.out).expanduser().resolve() / safe_name(s.get("code") or s["id"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in s.get("files") or []:
            if f["kind"] not in kinds:
                continue
            _, link, _ = api.call("GET", "/v1/samples/{}/files/{}".format(urllib.parse.quote(s["id"]), f["kind"]))
            dest = dest_dir / safe_name(link.get("filename") or f.get("filename") or f["kind"])
            n, sha = api.download(link["url"], dest, link.get("bytes"), link.get("sha256"))
            out.say("  saved {} ({:.1f} MB)".format(dest, n / 1e6))
            saved.append({"sample": s["id"], "format": f["kind"], "path": str(dest), "bytes": n, "sha256": sha})
    out.result({"ok": True, "files": saved, "license": lic},
               "Saved {} sample file(s) to {}. Free, no credits. Licence: {}".format(len(saved), Path(args.out).expanduser().resolve(), lic.get("url")))


def cmd_list(args, out):
    api = api_for(args, out)
    q = {"limit": args.limit}
    if args.status:
        q["status"] = args.status
    _, page, _ = api.call("GET", "/v1/heads?" + urllib.parse.urlencode(q))
    rows = page.get("data") or []
    lines = ["{}  {:<10}  {}  {}".format(h["id"], h.get("status"), (h.get("created_at") or "")[:16].replace("T", " "), h.get("name") or "") for h in rows]
    out.result({"ok": True, "heads": rows, "next_cursor": page.get("next_cursor")}, "\n".join(lines) if lines else "No heads yet.")


def people_in(folder, per_person):
    folder = Path(folder).expanduser()
    if folder.is_file():
        raise FNError("{} is a file, not a folder.".format(folder), code="invalid_request", exit_code=EXIT_USAGE,
                      hint="`batch` takes a folder of photos. For this one photo: python faceninja.py make {}".format(folder))
    if not folder.is_dir():
        raise FNError("No such folder: {}".format(folder), code="invalid_request", exit_code=EXIT_USAGE)
    imgs = lambda d: sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_TYPES)
    if per_person:
        people = [(d.name, imgs(d)) for d in sorted(folder.iterdir()) if d.is_dir() and not d.name.startswith(".")]
        people = [(n, ph) for n, ph in people if ph]
    else:
        people = [(p.stem, [p]) for p in imgs(folder)]
    if not people:
        raise FNError("No photos found in {}{}.".format(folder, " subfolders" if per_person else ""), code="invalid_request", exit_code=EXIT_USAGE,
                      hint=None if per_person else "One photo = one head. For several photos per person use --per-person-subfolders.")
    for n, ph in people:
        if len(ph) > MAX_PHOTOS:
            raise FNError("{}: {} photos; at most {} per head.".format(n, len(ph), MAX_PHOTOS), code="too_many_photos", exit_code=EXIT_USAGE)
        check_photos(ph)
    return people


def cmd_batch(args, out):
    people = people_in(args.folder, args.per_person_subfolders)
    formats = parse_formats(args.formats)
    out_dir = Path(args.out).expanduser().resolve()
    api = api_for(args, out)
    state = load_state(out_dir)
    plan = []
    for name, photos in people:
        fp = fingerprint(photos, name)
        prev = state["heads"].get(fp)
        plan.append({"name": name, "photos": [str(p) for p in photos], "fp": fp, "id": prev["id"] if prev else None})
    # heads made by an earlier run of this batch are resumed, not paid again (unless they failed)
    for p in plan:
        if p["id"]:
            try:
                _, h, _ = api.call("GET", "/v1/heads/{}".format(p["id"]))
                if h.get("status") in ("failed", "deleted"):
                    p["id"] = None
            except FNError as e:
                if e.status != 404:
                    raise
                p["id"] = None
    new = [p for p in plan if not p["id"]]
    out.say("Batch: {} person(s), {} new head(s) to create, {} resumed from an earlier run.".format(len(plan), len(new), len(plan) - len(new)))
    for p in plan:
        out.say("  {:<30} {} photo(s){}".format(p["name"][:30], len(p["photos"]), "  (resume " + p["id"] + ")" if p["id"] else ""))
    q = quote(api, out, len(new))
    if args.dry_run:
        return out.result({"ok": True, "dry_run": True, "quote": q, "plan": plan}, "Dry run: nothing uploaded or spent." + ("" if q["sufficient"] else
                          "\nThe balance is too low: a real run would stop with exit 5. Buy credits at {}/factory#/billing".format(args.base)))
    if len(new) > confirm_above(args.base) and not args.yes:
        raise FNError("This batch creates {} heads for {} credits ({}). Ask the user to confirm, then run again with --yes.".format(
            len(new), num(q["total_credits"]), "${:,.2f}".format(q["total_usd"])), code="confirmation_required", exit_code=EXIT_CONFIRM,
            data={"quote": q})
    if not q["sufficient"]:
        raise FNError("Not enough credits for {} heads: {} needed, {} available.".format(len(new), num(q["total_credits"]), num(q["balance"])),
                      code="insufficient_credits", exit_code=EXIT_CREDITS, hint="Buy credits at {}/factory#/billing, or make fewer heads.".format(args.base))
    if new:
        require_attestation(api, out)
    for p in new:
        out.say("Creating {}...".format(p["name"]))
        h = create_head(api, out, [Path(x) for x in p["photos"]], p["name"])
        p["id"] = h["id"]
        state["heads"][p["fp"]] = {"id": h["id"], "name": p["name"], "photos": p["photos"], "created_at": h.get("created_at")}
        save_state(out_dir, state)
    results = {p["id"]: {"name": p["name"], "id": p["id"], "status": None} for p in plan}
    if args.wait:
        start = time.time()
        pending = set(results)
        while pending:
            for hid in sorted(pending):
                _, h, _ = api.call("GET", "/v1/heads/{}".format(hid))
                r = results[hid]
                if describe(h) != r.get("last"):
                    out.say("  {}  {:<24} {}".format(time.strftime("%H:%M:%S"), r["name"][:24], describe(h)))
                    r["last"] = describe(h)
                r["status"] = h.get("status")
                if h.get("status") in FINAL:
                    pending.discard(hid)
                    if h["status"] == "completed":
                        r["files"], r["skipped"] = download_files(api, out, hid, formats, out_dir / safe_name(r["name"]), args.arkit)
                    else:
                        r["error"] = h.get("error")
            if not pending:
                break
            if args.max_wait and time.time() - start > args.max_wait:
                break
            time.sleep(15)
    for r in results.values():
        r.pop("last", None)
    done = [r for r in results.values() if r["status"] == "completed"]
    failed = [r for r in results.values() if r["status"] in ("failed", "deleted")]
    running = [r for r in results.values() if r["status"] not in FINAL]
    out.result({"ok": not failed and not running, "heads": list(results.values()), "completed": len(done), "failed": len(failed), "running": len(running),
                "out": str(out_dir)},
               "Batch: {} completed, {} failed, {} still running. Files in {}{}".format(
                   len(done), len(failed), len(running), out_dir,
                   "\nRun the same batch command again to keep waiting; nothing is created twice." if running else ""))
    if running:
        sys.exit(EXIT_PENDING)
    if failed:
        sys.exit(EXIT_FAILED)


# ---- staying current: self-update and the server's guidance ------------------------------------------------------------
#
# Once an hour at most (any /v1 answer also counts: it carries FaceNinja-Skill-Latest), every command asks the server for
# /skill/version.json. When it names a newer version: every file of the skill is downloaded, checked against its SHA-256,
# written to <skill>/.staged/ with a journal, the current files are copied to <skill>/.previous/, and the staged files
# replace them one by one (each replace is atomic). A run that finds .staged/ finishes the job first, so an interrupted
# update never leaves a half-written skill, and a file Windows keeps in use is swapped on the next run. Then the command
# runs again with the new code. Nothing outside the skill's own folder is written or run.

CHECK_EVERY_S = 3600
RETRY_OFFLINE_S = 600
SCRIPT = Path(os.path.abspath(__file__))
ARGV = []
UPDATED_ENV = "FACENINJA_SKILL_UPDATED_FROM"   # set for the run that continues a command with the new code
STATE = {"data": None, "dirty": False, "release": None}
DEFAULT_PACKAGE = ("SKILL.md", "README.md", "LICENSE", "reference/api.md", "scripts/faceninja.py")


def vtuple(s):
    m = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)(?![\d.])", str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b):
    ta, tb = vtuple(a), vtuple(b)
    return bool(ta and tb and ta > tb)


def trusted_source(base):
    """Code is only taken from an HTTPS server, or from this machine (test servers)."""
    u = urllib.parse.urlsplit(base)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"))


def autoupdate_off():
    return (os.environ.get("FACENINJA_NO_AUTOUPDATE") or "").strip().lower() not in ("", "0", "false", "no")


def plain(s, n=400):
    """Server text as one printable ASCII line (consoles on Windows are often not UTF-8)."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))
    return s.encode("ascii", "replace").decode("ascii")[:n]


def skill_state(base):
    """This server's entry in the shared state file: last check, latest version, guidance and the notices shown."""
    if STATE["data"] is None:
        d = read_json(config_dir() / "skill-state.json")
        STATE["data"] = d if isinstance(d, dict) else {}
    if not isinstance(STATE["data"].get(base), dict):
        STATE["data"][base] = {}
    return STATE["data"][base]


def save_skill_state():
    if STATE["dirty"] and STATE["data"] is not None:
        try:
            write_private(config_dir() / "skill-state.json", STATE["data"])
        except OSError:
            pass
        STATE["dirty"] = False


TEST_MODE_TEXT = ("FaceNinja test mode ({}): no real payments, no real heads. Pay with test cards; every head is a sample head, "
                  "ready in about a minute. Never present a test head as a real one.")


def note_test_mode(base, headers):
    """The test environment (sandbox.faceninja.net) marks every answer with FaceNinja-Test-Mode: say so once per command."""
    if headers is None or (headers.get("FaceNinja-Test-Mode") or "").lower() != "true":
        return
    out = Out.current
    if out is None or out.extra.get("test_mode"):
        return
    out.extra["test_mode"] = True
    out.say(TEST_MODE_TEXT.format(urllib.parse.urlsplit(base).netloc or base))


def note_skill_headers(base, url, headers):
    """Every /v1 answer names the latest skill and the guidance revision: a version check that costs nothing."""
    if headers is not None and url.startswith(base):
        note_test_mode(base, headers)
    if headers is None or not url.startswith(base + "/v1/"):
        return
    latest = headers.get("FaceNinja-Skill-Latest")
    if not vtuple(latest):
        return
    e = skill_state(base)
    e.update(latest=latest, checked_at=time.time())
    if vtuple(headers.get("FaceNinja-Skill-Min")):
        e["min"] = headers.get("FaceNinja-Skill-Min")
    if headers.get("FaceNinja-Guidance"):
        e["guidance_rev_seen"] = headers.get("FaceNinja-Guidance")
    STATE["dirty"] = True


def file_sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def safe_rel(rel):
    return isinstance(rel, str) and bool(re.match(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$", rel)) and ".." not in rel


def rmtree(p):
    if p.exists():
        shutil.rmtree(str(p), ignore_errors=True)


class Install:
    """Where this script lives: an installed skill (<skill>/scripts/faceninja.py next to <skill>/SKILL.md) or the script
    alone. Updates go through <root>/.staged/ and keep one rollback in <root>/.previous/ (.faceninja-staged/ and
    .faceninja-previous/ next to a lone script)."""

    def __init__(self, script=None):
        self.script = script or SCRIPT
        here = self.script.parent
        if here.name == "scripts" and (here.parent / "SKILL.md").is_file():
            self.root, self.kind, pre = here.parent, "skill", "."
        else:
            self.root, self.kind, pre = here, "script", ".faceninja-"
        self.staged, self.previous, self.lock = self.root / (pre + "staged"), self.root / (pre + "previous"), self.root / (pre + "update.lock")
        # the source copy in the FaceNinja repository (and the copy it publishes) is built, never updated in place
        self.source = any((p / "tools" / "skill" / "build.mjs").is_file() for p in (self.root.parent, self.root.parent.parent))

    @property
    def skill_md(self):
        return self.root / "SKILL.md" if self.kind == "skill" else None

    def why_not(self):
        """None when this copy may update itself, else the reason."""
        if self.source:
            return "this is the source copy in the FaceNinja repository, which is built, not updated"
        if not os.access(str(self.root), os.W_OK) or not os.access(str(self.script), os.W_OK):
            return "{} is not writable".format(self.root)
        return None

    def target(self, rel):
        if self.kind == "skill":
            return self.root / rel
        return self.script if rel == "faceninja.py" else None

    def wanted(self, release):
        """{published path: {sha256, bytes}}: what this copy takes from version.json."""
        if self.kind == "skill":
            pkg = release.get("package") or {}
        else:
            f = (release.get("files") or {}).get("faceninja.py")
            pkg = {"faceninja.py": f} if f else {}
        for rel, meta in pkg.items():
            if not safe_rel(rel) or not isinstance(meta, dict) or not re.match(r"^[0-9a-f]{64}$", str(meta.get("sha256") or "")):
                raise FNError("version.json lists an odd file ({!r}); kept version {}.".format(rel, __version__), code="skill_update_refused")
        if ("scripts/faceninja.py" if self.kind == "skill" else "faceninja.py") not in pkg:
            raise FNError("version.json does not list the script; kept version {}.".format(__version__), code="skill_update_refused")
        return pkg

    def take_lock(self):
        """One updater at a time (two agents may run commands at once). A lock left by a killed run expires."""
        for _ in range(2):
            try:
                fd = os.open(str(self.lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    pid = _int(self.lock.read_text().strip(), 0)
                    age = time.time() - self.lock.stat().st_mtime
                except OSError:
                    continue
                alive = age < 600
                if alive and os.name != "nt" and pid:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        alive = False
                    except OSError:
                        pass
                if alive:
                    return False
                try:
                    self.lock.unlink()
                except OSError:
                    return False
        return False

    def drop_lock(self):
        try:
            self.lock.unlink()
        except OSError:
            pass

    def leftovers(self):
        for p in self.root.glob(self.staged.name + ".tmp-*"):
            rmtree(p)
        rmtree(self.previous.with_name(self.previous.name + ".tmp"))

    def local_files(self, extra=()):
        if self.kind == "script":
            return {"faceninja.py": self.script}
        return {rel: self.root / rel for rel in list(DEFAULT_PACKAGE) + [r for r in extra if r not in DEFAULT_PACKAGE] if (self.root / rel).is_file()}

    def backup(self, extra=()):
        """The files in use now → .previous/ (one rollback)."""
        tmp = self.previous.with_name(self.previous.name + ".tmp")
        rmtree(tmp)
        for rel, src in self.local_files(extra).items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(tmp / rel))
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "VERSION.json").write_text(json.dumps({"version": __version__}), encoding="utf-8")
        rmtree(self.previous)
        os.replace(str(tmp), str(self.previous))

    def stage(self, blobs, journal):
        """Write the new files and their journal, then commit them in one rename (.staged/ exists only complete)."""
        tmp = self.root / "{}.tmp-{}".format(self.staged.name, os.getpid())
        rmtree(tmp)
        for rel, data in blobs.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            with open(str(tmp / rel), "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        with open(str(tmp / "update.json"), "w", encoding="utf-8") as f:
            json.dump(journal, f)
            f.flush()
            os.fsync(f.fileno())
        rmtree(self.staged)
        os.replace(str(tmp), str(self.staged))

    def apply_staged(self):
        """Swap the staged files in. Returns the journal with state "updated", or "staged" when a file is in use (Windows):
        the rest is swapped on the next run. Safe to run again after any interruption."""
        j = read_json(self.staged / "update.json")
        if not (isinstance(j, dict) and isinstance(j.get("files"), dict) and vtuple(j.get("to"))):
            rmtree(self.staged)
            return None
        busy = []
        for rel, sha in j["files"].items():
            dest = self.target(rel) if safe_rel(rel) else None
            if dest is None:
                continue
            if dest.is_file() and file_sha(dest) == sha:
                continue
            src = self.staged / rel
            if not src.is_file() or file_sha(src) != sha:
                rmtree(self.staged)
                raise FNError("The staged skill update is damaged; it was dropped.", code="skill_update_failed",
                              hint="Run: python faceninja.py update")
            mode = 0o755 if rel.endswith(".py") else 0o644
            if self.kind == "script" and dest.exists():
                mode = dest.stat().st_mode & 0o777
            try:
                os.chmod(str(src), mode)
            except OSError:
                pass
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(str(src), str(dest))
            except OSError:   # in use (Windows) or locked: try again on the next run
                busy.append(rel)
        if busy:
            return dict(j, state="staged", busy=busy)
        rmtree(self.staged)
        return dict(j, state="updated")


def fetch_release(base, timeout=15, retries=1):
    _, rel, _ = Api(base).call("GET", "/skill/version.json", auth=False, timeout=timeout, retries=retries)
    if not isinstance(rel, dict) or rel.get("name") != "faceninja" or not vtuple(rel.get("version")):
        raise FNError("{}/skill/version.json is not a FaceNinja skill release.".format(base), code="skill_update_refused")
    e = skill_state(base)
    e.update(latest=rel["version"], checked_at=time.time())
    if vtuple(rel.get("min_version")):
        e["min"] = rel["min_version"]
    STATE["dirty"], STATE["release"] = True, rel
    return rel


def fetch_skill_file(base, rel, meta, version):
    url = "{}/skill/{}?v={}".format(base, urllib.parse.quote(rel), urllib.parse.quote(version))
    req = urllib.request.Request(url, headers={"User-Agent": "faceninja-skill/{} (update)".format(__version__), "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            data = res.read(8 * 1024 * 1024 + 1)
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        raise FNError("Could not download the skill update ({}): {}".format(rel, getattr(e, "reason", e)), code="skill_update_failed",
                      hint="Nothing was changed; it is tried again later.")
    if hashlib.sha256(data).hexdigest() != meta["sha256"] or (meta.get("bytes") is not None and len(data) != meta.get("bytes")):
        raise FNError("The update's {} does not match its SHA-256 in version.json: refused, kept version {}.".format(rel, __version__),
                      code="skill_update_refused", hint="Nothing was changed. It is tried again later; if it keeps failing, write to support@faceninja.net.")
    return data


def changes_since(release, old):
    lines = ["{}: {}".format(h.get("version"), plain(h.get("changelog"), 300)) for h in (release.get("history") or [])
             if isinstance(h, dict) and newer(h.get("version"), old) and not newer(h.get("version"), release.get("version"))]
    return " | ".join(lines) or plain(release.get("changelog"), 300)


def install_update(base, inst, release):
    """Download and check every file, stage, back up, swap. Raises FNError and changes nothing when anything is wrong
    before the swap; returns None when another process is updating this copy right now."""
    files = inst.wanted(release)
    to = release["version"]
    blobs = {rel: fetch_skill_file(base, rel, meta, to) for rel, meta in files.items()}
    if not inst.take_lock():
        return None
    try:
        inst.leftovers()
        if inst.staged.exists():   # an earlier update still waiting for a file in use: finish it first
            inst.apply_staged()
        inst.backup(files)
        inst.stage(blobs, {"from": __version__, "to": to, "changes": changes_since(release, __version__),
                           "files": {rel: meta["sha256"] for rel, meta in files.items()}})
        return inst.apply_staged()
    finally:
        inst.drop_lock()


def announce(out, r, inst):
    where = " ({})".format(inst.skill_md) if inst.skill_md else ""
    if r["state"] == "updated":
        out.say("FaceNinja skill updated {} -> {}. Re-read SKILL.md{} before continuing. Changes: {}".format(
            r.get("from"), r.get("to"), where, r.get("changes") or "see SKILL.md"))
    else:
        out.say("FaceNinja skill update {} -> {} is downloaded and checked, but {} is in use; it is swapped in on the next run.".format(
            r.get("from"), r.get("to"), ", ".join(r.get("busy") or [])))
    out.extra["skill_update"] = {"from": r.get("from"), "to": r.get("to"), "state": r["state"], "changes": r.get("changes"),
                                 "skill_md": str(inst.skill_md) if inst.skill_md else None}


def update_now(base, out, inst, release=None):
    rel = release or STATE["release"] or fetch_release(base)
    if not newer(rel["version"], __version__):
        return None
    r = install_update(base, inst, rel)
    if r:
        announce(out, r, inst)
    return r


def continue_with_new_code(from_version):
    """Run the same command again with the updated script (a new process: the old code is not reused)."""
    save_skill_state()
    env = dict(os.environ)
    env[UPDATED_ENV] = from_version
    py = sys.executable or "python3"
    argv = [py, str(SCRIPT)] + list(ARGV)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        sys.exit(subprocess.call(argv, env=env))
    os.execve(py, argv, env)


def guidance(base, force=False):
    e = skill_state(base)
    g = e.get("guidance") if isinstance(e.get("guidance"), dict) else None
    changed = bool(g and e.get("guidance_rev_seen") and e.get("guidance_rev_seen") != g.get("rev"))
    if force or not g or changed or time.time() - e.get("guidance_at", 0) >= CHECK_EVERY_S:
        try:
            _, d, _ = Api(base).call("GET", "/v1/agent/guidance", auth=False, timeout=10 if force else 5, retries=1 if force else 0)
            if isinstance(d, dict):
                g = {"rev": d.get("rev"), "limits": d.get("limits") if isinstance(d.get("limits"), dict) else {},
                     "notices": [n for n in (d.get("notices") or []) if isinstance(n, dict) and n.get("id")][:20]}
                e["guidance"] = g
            e["guidance_at"] = time.time()
        except FNError:
            if force:
                raise
            e["guidance_at"] = time.time() - CHECK_EVERY_S + RETRY_OFFLINE_S
        STATE["dirty"] = True
    return g or {}


def _when(s):
    """Seconds since the epoch of "2026-11-02T03:00:00Z" (UTC), or None."""
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def active_notices(g, now=None):
    now = now or time.time()
    return [n for n in (g.get("notices") or []) if not (n.get("ends_at") and _when(n["ends_at"]) is not None and _when(n["ends_at"]) <= now)]


def notice_line(n):
    window = ""
    if n.get("starts_at") or n.get("ends_at"):
        window = " ({}{} UTC)".format(plain(n.get("starts_at") or "now", 20).replace("T", " ").rstrip("Z"),
                                      " to " + plain(n["ends_at"], 20).replace("T", " ").rstrip("Z") if n.get("ends_at") else "")
    return "  - [{}] {}{}".format(plain(n.get("kind") or "notice", 20), plain(n.get("text")), window)


def show_new_notices(base, out):
    """The server's notices this machine has not shown yet, once each."""
    e = skill_state(base)
    seen = e.get("seen") if isinstance(e.get("seen"), list) else []
    new = [n for n in active_notices(guidance(base)) if n.get("id") not in seen]
    if not new:
        return
    out.say("FaceNinja guidance from {} (follow it; each notice is shown once, `faceninja.py guidance` shows all):".format(base))
    for n in new:
        out.say(notice_line(n))
    e["seen"] = (seen + [n["id"] for n in new])[-200:]
    STATE["dirty"] = True
    out.extra["notices"] = [{k: n.get(k) for k in ("id", "kind", "text", "starts_at", "ends_at") if n.get(k)} for n in new]


def confirm_above(base):
    """Heads a batch makes without the user's confirmation: the server's guidance, else 5."""
    n = ((skill_state(base).get("guidance") or {}).get("limits") or {}).get("max_heads_without_asking")
    return n if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 50 else BATCH_CONFIRM_ABOVE


def stay_current(args, out):
    """Before every command: finish a staged update, update when the server has a newer skill (checked at most hourly),
    and show the server's new notices. Problems here never stop the command."""
    inst = Install()
    if os.environ.get(UPDATED_ENV):
        out.extra["skill_updated"] = {"from": os.environ[UPDATED_ENV], "to": __version__,
                                      "skill_md": str(inst.skill_md) if inst.skill_md else None, "note": "Re-read SKILL.md before continuing."}
    elif not inst.why_not() and trusted_source(args.base):
        try:
            waiting = False
            if (inst.staged / "update.json").is_file() and inst.take_lock():
                try:
                    r = inst.apply_staged()
                finally:
                    inst.drop_lock()
                if r:
                    announce(out, r, inst)
                    waiting = r["state"] != "updated"   # a file still in use: no new download until it is swapped
                    if r["state"] == "updated" and r.get("to") != __version__:
                        continue_with_new_code(__version__)
            e = skill_state(args.base)
            if time.time() - e.get("checked_at", 0) >= CHECK_EVERY_S:
                try:
                    fetch_release(args.base, timeout=5, retries=0)
                except FNError:
                    e["checked_at"] = time.time() - CHECK_EVERY_S + RETRY_OFFLINE_S
                    STATE["dirty"] = True
            latest = e.get("latest")
            failed = e.get("update_failed") if isinstance(e.get("update_failed"), dict) else {}
            if waiting or not newer(latest, __version__):
                pass
            elif autoupdate_off():
                out.say("FaceNinja skill {} is available (this is {}). Automatic updates are off (FACENINJA_NO_AUTOUPDATE); "
                        "update with: python faceninja.py update".format(latest, __version__))
                out.extra["skill_update_available"] = {"current": __version__, "latest": latest}
            elif latest == e.get("skip_version"):
                pass   # rolled back from this version on purpose; a later one is taken
            elif failed.get("version") == latest and time.time() - failed.get("at", 0) < RETRY_OFFLINE_S:
                pass   # it just failed (a download, a checksum): not again on every command
            else:
                try:
                    r = update_now(args.base, out, inst)
                except FNError:
                    e["update_failed"] = {"version": latest, "at": time.time()}
                    STATE["dirty"] = True
                    raise
                e.pop("update_failed", None)
                if r and r["state"] == "updated":
                    continue_with_new_code(__version__)
        except (FNError, OSError, ValueError) as x:
            out.say("Could not update the FaceNinja skill ({}); continuing with {}.".format(plain(getattr(x, "message", x), 200), __version__))
    if args.cmd != "guidance":
        show_new_notices(args.base, out)


def cmd_update(args, out):
    inst = Install()
    why = inst.why_not()
    if args.rollback:
        if why:
            raise FNError("Cannot roll back: {}.".format(why), code="invalid_request", exit_code=EXIT_USAGE)
        v = (read_json(inst.previous / "VERSION.json") or {}).get("version")
        if not vtuple(v):
            raise FNError("There is no earlier version to go back to.", code="no_previous_version", exit_code=EXIT_USAGE)
        files = sorted(str(p.relative_to(inst.previous)).replace(os.sep, "/") for p in inst.previous.rglob("*") if p.is_file() and p.name != "VERSION.json")
        blobs = {rel: (inst.previous / rel).read_bytes() for rel in files if safe_rel(rel)}
        if not inst.take_lock():
            raise FNError("Another update of this skill is running; try again in a minute.", code="update_in_progress", exit_code=EXIT_PENDING)
        try:
            inst.leftovers()
            inst.stage(blobs, {"from": __version__, "to": v, "changes": "rolled back", "files": {r: hashlib.sha256(b).hexdigest() for r, b in blobs.items()}})
            inst.backup(blobs)
            r = inst.apply_staged()
        finally:
            inst.drop_lock()
        e = skill_state(args.base)
        e["skip_version"] = __version__
        STATE["dirty"] = True
        return out.result({"ok": True, "from": __version__, "to": v, "state": r and r["state"], "skip_version": __version__},
                          "FaceNinja skill rolled back {} -> {}. Re-read SKILL.md. Automatic updates skip {} and take the next version.".format(__version__, v, __version__))
    if (inst.staged / "update.json").is_file() and not why and inst.take_lock():
        try:
            r = inst.apply_staged()
        finally:
            inst.drop_lock()
        if r:
            announce(out, r, inst)
    rel = fetch_release(args.base)
    info = {"ok": True, "current": __version__, "latest": rel["version"], "min_version": rel.get("min_version"),
            "update_available": newer(rel["version"], __version__)}
    if args.check or not info["update_available"]:
        return out.result(info, "FaceNinja skill {}; the latest is {}{}.".format(__version__, rel["version"],
                          " (run `faceninja.py update` to update)" if info["update_available"] else ": up to date"))
    if why or not trusted_source(args.base):
        raise FNError("Cannot update: {}.".format(why or "updates come only from an https:// server"), code="invalid_request", exit_code=EXIT_USAGE)
    r = update_now(args.base, out, inst, rel)
    if r is None:
        raise FNError("Another update of this skill is running; try again in a minute.", code="update_in_progress", exit_code=EXIT_PENDING)
    skill_state(args.base).pop("skip_version", None)
    out.result(dict(info, updated=r["state"] == "updated", state=r["state"], to=r["to"], changes=r.get("changes")),
               "Done: {}.".format("updated to " + r["to"] if r["state"] == "updated" else "staged, finishes on the next run"))


def cmd_guidance(args, out):
    g = guidance(args.base, force=True)
    notices = active_notices(g)
    e = skill_state(args.base)
    e["seen"] = ((e.get("seen") if isinstance(e.get("seen"), list) else []) + [n["id"] for n in notices if n["id"] not in (e.get("seen") or [])])[-200:]
    STATE["dirty"] = True
    limits = g.get("limits") or {}
    lines = ["FaceNinja guidance from {} (follow it):".format(args.base)] + [notice_line(n) for n in notices] + (
        ["Limits: " + ", ".join("{} {}".format(plain(k, 40), plain(",".join(v) if isinstance(v, list) else v, 60)) for k, v in limits.items())] if limits else [])
    out.result({"ok": True, "rev": g.get("rev"), "limits": limits, "notices": notices}, "\n".join(lines) if notices or limits else "No guidance right now.")


# ---- command line -----------------------------------------------------------------------------------------------------

def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="one JSON object on stdout, progress on stderr")
    common.add_argument("--base", default=argparse.SUPPRESS, help="API server (default $FACENINJA_API_BASE or {})".format(DEFAULT_BASE))

    ap = argparse.ArgumentParser(prog="faceninja.py", description="FaceNinja: photos or a description in, MetaHuman head out.", parents=[common])
    ap.add_argument("--version", action="version", version="faceninja.py " + __version__)
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("login", parents=[common], help="sign in by approving a code in the browser")
    s.add_argument("--no-wait", action="store_true", help="print the code and exit; run login again to wait for the approval")
    s.add_argument("--max-wait", type=int, default=0, help="stop waiting after this many seconds (exit 75)")
    s.add_argument("--no-browser", action="store_true", help="do not open the approval page")
    s.add_argument("--client-name", help="name of the API key this creates (default: agent and host)")
    s.add_argument("--force", action="store_true", help="sign in again even if a working key is saved")
    s.add_argument("--force-new", action="store_true", help="ask for a new code instead of resuming a pending one")
    s.set_defaults(fn=cmd_login)

    sub.add_parser("whoami", parents=[common], help="who is signed in").set_defaults(fn=cmd_whoami)
    sub.add_parser("balance", parents=[common], help="credits left and what a head costs").set_defaults(fn=cmd_balance)

    def head_opts(p):
        p.add_argument("--formats", default="glb,zip", help="what to download: glb,fbx,obj,stl,ply,zip (MetaHuman ZIP) or all (default glb,zip)")
        p.add_argument("--out", default="faceninja-out", help="folder for the files (default ./faceninja-out)")
        w = p.add_mutually_exclusive_group()
        w.add_argument("--wait", dest="wait", action="store_true", default=True, help="wait and download (default)")
        w.add_argument("--no-wait", dest="wait", action="store_false", help="create and return at once")
        p.add_argument("--max-wait", type=int, default=0, help="stop waiting after this many seconds (exit 75; run again to resume)")
        a = p.add_mutually_exclusive_group()
        a.add_argument("--arkit", dest="arkit", action="store_true", default=None, help="GLB/FBX with the 52 ARKit blendshapes (the default)")
        a.add_argument("--static", dest="arkit", action="store_false", help="GLB/FBX without blendshapes")
        p.add_argument("--dry-run", action="store_true", help="show the cost and balance, upload and spend nothing")

    s = sub.add_parser("make", parents=[common], help="make heads from photos or describe fictional adults")
    s.add_argument("--public-figure", action="store_true", help="declare that the uploaded photos depict a public figure; staff verification is required")
    s.add_argument("photos", nargs="*", metavar="PHOTO")
    s.add_argument("--describe", help="fictional adult appearance text (300 characters) or a structured JSON object")
    s.add_argument("--age", type=int, default=35, help="fictional age, 18-80 (default 35)")
    s.add_argument("--count", type=int, default=1, help="fictional crowd size, 1-20")
    s.add_argument("--diversity", help='JSON: age_range, sexes, descents, hair lists')
    s.add_argument("--fictional-confirmed", action="store_true", help="the user explicitly confirmed the fictional-person attestation")
    s.add_argument("--no-keep", action="store_true", help="opt out of retaining fictional build data for algorithm work")
    s.add_argument("--yes", action="store_true", help="the user confirmed the crowd cost")
    for key in ("sex", "descent", "skin_tone", "build", "facial_hair", "glasses", "hair_style", "hair_length", "hair_colour", "hair_dye"):
        s.add_argument("--" + key.replace("_", "-"))
    s.add_argument("--name", help="a name for the head (default: the first photo's file name)")
    s.add_argument("--new", action="store_true", help="make a new head even if these photos already made one in --out")
    head_opts(s)
    s.set_defaults(fn=cmd_make)

    s = sub.add_parser("portrait", parents=[common], help="review a fictional portrait, accept, redraw or cancel")
    s.add_argument("id")
    a = s.add_mutually_exclusive_group()
    a.add_argument("--accept", action="store_true", help="the user chose this portrait for the 3D build")
    a.add_argument("--redraw", action="store_true")
    a.add_argument("--cancel", action="store_true")
    s.add_argument("--draw", type=int, help="the draw number reviewed by the user")
    s.set_defaults(fn=cmd_portrait)

    s = sub.add_parser("status", parents=[common], help="where a head stands")
    s.add_argument("id")
    s.add_argument("--wait", action="store_true", help="wait until it is completed or failed")
    s.add_argument("--max-wait", type=int, default=0, help="stop waiting after this many seconds (exit 75)")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("download", parents=[common], help="download a finished head's files")
    s.add_argument("id")
    s.add_argument("--formats", default="glb,zip")
    s.add_argument("--out", default="faceninja-out")
    a = s.add_mutually_exclusive_group()
    a.add_argument("--arkit", dest="arkit", action="store_true", default=None)
    a.add_argument("--static", dest="arkit", action="store_false")
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("batch", parents=[common], help="one head per photo, or per subfolder with --per-person-subfolders")
    s.add_argument("folder")
    s.add_argument("--per-person-subfolders", action="store_true", help="each subfolder holds 1-15 photos of one person")
    s.add_argument("--yes", action="store_true", help="the user confirmed the cost (needed above {} heads)".format(BATCH_CONFIRM_ABOVE))
    head_opts(s)
    s.set_defaults(fn=cmd_batch)

    s = sub.add_parser("samples", parents=[common], help="the free sample heads: list them, or --download ID|all (no credits)")
    s.add_argument("--download", metavar="ID", help="a sample id or code, or all")
    s.add_argument("--formats", default="glb,fbx,zip")
    s.add_argument("--out", default="faceninja-samples")
    s.set_defaults(fn=cmd_samples)

    s = sub.add_parser("list", parents=[common], help="your heads, newest first")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--status")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("logout", parents=[common], help="revoke and forget the saved key")
    s.add_argument("--keep-key", action="store_true", help="forget the key here but leave it valid on the server")
    s.set_defaults(fn=cmd_logout)

    s = sub.add_parser("update", parents=[common], help="update the skill now (it also updates itself at most hourly)")
    u = s.add_mutually_exclusive_group()
    u.add_argument("--check", action="store_true", help="only say whether a newer version exists")
    u.add_argument("--rollback", action="store_true", help="go back to the version before the last update")
    s.set_defaults(fn=cmd_update)
    sub.add_parser("guidance", parents=[common], help="the server's current rules and notices for agents").set_defaults(fn=cmd_guidance)

    s = sub.add_parser("attest", parents=[common], help="the user confirms, once per computer, that they may use the photos (asked before the first head)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="only say whether it is confirmed (exit 3 if not)")
    g.add_argument("--revoke", action="store_true", help="forget the confirmation; the next head asks again")
    g.add_argument("--confirmed-by-user", action="store_true",
                   help="the user read the text and said yes just now (an agent passes this only after asking the user; never on its own)")
    s.set_defaults(fn=cmd_attest)

    global ARGV
    ARGV = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(argv)
    args.json = getattr(args, "json", False)
    args.base = norm_base(getattr(args, "base", None) or os.environ.get("FACENINJA_API_BASE"))
    out = Out(args.json)
    if not getattr(args, "fn", None):
        ap.print_help()
        return EXIT_USAGE
    try:
        if args.cmd != "update":
            stay_current(args, out)
        args.fn(args, out)
        return EXIT_OK
    except FNError as e:
        # the API refuses a skill this old: update, then run the command once more with the new code
        if (e.code == "skill_update_required" and not os.environ.get(UPDATED_ENV) and not autoupdate_off() and not Install().why_not()
                and trusted_source(args.base)):
            try:
                r = update_now(args.base, out, Install(), fetch_release(args.base))
                if r and r["state"] == "updated":
                    continue_with_new_code(__version__)
            except (FNError, OSError, ValueError) as x:
                out.say("Could not update the FaceNinja skill: {}".format(plain(getattr(x, "message", x), 200)))
        if args.json:
            err = {"code": e.code, "message": e.message}
            if e.hint:
                err["hint"] = e.hint
            if e.request_id:
                err["request_id"] = e.request_id
            if (e.data or {}).get("retry_after"):
                err["retry_after_s"] = e.data["retry_after"]
            extra = {k: v for k, v in (e.data or {}).items() if k != "retry_after"}
            extra.update(out.extra)
            print(json.dumps(dict({"ok": False, "error": err}, **extra), indent=2, default=str), flush=True)
        else:
            print("Error: {}".format(e.message), file=sys.stderr)
            if e.hint:
                print("  {}".format(e.hint), file=sys.stderr)
            if e.request_id:
                print("  (request id {})".format(e.request_id), file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\nStopped. A head that was created keeps building; check it with `faceninja.py list`.", file=sys.stderr)
        return 130
    except Exception as e:     # a bug or a full disk must reach an agent as one readable error, not a Python traceback
        msg = "{}: {}".format(type(e).__name__, e)
        hint = "Not a FaceNinja error. If a head was already created, run the same command again: it resumes. Otherwise report this to support@faceninja.net."
        if args.json:
            print(json.dumps(dict({"ok": False, "error": {"code": "unexpected_error", "message": msg, "hint": hint}}, **out.extra), indent=2, default=str), flush=True)
        else:
            print("Error: {}\n  {}".format(msg, hint), file=sys.stderr)
        return EXIT_ERROR
    finally:
        save_skill_state()


if __name__ == "__main__":
    sys.exit(main())
