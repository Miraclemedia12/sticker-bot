import asyncio
import json
import logging
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("stickerbot")

# ---------------------------------------------------------------------------
# SETTINGS (set these in Render -> Environment, NEVER write them in the code)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]            # from BotFather
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # your Telegram user id
MONGO_URI = os.environ.get("MONGO_URI")        # optional, keeps users forever
PORT = int(os.environ.get("PORT", "10000"))    # Render sets this itself

MAX_STATIC = 100 * 1024     # WhatsApp: static sticker <= 100 KB
MAX_ANIMATED = 500 * 1024   # WhatsApp: animated sticker <= 500 KB
MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram bot download limit

# Only 2 conversions at the same time (Render free has small memory)
SEM = asyncio.Semaphore(2)


# ---------------------------------------------------------------------------
# USERS STORAGE (for broadcast)
# ---------------------------------------------------------------------------
class Users:
    def __init__(self):
        self.ids = set()
        self.col = None
        self.path = Path("users.json")
        if MONGO_URI:
            from pymongo import MongoClient

            self.col = MongoClient(MONGO_URI)["stickerbot"]["users"]
            self.ids = {d["_id"] for d in self.col.find({}, {"_id": 1})}
            log.info("Loaded %d users from MongoDB", len(self.ids))
        elif self.path.exists():
            self.ids = set(json.loads(self.path.read_text()))
            log.warning("No MONGO_URI set: users saved in users.json (lost on redeploy)")

    def _save_file(self):
        if not self.col:
            self.path.write_text(json.dumps(sorted(self.ids)))

    def add(self, uid: int):
        if uid in self.ids:
            return
        self.ids.add(uid)
        if self.col:
            self.col.update_one({"_id": uid}, {"$set": {"_id": uid}}, upsert=True)
        else:
            self._save_file()

    def remove(self, uid: int):
        self.ids.discard(uid)
        if self.col:
            self.col.delete_one({"_id": uid})
        else:
            self._save_file()

    def all(self):
        return list(self.ids)


users = Users()


# ---------------------------------------------------------------------------
# KEEP-ALIVE WEB SERVER (UptimeRobot pings this)
# ---------------------------------------------------------------------------
class Ping(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"Sticker bot is running")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass


def run_web():
    HTTPServer(("0.0.0.0", PORT), Ping).serve_forever()


# ---------------------------------------------------------------------------
# CONVERSION
# ---------------------------------------------------------------------------
def fit_canvas(img: Image.Image) -> Image.Image:
    """Put image inside a 512x512 transparent square (no stretching)."""
    img = img.convert("RGBA")
    ratio = min(512 / img.width, 512 / img.height)
    size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
    img = img.resize(size, Image.LANCZOS)
    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    canvas.paste(img, ((512 - size[0]) // 2, (512 - size[1]) // 2), img)
    return canvas


def make_static(src: Path, dst: Path):
    with Image.open(src) as im:
        canvas = fit_canvas(im)
    for q in (95, 85, 75, 65, 50, 35, 20):
        canvas.save(dst, "WEBP", quality=q, method=6)
        if dst.stat().st_size <= MAX_STATIC:
            return
    raise ValueError("Image is too complex to fit WhatsApp size limit.")


async def make_animated(src: Path, dst: Path):
    """Video / GIF / webm sticker -> animated WebP 512x512 using ffmpeg."""
    is_webm = src.suffix == ".webm"
    for fps, q in ((15, 60), (15, 45), (12, 35), (10, 25), (8, 15)):
        vf = (
            f"fps={fps},"
            "scale=512:512:force_original_aspect_ratio=decrease,"
            "format=rgba,"
            "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000"
        )
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        if is_webm:
            cmd += ["-c:v", "libvpx-vp9"]  # keeps transparency of video stickers
        cmd += [
            "-i", str(src),
            "-t", "8",
            "-vf", vf,
            "-an",
            "-c:v", "libwebp",
            "-lossless", "0",
            "-q:v", str(q),
            "-compression_level", "6",
            "-loop", "0",
            str(dst),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            proc.kill()
            raise ValueError("Conversion took too long.")
        if proc.returncode != 0:
            log.error("ffmpeg error: %s", err.decode(errors="ignore"))
            raise ValueError("Could not read this video.")
        if dst.stat().st_size <= MAX_ANIMATED:
            return
    raise ValueError("Video is too big for a WhatsApp sticker. Send a shorter one.")


def tgs_to_webp(src: Path, dst: Path):
    """Telegram animated sticker (.tgs) -> animated WebP."""
    try:
        from rlottie_python import LottieAnimation
    except ImportError:
        raise ValueError("Animated .tgs stickers are not supported on this server.")

    anim = LottieAnimation.from_tgs(str(src))
    total = anim.lottie_animation_get_totalframe()
    rate = anim.lottie_animation_get_framerate() or 30
    step = max(1, round(rate / 15))
    idx = list(range(0, total, step))[:120]
    frames = [
        anim.render_pillow_frame(frame_num=i, width=512, height=512) for i in idx
    ]
    duration = int(1000 * step / rate)
    for q in (60, 45, 35, 25, 15):
        frames[0].save(
            dst,
            "WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=0,
            quality=q,
            method=4,
        )
        if dst.stat().st_size <= MAX_ANIMATED:
            return
    raise ValueError("This sticker is too big to convert.")


# ---------------------------------------------------------------------------
# HANDLERS
# ---------------------------------------------------------------------------
WELCOME = (
    "👋 Welcome to the WhatsApp Sticker Maker!\n\n"
    "Send me any of these:\n"
    "🖼 A picture -> normal sticker\n"
    "🎬 A short video or GIF -> moving sticker\n"
    "😀 A Telegram sticker (even moving ones) -> WhatsApp sticker\n\n"
    "I send back a .webp file. Tap the file -> Share -> WhatsApp -> pick a chat.\n"
    "Tip: for a pack you keep, open the file in a free 'Sticker Maker' app on your phone."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.add(update.effective_user.id)
    await update.message.reply_text(WELCOME)


async def text_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.add(update.effective_user.id)
    await update.message.reply_text(
        "Send me a picture, video, GIF or Telegram sticker and I will make a WhatsApp sticker 😊"
    )


def detect(msg):
    """Return (file_object, mode, extension) or None."""
    if msg.photo:
        return msg.photo[-1], "static", ".jpg"
    if msg.sticker:
        s = msg.sticker
        if s.is_animated:
            return s, "tgs", ".tgs"
        if s.is_video:
            return s, "anim", ".webm"
        return s, "static", ".webp"
    if msg.animation:
        return msg.animation, "anim", ".mp4"
    if msg.video:
        return msg.video, "anim", ".mp4"
    if msg.video_note:
        return msg.video_note, "anim", ".mp4"
    if msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime == "image/gif":
            return msg.document, "anim", ".gif"
        if mime.startswith("image/"):
            return msg.document, "static", ".img"
        if mime.startswith("video/"):
            return msg.document, "anim", ".mp4"
    return None


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    users.add(update.effective_user.id)

    found = detect(msg)
    if not found:
        await msg.reply_text("Sorry, send a picture, video, GIF or sticker 🙏")
        return
    file_obj, mode, ext = found

    if (getattr(file_obj, "file_size", 0) or 0) > MAX_DOWNLOAD:
        await msg.reply_text("File is too big (max 20MB). Send a smaller one.")
        return

    wait = await msg.reply_text("⏳ Making your sticker...")
    await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)

    async with SEM:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / f"input{ext}"
            dst = Path(tmp) / "sticker.webp"
            try:
                tg_file = await file_obj.get_file()
                await tg_file.download_to_drive(str(src))

                if mode == "static":
                    await asyncio.to_thread(make_static, src, dst)
                elif mode == "tgs":
                    await asyncio.to_thread(tgs_to_webp, src, dst)
                else:
                    await make_animated(src, dst)

                with open(dst, "rb") as f:
                    await msg.reply_document(
                        document=f,
                        filename="sticker.webp",
                        caption=(
                            "✅ Done! Tap this file -> Share -> WhatsApp.\n"
                            "Or open it in a Sticker Maker app to save it in your pack."
                        ),
                    )
                await wait.delete()
            except ValueError as e:
                await wait.edit_text(f"❌ {e}")
            except Exception:
                log.exception("conversion failed")
                await wait.edit_text("❌ Something went wrong. Try another file.")


# ---------------------------------------------------------------------------
# ADMIN: BROADCAST + STATS
# ---------------------------------------------------------------------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"👥 Total users: {len(users.all())}")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = update.message
    reply = msg.reply_to_message
    parts = (msg.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else None

    if not reply and not text:
        await msg.reply_text(
            "Usage:\n/broadcast your message here\n"
            "or reply to any message (photo, video...) with /broadcast"
        )
        return

    ids = users.all()
    status = await msg.reply_text(f"📢 Sending to {len(ids)} users...")
    ok = failed = 0

    async def send(uid):
        if reply:
            await reply.copy(chat_id=uid)
        else:
            await context.bot.send_message(uid, text)

    for uid in ids:
        try:
            await send(uid)
            ok += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await send(uid)
                ok += 1
            except Exception:
                failed += 1
        except Forbidden:  # user blocked the bot
            users.remove(uid)
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(f"✅ Broadcast finished\nSent: {ok}\nFailed: {failed}")


# ---------------------------------------------------------------------------
def main():
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", stats))

    media = (
        filters.PHOTO
        | filters.Sticker.ALL
        | filters.ANIMATION
        | filters.VIDEO
        | filters.VIDEO_NOTE
        | filters.Document.ALL
    ) & ~filters.COMMAND
    app.add_handler(MessageHandler(media, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_msg))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
