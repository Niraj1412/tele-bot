#!/usr/bin/env python3
"""
Telegram -> WhatsApp sticker exporter bot.
- When you send a sticker from any sticker set, the bot finds the set name,
  downloads the entire set, splits into chunks of <=30, converts to .wastickers,
  and sends the .wastickers files back to the user.

- Also accepts t.me/addstickers/PackName links.

Requirements:
- Python 3.9+
- sticker-convert CLI installed and on PATH
- ffmpeg installed (for animated stickers)
- pip: python-telegram-bot, python-dotenv, telethon (optional)
"""

import os
import sys
import shutil
import tempfile
import logging
import subprocess
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Load environment
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN environment variable is required", file=sys.stderr)
    sys.exit(2)

# Optional: Telethon credentials for private pack download support
TELETHON_API_ID = os.getenv("TELETHON_API_ID")
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH")
USE_TELETHON = bool(TELETHON_API_ID and TELETHON_API_HASH)

# sticker-convert binary (allow override, but fallback to PATH if override missing)
def _resolve_sticker_convert_bin():
    override = os.getenv("STICKER_CONVERT_BIN")
    if override:
        if Path(override).exists():
            return override
        # logger not yet defined; use stderr for this early warning
        print(f"sticker-convert binary not found at {override}; falling back to PATH lookup", file=sys.stderr)
    found = shutil.which("sticker-convert")
    if found:
        return found
    # keep last resort name so error messages are clear if it truly is missing
    return "sticker-convert"

STICKER_CONVERT_BIN = _resolve_sticker_convert_bin()

# Maximum stickers per WhatsApp pack
MAX_PER_PACK = int(os.getenv("MAX_PER_PACK", "30"))

# Timeout for subprocesses (seconds)
CMD_TIMEOUT = int(os.getenv("CMD_TIMEOUT", "600"))

# sticker-convert tuning
CONVERT_PROCESSES = int(os.getenv("STICKER_CONVERT_PROCESSES", "0"))  # 0 => let sticker-convert decide
CONVERT_STEPS = os.getenv("STICKER_CONVERT_STEPS")  # optional override

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)
logger = logging.getLogger("tg-sticker-exporter")

def sticker_convert_supports(flag: str) -> bool:
    """Return True if the installed sticker-convert binary advertises a CLI flag."""
    try:
        res = subprocess.run([STICKER_CONVERT_BIN, "--help"], capture_output=True, text=True)
        if res.returncode != 0:
            return False
        return flag in (res.stdout or "")
    except FileNotFoundError:
        logger.error("sticker-convert binary not found at %s", STICKER_CONVERT_BIN)
    except Exception as e:
        logger.warning("Could not inspect sticker-convert flags: %s", e)
    return False


HAS_DOWNLOAD_TELETHON_FLAG = sticker_convert_supports("--download-telegram-telethon")
if USE_TELETHON and not HAS_DOWNLOAD_TELETHON_FLAG:
    logger.warning(
        "TELETHON_API_ID/HASH provided but sticker-convert does not support "
        "--download-telegram-telethon. Falling back to normal download; private packs may fail."
    )

# Helper: run command list and return (rc, stdout, stderr)
def run_cmd(cmd_list, cwd=None, timeout=CMD_TIMEOUT):
    logger.info("Run: %s", " ".join(cmd_list))
    try:
        res = subprocess.run(cmd_list, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        logger.debug("stdout: %s", (res.stdout or "")[:2000])
        if res.returncode != 0:
            logger.warning("Exit %s stderr: %s", res.returncode, (res.stderr or "")[:2000])
        return res.returncode, (res.stdout or ""), (res.stderr or "")
    except subprocess.TimeoutExpired as e:
        logger.error("Command timeout: %s", e)
        return 124, "", f"timeout: {e}"

# Extract pack name from a t.me link or plain text
def extract_pack_name_from_text(text: str):
    text = (text or "").strip()
    if "t.me/addstickers/" in text:
        return text.split("/")[-1].strip()
    # if user provided plain pack name
    if text and all(ch.isalnum() or ch in "_-." for ch in text):
        return text
    return None

# /start handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a sticker (from any sticker pack) and I'll export the whole pack "
        "into WhatsApp-compatible .wastickers files (chunks of <=30). "
        "You can also send a pack link like https://t.me/addstickers/PackName."
    )

# Text handler for links or pack names
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    pack_name = extract_pack_name_from_text(text)
    if not pack_name:
        await update.message.reply_text("Send a sticker from the pack or a t.me/addstickers/PackName link.")
        return
    await update.message.reply_text(f"Received pack name/link: `{pack_name}` — processing...", parse_mode="Markdown")
    await process_pack(update, context, pack_name)

# Sticker handler: when user sends a sticker message
async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sticker = update.message.sticker
        if not sticker:
            await update.message.reply_text("I couldn't find a sticker in your message. Please send one from the pack.")
            return

        set_name = getattr(sticker, "set_name", None)
        if not set_name:
            # fallback: try sticker.set_name may be None in some edge cases
            await update.message.reply_text(
                "Couldn't determine sticker set name from that sticker. "
                "Please send the sticker directly from the original pack (not a forwarded copy), "
                "or send the pack link."
            )
            return

        await update.message.reply_text(f"Detected sticker pack `{set_name}` — processing into WhatsApp chunks...", parse_mode="Markdown")
        await process_pack(update, context, set_name)

    except Exception as e:
        logger.exception("Error in sticker handler: %s", e)
        await update.message.reply_text("An error occurred while handling your sticker. See logs.")

# Core: download pack, chunk, convert, send
async def process_pack(update: Update, context: ContextTypes.DEFAULT_TYPE, pack_name: str):
    """
    Downloads sticker pack via sticker-convert, splits into <=MAX_PER_PACK,
    converts chunk to .wastickers via sticker-convert and sends resulting files.
    """
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("User %s requested pack %s", user.id if user else None, pack_name)

    temp_root = Path(tempfile.mkdtemp(prefix=f"pack_{pack_name}_"))
    logger.info("Workspace created: %s", temp_root)

    try:
        full_dir = temp_root / "full_pack"
        full_dir.mkdir(parents=True, exist_ok=True)

        # Build download command
        download_cmd = [STICKER_CONVERT_BIN]
        # if pack is private and env has telethon credentials, use telethon flag (if supported)
        if USE_TELETHON and HAS_DOWNLOAD_TELETHON_FLAG:
            download_cmd += ["--download-telegram-telethon"]
        download_cmd += [
            "--download-telegram", f"https://t.me/addstickers/{pack_name}",
            "--telegram-token", BOT_TOKEN,
            "--no-compress",
            "--output-dir", str(full_dir)
        ]

        rc, out, err = run_cmd(download_cmd, cwd=temp_root)
        if rc != 0:
            # Inform user - include helpful hint
            await update.message.reply_text(
                "Failed to download sticker pack. It might be private or non-existent. "
                "If it's private, make sure TELETHON_API_ID and TELETHON_API_HASH are set in the bot env "
                "and that your sticker-convert build supports Telethon downloads."
            )
            logger.error("Download command failed: %s", err)
            return

        # list sticker files (common extensions, include .webm for video stickers)
        sticker_files = sorted([p for p in full_dir.iterdir() if p.suffix.lower() in (".webp", ".tgs", ".png", ".webm")])
        total = len(sticker_files)
        logger.info("Found %s sticker files in pack %s", total, pack_name)

        if total == 0:
            await update.message.reply_text("No sticker files found after download. Aborting.")
            return

        await update.message.reply_text(f"Downloaded {total} stickers — splitting into chunks of ≤{MAX_PER_PACK} and converting...")

        output_wastickers = []
        chunk_index = 0
        for i in range(0, total, MAX_PER_PACK):
            chunk_index += 1
            chunk = sticker_files[i:i + MAX_PER_PACK]
            chunk_dir = temp_root / f"chunk_{chunk_index}"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            for s in chunk:
                target = chunk_dir / s.name
                try:
                    os.link(s, target)
                except Exception:
                    shutil.copy(s, target)

            out_dir = temp_root / f"output_{chunk_index}"
            out_dir.mkdir(parents=True, exist_ok=True)

            convert_cmd = [
                STICKER_CONVERT_BIN,
                "--input-dir", str(chunk_dir),
                "--preset", "whatsapp",
                "--export-whatsapp",
                "--output-dir", str(out_dir),
                "--title", f"{pack_name} - part {chunk_index}",
                "--author", "Converted Bot"
            ]
            if CONVERT_PROCESSES > 0:
                convert_cmd += ["--processes", str(CONVERT_PROCESSES)]
            if CONVERT_STEPS:
                convert_cmd += ["--steps", str(CONVERT_STEPS)]

            rc2, o2, e2 = run_cmd(convert_cmd, cwd=temp_root)
            if rc2 != 0:
                logger.error("Conversion failed for chunk %s: %s", chunk_index, e2)
                await update.message.reply_text(f"Conversion failed for chunk {chunk_index}. Continuing with other chunks.")
                continue

            # find any .wastickers file (sticker-convert may produce .wastickers or a zip)
            found = list(out_dir.glob("*.wastickers"))
            if not found:
                # fallback: rename .zip to .wastickers if present
                zips = list(out_dir.glob("*.zip"))
                for z in zips:
                    target = out_dir / (z.stem + ".wastickers")
                    try:
                        z.rename(target)
                        found.append(target)
                    except Exception:
                        pass

            if found:
                logger.info("Created export: %s", found[0])
                output_wastickers.append(found[0])
            else:
                logger.warning("No .wastickers produced for chunk %s", chunk_index)

        if not output_wastickers:
            await update.message.reply_text("No .wastickers files were produced. Conversion likely failed.")
            return

        # Send results back
        for pf in output_wastickers:
            try:
                # open file in binary and send
                with open(pf, "rb") as fh:
                    await context.bot.send_document(chat_id=chat_id, document=fh, filename=pf.name)
            except Exception as send_err:
                logger.exception("Failed to send %s: %s", pf, send_err)
                await update.message.reply_text(f"Failed to send file {pf.name}")

        await update.message.reply_text("All done — download the .wastickers on your phone and open them in your app to import to WhatsApp.")
        logger.info("Completed processing for pack %s (user=%s)", pack_name, user.id if user else None)

    except Exception as e:
        logger.exception("Unexpected error in process_pack: %s", e)
        await update.message.reply_text(f"An unexpected error occurred: {e}")
    finally:
        # Cleanup workspace
        try:
            shutil.rmtree(temp_root)
            logger.debug("Removed tempdir %s", temp_root)
        except Exception:
            logger.debug("Could not remove tempdir %s", temp_root)

# Build and run the bot
def build_application():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    # Text handler for links/names
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Sticker handler
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    return app

def main():
    app = build_application()
    logger.info("Starting bot (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
