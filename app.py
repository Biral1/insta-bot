import os
import re
import threading
import tempfile
from pathlib import Path

import requests
import yt_dlp

from flask import Flask, request, jsonify
from supabase import create_client


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Your Telegram admin ID
ADMIN_ID = 1791464015


# ============================================================
# VALIDATE ENVIRONMENT
# ============================================================

required_variables = {
    "BOT_TOKEN": BOT_TOKEN,
    "WEBHOOK_SECRET": WEBHOOK_SECRET,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
}

missing = [
    name
    for name, value in required_variables.items()
    if not value
]

if missing:
    raise RuntimeError(
        "Missing environment variables: "
        + ", ".join(missing)
    )


# ============================================================
# INITIALIZE
# ============================================================

app = Flask(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


# ============================================================
# TELEGRAM API
# ============================================================

def telegram(method, data=None, files=None):

    url = f"{TELEGRAM_API}/{method}"

    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=120,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "description",
                "Telegram API error"
            )
        )

    return result


def send_message(chat_id, text):

    return telegram(
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
        },
    )


# ============================================================
# USER DATABASE
# ============================================================

def is_allowed(user_id):

    # Admin is always allowed
    if user_id == ADMIN_ID:
        return True

    try:

        result = (
            supabase
            .table("allowed_users")
            .select("telegram_id")
            .eq("telegram_id", user_id)
            .limit(1)
            .execute()
        )

        return bool(result.data)

    except Exception as e:

        print(
            "DATABASE ERROR while checking user:",
            repr(e)
        )

        # Fail closed:
        # if database is unavailable, don't allow users.
        return False


def add_user(user_id):

    result = (
        supabase
        .table("allowed_users")
        .upsert({
            "telegram_id": user_id
        })
        .execute()
    )

    return result


def remove_user(user_id):

    result = (
        supabase
        .table("allowed_users")
        .delete()
        .eq("telegram_id", user_id)
        .execute()
    )

    return result


def get_users():

    result = (
        supabase
        .table("allowed_users")
        .select("telegram_id, added_at")
        .order("added_at", desc=False)
        .execute()
    )

    return result.data or []


# ============================================================
# INSTAGRAM URL
# ============================================================

def is_instagram_url(text):

    if not text:
        return False

    pattern = (
        r"https?://"
        r"(?:www\.)?"
        r"instagram\.com/"
        r"(?:reel|p|tv)/"
        r"[^\s]+"
    )

    return bool(
        re.search(
            pattern,
            text,
            re.IGNORECASE
        )
    )


# ============================================================
# INSTAGRAM DOWNLOAD
# ============================================================

def download_instagram(url, output_dir):

    output_template = str(
        Path(output_dir) / "%(id)s.%(ext)s"
    )

    options = {

        "outtmpl": output_template,

        "format": "bv*+ba/b",

        "merge_output_format": "mp4",

        "noplaylist": True,

        "quiet": True,

        "no_warnings": True,

        "extractor_args": {
            "instagram": {
                "skip": "dash"
            }
        },
    }

    # Optional Instagram cookies.
    #
    # If we later upload a cookies file to Render
    # as a Secret File, yt-dlp will automatically use it.

    cookies_path = (
        "/etc/secrets/instagram_cookies.txt"
    )

    if os.path.exists(cookies_path):

        print("Using Instagram cookies.")

        options["cookiefile"] = cookies_path

    else:

        print(
            "No Instagram cookies file found."
        )

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

        downloaded = ydl.prepare_filename(
            info
        )

        downloaded_path = Path(
            downloaded
        )

        # Look for the resulting file.
        #
        # yt-dlp may change the extension when
        # merging video/audio.

        candidates = list(
            Path(output_dir).glob(
                downloaded_path.stem + ".*"
            )
        )

        if not candidates:

            raise FileNotFoundError(
                "Downloaded video could not be found."
            )

        # Prefer MP4
        mp4_files = [
            file
            for file in candidates
            if file.suffix.lower() == ".mp4"
        ]

        if mp4_files:
            return mp4_files[0]

        return candidates[0]


# ============================================================
# DOWNLOAD PROCESS
# ============================================================

def process_download(chat_id, user_id, url):

    try:

        send_message(
            chat_id,
            "⏳ Downloading Instagram video..."
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            video_file = download_instagram(
                url,
                temp_dir
            )

            file_size = (
                video_file.stat().st_size
            )

            # Telegram Bot API limit
            # for this implementation.
            if file_size > 50 * 1024 * 1024:

                send_message(
                    chat_id,
                    "❌ The video is larger than "
                    "Telegram's 50 MB bot upload limit."
                )

                return

            send_message(
                chat_id,
                "📤 Uploading video..."
            )

            with open(
                video_file,
                "rb"
            ) as video:

                telegram(
                    "sendVideo",
                    data={
                        "chat_id": chat_id,
                        "supports_streaming": "true",
                    },
                    files={
                        "video": video,
                    },
                )

            send_message(
                chat_id,
                "✅ Done!"
            )

    except Exception as e:

        print(
            "DOWNLOAD ERROR:",
            repr(e)
        )

        send_message(
            chat_id,
            "❌ Download failed.\n\n"
            "The Instagram video may be private, "
            "require login, unavailable, or "
            "temporarily blocked by Instagram."
        )


# ============================================================
# COMMANDS
# ============================================================

def handle_command(
    chat_id,
    user_id,
    text
):

    command = text.strip()

    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if command == "/start":

        if user_id == ADMIN_ID:

            send_message(
                chat_id,
                "👋 Welcome, Admin!\n\n"
                "Instagram Downloader is ready.\n\n"
                "Admin commands:\n"
                "/users\n"
                "/adduser USER_ID\n"
                "/removeuser USER_ID\n"
                "/id"
            )

        elif is_allowed(user_id):

            send_message(
                chat_id,
                "👋 Welcome!\n\n"
                "Send me an Instagram Reel or "
                "video URL."
            )

        else:

            send_message(
                chat_id,
                "❌ You are not authorized "
                "to use this bot."
            )

        return True

    # --------------------------------------------------------
    # /help
    # --------------------------------------------------------

    if command == "/help":

        if user_id == ADMIN_ID:

            send_message(
                chat_id,
                "👑 Admin commands:\n\n"
                "/users - list allowed users\n"
                "/adduser USER_ID - allow a user\n"
                "/removeuser USER_ID - remove a user\n"
                "/id - show your Telegram ID\n\n"
                "You can also send an Instagram URL."
            )

        elif is_allowed(user_id):

            send_message(
                chat_id,
                "Send me an Instagram Reel or "
                "video URL."
            )

        else:

            send_message(
                chat_id,
                "❌ You are not authorized "
                "to use this bot."
            )

        return True

    # --------------------------------------------------------
    # /id
    # --------------------------------------------------------

    if command == "/id":

        send_message(
            chat_id,
            f"Your Telegram ID is:\n{user_id}"
        )

        return True

    # --------------------------------------------------------
    # ADMIN: /users
    # --------------------------------------------------------

    if command == "/users":

        if user_id != ADMIN_ID:

            send_message(
                chat_id,
                "❌ Admin only."
            )

            return True

        try:

            users = get_users()

            if not users:

                send_message(
                    chat_id,
                    "👥 No users are currently allowed."
                )

                return True

            lines = [
                "👥 Allowed users:\n"
            ]

            for index, user in enumerate(
                users,
                start=1
            ):

                telegram_id = user[
                    "telegram_id"
                ]

                lines.append(
                    f"{index}. {telegram_id}"
                )

            lines.append(
                f"\n👑 Admin: {ADMIN_ID}"
            )

            send_message(
                chat_id,
                "\n".join(lines)
            )

        except Exception as e:

            print(
                "USERS ERROR:",
                repr(e)
            )

            send_message(
                chat_id,
                "❌ Could not read user database."
            )

        return True

    # --------------------------------------------------------
    # ADMIN: /adduser
    # --------------------------------------------------------

    if command.startswith("/adduser"):

        if user_id != ADMIN_ID:

            send_message(
                chat_id,
                "❌ Admin only."
            )

            return True

        parts = command.split()

        if len(parts) != 2:

            send_message(
                chat_id,
                "Usage:\n"
                "/adduser 123456789"
            )

            return True

        try:

            new_user_id = int(parts[1])

            if new_user_id == ADMIN_ID:

                send_message(
                    chat_id,
                    "ℹ️ You are already the admin."
                )

                return True

            add_user(new_user_id)

            send_message(
                chat_id,
                f"✅ User {new_user_id} added."
            )

        except ValueError:

            send_message(
                chat_id,
                "❌ User ID must be a number."
            )

        except Exception as e:

            print(
                "ADD USER ERROR:",
                repr(e)
            )

            send_message(
                chat_id,
                "❌ Could not add user."
            )

        return True

    # --------------------------------------------------------
    # ADMIN: /removeuser
    # --------------------------------------------------------

    if command.startswith("/removeuser"):

        if user_id != ADMIN_ID:

            send_message(
                chat_id,
                "❌ Admin only."
            )

            return True

        parts = command.split()

        if len(parts) != 2:

            send_message(
                chat_id,
                "Usage:\n"
                "/removeuser 123456789"
            )

            return True

        try:

            remove_id = int(parts[1])

            if remove_id == ADMIN_ID:

                send_message(
                    chat_id,
                    "❌ You cannot remove the admin."
                )

                return True

            remove_user(remove_id)

            send_message(
                chat_id,
                f"✅ User {remove_id} removed."
            )

        except ValueError:

            send_message(
                chat_id,
                "❌ User ID must be a number."
            )

        except Exception as e:

            print(
                "REMOVE USER ERROR:",
                repr(e)
            )

            send_message(
                chat_id,
                "❌ Could not remove user."
            )

        return True

    return False


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.post("/webhook/<secret>")
def webhook(secret):

    # Verify webhook secret
    if secret != WEBHOOK_SECRET:

        return jsonify({
            "ok": False
        }), 403

    update = request.get_json(
        silent=True
    )

    if not update:

        return jsonify({
            "ok": True
        })

    message = update.get("message")

    if not message:

        return jsonify({
            "ok": True
        })

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get("id")

    user = message.get(
        "from",
        {}
    )

    user_id = user.get("id")

    text = message.get(
        "text",
        ""
    ).strip()

    if not chat_id or not user_id:

        return jsonify({
            "ok": True
        })

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    if text.startswith("/"):

        handled = handle_command(
            chat_id,
            user_id,
            text
        )

        if handled:

            return jsonify({
                "ok": True
            })

    # --------------------------------------------------------
    # Check authorization BEFORE downloading
    # --------------------------------------------------------

    if not is_allowed(user_id):

        send_message(
            chat_id,
            "❌ You are not authorized "
            "to use this bot."
        )

        return jsonify({
            "ok": True
        })

    # --------------------------------------------------------
    # Instagram URL
    # --------------------------------------------------------

    if is_instagram_url(text):

        thread = threading.Thread(
            target=process_download,
            args=(
                chat_id,
                user_id,
                text
            ),
            daemon=True
        )

        thread.start()

        return jsonify({
            "ok": True
        })

    # --------------------------------------------------------
    # Unknown message
    # --------------------------------------------------------

    send_message(
        chat_id,
        "Please send an Instagram Reel/video URL."
    )

    return jsonify({
        "ok": True
    })


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def home():

    return jsonify({
        "status": "running",
        "service": "Instagram Telegram Bot"
    })


# ============================================================
# RUN LOCALLY
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
