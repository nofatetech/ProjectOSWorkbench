"""Publish a vault note to WordPress.com (Phase 1 — core engine, no UI).

Model (mirrors Notion's "create a published link"): a note carries its publish
state in its OWN frontmatter, so publishing and re-publishing are the same act.

    publish: draft            # intent (draft-first default)
    published_url: https://…  # written back on success — your "published link"
    wp_post_id: 12345         # written back → next push UPDATES, never duplicates
    published_at: 2026-06-27

Auth: WordPress.com REST v1.1 over OAuth2. For a single-user personal tool we use
the *password grant* — one POST mints a Bearer token from stored credentials, no
browser redirect / callback server. Tokens last ~2 weeks; we just mint a fresh
one per publish so there's no refresh logic to carry. (If the account has 2FA,
the stored `password` must be a WordPress.com *Application Password*.)

This module is deliberately UI-free and flet-free so it's unit-testable and
runnable from a script (see scripts/wp_publish_check.py). The Phase 2 button and
a later `publish_note` agent tool both call publish_note().
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import frontmatter
import httpx
import markdown as _markdown

TOKEN_URL = "https://public-api.wordpress.com/oauth2/token"
API_BASE = "https://public-api.wordpress.com/rest/v1.1"
# Built-in python-markdown extensions: `extra` = tables/fenced-code/footnotes/etc.,
# `sane_lists` = predictable list nesting, `smarty` = curly quotes/dashes.
MD_EXTENSIONS = ["extra", "sane_lists", "smarty"]
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TRUTHY_STATUS = {"publish", "public", "live", "true", "yes", "1"}


class PublishError(Exception):
    """Anything that stops a publish — bad creds, network, API rejection. The
    message is meant to be shown to the user verbatim."""


@dataclass
class WPCreds:
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""   # WP.com password OR an Application Password (if 2FA)
    site: str = ""       # e.g. "myweb1712.wordpress.com" — the REST <site> segment

    def validate(self) -> None:
        missing = [n for n in ("client_id", "client_secret", "username",
                               "password", "site")
                   if not str(getattr(self, n)).strip()]
        if missing:
            raise PublishError(
                "Missing WordPress.com credentials: " + ", ".join(missing)
                + ". Set them in Settings → PUBLISHING.")


def creds_from_config(cfg) -> WPCreds:
    """Pull WP.com creds off a Config object (getattr so this stays decoupled
    from the exact dataclass)."""
    return WPCreds(
        client_id=getattr(cfg, "wpcom_client_id", "") or "",
        client_secret=getattr(cfg, "wpcom_client_secret", "") or "",
        username=getattr(cfg, "wpcom_username", "") or "",
        password=getattr(cfg, "wpcom_password", "") or "",
        site=getattr(cfg, "wpcom_site", "") or "",
    )


@dataclass
class PublishResult:
    action: str          # "created" | "updated" | "dry-run"
    status: str          # "draft" | "publish"
    title: str
    url: str = ""
    post_id: str = ""
    html_len: int = 0
    html: str = ""       # populated only on dry-run, for preview


# --- frontmatter writeback (targeted, churn-free) ---------------------------
# Copy of vault._set_fm_field (kept here so publish.py imports neither flet nor
# vault). Targeted line edit, not a yaml round-trip, so hand-formatting + key
# order + comments survive.
def _set_fm_field(fm_block: str, key: str, value: Optional[str]) -> str:
    lines = fm_block.split("\n")
    out: list[str] = []
    found = False
    for ln in lines:
        if re.match(rf"^{re.escape(key)}\s*:", ln):
            found = True
            if value is None:
                continue
            out.append(f"{key}: {value}")
        else:
            out.append(ln)
    if not found and value is not None:
        out.append(f"{key}: {value}")
    return "\n".join(out)


def _resolve_title(meta: dict, body: str, path: Path) -> tuple[str, str]:
    """Title precedence: frontmatter `title:` → first `# H1` → filename stem.
    Returns (title, source) where source ∈ {"frontmatter","h1","filename"}."""
    t = str(meta.get("title") or "").strip()
    if t:
        return t, "frontmatter"
    m = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    if m:
        return m.group(1).strip(), "h1"
    return path.stem, "filename"


def _prep_body(body: str, title_source: str) -> str:
    """Clean the markdown body for publishing: drop the leading H1 when it's the
    title source (WP shows the title separately — avoid a duplicate heading), and
    flatten wikilinks `[[A|B]]`/`[[A]]` to their display text (WP can't resolve
    them)."""
    if title_source == "h1":
        body = re.sub(r"^#\s+.+?\s*$\n?", "", body, count=1, flags=re.M)
    body = _WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1], body)
    return body.strip()


def _to_html(md_body: str) -> str:
    return _markdown.markdown(md_body, extensions=MD_EXTENSIONS)


def _norm_tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,\n]", raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        return []
    return [p.strip() for p in parts if p and p.strip()]


def _normalize_status(raw, default: str = "draft") -> str:
    s = str(raw if raw is not None else default).strip().lower()
    return "publish" if s in _TRUTHY_STATUS else "draft"


def build_payload(path: Path, status: Optional[str] = None,
                  default_status: str = "draft") -> tuple[dict, PublishResult, str]:
    """Pure: read the note → (WP REST payload, partial PublishResult, post_id).
    No network. The button/tool and the dry-run path both build through here."""
    path = Path(path)
    if not path.is_file():
        raise PublishError(f"Note not found: {path}")
    post = frontmatter.load(str(path))
    meta, body = post.metadata, (post.content or "")

    eff_status = _normalize_status(
        status if status is not None else meta.get("publish", default_status),
        default_status)
    title, tsource = _resolve_title(meta, body, path)
    html = _to_html(_prep_body(body, tsource))
    post_id = str(meta.get("wp_post_id") or "").strip()

    payload: dict = {"title": title, "content": html, "status": eff_status}
    tags = _norm_tags(meta.get("tags"))
    if tags:
        payload["tags"] = ",".join(tags)
    if meta.get("slug"):
        payload["slug"] = str(meta["slug"]).strip()
    if meta.get("excerpt"):
        payload["excerpt"] = str(meta["excerpt"]).strip()

    result = PublishResult(action="", status=eff_status, title=title,
                           post_id=post_id, html_len=len(html), html=html)
    return payload, result, post_id


def mint_token(creds: WPCreds, *, timeout: float = 30.0) -> str:
    """Exchange stored credentials for a Bearer token (OAuth2 password grant)."""
    creds.validate()
    try:
        r = httpx.post(TOKEN_URL, data={
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "grant_type": "password",
            "username": creds.username,
            "password": creds.password,
        }, timeout=timeout)
    except httpx.HTTPError as ex:
        raise PublishError(f"Token request failed (network): {ex}") from ex
    if r.status_code != 200:
        raise PublishError(_explain_token_error(r))
    try:
        tok = r.json().get("access_token")
    except Exception:
        tok = None
    if not tok:
        raise PublishError(f"No access_token in token response: {r.text[:300]}")
    return tok


def _explain_token_error(r: httpx.Response) -> str:
    try:
        data = r.json()
    except Exception:
        return f"Token request failed (HTTP {r.status_code}): {r.text[:300]}"
    err = data.get("error", "")
    desc = data.get("error_description", "") or str(data)
    hint = ""
    if err == "invalid_client":
        hint = " — check client_id / client_secret (and that the app Type is 'Web')."
    elif err in ("invalid_request", "needs_2fa") or "two" in desc.lower() or "2fa" in desc.lower():
        hint = (" — if the account has 2FA, the password must be a WordPress.com "
                "Application Password (Account → Security), not your login password.")
    elif err == "invalid_grant":
        hint = " — username or password rejected."
    return f"Auth failed ({err or 'HTTP ' + str(r.status_code)}): {desc}{hint}"


def _explain_api_error(r: httpx.Response) -> str:
    try:
        data = r.json()
    except Exception:
        return f"Publish failed (HTTP {r.status_code}): {r.text[:300]}"
    msg = data.get("message") or data.get("error") or str(data)
    return f"Publish failed (HTTP {r.status_code}): {msg}"


def _writeback(path: Path, post_id: str, url: str, status: str) -> None:
    """Record the publish result in the note's own frontmatter (targeted edit)."""
    text = path.read_text(encoding="utf-8")
    fields = {
        "wp_post_id": post_id,
        "published_url": url,
        "published_at": date.today().isoformat(),
        "publish": status,
    }
    m = _FM_RE.match(text)
    if m:
        fm, body = m.group(1), m.group(2)
        for k, v in fields.items():
            fm = _set_fm_field(fm, k, v)
        path.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")
    else:
        fm = "\n".join(f"{k}: {v}" for k, v in fields.items())
        path.write_text(f"---\n{fm}\n---\n\n{text}", encoding="utf-8")


def publish_note(creds: WPCreds, path, *, status: Optional[str] = None,
                 default_status: str = "draft", dry_run: bool = False,
                 timeout: float = 30.0) -> PublishResult:
    """Publish (or update) a note on WordPress.com.

    status:         override the note's `publish:` intent for this call.
    default_status: used when neither `status` nor `publish:` is set (config default).
    dry_run:        build everything, render HTML, but make NO network call and
                    write NOTHING back. Use to preview before going live.
    """
    path = Path(path)
    payload, result, post_id = build_payload(path, status=status,
                                             default_status=default_status)
    if dry_run:
        result.action = "dry-run"
        return result

    creds.validate()
    token = mint_token(creds, timeout=timeout)
    if post_id:
        endpoint = f"{API_BASE}/sites/{creds.site}/posts/{post_id}"
        action = "updated"
    else:
        endpoint = f"{API_BASE}/sites/{creds.site}/posts/new"
        action = "created"
    try:
        r = httpx.post(endpoint, headers={"Authorization": f"Bearer {token}"},
                       json=payload, timeout=timeout)
    except httpx.HTTPError as ex:
        raise PublishError(f"Publish request failed (network): {ex}") from ex
    if r.status_code not in (200, 201):
        raise PublishError(_explain_api_error(r))

    data = r.json()
    new_id = str(data.get("ID") or data.get("id") or post_id)
    post_url = data.get("URL") or data.get("short_URL") or data.get("link") or ""
    _writeback(path, new_id, post_url, result.status)

    result.action = action
    result.url = post_url
    result.post_id = new_id
    result.html = ""  # don't carry the rendered HTML back to the UI on a real run
    return result
