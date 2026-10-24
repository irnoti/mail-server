#!/usr/bin/env bash
# IRNoti mail server installer for Ubuntu 22.04 / 24.04.
#
# Installs and configures Postfix (SMTP in/out, port 25/465/587), Dovecot (IMAP 993, POP3 995, LMTP, quotas, Sieve), Rspamd (spam filter +
# DKIM signing), Let's Encrypt TLS, a firewall, fail2ban and the IRNoti management agent. When it ends it prints the values to paste into
# the "mailserver" provider form in the IRNoti admin panel.
#
#   sudo ./install.sh --hostname mail.example.com --admin-email you@example.com [--api-ip 203.0.113.10]
#
# Options:
#   --hostname H        FQDN of this server (must have an A record pointing at it and, for good deliverability, a matching PTR/rDNS)
#   --admin-email E     contact address for Let's Encrypt expiry notices and the postmaster alias
#   --api-ip IP         the IRNoti API server's address; the agent port is opened to this address only (recommended)
#   --agent-port N      port of the management agent (default 8443)
#   --self-signed       do not use Let's Encrypt (port 80 not reachable / testing); clients will see a certificate warning
#   --skip-checks       continue even if DNS/port checks fail
#   --uninstall-agent   remove only the agent service (mail data is kept)
#   -h, --help          this text
set -Eeuo pipefail
# IRNOTI_NO_SERVICES=1 (tests only): write every configuration but do not install packages, touch the firewall or start services.
NO_SERVICES="${IRNOTI_NO_SERVICES:-}"

HOSTNAME_FQDN=""; ADMIN_EMAIL=""; API_IP=""; AGENT_PORT=8443; SELF_SIGNED=0; SKIP_CHECKS=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR=/etc/irnoti-mail
LOG=/var/log/irnoti-mail-install.log

c_red=$'\e[31m'; c_grn=$'\e[32m'; c_ylw=$'\e[33m'; c_off=$'\e[0m'
say()  { printf '%s==>%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%sWARN:%s %s\n' "$c_ylw" "$c_off" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
trap 'die "failed at line $LINENO (see $LOG)"' ERR

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hostname) HOSTNAME_FQDN="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --api-ip) API_IP="${2:-}"; shift 2 ;;
    --agent-port) AGENT_PORT="${2:-}"; shift 2 ;;
    --self-signed) SELF_SIGNED=1; shift ;;
    --skip-checks) SKIP_CHECKS=1; shift ;;
    --uninstall-agent)
      systemctl disable --now irnoti-mail-agent 2>/dev/null || true
      rm -f /etc/systemd/system/irnoti-mail-agent.service /usr/local/lib/irnoti-mail/irnoti-mail-agent.py
      systemctl daemon-reload; echo "agent removed"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (use --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -n "$HOSTNAME_FQDN" && -n "$ADMIN_EMAIL" ]] || { usage; die "--hostname and --admin-email are required"; }
[[ "$HOSTNAME_FQDN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || die "--hostname must be a lower-case FQDN like mail.example.com"
[[ "$ADMIN_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[a-z]{2,}$ ]] || die "--admin-email is not a valid address"
[[ "$AGENT_PORT" =~ ^[0-9]{2,5}$ ]] || die "--agent-port must be a number"
[[ -z "$API_IP" || "$API_IP" =~ ^[0-9a-fA-F.:/]+$ ]] || die "--api-ip must be an IP address or CIDR"
exec > >(tee -a "$LOG") 2>&1

. /etc/os-release
case "${VERSION_ID:-}" in 22.04|24.04) ;; *) die "Ubuntu 22.04 or 24.04 is required (found ${PRETTY_NAME:-unknown})" ;; esac
[[ -f "$SCRIPT_DIR/irnoti-mail-agent.py" ]] || die "irnoti-mail-agent.py must be next to install.sh"

# ── pre-flight checks ─────────────────────────────────────────────────────────
say "Checking the server"
PUBLIC_IP="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
DNS_IP="$(getent ahostsv4 "$HOSTNAME_FQDN" | awk 'NR==1{print $1}' || true)"
problems=0
if [[ -z "$PUBLIC_IP" ]]; then warn "could not detect this server's public IP"; problems=1
elif [[ "$DNS_IP" != "$PUBLIC_IP" ]]; then warn "$HOSTNAME_FQDN resolves to '${DNS_IP:-nothing}' but this server is $PUBLIC_IP: create the A record first"; problems=1; fi
if ! timeout 6 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
  warn "outbound port 25 looks blocked by your provider: mail to other servers cannot be delivered until you ask them to open it"; problems=1; fi
if [[ -n "$PUBLIC_IP" ]]; then
  PTR="$(getent hosts "$PUBLIC_IP" | awk '{print $2}' | head -1 || true)"
  [[ "$PTR" == "$HOSTNAME_FQDN" ]] || { warn "reverse DNS (PTR) of $PUBLIC_IP is '${PTR:-unset}', it should be $HOSTNAME_FQDN (set it in your provider's panel; many servers reject mail without it)"; }
fi
if [[ $problems -ne 0 && $SKIP_CHECKS -ne 1 ]]; then die "fix the problems above or re-run with --skip-checks"; fi

# ── packages ──────────────────────────────────────────────────────────────────
say "Installing packages (this takes a few minutes)"
if [[ -z $NO_SERVICES ]]; then
  export DEBIAN_FRONTEND=noninteractive
  echo "postfix postfix/mailname string $HOSTNAME_FQDN" | debconf-set-selections
  echo "postfix postfix/main_mailer_type string Internet Site" | debconf-set-selections
  apt-get update -qq
  apt-get install -y -qq postfix dovecot-core dovecot-imapd dovecot-pop3d dovecot-lmtpd dovecot-sieve dovecot-managesieved \
    rspamd redis-server certbot ufw fail2ban openssl curl ca-certificates python3 >/dev/null
  hostnamectl set-hostname "$HOSTNAME_FQDN" || true
fi

# ── TLS certificate ───────────────────────────────────────────────────────────
CERT_DIR=/etc/letsencrypt/live/$HOSTNAME_FQDN
if [[ $SELF_SIGNED -eq 0 ]]; then
  say "Requesting a Let's Encrypt certificate (port 80 must be reachable)"
  systemctl stop nginx apache2 2>/dev/null || true
  if ! certbot certonly --standalone -d "$HOSTNAME_FQDN" -m "$ADMIN_EMAIL" --agree-tos --non-interactive; then
    die "certificate request failed. Check the A record and that port 80 is open, or use --self-signed"
  fi
  mkdir -p /etc/letsencrypt/renewal-hooks/deploy
  cat >/etc/letsencrypt/renewal-hooks/deploy/irnoti-mail <<'EOF'
#!/bin/sh
systemctl reload postfix dovecot 2>/dev/null || true
systemctl restart irnoti-mail-agent 2>/dev/null || true
EOF
  chmod +x /etc/letsencrypt/renewal-hooks/deploy/irnoti-mail
else
  warn "using a self-signed certificate: mail clients will warn, and other servers may refuse TLS"
  CERT_DIR=/etc/irnoti-mail/tls; mkdir -p "$CERT_DIR"
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" -subj "/CN=$HOSTNAME_FQDN" >/dev/null 2>&1
  chmod 640 "$CERT_DIR/privkey.pem"
fi
TLS_CERT="$CERT_DIR/fullchain.pem"; TLS_KEY="$CERT_DIR/privkey.pem"

# ── secrets (generated once, kept across re-runs) ─────────────────────────────
mkdir -p "$STATE_DIR"; chmod 750 "$STATE_DIR"
SECRETS="$STATE_DIR/secrets.env"
if [[ ! -f $SECRETS ]]; then
  umask 077
  {
    echo "AGENT_TOKEN=$(openssl rand -hex 24)"
    echo "MASTER_USER=irnoti-master"
    echo "MASTER_PASSWORD=$(openssl rand -hex 18)"
    echo "RELAY_USER=irnoti-relay"
    echo "RELAY_PASSWORD=$(openssl rand -hex 18)"
    echo "RSPAMD_PASSWORD=$(openssl rand -hex 12)"
  } >"$SECRETS"
  umask 022
fi
# shellcheck disable=SC1090
. "$SECRETS"
hash_pw() { printf '%s\n' "$1" | openssl passwd -6 -stdin; }

# ── virtual mail user and state files ─────────────────────────────────────────
say "Preparing mail storage"
getent group vmail >/dev/null || groupadd -g 5000 vmail
id vmail >/dev/null 2>&1 || useradd -g vmail -u 5000 -d /var/vmail -s /usr/sbin/nologin vmail
mkdir -p /var/vmail /etc/postfix/vmail /etc/dovecot/sieve /var/lib/rspamd/dkim
chown -R vmail:vmail /var/vmail; chmod 770 /var/vmail
for f in domains mailboxes aliases senders; do [[ -f /etc/postfix/vmail/$f ]] || : >/etc/postfix/vmail/$f; postmap hash:/etc/postfix/vmail/$f; done
# the relay account may send as any hosted domain; the master account reads mailboxes for the panel
if [[ ! -f /etc/dovecot/users ]]; then : >/etc/dovecot/users; fi
grep -q "^$RELAY_USER:" /etc/dovecot/users || echo "$RELAY_USER:{SHA512-CRYPT}$(hash_pw "$RELAY_PASSWORD")::::::" >>/etc/dovecot/users
echo "$MASTER_USER:{SHA512-CRYPT}$(hash_pw "$MASTER_PASSWORD")" >/etc/dovecot/master-users
chown root:dovecot /etc/dovecot/users /etc/dovecot/master-users; chmod 640 /etc/dovecot/users /etc/dovecot/master-users
chown _rspamd:_rspamd /var/lib/rspamd/dkim 2>/dev/null || true

# ── Dovecot ───────────────────────────────────────────────────────────────────
say "Configuring Dovecot (IMAP, POP3, LMTP, quota, Sieve)"
[[ -f /etc/dovecot/dovecot.conf.orig ]] || cp /etc/dovecot/dovecot.conf /etc/dovecot/dovecot.conf.orig
cat >/etc/dovecot/sieve/before.sieve <<'EOF'
require ["fileinto"];
# mail Rspamd marked as spam goes to Junk
if header :contains "X-Spam-Status" "Yes" { fileinto "Junk"; stop; }
EOF
cat >/etc/dovecot/dovecot.conf <<EOF
# Managed by IRNoti install.sh: put your own additions in /etc/dovecot/local.conf.
protocols = imap pop3 lmtp sieve
listen = *
ssl = required
ssl_cert = <$TLS_CERT
ssl_key = <$TLS_KEY
ssl_min_protocol = TLSv1.2
disable_plaintext_auth = yes
auth_mechanisms = plain login
auth_master_user_separator = *
mail_home = /var/vmail/%d/%n
mail_location = maildir:~/Maildir
mail_uid = vmail
mail_gid = vmail
first_valid_uid = 5000
last_valid_uid = 5000
mail_plugins = \$mail_plugins quota
log_path = syslog

service imap-login {
  inet_listener imap {
    port = 0
  }
  inet_listener imaps {
    port = 993
    ssl = yes
  }
}

service pop3-login {
  inet_listener pop3 {
    port = 0
  }
  inet_listener pop3s {
    port = 995
    ssl = yes
  }
}

service lmtp {
  unix_listener /var/spool/postfix/private/dovecot-lmtp {
    mode = 0600
    user = postfix
    group = postfix
  }
}

service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0660
    user = postfix
    group = postfix
  }
}

service managesieve-login {
  inet_listener sieve {
    port = 4190
  }
}

passdb {
  driver = passwd-file
  args = /etc/dovecot/master-users
  master = yes
  pass = yes
}

passdb {
  driver = passwd-file
  args = username_format=%u /etc/dovecot/users
}

userdb {
  driver = passwd-file
  args = username_format=%u /etc/dovecot/users
  default_fields = uid=vmail gid=vmail home=/var/vmail/%d/%n
}

namespace inbox {
  inbox = yes
  mailbox Drafts {
    special_use = \\Drafts
    auto = subscribe
  }
  mailbox Junk {
    special_use = \\Junk
    auto = subscribe
  }
  mailbox Sent {
    special_use = \\Sent
    auto = subscribe
  }
  mailbox Trash {
    special_use = \\Trash
    auto = subscribe
  }
  mailbox Archive {
    special_use = \\Archive
    auto = subscribe
  }
}

protocol lmtp {
  mail_plugins = \$mail_plugins sieve
}

protocol imap {
  mail_plugins = \$mail_plugins imap_quota
  mail_max_userip_connections = 40
}

protocol pop3 {
  mail_plugins = \$mail_plugins
  pop3_uidl_format = %08Xu%08Xv
}

plugin {
  quota = maildir:User quota
  quota_rule = *:storage=1G
  quota_grace = 10%%
  quota_status_success = DUNNO
  quota_status_nouser = DUNNO
  quota_status_overquota = "452 4.2.2 Mailbox is full"
  sieve_before = /etc/dovecot/sieve/before.sieve
  sieve = file:~/sieve;active=~/.dovecot.sieve
}

!include_try /etc/dovecot/local.conf
EOF
touch /etc/dovecot/local.conf
# compiled only now: sievec reads dovecot.conf, which must already be the new, valid one (a re-run over a broken config would fail otherwise)
sievec /etc/dovecot/sieve/before.sieve
doveconf -n >/dev/null || die "Dovecot configuration is invalid"

# ── Postfix ───────────────────────────────────────────────────────────────────
say "Configuring Postfix"
cp -n /etc/postfix/main.cf /etc/postfix/main.cf.orig || true
cp -n /etc/postfix/master.cf /etc/postfix/master.cf.orig || true
postconf -e "myhostname = $HOSTNAME_FQDN" "myorigin = $HOSTNAME_FQDN" "mydestination = localhost" "mynetworks = 127.0.0.0/8 [::1]/128" "inet_interfaces = all" "inet_protocols = ipv4" \
  "smtpd_banner = \$myhostname ESMTP" "biff = no" "compatibility_level = 3.6" "message_size_limit = 52428800" "mailbox_size_limit = 0" \
  "smtpd_tls_cert_file = $TLS_CERT" "smtpd_tls_key_file = $TLS_KEY" "smtpd_tls_security_level = may" "smtp_tls_security_level = may" \
  "smtpd_tls_protocols = >=TLSv1.2" "smtp_tls_protocols = >=TLSv1.2" "smtpd_tls_mandatory_protocols = >=TLSv1.2" "smtp_tls_loglevel = 1" "smtpd_tls_received_header = yes" \
  "virtual_mailbox_domains = hash:/etc/postfix/vmail/domains" "virtual_mailbox_maps = hash:/etc/postfix/vmail/mailboxes" "virtual_alias_maps = hash:/etc/postfix/vmail/aliases" \
  "virtual_transport = lmtp:unix:private/dovecot-lmtp" "smtpd_sender_login_maps = hash:/etc/postfix/vmail/senders" \
  "smtpd_sasl_type = dovecot" "smtpd_sasl_path = private/auth" "smtpd_sasl_auth_enable = no" "broken_sasl_auth_clients = yes" \
  "smtpd_helo_required = yes" "disable_vrfy_command = yes" "smtpd_delay_reject = yes" \
  "smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, defer_unauth_destination" \
  "smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_non_fqdn_recipient, reject_unknown_recipient_domain, reject_unauth_destination" \
  "smtpd_helo_restrictions = permit_mynetworks, reject_invalid_helo_hostname, reject_non_fqdn_helo_hostname" \
  "milter_protocol = 6" "milter_default_action = accept" "smtpd_milters = inet:127.0.0.1:11332" "non_smtpd_milters = \$smtpd_milters" \
  "milter_mail_macros = i {mail_addr} {client_addr} {client_name} {auth_authen}" "smtp_helo_name = $HOSTNAME_FQDN"
if ! grep -q "irnoti: submission" /etc/postfix/master.cf; then
cat >>/etc/postfix/master.cf <<'EOF'

# irnoti: submission (587 STARTTLS) and smtps (465 TLS): authenticated users only, sender must match the login
submission inet n - y - - smtpd
  -o syslog_name=postfix/submission
  -o smtpd_tls_security_level=encrypt
  -o smtpd_tls_auth_only=yes
  -o smtpd_sasl_auth_enable=yes
  -o smtpd_reject_unlisted_recipient=no
  -o smtpd_client_restrictions=permit_sasl_authenticated,reject
  -o smtpd_sender_restrictions=reject_sender_login_mismatch,permit_sasl_authenticated,reject
  -o smtpd_recipient_restrictions=reject_non_fqdn_recipient,reject_unknown_recipient_domain,permit_sasl_authenticated,reject
  -o milter_macro_daemon_name=ORIGINATING
smtps inet n - y - - smtpd
  -o syslog_name=postfix/smtps
  -o smtpd_tls_wrappermode=yes
  -o smtpd_sasl_auth_enable=yes
  -o smtpd_reject_unlisted_recipient=no
  -o smtpd_client_restrictions=permit_sasl_authenticated,reject
  -o smtpd_sender_restrictions=reject_sender_login_mismatch,permit_sasl_authenticated,reject
  -o smtpd_recipient_restrictions=reject_non_fqdn_recipient,reject_unknown_recipient_domain,permit_sasl_authenticated,reject
  -o milter_macro_daemon_name=ORIGINATING
EOF
fi
postfix check

# ── Rspamd (spam filter + DKIM) ───────────────────────────────────────────────
say "Configuring Rspamd"
mkdir -p /etc/rspamd/local.d /etc/rspamd/override.d
cat >/etc/rspamd/local.d/worker-proxy.inc <<'EOF'
milter = yes;
timeout = 120s;
upstream "local" { default = yes; self_scan = yes; }
bind_socket = "127.0.0.1:11332";
EOF
cat >/etc/rspamd/local.d/milter_headers.conf <<'EOF'
extended_spam_headers = true;
use = ["authentication-results", "x-spam-status", "x-spamd-result", "x-spam-level"];
authenticated_headers = ["authentication-results"];
EOF
cat >/etc/rspamd/local.d/dkim_signing.conf <<'EOF'
selector = "irnoti";
path = "/var/lib/rspamd/dkim/$domain.$selector.key";
allow_username_mismatch = true;
sign_authenticated = true;
sign_local = true;
use_domain = "header";
# the selector/path above are the *fallback* rule in Rspamd (there is no per-domain table), so it must be on
try_fallback = true;
EOF
cat >/etc/rspamd/local.d/redis.conf <<'EOF'
servers = "127.0.0.1";
EOF
cat >/etc/rspamd/local.d/classifier-bayes.conf <<'EOF'
autolearn = true;
EOF
echo "password = \"$(rspamadm pw -q -p "$RSPAMD_PASSWORD")\";" >/etc/rspamd/local.d/worker-controller.inc
echo "bind_socket = \"127.0.0.1:11334\";" >>/etc/rspamd/local.d/worker-controller.inc
rspamadm configtest >/dev/null || die "Rspamd configuration is invalid"

# ── management agent ──────────────────────────────────────────────────────────
say "Installing the management agent"
install -d /usr/local/lib/irnoti-mail
install -m 0755 "$SCRIPT_DIR/irnoti-mail-agent.py" /usr/local/lib/irnoti-mail/irnoti-mail-agent.py
TLS_CERT_REL="$TLS_CERT"; TLS_KEY_REL="$TLS_KEY"
cat >"$STATE_DIR/agent.json" <<EOF
{ "token": "$AGENT_TOKEN", "listen": "0.0.0.0", "port": $AGENT_PORT, "tlsCert": "$TLS_CERT_REL", "tlsKey": "$TLS_KEY_REL" }
EOF
chmod 600 "$STATE_DIR/agent.json"
cat >/etc/systemd/system/irnoti-mail-agent.service <<'EOF'
[Unit]
Description=IRNoti mail server management agent
After=network-online.target postfix.service dovecot.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/irnoti-mail/irnoti-mail-agent.py
Restart=always
RestartSec=3
User=root
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ── firewall and fail2ban ─────────────────────────────────────────────────────
if [[ -z $NO_SERVICES ]]; then
say "Configuring the firewall and fail2ban"
ufw allow OpenSSH >/dev/null; ufw allow 25/tcp >/dev/null; ufw allow 80/tcp >/dev/null
for p in 465 587 993 995; do ufw allow "$p"/tcp >/dev/null; done
if [[ -n "$API_IP" ]]; then ufw allow from "$API_IP" to any port "$AGENT_PORT" proto tcp >/dev/null
else warn "no --api-ip: the agent port $AGENT_PORT is open to everyone (it is protected by its token and TLS, but restrict it when you can)"; ufw allow "$AGENT_PORT"/tcp >/dev/null; fi
ufw --force enable >/dev/null
cat >/etc/fail2ban/jail.d/irnoti-mail.local <<'EOF'
[postfix]
enabled = true
mode = aggressive
[postfix-sasl]
enabled = true
[dovecot]
enabled = true
[sshd]
enabled = true
EOF
fi

# ── start everything ──────────────────────────────────────────────────────────
if [[ -z $NO_SERVICES ]]; then
say "Starting services"
systemctl daemon-reload
systemctl enable --now redis-server rspamd dovecot postfix fail2ban irnoti-mail-agent >/dev/null 2>&1
systemctl restart redis-server rspamd dovecot postfix fail2ban irnoti-mail-agent
sleep 2
bad=0
for s in postfix dovecot rspamd irnoti-mail-agent; do systemctl is-active --quiet "$s" || { warn "$s is not running: journalctl -u $s -n 50"; bad=1; }; done
[[ $bad -eq 0 ]] || die "some services did not start"
for port in 25 587 465 993 995 "$AGENT_PORT"; do (ss -ltn | grep -q ":$port ") || warn "nothing is listening on port $port"; done
fi

if [[ -z $NO_SERVICES ]]; then
  say "Running the self-test (creates and removes a throw-away domain)"
  sleep 5
  python3 "$SCRIPT_DIR/selftest.py" || warn "the self-test found problems (see above); the server may still work, but fix them before adding customers. Re-run: sudo $SCRIPT_DIR/selftest.py"
fi

SHOW_IP="${PUBLIC_IP:-the-IP-of-this-server}"
cat <<EOF

${c_grn}Done. The mail server is running.${c_off}

1) In the IRNoti admin panel add a provider: product "ایمیل", adapter "سرور ایمیل اختصاصی (Postfix + Dovecot روی اوبونتو)", with
   agentUrl        https://$HOSTNAME_FQDN:$AGENT_PORT
   agentToken      $AGENT_TOKEN
   hostname        $HOSTNAME_FQDN
   masterUser      $MASTER_USER
   masterPassword  $MASTER_PASSWORD
   relayUser       $RELAY_USER
   relayPassword   $RELAY_PASSWORD
   (these values are kept in $SECRETS; protect that file)

2) DNS for this host:  A  $HOSTNAME_FQDN -> $SHOW_IP   and PTR (reverse DNS) of that IP -> $HOSTNAME_FQDN
   Each customer domain then needs MX/SPF/DKIM/DMARC; the IRNoti panel shows the exact records after the domain is added.

3) Warm up slowly: new IPs have no reputation. Start with a few hundred mails a day and check https://www.mail-tester.com and your IP on https://mxtoolbox.com/blacklists.aspx.

Rspamd web UI (localhost only): ssh -L 11334:127.0.0.1:11334 root@$HOSTNAME_FQDN  then http://127.0.0.1:11334  (password in $SECRETS)
Log of this run: $LOG
EOF
