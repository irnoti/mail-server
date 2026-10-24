# IRNoti Mail Server — Setting up the mail server (Postfix + Dovecot + Rspamd) and connecting it to IRNoti

This guide walks you step by step through bringing up a **complete mail server** on Ubuntu that connects to the IRNoti panel and provides organizational email, SMTP, IMAP and POP3. For the domain logic of the service, see the main IRNoti repository: `docs/LOGIC.md` §109.

## 1. Architecture

```
 Customer (Outlook/mobile)     IRNoti panel and API                        Internet
 IMAP 993 / POP3 995 ─┐         ┌───────────────────────────────┐
 SMTP 587/465 ────────┤         │ NestJS API                    │
                      ▼         │  • EmailProvider "mailserver" │──HTTPS+token─┐
 ┌─────────────────────────────┐│  • IMAP sync (master user)    │──IMAPS 993───┤
 │ Mail server (Ubuntu)        ││  • Sending via SMTP 587       │──SMTP 587────┤
 │  Postfix  (SMTP 25/465/587) │└───────────────────────────────┘              │
 │  Dovecot  (IMAP/POP3/LMTP,  │◄──────────────────────────────────────────────┘
 │            quota, Sieve)    │      Inbound mail ←── customer domain MX ←── other servers
 │  Rspamd   (spam + DKIM)     │      Outbound mail ──► (DKIM signing) ──► destination servers
 │  irnoti-mail-agent (HTTPS)  │
 └─────────────────────────────┘
```

- **Management agent** (`irnoti-mail-agent.py`, Python standard library only): writes domains, mailboxes, aliases and quotas into the Postfix/Dovecot files. No separate database is needed, and if the agent breaks, mail delivery is unaffected (Postfix and Dovecot work independently).
- **Mailbox passwords** exist on the server only as `SHA512-CRYPT` hashes; the panel never knows them. The panel reads mail with the **Dovecot master user** (`user*master`) and sends through port 587 with the **relay user**; Postfix allows only this account (and the mailboxes themselves) to send on behalf of the hosted addresses (`smtpd_sender_login_maps`).
- **DKIM**: one 2048-bit key per domain in `/var/lib/rspamd/dkim/<domain>.irnoti.key`; Rspamd signs every outbound message. The public key is shown in the customer's panel.
- **Spam**: Rspamd scores every inbound message and adds an `X-Spam-Status: Yes/No` header; Sieve moves "Yes" messages to `Junk`. The panel shows the same folder as "Spam" and adds its own filter as well.

## 2. Prerequisites

| Item | Notes |
|---|---|
| Server | Ubuntu **22.04 or 24.04**, at least 2 GB RAM and 20 GB disk (+ space for mailboxes), a static public IPv4 |
| Outbound port 25 | Many providers (especially large clouds and some Iranian ones) block it. Ask or open a ticket before buying. Without it, mail cannot reach other servers |
| A record | `mail.example.com → server IP` |
| PTR (reverse DNS) | The server IP must resolve back to `mail.example.com`; set it in the provider's panel. Without it, Gmail/Yahoo and most servers reject the mail or mark it as spam |
| Inbound ports | 25, 80 (certificate issuance and renewal), 465, 587, 993, 995 and the agent port (default 8443, only for the API server's IP) |
| A domain you control | The server name (`mail.example.com`) comes from your own domain; customers' domains are separate and are added in the panel |

> **Iran:** To deliver to Gmail and other foreign services, the server needs an IP with a good reputation and an open port 25; this usually means a server outside the country. If you only need internal email, a domestic server is enough. For transactional sending without a dedicated server, an SMTP/API provider (section 8) is simpler.

## 3. Installation (one command)

```bash
git clone https://github.com/irnoti/mail-server.git
cd mail-server
sudo ./install.sh --hostname mail.example.com --admin-email you@example.com --api-ip <API server IP>
```

Options: `--agent-port` (default 8443), `--self-signed` (no Let's Encrypt; testing only), `--skip-checks` (continue despite DNS/port 25 warnings), `--uninstall-agent`.

The script performs, in order: 1) checks (OS, A and PTR records, port 25), 2) package installation, 3) a Let's Encrypt certificate with automatic renewal, 4) the `vmail` user and state files, 5) Dovecot configuration (IMAPS, POP3S, LMTP, quota, Sieve), 6) Postfix (virtual domains, `submission` 587 and `smtps` 465 with SASL and sender control, milter), 7) Rspamd (milter, DKIM, Redis), 8) the management agent and its systemd service, 9) UFW and fail2ban, 10) startup and port checks. Re-running it is safe (passwords are preserved). Log: `/var/log/irnoti-mail-install.log`.

At the end a **self-test** runs (`selftest.py`: it creates a temporary domain with two mailboxes, passes real messages through Postfix/Dovecot/Rspamd and removes everything afterwards). It checks: inbound delivery and rejection of a nonexistent recipient, spam filtering (Sieve and GTUBE rejection), IMAP login with the master user, sending on port 587 and rejection of logins without TLS, rejection of a foreign sender and of impersonating another mailbox, and **DKIM signing**. You can run it again at any time: `sudo ./selftest.py`.

When it finishes, the values needed for the provider form are printed; they are also stored in `/etc/irnoti-mail/secrets.env` (mode 0600).

## 4. Connecting to IRNoti

"Admin → Providers → Add": service **Email**, type **Dedicated mail server (Postfix + Dovecot on Ubuntu)**:

| Field | Value |
|---|---|
| agentUrl | `https://mail.example.com:8443` |
| agentToken | the printed token |
| hostname | `mail.example.com` (the domains' MX and SPF point to this) |
| masterUser / masterPassword | the Dovecot master user (used by the panel to read mailboxes) |
| relayUser / relayPassword | the relay user (SMTP 587) |
| imapPort / pop3Port / smtpPort | empty = 993 / 995 / 587 |

Priority and "public/restricted" work as with the other providers. You can create several servers; a new domain is assigned to the provider with the lowest priority value (a customer can also pass `providerId`).

> Passwords are stored in the database with AES-256 and read only when connecting. There is no separate "test connection"; adding a test domain (section 5) verifies the connection to the agent.

## 5. First domain and mailbox (test)

1. With a test user (KYC approved, in a group with the `email.hosted` capability, wallet funded), buy an **email package** (Admin → Email → Packages and pricing: create a package first).
2. In "Email → Domains", add a test domain. Put the records table in the domain's DNS:

| Record | Sample value |
|---|---|
| `TXT @` | `irnoti-verify=<token>` (proof of ownership) |
| `MX @` (10) | `mail.example.com` |
| `TXT @` | `v=spf1 a:mail.example.com ~all` |
| `TXT irnoti._domainkey` | `v=DKIM1; k=rsa; p=…` |
| `TXT _dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@<domain>; adkim=s; aspf=s` |

3. Click "Check DNS records" until the status becomes "Verified". 4. "Mailboxes" → create a mailbox (the automatic password is shown only once). 5. Send a message from the panel to your own Gmail and reply; in Gmail "Show original" should show `SPF: PASS`, `DKIM: PASS` and `DMARC: PASS`.

Mail client settings: `IMAP mail.example.com:993 (SSL)` or `POP3 :995 (SSL)`, `SMTP :587 (STARTTLS)` or `:465 (SSL)`, username = the full address.

## 6. After installation (operations)

**Health:** `sudo ./selftest.py`, `systemctl status postfix dovecot rspamd irnoti-mail-agent`, `postqueue -p` (queue), `journalctl -u postfix -f`, `doveadm who`, `rspamadm configtest`. The Rspamd web UI is local only: `ssh -L 11334:127.0.0.1:11334 root@mail.example.com`, then `http://127.0.0.1:11334` (password in `secrets.env`).

**IP reputation and deliverability:** A new IP has no reputation. For the first two weeks, start at a few hundred messages a day and increase slowly. Get a score from [mail-tester.com](https://www.mail-tester.com) (≥ 9), check the IP at [mxtoolbox.com/blacklists](https://mxtoolbox.com/blacklists.aspx), and enable [Google Postmaster Tools](https://postmaster.google.com) for the domain. Once SPF/DKIM are confirmed to work, move DMARC from `p=none` to `quarantine`. The system itself adds permanent bounces and complaints to the suppression list and suspends accounts with a high bounce rate.

**Backups** (daily, off the server itself): `/var/vmail` (mail), `/etc/dovecot/users` and `/etc/dovecot/master-users`, `/etc/postfix/vmail`, `/var/lib/rspamd/dkim` (DKIM keys; if lost, the DNS record of every domain must be changed), `/etc/irnoti-mail`, `/etc/letsencrypt`. Example: `rsync -aH --delete /var/vmail /etc/dovecot /etc/postfix/vmail /var/lib/rspamd/dkim /etc/irnoti-mail backup@host:/backups/mail/`. Restore: put the same paths back, run `postmap` on the four files `/etc/postfix/vmail/*`, then `systemctl restart postfix dovecot rspamd irnoti-mail-agent`.

**Maintenance:** run `apt update && apt upgrade` monthly. To update the agent, copy the new `irnoti-mail-agent.py` and run `systemctl restart irnoti-mail-agent`. The certificate renews automatically (check with `certbot renew --dry-run`).

**Scale and resilience:** One server handles from tens up to a few thousand mailboxes (the main limits are disk and IP reputation). High availability would need a second MX and shared storage, which are not ready in this version; fast backup and restore is the practical substitute.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Installation fails at "certbot" | The A record is not propagated yet, or port 80 is closed | `dig +short mail.example.com`, the provider's firewall; or use `--self-signed` |
| Mail to Gmail doesn't arrive / `Connection timed out` | Outbound port 25 is blocked | Ask the provider to open it: `nc -vz gmail-smtp-in.l.google.com 25` |
| Mail arrives but lands in spam | Wrong PTR or SPF/DKIM/DMARC; new IP | "Show original" in Gmail; PTR; the "IP reputation" section |
| The panel reports "mailbox login failed" | Wrong master user or password in the provider form | `doveadm auth test 'user@d*irnoti-master' <password>` on the server |
| `Relay access denied` when sending from the panel | Wrong relayUser/Password, or the sender is not in `senders` | `postmap -q 'user@d' hash:/etc/postfix/vmail/senders`; the `postfix/submission` log |
| "sender not owned by user" | The From address doesn't match the SMTP login | In Outlook log in with the mailbox's own address; or assign the alias to the mailbox |
| `554 5.7.1 Recipient address rejected` | The domain is not in `vmail/domains`, or the MX hasn't changed yet | `postmap -q example.ir hash:/etc/postfix/vmail/domains`; wait for DNS |
| "Mailbox is full" (452) | The mailbox quota is full | Increase the quota in the panel (`PATCH /email/mailboxes/:id`) or delete mail |
| The agent returns 401 | The provider form's token doesn't match `agent.json` | Re-enter the token from `/etc/irnoti-mail/secrets.env` |
| The agent is unreachable from the panel | UFW accepts only `--api-ip` and the API's IP changed | `ufw allow from <IP> to any port 8443` |

## 8. Alternatives without a dedicated server

- **SMTP relay** (Amazon SES SMTP, Brevo, etc.): the "Send via SMTP" provider with `host/port/username/password` and an `spf` field (e.g. `include:amazonses.com`). For transactional sending from "send-only" domains.
- **API**: Mailgun (`apiKey`, `region`, `signingKey`), SendGrid (`apiKey`), Postmark (`serverToken`). The domain is created with the provider, and the provider's SPF/DKIM records are shown in the panel. For delivery reports, point the provider's webhook at `/api/v1/hooks/email/<id>/<webhookSecret>`.
- Organizational mailboxes (receiving + IMAP/POP3) are possible only with a "dedicated mail server"; these providers are "send-only" (no `createMailbox` → a `501 not_supported` error when creating a mailbox).

## 9. Management agent reference

HTTPS on `agentPort` with `Authorization: Bearer <agentToken>`; JSON body up to 64 KB; every input is validated before use (domain names/addresses with strict regexes, disk paths built only from validated parts, commands run without a shell, passwords passed only via stdin).

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | Service status and the number of domains/mailboxes |
| `POST /v1/domains {domain}` | Create a domain and DKIM key ← `{dkimSelector, dkimPublic}` |
| `GET /v1/domains/:d` | The DKIM public key |
| `DELETE /v1/domains/:d` | Delete (409 if it has mailboxes) |
| `PUT /v1/mailboxes/:addr {password, quotaMb}` | Create or overwrite a mailbox |
| `PATCH /v1/mailboxes/:addr {password?, quotaMb?}` | Change password/quota |
| `DELETE /v1/mailboxes/:addr` | Delete a mailbox and its mail |
| `PUT /v1/aliases/:addr {targets[]}` | Alias; `@domain` = catch-all |
| `DELETE /v1/aliases/:addr` | Delete |
| `POST /v1/usage {addresses[]}` | Disk usage (bytes) of each mailbox |
| `GET /v1/queue` | Number of messages in the Postfix queue |

Files: `/etc/postfix/vmail/{domains,mailboxes,aliases,senders}` (+ `.db`), `/etc/dovecot/users` (`user:{SHA512-CRYPT}hash::::::userdb_quota_rule=*:storage=1024M`), `/etc/dovecot/master-users`, `/var/vmail/<domain>/<user>/Maildir`, `/var/lib/rspamd/dkim/<domain>.irnoti.key`, `/etc/irnoti-mail/{agent.json,secrets.env}`.

## 10. Security

- TLS ≥ 1.2 only; plaintext login (without TLS) is disabled (`disable_plaintext_auth`); ports 110 and 143 are not open.
- The agent port is opened in UFW only for the API server's IP, and additionally relies on a 48-character token (constant-time comparison) and TLS.
- fail2ban for SSH, Postfix and Dovecot; a limit of 40 concurrent IMAP connections per user/IP.
- Mailbox passwords are never stored or logged by the panel; resetting a password from the panel shows the new one only once.
- Root access to the server means access to all mail. Keep SSH key-only, with no password login, and limited to admins.

## 11. Webmail on the organization's own address (mail.company.com)

Each organization can register an address such as `mail.company.com` in the panel (Email → Domains → "Organization's dedicated webmail"), create a **CNAME** in its own DNS to the target the panel shows (`EMAIL_PORTAL_TARGET`; by default the host of `PUBLIC_WEB_URL`), and click "Check DNS". From then on, `https://mail.company.com` is the organization's branded login page.

For HTTPS on hosts you don't know in advance, the proxy in front of the web app must obtain the certificate on the first request. With Caddy, and only for verified addresses:

```caddyfile
{
  on_demand_tls {
    ask http://127.0.0.1:4000/api/v1/portal/host
  }
}
https:// {
  tls { on_demand }
  reverse_proxy 127.0.0.1:3000
}
```

Caddy passes the name to `ask` as `?domain=` (200 = known, 404 = refuse); proxies that expect the name in the path should use `/api/v1/portal/host/<address>`. An unverified address never gets a certificate. With Nginx/Traefik or Cloudflare it is enough that all verified addresses reach the same web app; the web app itself sends an unknown host back to the main site.
