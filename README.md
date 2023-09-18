# SMTP-TG
### SMTP to Telegram mail forwarder with file-backed persistent queue.
Libs: asyncio, aiosmtpd, email, aiogram.

Forwarding logic:
- receives everything
- numeric user in @tg forwarded to corresponding Telegram chat (12345678@tg to TG chat 12345678)
- any others to DEFAULT_CHAT
- preserve msgs order

Errors handling:
- Any network error defers sending with a progressive delay (from TG_RETRY_PERIOD_MIN to TG_RETRY_PERIOD_MAX, doubled on each subsequent error, reset at success)
- Other (permanent) errors sent to DEFAULT_CHAT if available, otherwise logged.

Telegram message formatting:
- From, To
- Subject: if present
- Body: result of email.message.EmailMessage.get_body()
- File with raw email attached if body not found or there is more than one part in multipart email.

Configuration with self-describing environment variables:
- SMTP_HOST
- SMTP_PORT
- MAIL_QUEUE_FOLDER
- TG_TOKEN
- TG_DEFAULT_CHAT
- TG_RETRY_PERIOD_MIN
- TG_RETRY_PERIOD_MAX
- STOP_TIMEOUT (timeout for waiting for graceful finish after SIGINT or SIGTERM signals)

Container build:

`podman build -t rokiden/smtptg:latest -t rokiden/smtptg:$(date +%s) .`

Container run:

`podman run --name smtptg -e MAIL_QUEUE_FOLDER=/mqueue -e TG_TOKEN=<YOUR_TOKEN> -e TG_DEFAULT_CHAT=<YOUR_CHAT_ID> -v /etc/localtime:/etc/localtime:ro -v ./mqueue:/mqueue rokiden/smtptg:latest`

### Note:
I'm using Postfix with big queue as *always-ready-to-receive* endpoint, configured to forward everything (except postmaster mail) to SMTP-TG when available, otherwise defer. SMTP-TG response OK only after saving msg to file. This way I get loss-proof mail delivery, with ability to restart/maintenance SMTP-TG container or whole Podman/Docket. 

Postfix config:
```
... defaults ...
inet_interfaces = 10.89.0.1, 127.0.0.1
inet_protocols = ipv4
mynetworks = 10.89.0.0/24, 127.0.0.0/8
# 10.89.0.0/24 podman network
myhostname = <MYHOSTNAME>
mydomain = lan
myorigin = $myhostname
mydestination = forcelocal
# mail sent to @forcelocal doesnt forward
relayhost = [smtptg.dns.podman]:2525
maximal_backoff_time = 10m
minimal_backoff_time = 1m
queue_run_delay = 1m
maximal_queue_lifetime = 30d
bounce_queue_lifetime = 0
smtpd_peername_lookup = no
notify_classes=resource, software, 2bounce
postmaster = <MYUSER>@forcelocal
2bounce_notice_recipient = $postmaster
bounce_notice_recipient = $postmaster
delay_notice_recipient = $postmaster
error_notice_recipient = $postmaster
```
