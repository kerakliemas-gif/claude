#!/usr/bin/env python3
"""English-Uzbek Vocabulary Quiz Bot — Per-user data, pagination, safe callbacks, HTML mode"""

import json
import random
import os
import re
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# ──────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_DATA_DIR = Path(__file__).parent / "user_data"
USER_DATA_DIR.mkdir(exist_ok=True)

UNITS_PER_PAGE = 8

# Direction codes
DIR_EN2UZ = "e"
DIR_UZ2EN = "u"
DIR_RANDOM = "r"

# Mode codes
MODE_CHOICE = "c"
MODE_WRITE  = "w"

# ──────────────────────────────────────
#  HTML ESCAPE HELPER
# ──────────────────────────────────────
def esc(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

# ──────────────────────────────────────
#  PER-USER DATA  (word - translation format)
# ──────────────────────────────────────
def user_file(user_id: int) -> Path:
    return USER_DATA_DIR / f"{user_id}.json"

def load_data(user_id: int) -> dict:
    f = user_file(user_id)
    if f.exists():
        data = json.loads(f.read_text())
        if isinstance(data, list):
            return {"units": {"Default": {"words": data, "last_quizzed": None}}}
        return data
    return {"units": {}}

def save_data(user_id: int, data: dict):
    user_file(user_id).write_text(json.dumps(data, ensure_ascii=False, indent=2))

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

# ──────────────────────────────────────
#  WORD POOL HELPERS
# ──────────────────────────────────────
def get_all_words(data: dict) -> list[dict]:
    all_w = []
    for name, unit in data["units"].items():
        for w in unit["words"]:
            all_w.append({**w, "_unit": name})
    return all_w

def get_pool(data: dict, label: str) -> list[dict]:
    if label == "__all__":
        return get_all_words(data)
    if label in data["units"]:
        return list(data["units"][label]["words"])
    return []

# ──────────────────────────────────────
#  SAFE CALLBACK INDEX HELPERS
#  Unit names are NEVER put in callback_data.
#  Instead we store a unit-index list in user_data
#  and pass the integer index.
# ──────────────────────────────────────
def get_unit_index(ctx, user_id: int, data: dict) -> list[str]:
    """Return (or rebuild) the ordered list of unit names for safe index lookups."""
    idx = ctx.user_data.get("unit_index", [])
    names = list(data["units"].keys())
    # Rebuild if stale
    if set(idx) != set(names):
        ctx.user_data["unit_index"] = names
        idx = names
    return idx

def unit_by_idx(ctx, i: int) -> str | None:
    idx = ctx.user_data.get("unit_index", [])
    if 0 <= i < len(idx):
        return idx[i]
    return None

def idx_of_unit(ctx, name: str) -> int:
    idx = ctx.user_data.get("unit_index", [])
    try:
        return idx.index(name)
    except ValueError:
        return -1

# ──────────────────────────────────────
#  QUIZ SESSION STATE
# ──────────────────────────────────────
def init_quiz(ctx, label, pool):
    ctx.user_data[f"q_asked_{label}"] = []
    ctx.user_data[f"q_correct_{label}"] = 0
    ctx.user_data[f"q_wrong_{label}"] = 0
    ctx.user_data[f"q_wrong_list_{label}"] = []
    ctx.user_data[f"q_total_{label}"] = len(pool)

def get_remaining(ctx, label, pool):
    asked = set(ctx.user_data.get(f"q_asked_{label}", []))
    return [w for w in pool if w["word"] not in asked]

def mark_asked(ctx, label, word):
    asked = ctx.user_data.get(f"q_asked_{label}", [])
    asked.append(word)
    ctx.user_data[f"q_asked_{label}"] = asked

def record_answer(ctx, label, is_correct, word=None, correct_answer=None):
    if is_correct:
        ctx.user_data[f"q_correct_{label}"] = ctx.user_data.get(f"q_correct_{label}", 0) + 1
    else:
        ctx.user_data[f"q_wrong_{label}"] = ctx.user_data.get(f"q_wrong_{label}", 0) + 1
        wl = ctx.user_data.get(f"q_wrong_list_{label}", [])
        if word and correct_answer:
            wl.append({"word": word, "answer": correct_answer})
        ctx.user_data[f"q_wrong_list_{label}"] = wl

def get_progress(ctx, label):
    asked = len(ctx.user_data.get(f"q_asked_{label}", []))
    total = ctx.user_data.get(f"q_total_{label}", 0)
    return asked, total

def build_results(ctx, label):
    correct   = ctx.user_data.get(f"q_correct_{label}", 0)
    wrong     = ctx.user_data.get(f"q_wrong_{label}", 0)
    total     = correct + wrong
    wrong_list = ctx.user_data.get(f"q_wrong_list_{label}", [])

    if total == 0:
        return "No questions answered."

    pct = round(correct / total * 100)

    if pct == 100:
        emoji, msg = "🏆", "Perfect score!"
    elif pct >= 80:
        emoji, msg = "🌟", "Great job!"
    elif pct >= 60:
        emoji, msg = "👍", "Good effort!"
    else:
        emoji, msg = "💪", "Keep practicing!"

    text = f"{emoji} <b>Results: {msg}</b>\n\n"
    text += f"✅ Correct: {correct}/{total} ({pct}%)\n"
    text += f"❌ Wrong: {wrong}/{total}\n"

    if wrong_list:
        text += "\n📝 <b>Words to review:</b>\n"
        for item in wrong_list:
            # Use " - " (minus) as separator
            text += f"• <b>{esc(item['word'])}</b> - {esc(item['answer'])}\n"

    return text

# ──────────────────────────────────────
#  MAIN MENU
# ──────────────────────────────────────
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Units",    callback_data="m:units"),
         InlineKeyboardButton("🎯 Quiz",     callback_data="m:quiz")],
        [InlineKeyboardButton("📊 Stats",    callback_data="m:stats"),
         InlineKeyboardButton("➕ New Unit", callback_data="m:newunit")],
        [InlineKeyboardButton("🗑 Reset All Data", callback_data="m:resetconfirm")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("awaiting", None)
    ctx.user_data.pop("write_quiz", None)
    uid = (update.effective_user or update.callback_query.from_user).id
    data = load_data(uid)
    total = sum(len(u["words"]) for u in data["units"].values())
    text = (
        "🇬🇧➡️🇺🇿 <b>Vocabulary Quiz Bot</b>\n\n"
        f"📊 {len(data['units'])} units, {total} words\n\n"
        "Choose an option:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_kb())

# ──────────────────────────────────────
#  UNITS LIST  (with pagination)
# ──────────────────────────────────────
async def units_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    uid   = query.from_user.id
    data  = load_data(uid)

    # Rebuild unit index
    unit_names = list(data["units"].keys())
    ctx.user_data["unit_index"] = unit_names

    if not unit_names:
        kb = [
            [InlineKeyboardButton("➕ New Unit", callback_data="m:newunit")],
            [InlineKeyboardButton("⬅️ Back",    callback_data="m:main")],
        ]
        await query.edit_message_text("📭 No units yet.", reply_markup=InlineKeyboardMarkup(kb))
        return

    total_pages = max(1, (len(unit_names) + UNITS_PER_PAGE - 1) // UNITS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_i = page * UNITS_PER_PAGE
    page_names = unit_names[start_i: start_i + UNITS_PER_PAGE]

    buttons = []
    for name in page_names:
        i     = unit_names.index(name)
        words = len(data["units"][name]["words"])
        buttons.append([InlineKeyboardButton(
            f"📁 {name} ({words})",
            callback_data=f"unit:{i}"
        )])

    # Pagination row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"upg:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️",    callback_data=f"upg:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ New Unit", callback_data="m:newunit")])
    buttons.append([InlineKeyboardButton("⬅️ Back",    callback_data="m:main")])

    header = f"📁 <b>Your Units</b>  (page {page+1}/{total_pages})"
    await query.edit_message_text(header, parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup(buttons))

# ──────────────────────────────────────
#  SINGLE UNIT VIEW
# ──────────────────────────────────────
async def unit_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    raw   = query.data.split(":", 1)[1]

    # Accept both "unit:<index>" (new) and "unit:<name>" (legacy fallback)
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)  # ensure index is fresh

    if raw.lstrip("-").isdigit():
        i    = int(raw)
        name = unit_by_idx(ctx, i)
    else:
        # Legacy: name in callback (migration path)
        name = raw
        i    = idx_of_unit(ctx, name)

    if not name or name not in data["units"]:
        await query.edit_message_text("❌ Unit not found")
        return

    unit = data["units"][name]
    wc   = len(unit["words"])
    last = unit.get("last_quizzed") or "never"

    if unit["words"]:
        # " - " (minus) separator
        word_lines = "\n".join(
            f"• {esc(w['word'])} - {esc(w['meaning'])}"
            for w in sorted(unit["words"], key=lambda x: x["word"])
        )
    else:
        word_lines = "(empty)"

    text = f"📁 <b>{esc(name)}</b>\n📊 {wc} words | Last quiz: {esc(last)}\n\n{word_lines}"
    if len(text) > 4000:
        text = text[:3990] + "\n..."

    kb = [
        [InlineKeyboardButton("🎯 Quiz",       callback_data=f"qpick:{i}"),
         InlineKeyboardButton("📝 Add Words",  callback_data=f"addunit:{i}")],
        [InlineKeyboardButton("✏️ Rename",     callback_data=f"ren:{i}"),
         InlineKeyboardButton("🗑 Delete",     callback_data=f"delc:{i}")],
        [InlineKeyboardButton("🧹 Clear Words",callback_data=f"clrc:{i}"),
         InlineKeyboardButton("📤 Export Unit",callback_data=f"expunit:{i}")],
        [InlineKeyboardButton("⬅️ Back",       callback_data="m:units")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  NEW UNIT
# ──────────────────────────────────────
async def newunit_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.edit_message_text("📁 Send the name for the new unit:")
    ctx.user_data["awaiting"] = "newunit_name"

async def newunit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /newunit Name")
        return
    name = " ".join(ctx.args).strip()
    uid  = update.effective_user.id
    data = load_data(uid)
    if name in data["units"]:
        await update.message.reply_text(f"❌ '{esc(name)}' already exists", parse_mode="HTML")
        return
    data["units"][name] = {"words": [], "last_quizzed": None}
    save_data(uid, data)
    await update.message.reply_text(
        f"✅ Created <b>{esc(name)}</b>", parse_mode="HTML", reply_markup=main_menu_kb())

# ──────────────────────────────────────
#  RENAME
# ──────────────────────────────────────
async def rename_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    raw      = query.data.split(":", 1)[1]
    uid      = query.from_user.id
    data     = load_data(uid)
    get_unit_index(ctx, uid, data)
    name     = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if not name:
        await query.edit_message_text("❌ Unit not found")
        return
    ctx.user_data["awaiting"]     = "rename"
    ctx.user_data["rename_unit"]  = name
    await query.edit_message_text(
        f"✏️ Send new name for <b>{esc(name)}</b>:", parse_mode="HTML")

async def rename_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if "|" not in text:
        await update.message.reply_text("Usage: /rename Old | New")
        return
    old, new = [s.strip() for s in text.split("|", 1)]
    uid  = update.effective_user.id
    data = load_data(uid)
    if old not in data["units"]:
        await update.message.reply_text(f"❌ '{esc(old)}' not found", parse_mode="HTML")
        return
    if new in data["units"]:
        await update.message.reply_text(f"❌ '{esc(new)}' already exists", parse_mode="HTML")
        return
    data["units"][new] = data["units"].pop(old)
    save_data(uid, data)
    await update.message.reply_text(
        f"✅ <b>{esc(old)}</b> → <b>{esc(new)}</b>", parse_mode="HTML")

# ──────────────────────────────────────
#  DELETE / CLEAR
# ──────────────────────────────────────
async def del_confirm(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    name  = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if not name:
        await query.edit_message_text("❌ Unit not found")
        return
    wc = len(data["units"].get(name, {}).get("words", []))
    i  = idx_of_unit(ctx, name)
    kb = [[
        InlineKeyboardButton("✅ Yes",    callback_data=f"del:{i}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"unit:{i}"),
    ]]
    await query.edit_message_text(
        f"⚠️ Delete <b>{esc(name)}</b> ({wc} words)?",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def del_unit(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    name  = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if name and name in data["units"]:
        del data["units"][name]
        save_data(uid, data)
    # Rebuild index and go back to units list
    ctx.user_data["unit_index"] = list(data["units"].keys())
    await units_menu(update, ctx)

async def clear_confirm(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    name  = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if not name:
        await query.edit_message_text("❌ Unit not found")
        return
    wc = len(data["units"].get(name, {}).get("words", []))
    i  = idx_of_unit(ctx, name)
    kb = [[
        InlineKeyboardButton("✅ Yes",    callback_data=f"clr:{i}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"unit:{i}"),
    ]]
    await query.edit_message_text(
        f"⚠️ Clear all {wc} words from <b>{esc(name)}</b>?",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def clear_unit(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    name  = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if name and name in data["units"]:
        data["units"][name]["words"] = []
        save_data(uid, data)
    query.data = f"unit:{idx_of_unit(ctx, name)}"
    await unit_view(update, ctx)

# ──────────────────────────────────────
#  EXPORT SINGLE UNIT
# ──────────────────────────────────────
async def export_unit(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    name  = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if not name or name not in data["units"]:
        await query.edit_message_text("❌ Unit not found")
        return

    words = data["units"][name]["words"]
    export_data = {name: [{"word": w["word"], "meaning": w["meaning"]} for w in words]}
    json_bytes  = json.dumps(export_data, ensure_ascii=False, indent=2).encode("utf-8")

    safe_name = re.sub(r'[^\w\s-]', '_', name).strip()
    filename  = f"{safe_name}.json"

    await query.message.reply_document(
        document=InputFile(BytesIO(json_bytes), filename=filename),
        caption=f"📤 <b>{esc(name)}</b> — {len(words)} words exported",
        parse_mode="HTML",
    )

# ──────────────────────────────────────
#  RESET ALL
# ──────────────────────────────────────
async def reset_confirm(update, ctx):
    query = update.callback_query
    uid   = query.from_user.id
    data  = load_data(uid)
    total = sum(len(u["words"]) for u in data["units"].values())
    kb = [
        [InlineKeyboardButton("⚠️ YES, DELETE EVERYTHING", callback_data="m:resetall")],
        [InlineKeyboardButton("❌ Cancel",                  callback_data="m:main")],
    ]
    await query.edit_message_text(
        f"🚨 <b>Delete ALL data?</b>\n\n{len(data['units'])} units, {total} words gone forever.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def reset_all(update, ctx):
    query = update.callback_query
    uid   = query.from_user.id
    save_data(uid, {"units": {}})
    ctx.user_data["unit_index"] = []
    await start(update, ctx)

# ──────────────────────────────────────
#  STATS
# ──────────────────────────────────────
async def stats(update, ctx):
    query = update.callback_query
    uid   = query.from_user.id
    data  = load_data(uid)
    total = sum(len(u["words"]) for u in data["units"].values())
    lines = []
    for name, unit in data["units"].items():
        last = unit.get("last_quizzed") or "never"
        lines.append(f"• <b>{esc(name)}</b>: {len(unit['words'])} words (quiz: {esc(last)})")
    text  = f"📊 <b>Stats</b>\n\n🗂 {len(data['units'])} units | 📝 {total} words\n\n"
    text += "\n".join(lines) if lines else "No data yet"
    kb    = [[InlineKeyboardButton("⬅️ Back", callback_data="m:main")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  QUIZ — Unit picker
# ──────────────────────────────────────
async def quiz_menu(update, ctx):
    query = update.callback_query
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    total = sum(len(u["words"]) for u in data["units"].values())
    if total < 4:
        kb = [[InlineKeyboardButton("⬅️ Back", callback_data="m:main")]]
        await query.edit_message_text(
            f"📝 Need at least 4 words. You have {total}.",
            reply_markup=InlineKeyboardMarkup(kb))
        return

    unit_names = list(data["units"].keys())
    buttons    = []
    if total >= 4:
        buttons.append([InlineKeyboardButton(
            f"🎯 All Words ({total})", callback_data="qpick:__all__")])
    for i, name in enumerate(unit_names):
        wc = len(data["units"][name]["words"])
        if wc >= 4:
            buttons.append([InlineKeyboardButton(
                f"📁 {name} ({wc})", callback_data=f"qpick:{i}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="m:main")])
    await query.edit_message_text(
        "🎯 <b>Choose what to quiz:</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

# ──────────────────────────────────────
#  QUIZ — Direction picker
# ──────────────────────────────────────
async def quiz_direction_picker(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)

    if raw == "__all__":
        label = "__all__"
        pool  = get_all_words(data)
    else:
        get_unit_index(ctx, uid, data)
        i    = int(raw)
        name = unit_by_idx(ctx, i)
        if not name:
            await query.edit_message_text("❌ Unit not found")
            return
        label = str(i)   # use index as quiz label
        pool  = list(data["units"][name]["words"])

    ctx.user_data["quiz_label_name"] = "__all__" if raw == "__all__" else name

    # Reset any previous quiz session for this label so "Retry"/restart works
    # (a finished quiz leaves all words marked as asked; clear it here).
    init_quiz(ctx, label, pool)

    kb = [
        [InlineKeyboardButton("🇬🇧 → 🇺🇿", callback_data=f"qmode:{label}:{DIR_EN2UZ}")],
        [InlineKeyboardButton("🇺🇿 → 🇬🇧", callback_data=f"qmode:{label}:{DIR_UZ2EN}")],
        [InlineKeyboardButton("🔀 Random",   callback_data=f"qmode:{label}:{DIR_RANDOM}")],
        [InlineKeyboardButton("⬅️ Back",     callback_data="m:quiz")],
    ]
    unit_label = "All Words" if raw == "__all__" else esc(ctx.user_data["quiz_label_name"])
    await query.edit_message_text(
        f"🎯 <b>Quiz: {unit_label}</b> ({len(pool)} words)\n\nChoose direction:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  QUIZ — Mode picker
# ──────────────────────────────────────
async def quiz_mode_picker(update, ctx):
    query  = update.callback_query
    parts  = query.data.split(":", 2)
    label  = parts[1]
    direction = parts[2]

    kb = [
        [InlineKeyboardButton("🔘 Multiple Choice", callback_data=f"qgo:{label}:{direction}:{MODE_CHOICE}")],
        [InlineKeyboardButton("✍️ Write Answer",    callback_data=f"qgo:{label}:{direction}:{MODE_WRITE}")],
        [InlineKeyboardButton("⬅️ Back",            callback_data=f"qpick:{label}")],
    ]
    unit_label = ctx.user_data.get("quiz_label_name", "?")
    dir_label  = {"e": "🇬🇧→🇺🇿", "u": "🇺🇿→🇬🇧", "r": "🔀 Random"}.get(direction, "?")
    await query.edit_message_text(
        f"🎯 <b>Quiz: {esc(unit_label)}</b> | {dir_label}\n\nChoose mode:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  QUIZ — Resolve label → pool
# ──────────────────────────────────────
def resolve_quiz_pool(uid, label, ctx, data):
    """Return (pool, display_name). label is '__all__' or a unit index string."""
    if label == "__all__":
        return get_all_words(data), "All Words"
    i    = int(label)
    name = unit_by_idx(ctx, i)
    if not name or name not in data["units"]:
        return [], "?"
    return list(data["units"][name]["words"]), name

# ──────────────────────────────────────
#  QUIZ — Start / Next question
# ──────────────────────────────────────
async def do_quiz(update, ctx):
    query  = update.callback_query
    uid    = query.from_user.id
    parts  = query.data.split(":", 3)
    label  = parts[1]
    direction = parts[2] if len(parts) > 2 else DIR_RANDOM
    mode   = parts[3] if len(parts) > 3 else MODE_CHOICE

    data = load_data(uid)
    get_unit_index(ctx, uid, data)
    pool, display_name = resolve_quiz_pool(uid, label, ctx, data)

    # Save last_quizzed
    if label != "__all__":
        name = unit_by_idx(ctx, int(label))
        if name and name in data["units"]:
            data["units"][name]["last_quizzed"] = now_str()
            save_data(uid, data)

    if mode == MODE_CHOICE and len(pool) < 4:
        await query.edit_message_text(f"📝 Need at least 4 words for choice mode. This has {len(pool)}.")
        return
    if len(pool) < 1:
        await query.edit_message_text("📝 No words in this unit.")
        return

    remaining = get_remaining(ctx, label, pool)
    if len(remaining) == len(pool):
        init_quiz(ctx, label, pool)
        remaining = pool[:]

    if not remaining:
        await show_results(query, ctx, label)
        return

    correct = random.choice(remaining)
    mark_asked(ctx, label, correct["word"])
    asked, total = get_progress(ctx, label)

    if direction == DIR_EN2UZ:
        show_english = True
    elif direction == DIR_UZ2EN:
        show_english = False
    else:
        show_english = random.choice([True, False])

    if mode == MODE_WRITE:
        if show_english:
            question = (f"✍️ <b>{asked}/{total}</b>\n\n"
                        f"🇬🇧 What does <b>{esc(correct['word'])}</b> mean?\n\n<i>Type your answer:</i>")
            expected = correct["meaning"]
        else:
            question = (f"✍️ <b>{asked}/{total}</b>\n\n"
                        f"🇺🇿 What is <b>{esc(correct['meaning'])}</b> in English?\n\n<i>Type your answer:</i>")
            expected = correct["word"]

        ctx.user_data["awaiting"]   = "write_answer"
        ctx.user_data["write_quiz"] = {
            "label": label, "direction": direction, "mode": mode,
            "correct_word": correct["word"], "correct_meaning": correct["meaning"],
            "expected": expected, "show_english": show_english,
        }
        kb = [[InlineKeyboardButton("⏭ Skip", callback_data=f"skip:{label}:{direction}:{mode}")]]
        await query.edit_message_text(question, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    else:
        # Choice mode — options use ONLY their display text in the button, answer index in callback
        others = [w for w in pool if w["word"] != correct["word"]]
        wrong  = random.sample(others, min(3, len(others)))
        options = [correct] + wrong
        random.shuffle(options)

        # Build a short option index in user_data to avoid putting words in callback
        opt_idx = {str(j): opt["word"] for j, opt in enumerate(options)}
        ctx.user_data["quiz_options"] = opt_idx
        ctx.user_data["quiz_correct"] = correct["word"]

        if show_english:
            question = (f"🔘 <b>{asked}/{total}</b>\n\n"
                        f"🇬🇧 What does <b>{esc(correct['word'])}</b> mean?")
            keyboard = [
                [InlineKeyboardButton(opt["meaning"],
                    callback_data=f"a:{j}:{label}:{direction}:{mode}")]
                for j, opt in enumerate(options)
            ]
        else:
            question = (f"🔘 <b>{asked}/{total}</b>\n\n"
                        f"🇺🇿 What is <b>{esc(correct['meaning'])}</b> in English?")
            keyboard = [
                [InlineKeyboardButton(opt["word"],
                    callback_data=f"a:{j}:{label}:{direction}:{mode}")]
                for j, opt in enumerate(options)
            ]

        await query.edit_message_text(
            question, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ──────────────────────────────────────
#  QUIZ — Choice answer  (index-based)
# ──────────────────────────────────────
async def quiz_answer(update, ctx):
    query  = update.callback_query
    parts  = query.data.split(":", 4)
    # format: a:<opt_idx>:<label>:<direction>:<mode>
    if len(parts) < 5:
        return
    opt_j     = parts[1]
    label     = parts[2]
    direction = parts[3]
    mode      = parts[4]
    uid       = query.from_user.id

    opt_map     = ctx.user_data.get("quiz_options", {})
    correct_word = ctx.user_data.get("quiz_correct", "")
    chosen_word  = opt_map.get(opt_j, "")

    data = load_data(uid)
    all_words = get_all_words(data)
    correct_obj = next((w for w in all_words if w["word"] == correct_word), None)

    is_correct = (chosen_word == correct_word)
    if correct_obj:
        record_answer(ctx, label, is_correct, correct_obj["word"], correct_obj["meaning"])

    asked, total = get_progress(ctx, label)

    if is_correct:
        text = f"✅ Correct! 🎉\n\n📊 {asked}/{total}"
    else:
        if correct_obj:
            text = (f"❌ Wrong!\n\n"
                    f"✅ <b>{esc(correct_obj['word'])}</b> - {esc(correct_obj['meaning'])}\n\n"
                    f"📊 {asked}/{total}")
        else:
            text = f"❌ Wrong!\n\n📊 {asked}/{total}"

    get_unit_index(ctx, uid, data)
    pool, _ = resolve_quiz_pool(uid, label, ctx, data)
    remaining = get_remaining(ctx, label, pool)

    if not remaining:
        text += "\n\n" + build_results(ctx, label)
        kb = [
            [InlineKeyboardButton("🔄 Retry", callback_data=f"qpick:{label}")],
            [InlineKeyboardButton("🏠 Menu",  callback_data="m:main")],
        ]
    else:
        kb = [
            [InlineKeyboardButton("➡️ Next", callback_data=f"qgo:{label}:{direction}:{mode}")],
            [InlineKeyboardButton("🏠 Menu", callback_data="m:main")],
        ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  QUIZ — Skip (write mode)
# ──────────────────────────────────────
async def quiz_skip(update, ctx):
    query  = update.callback_query
    parts  = query.data.split(":", 3)
    label  = parts[1]
    direction = parts[2] if len(parts) > 2 else DIR_RANDOM
    mode   = parts[3] if len(parts) > 3 else MODE_WRITE
    uid    = query.from_user.id

    wq = ctx.user_data.pop("write_quiz", None)
    ctx.user_data.pop("awaiting", None)

    if wq:
        record_answer(ctx, label, False, wq["correct_word"], wq["correct_meaning"])
        asked, total = get_progress(ctx, label)
        text = (f"⏭ Skipped!\n\n"
                f"✅ <b>{esc(wq['correct_word'])}</b> - {esc(wq['correct_meaning'])}\n\n"
                f"📊 {asked}/{total}")
    else:
        asked, total = get_progress(ctx, label)
        text = f"⏭ Skipped\n\n📊 {asked}/{total}"

    data = load_data(uid)
    get_unit_index(ctx, uid, data)
    pool, _ = resolve_quiz_pool(uid, label, ctx, data)
    remaining = get_remaining(ctx, label, pool)

    if not remaining:
        text += "\n\n" + build_results(ctx, label)
        kb = [
            [InlineKeyboardButton("🔄 Retry", callback_data=f"qpick:{label}")],
            [InlineKeyboardButton("🏠 Menu",  callback_data="m:main")],
        ]
    else:
        kb = [
            [InlineKeyboardButton("➡️ Next", callback_data=f"qgo:{label}:{direction}:{mode}")],
            [InlineKeyboardButton("🏠 Menu", callback_data="m:main")],
        ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  QUIZ — Final results
# ──────────────────────────────────────
async def show_results(query, ctx, label):
    text = build_results(ctx, label)
    kb = [
        [InlineKeyboardButton("🔄 Retry", callback_data=f"qpick:{label}")],
        [InlineKeyboardButton("🏠 Menu",  callback_data="m:main")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  QUIZ via /quiz command
# ──────────────────────────────────────
async def quiz_cmd(update, ctx):
    uid   = update.effective_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    label = " ".join(ctx.args).strip() if ctx.args else "__all__"

    if label != "__all__":
        # Treat as unit name — find its index
        idx = idx_of_unit(ctx, label)
        if idx < 0:
            await update.message.reply_text(f"❌ Unit '{esc(label)}' not found", parse_mode="HTML")
            return
        label = str(idx)

    pool, dname = resolve_quiz_pool(uid, label, ctx, data)
    if len(pool) < 2:
        await update.message.reply_text(f"📝 Need more words. You have {len(pool)}.")
        return

    # Reset any previous quiz session so restarting via /quiz works
    init_quiz(ctx, label, pool)

    kb = [
        [InlineKeyboardButton("🇬🇧 → 🇺🇿", callback_data=f"qmode:{label}:{DIR_EN2UZ}")],
        [InlineKeyboardButton("🇺🇿 → 🇬🇧", callback_data=f"qmode:{label}:{DIR_UZ2EN}")],
        [InlineKeyboardButton("🔀 Random",   callback_data=f"qmode:{label}:{DIR_RANDOM}")],
    ]
    ctx.user_data["quiz_label_name"] = dname
    await update.message.reply_text(
        f"🎯 <b>Quiz: {esc(dname)}</b> ({len(pool)} words)\n\nChoose direction:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

# ──────────────────────────────────────
#  ADD WORDS
# ──────────────────────────────────────
async def add_prompt_button(update, ctx):
    query = update.callback_query
    raw   = query.data.split(":", 1)[1]
    uid   = query.from_user.id
    data  = load_data(uid)
    get_unit_index(ctx, uid, data)
    name  = unit_by_idx(ctx, int(raw)) if raw.isdigit() else raw
    if not name:
        await query.edit_message_text("❌ Unit not found")
        return
    ctx.user_data["awaiting"]      = "add_words"
    ctx.user_data["adding_to_unit"] = name
    await query.edit_message_text(
        f"📝 <b>Adding to: {esc(name)}</b>\n\nSend: <code>word - meaning</code> (one per line)\n/done when finished",
        parse_mode="HTML")

async def add_cmd(update, ctx):
    if not ctx.args:
        uid   = update.effective_user.id
        data  = load_data(uid)
        units = ", ".join(data["units"].keys()) or "none"
        await update.message.reply_text(f"Usage: /add UnitName\n\nUnits: {esc(units)}", parse_mode="HTML")
        return
    name = " ".join(ctx.args).strip()
    uid  = update.effective_user.id
    data = load_data(uid)
    if name not in data["units"]:
        data["units"][name] = {"words": [], "last_quizzed": None}
        save_data(uid, data)
    ctx.user_data["awaiting"]      = "add_words"
    ctx.user_data["adding_to_unit"] = name
    await update.message.reply_text(
        f"📝 <b>Adding to: {esc(name)}</b>\n\nSend: <code>word - meaning</code> (one per line)\n/done when finished",
        parse_mode="HTML")

async def done_cmd(update, ctx):
    unit_name = ctx.user_data.pop("adding_to_unit", None)
    ctx.user_data.pop("awaiting", None)
    if unit_name:
        await update.message.reply_text(
            f"✅ Done adding to <b>{esc(unit_name)}</b>",
            parse_mode="HTML", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text("Nothing to finish.", reply_markup=main_menu_kb())

# ──────────────────────────────────────
#  TEXT MESSAGE HANDLER
# ──────────────────────────────────────
async def handle_text(update, ctx):
    awaiting = ctx.user_data.get("awaiting")
    text     = update.message.text.strip()
    uid      = update.effective_user.id

    # --- Write-mode quiz answer ---
    if awaiting == "write_answer":
        wq = ctx.user_data.get("write_quiz")
        if not wq:
            ctx.user_data.pop("awaiting", None)
            return

        ctx.user_data.pop("awaiting", None)
        ctx.user_data.pop("write_quiz", None)

        label     = wq["label"]
        direction = wq["direction"]
        mode      = wq["mode"]
        expected  = wq["expected"]

        is_correct = text.lower().strip() == expected.lower().strip()
        record_answer(ctx, label, is_correct, wq["correct_word"], wq["correct_meaning"])
        asked, total_q = get_progress(ctx, label)

        if is_correct:
            reply = f"✅ Correct! 🎉\n\n📊 {asked}/{total_q}"
        else:
            reply = (f"❌ Wrong! You wrote: <i>{esc(text)}</i>\n\n"
                     f"✅ <b>{esc(wq['correct_word'])}</b> - {esc(wq['correct_meaning'])}\n\n"
                     f"📊 {asked}/{total_q}")

        data      = load_data(uid)
        get_unit_index(ctx, uid, data)
        pool, _   = resolve_quiz_pool(uid, label, ctx, data)
        remaining = get_remaining(ctx, label, pool)

        if not remaining:
            reply += "\n\n" + build_results(ctx, label)
            kb = [
                [InlineKeyboardButton("🔄 Retry", callback_data=f"qpick:{label}")],
                [InlineKeyboardButton("🏠 Menu",  callback_data="m:main")],
            ]
        else:
            kb = [
                [InlineKeyboardButton("➡️ Next", callback_data=f"qgo:{label}:{direction}:{mode}")],
                [InlineKeyboardButton("🏠 Menu", callback_data="m:main")],
            ]

        await update.message.reply_text(reply, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return

    # --- New unit name ---
    if awaiting == "newunit_name":
        ctx.user_data.pop("awaiting")
        name = text
        data = load_data(uid)
        if name in data["units"]:
            await update.message.reply_text(
                f"❌ '{esc(name)}' already exists", parse_mode="HTML", reply_markup=main_menu_kb())
            return
        data["units"][name] = {"words": [], "last_quizzed": None}
        save_data(uid, data)
        ctx.user_data["awaiting"]      = "add_words"
        ctx.user_data["adding_to_unit"] = name
        await update.message.reply_text(
            f"✅ Created <b>{esc(name)}</b>\n\nSend words: <code>word - meaning</code>\n/done when finished",
            parse_mode="HTML")
        return

    # --- Rename ---
    if awaiting == "rename":
        ctx.user_data.pop("awaiting")
        old_name = ctx.user_data.pop("rename_unit", None)
        data     = load_data(uid)
        if not old_name or old_name not in data["units"]:
            await update.message.reply_text("❌ Unit not found", reply_markup=main_menu_kb())
            return
        if text in data["units"]:
            await update.message.reply_text(
                f"❌ '{esc(text)}' already exists", parse_mode="HTML", reply_markup=main_menu_kb())
            return
        data["units"][text] = data["units"].pop(old_name)
        save_data(uid, data)
        await update.message.reply_text(
            f"✅ <b>{esc(old_name)}</b> → <b>{esc(text)}</b>",
            parse_mode="HTML", reply_markup=main_menu_kb())
        return

    # --- Adding words ---
    if awaiting == "add_words":
        unit_name = ctx.user_data.get("adding_to_unit")
        if not unit_name:
            ctx.user_data.pop("awaiting", None)
            return
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        data  = load_data(uid)
        if unit_name not in data["units"]:
            data["units"][unit_name] = {"words": [], "last_quizzed": None}
        unit = data["units"][unit_name]
        added, updated, errors = [], [], []
        for line in lines:
            if " - " not in line:
                errors.append(line)
                continue
            p       = line.split(" - ", 1)
            word    = p[0].strip().lower()
            meaning = p[1].strip()
            if not word or not meaning:
                errors.append(line)
                continue
            found = False
            for w in unit["words"]:
                if w["word"] == word:
                    w["meaning"] = meaning
                    updated.append(f"<b>{esc(word)}</b> - {esc(meaning)}")
                    found = True
                    break
            if not found:
                unit["words"].append({"word": word, "meaning": meaning})
                added.append(f"<b>{esc(word)}</b> - {esc(meaning)}")
        save_data(uid, data)
        msg = []
        if added:
            msg.append(f"✅ Added ({len(added)}):\n" + "\n".join(f"• {a}" for a in added))
        if updated:
            msg.append(f"✏️ Updated ({len(updated)}):\n" + "\n".join(f"• {u}" for u in updated))
        if errors:
            msg.append(f"❌ Skipped:\n" + "\n".join(f"• {esc(e)}" for e in errors))
        msg.append(f"\n📊 {esc(unit_name)}: {len(unit['words'])} words | Send more or /done")
        await update.message.reply_text("\n\n".join(msg), parse_mode="HTML")
        return

    # --- Default ---
    if " - " in text:
        await update.message.reply_text(
            "💡 Use /add UnitName first.\nOr tap /start", reply_markup=main_menu_kb())

# ──────────────────────────────────────
#  CALLBACK ROUTER
# ──────────────────────────────────────
async def callback_router(update, ctx):
    query = update.callback_query
    await query.answer()
    d = query.data

    if   d == "m:main":          await start(update, ctx)
    elif d == "m:units":         await units_menu(update, ctx)
    elif d == "m:quiz":          await quiz_menu(update, ctx)
    elif d == "m:stats":         await stats(update, ctx)
    elif d == "m:newunit":       await newunit_prompt(update, ctx)
    elif d == "m:resetconfirm":  await reset_confirm(update, ctx)
    elif d == "m:resetall":      await reset_all(update, ctx)
    elif d.startswith("upg:"):
        page = int(d.split(":", 1)[1])
        await units_menu(update, ctx, page=page)
    elif d.startswith("unit:"):    await unit_view(update, ctx)
    elif d.startswith("addunit:"): await add_prompt_button(update, ctx)
    elif d.startswith("ren:"):     await rename_prompt(update, ctx)
    elif d.startswith("delc:"):    await del_confirm(update, ctx)
    elif d.startswith("del:"):     await del_unit(update, ctx)
    elif d.startswith("clrc:"):    await clear_confirm(update, ctx)
    elif d.startswith("clr:"):     await clear_unit(update, ctx)
    elif d.startswith("expunit:"): await export_unit(update, ctx)
    elif d.startswith("qpick:"):   await quiz_direction_picker(update, ctx)
    elif d.startswith("qmode:"):   await quiz_mode_picker(update, ctx)
    elif d.startswith("qgo:"):     await do_quiz(update, ctx)
    elif d.startswith("a:"):       await quiz_answer(update, ctx)
    elif d.startswith("skip:"):    await quiz_skip(update, ctx)

# ──────────────────────────────────────
#  MAIN
# ──────────────────────────────────────
def main():
    if not TOKEN:
        print("❌ Set TELEGRAM_BOT_TOKEN environment variable")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    start))
    app.add_handler(CommandHandler("newunit", newunit_cmd))
    app.add_handler(CommandHandler("rename",  rename_cmd))
    app.add_handler(CommandHandler("quiz",    quiz_cmd))
    app.add_handler(CommandHandler("add",     add_cmd))
    app.add_handler(CommandHandler("done",    done_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🚀 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
