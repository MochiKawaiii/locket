"""Out-of-band notifications. Today: Telegram only."""

import json
import os
import time

import requests


def send_telegram_notification(username, uid, product_id, raw_json):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        print("Telegram notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return

    subscription_info = json.dumps(
        raw_json.get("subscriber", {}).get("entitlements", {}).get("Gold", {}),
        indent=2,
    )

    message = (
        f"✅ <b>Locket Gold Unlocked!</b>\n\n"
        f"👤 <b>User:</b> {username} ({uid})\n"
        f"⏰ <b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"<b>Subscription Info:</b>\n<pre>{subscription_info}</pre>"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        print(f"Failed to send Telegram notification: {e}")
