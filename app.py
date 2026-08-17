import os
import re
import threading
import tempfile
from pathlib import Path

import requests
import yt_dlp

from flask import Flask, request, jsonify


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-this-secret")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)


# --------------------------------------------------
# Telegram helpers
# --------------------------------------------------

def telegram(method, data=None, files=None):
    url = f"{TELEGRAM_API}/{method}"

    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=60,
    )

    response.raise_for_status()
    return response.json()


def send_message(chat_id, text):
    return telegram(
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
        },
    )


# --------------------------------------------------
# Instagram URL detection
# --------------------------------------------------

def is_instagram_url(text):
    if not text:
        return False

    pattern = r"https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[^\s]+"

    return bool(re.search(pattern, text, re.IGNORECASE))


# --------------------------------------------------
# Download
# --------------------------------------------------

def download_instagram(url, output_dir):

    output_template = str(
        Path(output_dir) / "%(id)s.%(ext)s"
    )

    options = {
        "outtmpl": output_template,

        # Prefer MP4 when available
        "format": "bv*+ba/b",

        "merge_output_format": "mp4",

        # Don't download playlists
        "noplaylist": True,

        # Keep logs useful
        "quiet": True,

        # Current yt-dlp option for Instagram
        "extractor_args": {
            "instagram": {
                "skip": "dash"
            }
        },
    }

    # Optional Instagram cookies.
    #
    # If you later upload a cookies.txt file to Render
    # as a Secret File, this automatically uses it.
    cookies_path = "/etc/secrets/instagram_cookies.txt"

    if os.path.exists(cookies_path):
        options["cookiefile"] = cookies_path

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        downloaded = ydl.prepare_filename(info)

        # yt-dlp may change the extension after merging
        base = Path(downloaded).with_suffix("")

        possible_files = list(Path(output_dir).glob(
            base.name + ".*"
        ))

        if not possible_files:
            raise FileNotFoundError(
                "Downloaded file could not be located."
            )

        return possible_files[0]


# --------------------------------------------------
# Process a download
# --------------------------------------------------

def process_download(chat_id, url):

    try:
        send_message(
            chat_id,
            "⏳ Downloading your Instagram video..."
        )

        with tempfile.TemporaryDirectory() as temp_dir:

            video_file = download_instagram(
                url,
                temp_dir
            )

            file_size = video_file.stat().st_size

            # Telegram Bot API currently allows bot uploads
            # of up to 50 MB for videos/documents.
            if file_size > 50 * 1024 * 1024:

                send_message(
                    chat_id,
                    "❌ The video is larger than Telegram's "
                    "current 50 MB bot upload limit."
                )

                return

            send_message(
                chat_id,
                "📤 Uploading video to Telegram..."
            )

            with open(video_file, "rb") as video:

                result = telegram(
                    "sendVideo",
                    data={
                        "chat_id": chat_id,
                        "supports_streaming": "true",
                    },
                    files={
                        "video": video,
                    },
                )

            if not result.get("ok"):
                raise RuntimeError(
                    result.get("description", "Telegram error")
                )

            send_message(
                chat_id,
                "✅ Done!"
            )

    except Exception as e:

        print("DOWNLOAD ERROR:", repr(e))

        send_message(
            chat_id,
            "❌ Download failed.\n\n"
            "The Instagram post may require login, "
            "be private, unavailable, or temporarily "
            "blocked by Instagram."
        )


# --------------------------------------------------
# Telegram webhook
# --------------------------------------------------

@app.post("/webhook/<secret>")
def webhook(secret):

    if secret != WEBHOOK_SECRET:
        return jsonify({
            "ok": False
        }), 403

    update = request.get_json(silent=True)

    if not update:
        return jsonify({
            "ok": True
        })

    message = update.get("message")

    if not message:
        return jsonify({
            "ok": True
        })

    chat = message.get("chat", {})
    chat_id = chat.get("id")

    text = message.get("text", "")

    if not chat_id:
        return jsonify({
            "ok": True
        })

    if text == "/start":

        send_message(
            chat_id,
            "👋 Instagram Downloader\n\n"
            "Send me an Instagram Reel or video URL."
        )

        return jsonify({
            "ok": True
        })

    if text == "/help":

        send_message(
            chat_id,
            "Send an Instagram Reel/video URL and "
            "I'll try to download it.\n\n"
            "Example:\n"
            "https://www.instagram.com/reel/..."
        )

        return jsonify({
            "ok": True
        })

    if is_instagram_url(text):

        # Start the download in the background so the
        # Telegram webhook receives a quick HTTP response.
        thread = threading.Thread(
            target=process_download,
            args=(chat_id, text),
            daemon=True,
        )

        thread.start()

        return jsonify({
            "ok": True
        })

    send_message(
        chat_id,
        "Please send a valid Instagram Reel/video URL."
    )

    return jsonify({
        "ok": True
    })


# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/")
def home():

    return jsonify({
        "status": "running",
        "service": "Instagram Telegram Bot"
    })


# --------------------------------------------------
# Local development
# --------------------------------------------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
