#!/usr/bin/env python3
"""IRNoti mail server agent.

A small management API for the Postfix + Dovecot + Rspamd server that install.sh sets up. The IRNoti platform calls it over HTTPS with a
bearer token to create domains, mailboxes and aliases, and to read mailbox sizes. It keeps its state in flat files that Postfix and
Dovecot read, so there is no database to run. Standard library only (Python 3.8+).

Environment (set by the systemd unit):
  IRNOTI_MAIL_CONFIG   path of the JSON config (default /etc/irnoti-mail/agent.json)
  IRNOTI_MAIL_ROOT     prefix for every path, for tests (default empty = the real system)
  IRNOTI_MAIL_DRYRUN   "1" = never run system commands (tests)
"""
import hmac
import http.server
import json
import os
import re
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

VERSION = "1.0.0"
ROOT = os.environ.get("IRNOTI_MAIL_ROOT", "")
DRYRUN = os.environ.get("IRNOTI_MAIL_DRYRUN") == "1"
CONFIG_PATH = os.environ.get("IRNOTI_MAIL_CONFIG", ROOT + "/etc/irnoti-mail/agent.json")

POSTFIX_DIR = ROOT + "/etc/postfix/vmail"
DOVECOT_USERS = ROOT + "/etc/dovecot/users"
VMAIL_BASE = ROOT + "/var/vmail"
DKIM_DIR = ROOT + "/var/lib/rspamd/dkim"
SELECTOR = "irnoti"
RELAY_USER = "irnoti-relay"

DOMAIN_RE = re.compile(r"^(?=.{4,190}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}$")
LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def sh(args, input_text=None):
    """Runs a command without a shell. Passwords go through stdin, never the argument list (which any local user can read in ps)."""
    if DRYRUN:
        return ""
    res = subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise ApiError(500, "command failed: %s: %s" % (args[0], res.stderr.strip()[:200]))
    return res.stdout


def read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.strip()]
    except FileNotFoundError:
        return []


def write_atomic(path, lines, mode=0o640, group=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(l + "\n" for l in lines))
    os.chmod(tmp, mode)
    if group and not DRYRUN:
        try:
            shutil.chown(tmp, group=group)  # the Dovecot auth process reads this file as the "dovecot" user
        except (LookupError, PermissionError):
            pass
    # Dovecot reloads a passwd-file only when its mtime (whole seconds) or size changed. Two edits within one second that keep the size
    # (a password change, the second mailbox created right after the first) would otherwise be invisible to it until the next edit.
    try:
        prev = int(os.stat(path).st_mtime)
    except FileNotFoundError:
        prev = 0
    stamp = max(int(time.time()), prev + 1)
    os.utime(tmp, (stamp, stamp))
    os.replace(tmp, path)


def postmap(path):
    sh(["postmap", "hash:" + path.replace(ROOT, "", 1) if ROOT else "hash:" + path])


def check_domain(d):
    d = (d or "").strip().lower()
    if not DOMAIN_RE.match(d):
        raise ApiError(400, "invalid domain")
    return d


def check_address(a):
    a = urllib.parse.unquote(a or "").strip().lower()
    local, _, domain = a.rpartition("@")
    if not LOCAL_RE.match(local) or not DOMAIN_RE.match(domain):
        raise ApiError(400, "invalid address")
    return local, domain


def hash_password(pw):
    if not isinstance(pw, str) or len(pw) < 10 or len(pw) > 200 or "\n" in pw or "\r" in pw:
        raise ApiError(400, "password must be 10-200 characters")
    if DRYRUN:
        return "{SHA512-CRYPT}$6$dryrun$" + str(abs(hash(pw)))
    return "{SHA512-CRYPT}" + sh(["openssl", "passwd", "-6", "-stdin"], pw + "\n").strip()


class State:
    """domains / mailboxes / aliases, each a flat file; the Postfix hash maps and the Dovecot passwd-file are rebuilt from them on every change."""

    @staticmethod
    def domains():
        return [l.split()[0] for l in read_lines(POSTFIX_DIR + "/domains")]

    @staticmethod
    def users():
        out = {}
        for l in read_lines(DOVECOT_USERS):
            name, _, rest = l.partition(":")
            out[name] = rest
        return out

    @staticmethod
    def aliases():
        out = {}
        for l in read_lines(POSTFIX_DIR + "/aliases"):
            k, _, v = l.partition(" ")
            out[k] = [t for t in v.split(",") if t]
        return out

    @classmethod
    def save(cls, domains, users, aliases):
        write_atomic(POSTFIX_DIR + "/domains", ["%s OK" % d for d in sorted(domains)])
        mailboxes = sorted(u for u in users if "@" in u)
        write_atomic(POSTFIX_DIR + "/mailboxes", ["%s %s/%s/" % (u, u.split("@")[1], u.split("@")[0]) for u in mailboxes])
        write_atomic(POSTFIX_DIR + "/aliases", ["%s %s" % (k, ",".join(v)) for k, v in sorted(aliases.items())])
        # who may send as what: a mailbox as itself, the relay account as anything on a hosted domain
        senders = ["%s %s,%s" % (u, u, RELAY_USER) for u in mailboxes]
        senders += ["@%s %s" % (d, RELAY_USER) for d in sorted(domains)]
        for k, targets in sorted(aliases.items()):
            senders.append("%s %s" % (k, ",".join(sorted(set(targets + [RELAY_USER])))))
        write_atomic(POSTFIX_DIR + "/senders", senders)
        write_atomic(DOVECOT_USERS, ["%s:%s" % (u, rest) for u, rest in sorted(users.items())], 0o640, group="dovecot")
        for name in ("domains", "mailboxes", "aliases", "senders"):
            postmap(POSTFIX_DIR + "/" + name)
        # Postfix daemons only notice a rebuilt table on their next lookup and answer that one from the old data, so a mailbox created a
        # moment ago would be "unknown" for the first mail. A (graceful) reload starts fresh processes at once.
        # Dovecot's passwd-file lookups re-stat the file at most once a second, so a login right after a change could still see the old
        # users; reloading restarts its auth processes with a fresh read.
        for reload_cmd in (["postfix", "reload"], ["doveadm", "reload"]):
            try:
                sh(reload_cmd)
            except ApiError:
                pass  # the service is not running: it reads its tables when it starts


def user_line(hashed, quota_mb):
    return "%s::::::userdb_quota_rule=*:storage=%dM" % (hashed, quota_mb)


def quota_of(line):
    m = re.search(r"storage=(\d+)M", line or "")
    return int(m.group(1)) if m else 1024


def parse_hash(line):
    return line.split(":", 1)[0]


# ── operations ──
def add_domain(domain):
    domain = check_domain(domain)
    dkim = DKIM_DIR + "/%s.%s.key" % (domain, SELECTOR)
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if not os.path.exists(dkim):
        os.makedirs(DKIM_DIR, exist_ok=True)
        if DRYRUN:
            open(dkim, "w").write("dryrun-private-key\n")
        else:
            sh(["openssl", "genrsa", "-out", dkim, "2048"])
            try:
                shutil.chown(dkim, user="_rspamd", group="_rspamd")
            except (LookupError, PermissionError):
                pass
        os.chmod(dkim, 0o640)
    if domain not in domains:
        domains.append(domain)
        State.save(domains, users, aliases)
    return {"dkimSelector": SELECTOR, "dkimPublic": dkim_public(dkim)}


def dkim_public(path):
    if DRYRUN:
        import base64
        return base64.b64encode(("dryrun-public-" + os.path.basename(path)).encode()).decode()
    der = subprocess.run(["openssl", "rsa", "-in", path, "-pubout", "-outform", "DER"], capture_output=True, timeout=30)
    if der.returncode != 0:
        raise ApiError(500, "cannot read DKIM key")
    import base64
    return base64.b64encode(der.stdout).decode()


def remove_domain(domain):
    domain = check_domain(domain)
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if any(u.endswith("@" + domain) for u in users):
        raise ApiError(409, "domain still has mailboxes")
    domains = [d for d in domains if d != domain]
    aliases = {k: v for k, v in aliases.items() if not k.endswith("@" + domain)}
    State.save(domains, users, aliases)
    for suffix in (".key",):
        try:
            os.remove(DKIM_DIR + "/%s.%s%s" % (domain, SELECTOR, suffix))
        except FileNotFoundError:
            pass


def put_mailbox(address, body):
    local, domain = check_address(address)
    addr = "%s@%s" % (local, domain)
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if domain not in domains:
        raise ApiError(404, "domain is not hosted here")
    quota = int(body.get("quotaMb") or 1024)
    if quota < 50 or quota > 1_000_000:
        raise ApiError(400, "quotaMb must be 50-1000000")
    users[addr] = user_line(hash_password(body.get("password")), quota)
    State.save(domains, users, aliases)
    os.makedirs("%s/%s/%s" % (VMAIL_BASE, domain, local), mode=0o700, exist_ok=True)
    if not DRYRUN:
        try:
            shutil.chown("%s/%s/%s" % (VMAIL_BASE, domain, local), user="vmail", group="vmail")
        except (LookupError, PermissionError):
            pass


def patch_mailbox(address, body):
    local, domain = check_address(address)
    addr = "%s@%s" % (local, domain)
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if addr not in users:
        raise ApiError(404, "mailbox not found")
    cur = users[addr]
    hashed = hash_password(body["password"]) if body.get("password") else parse_hash(cur)
    quota = int(body["quotaMb"]) if body.get("quotaMb") else quota_of(cur)
    if quota < 50 or quota > 1_000_000:
        raise ApiError(400, "quotaMb must be 50-1000000")
    users[addr] = user_line(hashed, quota)
    State.save(domains, users, aliases)


def delete_mailbox(address):
    local, domain = check_address(address)
    addr = "%s@%s" % (local, domain)
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if addr not in users:
        raise ApiError(404, "mailbox not found")
    del users[addr]
    aliases = {k: [t for t in v if t != addr] for k, v in aliases.items()}
    aliases = {k: v for k, v in aliases.items() if v}
    State.save(domains, users, aliases)
    # the path is built from the validated parts only, so nothing outside /var/vmail/<domain>/<local> can be reached
    shutil.rmtree("%s/%s/%s" % (VMAIL_BASE, domain, local), ignore_errors=True)


def put_alias(address, body):
    key = urllib.parse.unquote(address).strip().lower()
    if key.startswith("@"):
        domain = check_domain(key[1:])
    else:
        local, domain = check_address(key)
        key = "%s@%s" % (local, domain)
    targets = body.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ApiError(400, "targets must be 1-20 addresses")
    clean = []
    for t in targets:
        l, d = check_address(t)
        clean.append("%s@%s" % (l, d))
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if domain not in domains:
        raise ApiError(404, "domain is not hosted here")
    if key in users:
        raise ApiError(409, "a mailbox with this address exists")
    aliases[key] = sorted(set(clean))
    State.save(domains, users, aliases)


def delete_alias(address):
    key = urllib.parse.unquote(address).strip().lower()
    domains, users, aliases = State.domains(), State.users(), State.aliases()
    if key in aliases:
        del aliases[key]
        State.save(domains, users, aliases)


def usage(addresses):
    out = {}
    for a in addresses[:500]:
        local, domain = check_address(a)
        addr = "%s@%s" % (local, domain)
        if DRYRUN:
            out[addr] = 0
            continue
        try:
            res = subprocess.run(["doveadm", "-f", "json", "quota", "get", "-u", addr], capture_output=True, text=True, timeout=20)
            rows = json.loads("[" + ",".join(l for l in res.stdout.splitlines() if l.strip().startswith("{")) + "]") if res.stdout.strip().startswith("{") else json.loads(res.stdout or "[]")
            storage = next((r for r in rows if r.get("type") == "STORAGE"), None)
            out[addr] = int(storage["value"]) * 1024 if storage else 0
        except Exception:  # an unreadable mailbox must not break the whole batch
            out[addr] = 0
    return out


def queue_summary():
    if DRYRUN:
        return {"queued": 0}
    res = subprocess.run(["postqueue", "-j"], capture_output=True, text=True, timeout=20)
    rows = [json.loads(l) for l in res.stdout.splitlines() if l.strip()]
    return {"queued": len(rows), "deferred": sum(1 for r in rows if r.get("queue_name") == "deferred")}


def health():
    out = {"ok": True, "version": VERSION, "domains": len(State.domains()), "mailboxes": len([u for u in State.users() if "@" in u])}
    if not DRYRUN:
        for svc in ("postfix", "dovecot", "rspamd"):
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            out[svc] = r.stdout.strip()
            out["ok"] = out["ok"] and r.stdout.strip() == "active"
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "irnoti-mail-agent"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def reply(self, status, body=None):
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        token = self.server.token.encode()
        given = (self.headers.get("Authorization") or "")
        return given.startswith("Bearer ") and hmac.compare_digest(given[7:].encode(), token)

    def handle_any(self, method):
        try:
            # the body is always consumed, even for a refused request: leftover bytes would be read as the next request on a kept-alive connection
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                self.close_connection = True
                return self.reply(413, {"error": "body too large"})
            raw = self.rfile.read(length) if length else b""
            if not self.authorized():
                return self.reply(401, {"error": "unauthorized"})
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return self.reply(400, {"error": "invalid JSON"})
            if not isinstance(body, dict):
                return self.reply(400, {"error": "body must be an object"})
            path = urllib.parse.urlsplit(self.path).path
            parts = [p for p in path.split("/") if p]
            with LOCK:
                result = self.route(method, parts, body)
            if result is None:
                return self.reply(204 if method == "DELETE" else 200, {})
            return self.reply(200, result)
        except ApiError as e:
            return self.reply(e.status, {"error": e.message})
        except Exception as e:  # never leak a traceback to the caller
            sys.stderr.write("error: %r\n" % (e,))
            return self.reply(500, {"error": "internal error"})

    def route(self, method, parts, body):
        if parts[:1] != ["v1"] or len(parts) < 2:
            raise ApiError(404, "not found")
        res = parts[1]
        arg = parts[2] if len(parts) > 2 else None
        if res == "health" and method == "GET":
            return health()
        if res == "queue" and method == "GET":
            return queue_summary()
        if res == "domains":
            if method == "POST" and arg is None:
                return add_domain(body.get("domain"))
            if method == "DELETE" and arg:
                return remove_domain(arg)
            if method == "GET" and arg:
                d = check_domain(arg)
                if d not in State.domains():
                    raise ApiError(404, "domain not found")
                return {"dkimSelector": SELECTOR, "dkimPublic": dkim_public(DKIM_DIR + "/%s.%s.key" % (d, SELECTOR))}
        if res == "mailboxes" and arg:
            if method == "PUT":
                return put_mailbox(arg, body)
            if method == "PATCH":
                return patch_mailbox(arg, body)
            if method == "DELETE":
                return delete_mailbox(arg)
        if res == "aliases" and arg:
            if method == "PUT":
                return put_alias(arg, body)
            if method == "DELETE":
                return delete_alias(arg)
        if res == "usage" and method == "POST":
            addresses = body.get("addresses")
            if not isinstance(addresses, list):
                raise ApiError(400, "addresses must be a list")
            return {"usage": usage(addresses)}
        raise ApiError(404, "not found")

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_PATCH(self): self.handle_any("PATCH")
    def do_DELETE(self): self.handle_any("DELETE")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    token = ""


def main():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    token = cfg.get("token", "")
    if len(token) < 24:
        sys.exit("config: token must be at least 24 characters")
    host, port = cfg.get("listen", "0.0.0.0"), int(cfg.get("port", 8443))
    srv = Server((host, port), Handler)
    srv.token = token
    if cfg.get("tlsCert") and cfg.get("tlsKey"):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(ROOT + cfg["tlsCert"], ROOT + cfg["tlsKey"])
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    sys.stderr.write("irnoti-mail-agent %s listening on %s:%d\n" % (VERSION, host, port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
