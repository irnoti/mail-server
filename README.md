# IRNoti Mail Server — راه‌اندازی سرور ایمیل (Postfix + Dovecot + Rspamd) و اتصال به IRNoti

این راهنما قدم‌به‌قدم می‌گوید چطور یک **سرور ایمیل کامل** روی اوبونتو بالا بیاورید که به پنل IRNoti وصل شود و ایمیل سازمانی، SMTP، IMAP و POP3 بدهد. منطق دامنهٔ سرویس در مخزن اصلی IRNoti: `docs/LOGIC.md` §۱۰۹.

## ۱. معماری

```
 مشتری (Outlook/موبایل)        پنل و API IRNoti                          اینترنت
 IMAP 993 / POP3 995 ─┐         ┌──────────────────────────────┐
 SMTP 587/465 ────────┤         │ NestJS API                   │
                      ▼         │  • EmailProvider "mailserver"│──HTTPS+توکن──┐
 ┌─────────────────────────────┐│  • همگام‌سازی IMAP (master)  │──IMAPS 993───┤
 │ سرور ایمیل (Ubuntu)         ││  • ارسال با SMTP 587         │──SMTP 587────┤
 │  Postfix  (SMTP 25/465/587) │└──────────────────────────────┘              │
 │  Dovecot  (IMAP/POP3/LMTP,  │◄─────────────────────────────────────────────┘
 │            سهمیه، Sieve)    │      نامهٔ ورودی ←── MX دامنهٔ مشتری ←── سرورهای دیگر
 │  Rspamd   (هرزنامه + DKIM)  │      نامهٔ خروجی ──► (امضای DKIM) ──► سرورهای مقصد
 │  irnoti-mail-agent (HTTPS)  │
 └─────────────────────────────┘
```

- **عامل مدیریتی** (`irnoti-mail-agent.py`، فقط کتابخانهٔ استاندارد پایتون): دامنه، صندوق، نام مستعار و سهمیه را در فایل‌های Postfix/Dovecot می‌نویسد. پایگاه‌دادهٔ جدا لازم نیست و خراب‌شدنش مشکلی برای تحویل نامه ایجاد نمی‌کند (Postfix و Dovecot مستقل کار می‌کنند).
- **رمز صندوق‌ها** فقط به‌صورت هش `SHA512-CRYPT` روی سرور است؛ پنل آن را نمی‌داند. پنل با **کاربر اصلی Dovecot** (`user*master`) نامه‌ها را می‌خواند و با **کاربر ارسال** از پورت ۵۸۷ می‌فرستد؛ Postfix فقط به همین حساب (و خود صندوق‌ها) اجازهٔ ارسال به‌نام آدرس‌های میزبانی‌شده را می‌دهد (`smtpd_sender_login_maps`).
- **DKIM**: برای هر دامنه یک کلید ۲۰۴۸بیتی در `/var/lib/rspamd/dkim/<دامنه>.irnoti.key`؛ Rspamd هر نامهٔ خروجی را امضا می‌کند. کلید عمومی در پنل مشتری نمایش داده می‌شود.
- **هرزنامه**: Rspamd هر نامهٔ ورودی را امتیاز می‌دهد، سرایند `X-Spam-Status: Yes/No` می‌گذارد و Sieve نامهٔ «Yes» را به `Junk` می‌برد؛ پنل همان پوشه را «هرزنامه» نشان می‌دهد و فیلتر خودش را هم اضافه می‌کند.

## ۲. پیش‌نیازها

| مورد | توضیح |
|---|---|
| سرور | Ubuntu **22.04 یا 24.04**، حداقل ۲ گیگابایت RAM و ۲۰ گیگابایت دیسک (+ حجم صندوق‌ها)، IPv4 عمومی ثابت |
| پورت ۲۵ خروجی | بسیاری از ارائه‌دهنده‌ها (به‌ویژه ابری‌های بزرگ و بعضی ایرانی) آن را می‌بندند. پیش از خرید بپرسید یا تیکت بزنید. بدون آن نامه به سرورهای دیگر نمی‌رسد |
| رکورد A | `mail.example.com → IP سرور` |
| PTR (Reverse DNS) | IP سرور باید به `mail.example.com` برگردد؛ از پنل ارائه‌دهنده تنظیم می‌شود. بدون آن Gmail/Yahoo و بیشتر سرورها نامه را رد یا هرزنامه می‌کنند |
| پورت‌های ورودی | ۲۵، ۸۰ (دریافت و تمدید گواهی)، ۴۶۵، ۵۸۷، ۹۹۳، ۹۹۵ و پورت عامل (پیش‌فرض ۸۴۴۳، فقط برای IP سرور API) |
| دامنه‌ای که روی آن بنویسید | نام سرور (`mail.example.com`) از دامنهٔ خودتان؛ دامنهٔ مشتریان جداست و در پنل اضافه می‌شود |

> **ایران:** برای ایمیل به مقصد Gmail و سرویس‌های خارجی، سرور باید IP با اعتبار و پورت ۲۵ باز داشته باشد؛ معمولاً سرور خارج از کشور. اگر فقط ایمیل داخلی لازم است سرور داخلی هم کافی است. برای ارسال تراکنشی بدون سرور اختصاصی، پرووایدر SMTP/API (بخش ۸) ساده‌تر است.

## ۳. نصب (یک دستور)

```bash
git clone https://github.com/irnoti/mail-server.git
cd mail-server
sudo ./install.sh --hostname mail.example.com --admin-email you@example.com --api-ip <IP سرور API>
```

گزینه‌ها: `--agent-port` (پیش‌فرض ۸۴۴۳)، `--self-signed` (بدون Let's Encrypt؛ فقط تست)، `--skip-checks` (ادامه با وجود هشدار DNS/پورت ۲۵)، `--uninstall-agent`.

اسکریپت به‌ترتیب: ۱) بررسی (سیستم‌عامل، A و PTR، پورت ۲۵)، ۲) نصب بسته‌ها، ۳) گواهی Let's Encrypt با تمدید خودکار، ۴) کاربر `vmail` و فایل‌های وضعیت، ۵) پیکربندی Dovecot (IMAPS، POP3S، LMTP، سهمیه، Sieve)، ۶) Postfix (دامنه‌های مجازی، `submission` ۵۸۷ و `smtps` ۴۶۵ با SASL و کنترل فرستنده، milter)، ۷) Rspamd (milter، DKIM، Redis)، ۸) عامل مدیریتی و سرویس systemd، ۹) UFW و fail2ban، ۱۰) راه‌اندازی و بررسی پورت‌ها. دوباره اجرا کردن امن است (رمزها حفظ می‌شود). لاگ: `/var/log/irnoti-mail-install.log`.

در پایان **خودآزمون** اجرا می‌شود (`selftest.py`: یک دامنهٔ موقت با دو صندوق می‌سازد، نامهٔ واقعی از Postfix/Dovecot/Rspamd عبور می‌دهد و همه را پاک می‌کند): دریافت ورودی و رد گیرندهٔ ناموجود، فیلتر هرزنامه (Sieve و رد GTUBE)، ورود IMAP با master-user، ارسال از پورت ۵۸۷ و رد ورود بدون TLS، رد فرستندهٔ بیگانه و جعل هویت صندوق دیگر، و **امضای DKIM**. هر زمان می‌توانید دوباره اجرا کنید: `sudo ./selftest.py`.

در پایان مقادیر لازم برای فرم پرووایدر چاپ می‌شود و در `/etc/irnoti-mail/secrets.env` (دسترسی ۰۶۰۰) هم هست.

## ۴. اتصال به IRNoti

«ادمین ← پرووایدرها ← افزودن»: سرویس **ایمیل**، نوع **سرور ایمیل اختصاصی (Postfix + Dovecot روی اوبونتو)**:

| فیلد | مقدار |
|---|---|
| agentUrl | `https://mail.example.com:8443` |
| agentToken | توکن چاپ‌شده |
| hostname | `mail.example.com` (MX و SPF دامنه‌ها به این اشاره می‌کنند) |
| masterUser / masterPassword | کاربر اصلی Dovecot (برای خواندن صندوق‌ها در پنل) |
| relayUser / relayPassword | کاربر ارسال (SMTP ۵۸۷) |
| imapPort / pop3Port / smtpPort | خالی = ۹۹۳ / ۹۹۵ / ۵۸۷ |

اولویت و «عمومی/محدود» مثل بقیهٔ پرووایدرها. می‌توانید چند سرور بسازید؛ دامنهٔ جدید به پرووایدر با کمترین اولویت داده می‌شود (مشتری هم می‌تواند `providerId` بدهد).

> رمزها در دیتابیس با AES-256 ذخیره می‌شوند و فقط هنگام اتصال خوانده می‌شوند. «تست اتصال» جدا ندارد؛ با افزودن یک دامنهٔ آزمایشی (بخش ۵) اتصال عامل سنجیده می‌شود.

## ۵. اولین دامنه و صندوق (تست)

۱. با یک کاربر آزمایشی (KYC تأییدشده، گروه با قابلیت `email.hosted`، کیف پول شارژ) یک **بستهٔ ایمیل** بخرید (ادمین ← ایمیل ← بسته‌ها و قیمت‌گذاری: ابتدا یک بسته بسازید).
۲. «ایمیل ← دامنه»: دامنهٔ آزمایشی را اضافه کنید. جدول رکوردها را در DNS دامنه بگذارید:

| رکورد | مقدار نمونه |
|---|---|
| `TXT @` | `irnoti-verify=<توکن>` (اثبات مالکیت) |
| `MX @` (۱۰) | `mail.example.com` |
| `TXT @` | `v=spf1 a:mail.example.com ~all` |
| `TXT irnoti._domainkey` | `v=DKIM1; k=rsa; p=…` |
| `TXT _dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@دامنه; adkim=s; aspf=s` |

۳. «بررسی رکوردهای DNS» را بزنید تا وضعیت «تأیید شده» شود. ۴. «صندوق‌ها» ← ساخت صندوق (رمز خودکار فقط یک‌بار دیده می‌شود). ۵. از پنل نامه‌ای به Gmail خودتان بفرستید و جواب بدهید؛ در Gmail «Show original» سه مورد `SPF: PASS`، `DKIM: PASS`، `DMARC: PASS` را نشان دهد.

اتصال نرم‌افزار ایمیل: `IMAP mail.example.com:993 (SSL)` یا `POP3 :995 (SSL)`، `SMTP :587 (STARTTLS)` یا `:465 (SSL)`، نام کاربری = نشانی کامل.

## ۶. بعد از نصب (عملیات)

**سلامت:** `sudo ./selftest.py`، `systemctl status postfix dovecot rspamd irnoti-mail-agent`، `postqueue -p` (صف)، `journalctl -u postfix -f`، `doveadm who`، `rspamadm configtest`. رابط وب Rspamd فقط محلی: `ssh -L 11334:127.0.0.1:11334 root@mail.example.com` سپس `http://127.0.0.1:11334` (رمز در `secrets.env`).

**اعتبار IP و تحویل‌پذیری:** IP تازه اعتبار ندارد. دو هفتهٔ اول حجم را از چند صد نامه در روز شروع و آهسته زیاد کنید. با [mail-tester.com](https://www.mail-tester.com) امتیاز بگیرید (≥۹)، IP را در [mxtoolbox.com/blacklists](https://mxtoolbox.com/blacklists.aspx) بسنجید، و [Google Postmaster Tools](https://postmaster.google.com) را روی دامنه فعال کنید. DMARC را بعد از اطمینان از SPF/DKIM از `p=none` به `quarantine` ببرید. سامانه خودش bounce دائمی و شکایت را در فهرست ممنوعه می‌گذارد و حساب با نرخ بازگشت بالا را متوقف می‌کند.

**پشتیبان‌گیری** (روزانه، خارج از همان سرور): `/var/vmail` (نامه‌ها)، `/etc/dovecot/users` و `/etc/dovecot/master-users`، `/etc/postfix/vmail`، `/var/lib/rspamd/dkim` (کلیدهای DKIM؛ با از دست رفتن آن‌ها باید رکورد DNS همهٔ دامنه‌ها عوض شود)، `/etc/irnoti-mail`، `/etc/letsencrypt`. نمونه: `rsync -aH --delete /var/vmail /etc/dovecot /etc/postfix/vmail /var/lib/rspamd/dkim /etc/irnoti-mail backup@host:/backups/mail/`. بازیابی: همان مسیرها را برگردانید، `postmap` روی چهار فایل `/etc/postfix/vmail/*` و `systemctl restart postfix dovecot rspamd irnoti-mail-agent`.

**نگهداری:** `apt update && apt upgrade` ماهانه؛ بروزرسانی عامل: فایل جدید `irnoti-mail-agent.py` را کپی کنید و `systemctl restart irnoti-mail-agent`. گواهی خودکار تمدید می‌شود (`certbot renew --dry-run` را بسنجید).

**مقیاس و پایداری:** یک سرور ده‌ها صندوق تا چند هزار صندوق را تحمل می‌کند (محدودیت اصلی دیسک و اعتبار IP است). برای دسترس‌پذیری بالا MX دوم و ذخیره‌سازی مشترک لازم است که در این نسخه آماده نیست؛ پشتیبان‌گیری و بازیابی سریع جایگزین عملی آن است.

## ۷. عیب‌یابی

| نشانه | علت محتمل | راه‌حل |
|---|---|---|
| نصب روی «certbot» شکست می‌خورد | A هنوز منتشر نشده یا پورت ۸۰ بسته است | `dig +short mail.example.com`، فایروال ارائه‌دهنده؛ یا `--self-signed` |
| نامه به Gmail نمی‌رسد/`Connection timed out` | پورت ۲۵ خروجی بسته | از ارائه‌دهنده بخواهید باز کند: `nc -vz gmail-smtp-in.l.google.com 25` |
| نامه می‌رسد ولی هرزنامه است | PTR یا SPF/DKIM/DMARC نادرست؛ IP جدید | `Show original` در Gmail؛ PTR؛ بخش «اعتبار IP» |
| پنل «ورود به صندوق ناموفق» می‌دهد | master-user یا رمز اشتباه در فرم پرووایدر | `doveadm auth test 'user@d*irnoti-master' <رمز>` روی سرور |
| `Relay access denied` هنگام ارسال از پنل | relayUser/Password اشتباه یا فرستنده در `senders` نیست | `postmap -q 'user@d' hash:/etc/postfix/vmail/senders`؛ لاگ `postfix/submission` |
| «sender not owned by user» | آدرس From با ورود SMTP نمی‌خواند | با Outlook از همان آدرس صندوق وارد شوید؛ یا نام مستعار را به صندوق بدهید |
| `554 5.7.1 Recipient address rejected` | دامنه در `vmail/domains` نیست یا MX هنوز عوض نشده | `postmap -q example.ir hash:/etc/postfix/vmail/domains`؛ صبر برای DNS |
| «Mailbox is full» (۴۵۲) | سهمیهٔ صندوق پر است | افزایش حجم در پنل (`PATCH /email/mailboxes/:id`) یا حذف نامه |
| عامل ۴۰۱ می‌دهد | توکن فرم پرووایدر با `agent.json` نمی‌خواند | توکن را از `/etc/irnoti-mail/secrets.env` دوباره وارد کنید |
| عامل از پنل در دسترس نیست | UFW فقط `--api-ip` را می‌پذیرد و IP API عوض شده | `ufw allow from <IP> to any port 8443` |

## ۸. جایگزین‌ها بدون سرور اختصاصی

- **رلهٔ SMTP** (Amazon SES SMTP، Brevo و…): پرووایدر «ارسال از طریق SMTP» با `host/port/username/password` و فیلد `spf` (مثلاً `include:amazonses.com`). برای ارسال تراکنشی از دامنه‌های «فقط ارسال».
- **API**: Mailgun (`apiKey`، `region`، `signingKey`)، SendGrid (`apiKey`)، Postmark (`serverToken`). دامنه با پرووایدر ساخته می‌شود و رکوردهای SPF/DKIM پرووایدر در پنل نمایش داده می‌شود. گزارش تحویل: وب‌هوک پرووایدر را روی `/api/v1/hooks/email/<شناسه>/<webhookSecret>` بگذارید.
- صندوق سازمانی (دریافت + IMAP/POP3) فقط با «سرور ایمیل اختصاصی» ممکن است؛ این پرووایدرها «فقط ارسال»‌اند (بدون `createMailbox` → خطای `501 not_supported` برای ساخت صندوق).

## ۹. مرجع عامل مدیریتی

HTTPS روی `agentPort` با `Authorization: Bearer <agentToken>`؛ بدنهٔ JSON حداکثر ۶۴ کیلوبایت؛ همهٔ ورودی‌ها قبل از استفاده اعتبارسنجی می‌شوند (نام دامنه/آدرس با regex سخت‌گیرانه، مسیر دیسک فقط از بخش‌های اعتبارسنجی‌شده ساخته می‌شود، فرمان‌ها بدون shell و رمزها فقط از stdin).

| متد و مسیر | کار |
|---|---|
| `GET /v1/health` | وضعیت سرویس‌ها و تعداد دامنه/صندوق |
| `POST /v1/domains {domain}` | ساخت دامنه و کلید DKIM ← `{dkimSelector, dkimPublic}` |
| `GET /v1/domains/:d` | کلید عمومی DKIM |
| `DELETE /v1/domains/:d` | حذف (۴۰۹ اگر صندوق دارد) |
| `PUT /v1/mailboxes/:addr {password, quotaMb}` | ساخت یا بازنویسی صندوق |
| `PATCH /v1/mailboxes/:addr {password?, quotaMb?}` | تغییر رمز/سهمیه |
| `DELETE /v1/mailboxes/:addr` | حذف صندوق و نامه‌هایش |
| `PUT /v1/aliases/:addr {targets[]}` | نام مستعار؛ `@domain` = Catch-all |
| `DELETE /v1/aliases/:addr` | حذف |
| `POST /v1/usage {addresses[]}` | حجم مصرفی (بایت) هر صندوق |
| `GET /v1/queue` | تعداد نامه‌های صف Postfix |

فایل‌ها: `/etc/postfix/vmail/{domains,mailboxes,aliases,senders}` (+ `.db`)، `/etc/dovecot/users` (`user:{SHA512-CRYPT}hash::::::userdb_quota_rule=*:storage=1024M`)، `/etc/dovecot/master-users`، `/var/vmail/<دامنه>/<کاربر>/Maildir`، `/var/lib/rspamd/dkim/<دامنه>.irnoti.key`، `/etc/irnoti-mail/{agent.json,secrets.env}`.

## ۱۰. امنیت

- فقط TLS ≥ 1.2؛ ورود متن‌ساده (بدون TLS) غیرفعال است (`disable_plaintext_auth`)؛ پورت‌های ۱۱۰ و ۱۴۳ باز نیستند.
- پورت عامل فقط برای IP سرور API در UFW باز می‌شود و علاوه بر آن به توکن ۴۸ نویسه‌ای (مقایسهٔ زمان‌ثابت) و TLS وابسته است.
- fail2ban برای SSH، Postfix و Dovecot؛ محدودیت ۴۰ اتصال هم‌زمان IMAP برای هر کاربر/IP.
- رمز صندوق هرگز در پنل ذخیره یا لاگ نمی‌شود؛ بازنشانی رمز از پنل رمز جدید را یک‌بار نشان می‌دهد.
- دسترسی root به سرور = دسترسی به همهٔ نامه‌ها. SSH را با کلید و بدون رمز و فقط برای ادمین‌ها نگه دارید.

## ۱۱. وب‌میل با نشانی خود سازمان (mail.company.com)

هر سازمان می‌تواند در پنل (ایمیل ← دامنه‌ها ← «وب‌میل اختصاصی سازمان») نشانی‌ای مثل `mail.company.com` ثبت کند، در DNS خودش یک **CNAME** به مقصدی که پنل نشان می‌دهد (`EMAIL_PORTAL_TARGET`؛ پیش‌فرض همان میزبان `PUBLIC_WEB_URL`) بسازد و «بررسی DNS» بزند. از آن لحظه `https://mail.company.com` صفحهٔ ورود برند سازمان است.

برای HTTPS روی میزبان‌هایی که از قبل نمی‌شناسید، پروکسی جلوی وب باید گواهی را هنگام اولین درخواست بگیرد. با Caddy و فقط برای نشانی‌های تأییدشده:

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

Caddy نام را به‌صورت `?domain=` به `ask` می‌دهد (۲۰۰ = شناخته‌شده، ۴۰۴ = رد)؛ پروکسی‌هایی که نام را در مسیر می‌خواهند از `/api/v1/portal/host/<نشانی>` استفاده کنند. نشانی تأییدنشده هرگز گواهی نمی‌گیرد. با Nginx/Traefik یا Cloudflare هم کافی است همهٔ نشانی‌های تأییدشده به همان وب برسند؛ وب خودش میزبان ناشناخته را به سایت اصلی برمی‌گرداند.
