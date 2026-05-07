# VPS Deployment

Hướng dẫn deploy lên Ubuntu/Debian VPS với:
- gunicorn (WSGI server)
- systemd (process supervisor + auto-restart)
- nginx (reverse proxy + TLS termination)
- Let's Encrypt qua certbot (HTTPS miễn phí)

Giả định: bạn có VPS Ubuntu 22.04+/Debian 12+ với SSH root, một domain `YOUR_DOMAIN` đã trỏ A record về IP VPS.

Mọi lệnh sau đây chạy với quyền `root` (qua `sudo` hoặc `sudo -i`) trừ khi nói rõ "as locket user".

---

## 1. Cài gói hệ thống

```bash
apt update
apt install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx
```

## 2. Tạo user `locket`

```bash
adduser --system --group --shell /bin/bash --home /home/locket locket
```

`--system` không có shell login bằng password (chỉ chạy app). `--shell /bin/bash` cần thiết để bạn có thể `sudo -u locket -i` khi debug.

## 3. Clone repo + venv

```bash
sudo -u locket -i
# (đang là user locket)
cd /home/locket
git clone https://github.com/YOU/LocketGoldUsername.git
cd LocketGoldUsername
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
exit
# (quay lại root)
```

## 4. Tạo file `.env`

```bash
sudo -u locket nano /home/locket/LocketGoldUsername/.env
```

Điền:

```env
# Locket fallback creds — admin panel sẽ thêm thêm vào DB.
EMAIL=your_locket_email@example.com
PASSWORD=your_locket_password

# Gist tokens (optional — admin panel có thể thay thế qua UI).
gist_token_url=https://gist.githubusercontent.com/.../raw/

# Telegram (optional).
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Admin panel.
ADMIN_PASSWORD=$(openssl rand -base64 24)
FLASK_SECRET_KEY=$(openssl rand -hex 32)

# Bật flag này khi đã có nginx + TLS phía trước (sau bước 7).
BEHIND_HTTPS=1
```

> Mẹo: chạy `openssl rand -base64 24` và `openssl rand -hex 32` trên máy bạn rồi paste vào, vì `nano` không evaluate `$(...)`.

Khoá file:
```bash
chmod 600 /home/locket/LocketGoldUsername/.env
chown locket:locket /home/locket/LocketGoldUsername/.env
```

## 5. Cài systemd service

```bash
cp /home/locket/LocketGoldUsername/deploy/locket.service /etc/systemd/system/locket.service
systemctl daemon-reload
systemctl enable --now locket
systemctl status locket
```

Kết quả mong đợi: `Active: active (running)`. Logs:

```bash
journalctl -u locket -f
```

Bạn sẽ thấy:
```
db: initialized at locket.db
AccountRotator: loaded 1 account(s)
Worker xxxxxxx started (slot ..., your_email@...)
Queue manager initialized with 1 worker(s)
[INFO] Listening at: http://127.0.0.1:5001
```

Test local:
```bash
curl -I http://127.0.0.1:5001/
# 200 OK
```

## 6. Cấu hình nginx

```bash
cp /home/locket/LocketGoldUsername/deploy/nginx.conf /etc/nginx/sites-available/locket
sed -i 's/YOUR_DOMAIN/locket.example.com/g' /etc/nginx/sites-available/locket
ln -sf /etc/nginx/sites-available/locket /etc/nginx/sites-enabled/locket
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

> Nếu `nginx -t` lỗi vì cert chưa có (file 443 reference `/etc/letsencrypt/...`), tạm bỏ block 443 trước, chạy certbot ở bước 7, rồi paste lại block 443 sau. Hoặc dùng `certbot --nginx` (bước 7) sẽ tự inject SSL block.

## 7. Cấp HTTPS với Let's Encrypt

```bash
certbot --nginx -d locket.example.com
```

Theo prompt:
- Email: dùng email thật (Let's Encrypt gửi cảnh báo cert sắp hết hạn)
- Đồng ý ToS
- Có / không subscribe newsletter
- **Chọn 2** — redirect HTTP → HTTPS

Certbot sẽ:
- Cấp cert
- Sửa `/etc/nginx/sites-available/locket` thêm SSL block (nếu bạn dùng config gốc)
- Reload nginx

Test:
```bash
curl -I https://locket.example.com/
# HTTP/2 200
```

Auto-renew đã được setup (cron + timer). Verify:
```bash
systemctl list-timers | grep certbot
certbot renew --dry-run
```

## 8. Khoá firewall

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'   # 80 + 443
ufw enable
ufw status
```

Port 5001 KHÔNG mở ra ngoài — gunicorn chỉ listen 127.0.0.1.

## 9. Login admin lần đầu

Mở browser: `https://locket.example.com/admin/login`

Nhập password đã set ở bước 4 (`ADMIN_PASSWORD` trong `.env`).

Sau khi login:
- **Tài khoản**: thêm/xoá Locket account, Test login trước khi add
- **Tokens**: paste payload RevenueCat (admin panel ưu tiên, gist là fallback)
- **Queue**: xem realtime jobs đang xử lý
- **Lịch sử**: 30 job gần nhất

---

## Vận hành thường ngày

| Task | Lệnh |
|---|---|
| Xem log live | `journalctl -u locket -f` |
| Restart app | `systemctl restart locket` |
| Reload nginx | `systemctl reload nginx` |
| Xem trạng thái | `systemctl status locket` |
| Pull update từ git | `sudo -u locket -i` → `cd LocketGoldUsername && git pull && .venv/bin/pip install -r requirements.txt` → `exit` → `systemctl restart locket` |
| Backup DB | `sudo -u locket cp /home/locket/LocketGoldUsername/locket.db ~/locket-backup-$(date +%F).db` |

## Backup chiến lược

`locket.db` chứa accounts (password plaintext!), tokens RevenueCat, queue history. **Backup định kỳ** rất quan trọng vì nếu mất DB thì phải nhập lại tất cả tài khoản qua admin panel.

Cron đơn giản (crontab -e dưới root):
```cron
0 3 * * * sudo -u locket sqlite3 /home/locket/LocketGoldUsername/locket.db ".backup /home/locket/backups/locket-$(date +\%F).db"
```

`.backup` của sqlite hoạt động an toàn ngay cả khi app đang ghi (dùng SQLite online backup API).

## Troubleshooting

**App khởi động fail**: `journalctl -u locket -n 50` xem stack trace. 90% là do `.env` thiếu `EMAIL`/`PASSWORD` hoặc `ADMIN_PASSWORD`.

**Login admin redirect loop**: nguyên nhân thường là `BEHIND_HTTPS=1` nhưng truy cập qua HTTP, hoặc nginx không pass `X-Forwarded-Proto`. Verify `proxy_set_header X-Forwarded-Proto $scheme;` trong `/etc/nginx/sites-available/locket`.

**502 Bad Gateway**: gunicorn chưa chạy hoặc crash. `systemctl status locket`.

**Queue không xử lý**: vào admin panel xem có account nào không. Logs sẽ in `Worker xxx exited` nếu hết account.

**Đổi domain**: sửa `server_name` trong nginx config + `certbot --nginx -d new.example.com` + `systemctl reload nginx`. App không cần restart.
