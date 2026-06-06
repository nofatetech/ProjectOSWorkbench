"""Telegram → Inbox capture daemon (standalone, decoupled from the Flet app).

Long-polls Telegram's getUpdates (outbound HTTPS only — no public URL, no
webhook, works behind NAT) and writes each incoming text message into the
vault's 00_Inbox/ as a .md note. This is just another writer into the inbox,
exactly parallel to the in-app quick-capture — Workbench and Obsidian pick the
files up automatically (direct file I/O, the locked integration model).

Run it:
    uv run python src/telegram_capture.py        # from the repo root
    # or set WORKBENCH_TELEGRAM_VERBOSE=1 for chatty logs

Config: ~/.workbench/telegram.json  (chmod 600), mirroring config.json:
    {
      "bot_token": "…from @BotFather…",
      "owner_id": 0          # 0 = unclaimed; first sender claims it (see below)
    }

Security — single-owner claim:
    A bot token is effectively public, so we never accept notes from arbitrary
    senders. On first run with owner_id == 0, the FIRST person to message the
    bot is claimed as the owner (id persisted); everyone else is ignored. To be
    safe, send the first message yourself right after starting the daemon.
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

# Same resolution as main.py: repo_root/vault is a symlink to the real vault.
VAULT_PATH = Path(__file__).resolve().parent.parent / "vault"
INBOX_DIR = VAULT_PATH / "00_Inbox"

CONFIG_PATH = Path.home() / ".workbench" / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30  # seconds — long-poll; Telegram holds the request open
VERBOSE = bool(__import__("os").environ.get("WORKBENCH_TELEGRAM_VERBOSE"))


def log(*a):
    print("[telegram]", *a, file=sys.stderr, flush=True)


def vlog(*a):
    if VERBOSE:
        log(*a)


# --- config -----------------------------------------------------------------

def load_cfg() -> dict:
    if not CONFIG_PATH.exists():
        log(f"no config at {CONFIG_PATH} — create it with a bot_token. See module docstring.")
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("bot_token"):
        log("config has no bot_token.")
        sys.exit(1)
    cfg.setdefault("owner_id", 0)
    return cfg


def save_cfg(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:
        pass


# --- inbox write (parallel to main._quick_capture_note, + a source stamp) ---

def write_inbox_note(text: str, sender_name: str) -> Path:
    """Write a capture note into 00_Inbox/. Slug from the first line, de-duped,
    with light frontmatter so phone-captures are distinguishable at triage."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    body = text.strip()
    first = body.splitlines()[0] if body else "capture"
    slug = re.sub(r"[^\w\- ]", "", first).strip().replace(" ", "-")[:48] or "capture"
    path = INBOX_DIR / f"{slug}.md"
    n = 2
    while path.exists():
        path = INBOX_DIR / f"{slug}-{n}.md"
        n += 1
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    frontmatter = (
        "---\n"
        "type: note\n"
        "source: telegram\n"
        f"from: {sender_name}\n"
        f"captured: {stamp}\n"
        "---\n\n"
    )
    path.write_text(frontmatter + body + "\n", encoding="utf-8")
    return path


# --- telegram api -----------------------------------------------------------

def tg(client: httpx.Client, token: str, method: str, **params):
    """Call a Bot API method; return the 'result' field or None on error."""
    try:
        r = client.get(API.format(token=token, method=method), params=params)
        data = r.json()
        if not data.get("ok"):
            log(f"{method} not ok: {data.get('description')}")
            return None
        return data.get("result")
    except Exception as ex:
        vlog(f"{method} error: {ex}")
        return None


def reply(client, token, chat_id, text):
    tg(client, token, "sendMessage", chat_id=chat_id, text=text)


# --- main loop --------------------------------------------------------------

def handle_message(client, token, cfg, msg) -> None:
    chat_id = msg.get("chat", {}).get("id")
    frm = msg.get("from", {}) or {}
    uid = frm.get("id")
    name = (frm.get("username") or frm.get("first_name") or str(uid))

    # Single-owner claim / allow-list.
    if not cfg.get("owner_id"):
        cfg["owner_id"] = uid
        save_cfg(cfg)
        log(f"claimed owner: {name} (id {uid})")
        reply(client, token, chat_id,
              f"✓ This bot is now bound to you ({name}). Send me anything and "
              "it lands in your Workbench inbox.")
        # fall through and also capture this first message
    elif uid != cfg["owner_id"]:
        log(f"ignored message from non-owner {name} (id {uid})")
        return

    text = msg.get("text")
    if not text:
        reply(client, token, chat_id,
              "Only text is captured for now (photos/voice/files coming later).")
        return

    # Bot commands (/start, /help, …) are Telegram UI chatter, not notes.
    if text.startswith("/"):
        vlog(f"ignored command: {text.splitlines()[0]}")
        reply(client, token, chat_id, "Send me a note and I'll save it to your inbox.")
        return

    path = write_inbox_note(text, name)
    log(f"saved {path.name}")
    reply(client, token, chat_id, f"✓ saved to inbox as {path.name}")


def main() -> None:
    cfg = load_cfg()
    token = cfg["bot_token"]
    with httpx.Client(timeout=POLL_TIMEOUT + 10) as client:
        me = tg(client, token, "getMe")
        if not me:
            log("getMe failed — is the bot_token valid?")
            sys.exit(1)
        log(f"connected as @{me.get('username')} — "
            f"{'owner ' + str(cfg['owner_id']) if cfg.get('owner_id') else 'UNCLAIMED (first sender claims it)'}")
        log(f"writing to {INBOX_DIR}")

        offset = None
        while True:
            updates = tg(client, token, "getUpdates",
                         offset=offset, timeout=POLL_TIMEOUT) or []
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if msg:
                    try:
                        handle_message(client, token, cfg, msg)
                    except Exception as ex:
                        log(f"handler error (skipped): {ex}")
            if not updates:
                time.sleep(1)  # gentle gap if long-poll returned empty


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped.")
