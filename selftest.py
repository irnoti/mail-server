#!/usr/bin/env python3
"""Self-test for a server set up by install.sh: creates a throw-away domain with two mailboxes through the management agent, pushes real
mail through Postfix/Dovecot/Rspamd on localhost and checks the results, then deletes everything it created.

    sudo ./selftest.py          # exit code 0 = every check passed
"""
import email
import imaplib
import json
import ssl
import smtplib
import sys
import time
import urllib.request

CONFIG = "/etc/irnoti-mail/agent.json"
SECRETS = "/etc/irnoti-mail/secrets.env"
DOMAIN = "selftest.example"
A, B = "alice@" + DOMAIN, "bob@" + DOMAIN
PASS_A, PASS_B = "SelfTest-alice-1234567", "SelfTest-bob-12345678"

cfg = json.load(open(CONFIG))
sec = dict(l.strip().split("=", 1) for l in open(SECRETS) if "=" in l)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))


def agent(method, path, body=None):
    req = urllib.request.Request(f"https://127.0.0.1:{cfg['port']}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def mime(frm, to, subject, body, extra=""):
    return f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: <{time.time_ns()}@selftest>\r\nDate: {email.utils.formatdate()}\r\n{extra}\r\n{body}\r\n"


def inbox(login, box, subject):
    im = imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx)
    im.login(login[0], login[1])
    try:
        for _ in range(15):  # delivery is asynchronous
            im.select(box)
            _, d = im.search(None, f'(HEADER Subject "{subject}")')
            if d[0].split():
                _, data = im.fetch(d[0].split()[-1], "(RFC822.HEADER)")
                return email.message_from_bytes(data[0][1])
            time.sleep(1)
    finally:
        im.logout()
    return None


print("IRNoti mail server self-test")
try:
    dom = agent("POST", "/v1/domains", {"domain": DOMAIN})
    check("agent: create domain + DKIM key", bool(dom.get("dkimPublic")))
    agent("PUT", f"/v1/mailboxes/{A}", {"password": PASS_A, "quotaMb": 100})
    agent("PUT", f"/v1/mailboxes/{B}", {"password": PASS_B, "quotaMb": 100})
    check("agent: create two mailboxes", True)
    master = lambda addr: (f"{addr}*{sec['MASTER_USER']}", sec["MASTER_PASSWORD"])

    with smtplib.SMTP("127.0.0.1", 25, timeout=30) as s:
        s.sendmail("someone@elsewhere.example", [A], mime("someone@elsewhere.example", A, "st inbound", "hello"))
        try:
            s.sendmail("x@elsewhere.example", ["nobody@" + DOMAIN], mime("x@elsewhere.example", "nobody@" + DOMAIN, "st unknown", "x"))
            check("SMTP 25: unknown recipient rejected", False)
        except smtplib.SMTPRecipientsRefused:
            check("SMTP 25: unknown recipient rejected", True)
    check("inbound mail delivered (Postfix -> LMTP -> Dovecot)", inbox(master(A), "INBOX", "st inbound") is not None)

    # Rspamd overwrites X-Spam-* headers, so the Sieve rule is tested by delivering locally; the real verdict path by the GTUBE test string
    try:
        with smtplib.LMTP("/var/spool/postfix/private/dovecot-lmtp") as lmtp:
            lmtp.sendmail("spam@elsewhere.example", [A], mime("spam@elsewhere.example", A, "st spam", "spam", "X-Spam-Status: Yes, score=20\r\n"))
        filed = inbox(master(A), "Junk", "st spam") is not None
    except Exception as e:
        filed = False
        print("   LMTP:", repr(e)[:120])
    check("Sieve: mail marked X-Spam-Status: Yes is filed in Junk", filed)
    with smtplib.SMTP("127.0.0.1", 25, timeout=30) as s:
        try:
            s.sendmail("spam@elsewhere.example", [A], mime("spam@elsewhere.example", A, "st gtube", "XJS*C4JDBQADN1.NSBN3*2IDNEN*GTUBE-STANDARD-ANTI-UBE-TEST-EMAIL*C.34X"))
            check("Rspamd: the GTUBE test spam is rejected at SMTP time", False)
        except smtplib.SMTPDataError as e:
            check("Rspamd: the GTUBE test spam is rejected at SMTP time", e.smtp_code >= 500, str(e.smtp_code))

    check("IMAP: master login reads a mailbox without its password", inbox(master(B), "INBOX", "nothing") is None)
    try:
        imaplib.IMAP4_SSL("127.0.0.1", 993, ssl_context=ctx).login(f"{A}*{sec['MASTER_USER']}", "wrong")
        check("IMAP: wrong master password rejected", False)
    except imaplib.IMAP4.error:
        check("IMAP: wrong master password rejected", True)

    with smtplib.SMTP("127.0.0.1", 587, timeout=30) as s:
        try:
            s.login(sec["RELAY_USER"], sec["RELAY_PASSWORD"])
            check("submission 587: login refused before STARTTLS", False)
        except smtplib.SMTPException:
            check("submission 587: login refused before STARTTLS", True)
        s.starttls(context=ctx)
        s.login(sec["RELAY_USER"], sec["RELAY_PASSWORD"])
        s.sendmail("billing@" + DOMAIN, [A], mime("billing@" + DOMAIN, A, "st relay", "sent by the platform"))
        try:
            s.sendmail("ceo@bank.example", [A], mime("ceo@bank.example", A, "st spoof", "x"))
            check("submission 587: foreign sender refused", False)
        except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused):
            check("submission 587: foreign sender refused", True)
    msg = inbox(master(A), "INBOX", "st relay")
    check("platform relay can send as any address of a hosted domain", msg is not None)
    check("outgoing mail is DKIM-signed for the customer's domain (Rspamd)", bool(msg and f"d={DOMAIN}" in (msg["DKIM-Signature"] or "")),
          "no DKIM-Signature: check 'systemctl status rspamd' and /var/lib/rspamd/dkim" if not (msg and msg["DKIM-Signature"]) else "")

    with smtplib.SMTP("127.0.0.1", 587, timeout=30) as s:
        s.starttls(context=ctx)
        s.login(B, PASS_B)
        try:
            s.sendmail(A, [A], mime(A, A, "st impersonate", "x"))
            check("a mailbox cannot send as another mailbox", False)
        except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused):
            check("a mailbox cannot send as another mailbox", True)
except Exception as e:  # report, but still clean up below
    check("self-test aborted", False, repr(e)[:200])
finally:
    for addr in (A, B):
        try:
            agent("DELETE", f"/v1/mailboxes/{addr}")
        except Exception:
            pass
    try:
        agent("DELETE", f"/v1/domains/{DOMAIN}")
    except Exception:
        pass

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if results and all(results) else 1)
