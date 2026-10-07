"""Shopify store management for the chat agent, beyond product/blog publishing.

Read-only GraphQL, vault↔store sync status, Online Store pages, unpublish,
image uploads, and theme files. Every function returns a human-readable string
for the model and raises ShopifyPublishError on actionable failures.

Safety rails: shopify_query refuses mutations; theme writes refuse the live
(MAIN) theme, so edits go to a duplicate the user previews and publishes in
Shopify admin. Nothing here deletes remote data.
"""

import json
import mimetypes
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter
import httpx
import markdown

from shopify_publish import (ShopifyCreds, ShopifyPublishError, _graphql, _writeback,
                             _LIVE)

QUERY_CAP = 16_000       # chars of query JSON returned to the model
THEME_FILE_CAP = 24_000  # chars of one theme file returned to the model
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_STRING_RE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"')
_COMMENT_RE = re.compile(r"#[^\n]*")
_ID_KEYS = {"shopify_product_id": "product", "shopify_article_id": "article",
            "shopify_page_id": "page"}
_SKIP_PARTS = {".git", ".obsidian", ".claude"}


def _checked(creds: ShopifyCreds, operation: str, query: str, variables: dict) -> dict:
    node = _graphql(creds, query, variables).get(operation)
    if not isinstance(node, dict):
        raise ShopifyPublishError(f"Shopify returned no {operation} result.")
    errors = node.get("userErrors") or []
    if errors:
        raise ShopifyPublishError("; ".join(str(e.get("message", e)) for e in errors))
    return node


def _numeric_id(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


def _resolve(vault_root: Path, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else vault_root / p


# --- Read-only GraphQL --------------------------------------------------------

def is_read_only(query: str) -> bool:
    """True when the GraphQL document contains no mutation/subscription operation.
    String literals and comments are stripped first so text can't hide one."""
    bare = _COMMENT_RE.sub("", _STRING_RE.sub('""', query))
    return not re.search(r"\b(mutation|subscription)\b", bare)


def run_query(creds: ShopifyCreds, query: str, variables: Optional[dict] = None) -> str:
    if not query.strip():
        raise ShopifyPublishError("Empty query.")
    if not is_read_only(query):
        raise ShopifyPublishError(
            "shopify_query is read-only; mutations go through the dedicated tools.")
    if isinstance(variables, str):
        variables = json.loads(variables) if variables.strip() else None
    out = json.dumps(_graphql(creds, query, variables), ensure_ascii=False, indent=1)
    return out if len(out) <= QUERY_CAP else out[:QUERY_CAP] + "\n…[truncated — narrow the query]"


# --- Vault ↔ store sync -------------------------------------------------------

_NODES = """query($ids: [ID!]!) {
  nodes(ids: $ids) {
    id
    ... on Product { title status handle updatedAt }
    ... on Article { title isPublished handle updatedAt blog { handle } }
    ... on Page { title isPublished handle updatedAt }
  }
}"""
_REMOTE_ALL = """{
  products(first: 100, sortKey: UPDATED_AT, reverse: true) { nodes { id title status } }
  pages(first: 100) { nodes { id title isPublished } }
  articles(first: 100, sortKey: UPDATED_AT, reverse: true) { nodes { id title isPublished } }
}"""


def linked_notes(vault_root: Path) -> list[dict]:
    """Vault notes carrying a Shopify product/article/page ID in frontmatter."""
    out = []
    for p in sorted(vault_root.rglob("*.md")):
        if _SKIP_PARTS.intersection(p.relative_to(vault_root).parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "shopify_" not in text[:4000]:
            continue
        try:
            meta = frontmatter.loads(text).metadata
        except Exception:
            continue
        for key, kind in _ID_KEYS.items():
            gid = str(meta.get(key) or "").strip()
            if gid:
                out.append({"path": p, "rel": str(p.relative_to(vault_root)), "kind": kind,
                            "id": gid, "publish": str(meta.get("publish") or "").strip(),
                            "store": str(meta.get("shopify_store") or "").strip(),
                            "mtime": p.stat().st_mtime})
                break
    return out


def _remote_state(node: dict) -> str:
    if "status" in node:
        return node["status"].lower()
    return "live" if node.get("isPublished") else "hidden"


def sync_status(creds: ShopifyCreds, vault_root: Path, path: str = "") -> str:
    notes = linked_notes(vault_root)
    if path:
        target = _resolve(vault_root, path).resolve()
        notes = [n for n in notes if n["path"].resolve() == target]
        if not notes:
            return f"{path}: not linked to Shopify (no shopify_*_id in frontmatter)."
    notes = [n for n in notes if not n["store"] or n["store"] == creds.store]
    remote: dict[str, dict] = {}
    ids = [n["id"] for n in notes]
    for i in range(0, len(ids), 50):
        for node in _graphql(creds, _NODES, {"ids": ids[i:i + 50]}).get("nodes") or []:
            if node:
                remote[node["id"]] = node

    lines = [f"Store {creds.store} · {len(notes)} linked notes"]
    for n in notes:
        node = remote.get(n["id"])
        if not node:
            lines.append(f"- ✗ {n['rel']} · {n['kind']} {n['id']} NOT FOUND in store "
                         f"(deleted remotely? clear the ID to re-create)")
            continue
        state = _remote_state(node)
        flags = []
        updated = datetime.fromisoformat(node["updatedAt"].replace("Z", "+00:00"))
        if n["mtime"] > updated.timestamp() + 120:
            flags.append("note edited after last push → publish_note to update")
        want_live = n["publish"].lower() in _LIVE
        if want_live != (state in ("active", "live")):
            flags.append(f"note says publish: {n['publish'] or '—'}")
        mark = "⚠" if flags else "✓"
        lines.append(f"- {mark} {n['rel']} · {n['kind']} {state} · “{node.get('title')}”"
                     + (f" · {'; '.join(flags)}" if flags else ""))

    if not path:
        linked = set(remote)
        data = _graphql(creds, _REMOTE_ALL)
        unlinked = []
        for kind in ("products", "pages", "articles"):
            for node in (data.get(kind) or {}).get("nodes") or []:
                if node["id"] not in linked:
                    unlinked.append(f"  - {kind[:-1]} {_remote_state(node)} · "
                                    f"“{node['title']}” · {node['id']}")
        if unlinked:
            lines.append(f"\nIn the store but not linked to any note ({len(unlinked)}):")
            lines += unlinked
    return "\n".join(lines)


# --- Online Store pages -------------------------------------------------------

_PAGE_GET = "query($id: ID!) { page(id: $id) { id handle isPublished } }"
_PAGE_CREATE = """mutation($page: PageCreateInput!) {
  pageCreate(page: $page) { page { id handle isPublished } userErrors { field message } }
}"""
_PAGE_UPDATE = """mutation($id: ID!, $page: PageUpdateInput!) {
  pageUpdate(id: $id, page: $page) { page { id handle isPublished } userErrors { field message } }
}"""


def build_page(path: Path, status: Optional[str] = None) -> tuple[dict, str, str]:
    """(PageCreate/UpdateInput payload, draft|publish, existing page ID or '')."""
    post = frontmatter.load(str(path))
    meta, body = post.metadata, post.content or ""
    if str(meta.get("type") or "").strip().lower() != "page":
        raise ShopifyPublishError("Page publishing requires `type: page` in frontmatter.")
    if meta.get("shopify_product_id") or meta.get("shopify_article_id"):
        raise ShopifyPublishError("This note is already linked to a product or article.")
    title = str(meta.get("title") or "").strip()
    h1 = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    if not title:
        title = h1.group(1).strip() if h1 else path.stem
    if h1:
        body = body.replace(h1.group(0), "", 1)
    raw = status if status is not None else meta.get("publish", "draft")
    state = "publish" if str(raw).strip().lower() in _LIVE else "draft"
    if state == "publish" and not body.strip():
        raise ShopifyPublishError("Write the page body before making it live.")
    body = _WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1], body)
    payload = {"title": title, "isPublished": state == "publish",
               "body": markdown.markdown(body.strip(), extensions=["extra", "sane_lists", "smarty"])}
    if str(meta.get("handle") or "").strip():
        payload["handle"] = str(meta["handle"]).strip()
    if str(meta.get("template") or "").strip():
        payload["templateSuffix"] = str(meta["template"]).strip()
    return payload, state, str(meta.get("shopify_page_id") or "").strip()


def publish_page(creds: ShopifyCreds, path: Path, status: Optional[str] = None) -> str:
    payload, state, page_id = build_page(path, status)
    store = str(frontmatter.load(str(path)).metadata.get("shopify_store") or "").strip()
    if page_id and store and store != creds.store:
        raise ShopifyPublishError("This page note belongs to a different Shopify store.")
    if page_id:
        if not (_graphql(creds, _PAGE_GET, {"id": page_id}).get("page") or {}).get("id"):
            raise ShopifyPublishError(f"Page {page_id} no longer exists; clear shopify_page_id "
                                      "to create it again.")
        page = _checked(creds, "pageUpdate", _PAGE_UPDATE, {"id": page_id, "page": payload})["page"]
        action = "updated"
    else:
        page = _checked(creds, "pageCreate", _PAGE_CREATE, {"page": payload})["page"]
        action = "created"
    url = f"https://{creds.store}/pages/{page['handle']}" if page.get("isPublished") else ""
    state = "publish" if page.get("isPublished") else "draft"
    _writeback(path, {"shopify_page_id": page["id"], "shopify_store": creds.store,
                      "shopify_published_url": url,
                      "published_at": date.today().isoformat(), "publish": state})
    return f"{action} page ({state}): {payload['title']} · url={url}"


# --- Unpublish / archive ------------------------------------------------------

_PRODUCT_STATUS = """mutation($product: ProductUpdateInput!) {
  productUpdate(product: $product) { product { id status } userErrors { field message } }
}"""
_ARTICLE_HIDE = """mutation($id: ID!) {
  articleUpdate(id: $id, article: {isPublished: false}) {
    article { id isPublished } userErrors { field message }
  }
}"""
_PAGE_HIDE = """mutation($id: ID!) {
  pageUpdate(id: $id, page: {isPublished: false}) {
    page { id isPublished } userErrors { field message }
  }
}"""


def unpublish(creds: ShopifyCreds, path: Path, archive: bool = False) -> str:
    """Take a linked note's remote item off the storefront (product → DRAFT or
    ARCHIVED; article/page → hidden). Nothing is deleted."""
    meta = frontmatter.load(str(path)).metadata
    store = str(meta.get("shopify_store") or "").strip()
    if store and store != creds.store:
        raise ShopifyPublishError("This note belongs to a different Shopify store.")
    if meta.get("shopify_product_id"):
        status = "ARCHIVED" if archive else "DRAFT"
        _checked(creds, "productUpdate", _PRODUCT_STATUS,
                 {"product": {"id": str(meta["shopify_product_id"]), "status": status}})
        what = f"product → {status.lower()}"
        publish = "archived" if archive else "draft"
    elif meta.get("shopify_article_id"):
        _checked(creds, "articleUpdate", _ARTICLE_HIDE, {"id": str(meta["shopify_article_id"])})
        what, publish = "article → hidden", "draft"
    elif meta.get("shopify_page_id"):
        _checked(creds, "pageUpdate", _PAGE_HIDE, {"id": str(meta["shopify_page_id"])})
        what, publish = "page → hidden", "draft"
    else:
        raise ShopifyPublishError("This note isn't linked to a Shopify product, article or page.")
    _writeback(path, {"publish": publish, "shopify_published_url": ""})
    return f"Unpublished {path.name}: {what}. Re-publish with publish_note(status=\"publish\")."


# --- Images -------------------------------------------------------------------

_STAGED = """mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""
_ATTACH = """mutation($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
  productUpdate(product: $product, media: $media) {
    product { id media(last: 1) { nodes { id status } } }
    userErrors { field message }
  }
}"""
_FILE_CREATE = """mutation($files: [FileCreateInput!]!) {
  fileCreate(files: $files) { files { id fileStatus } userErrors { field message } }
}"""
_FILE_GET = """query($id: ID!) {
  node(id: $id) { ... on MediaImage { fileStatus image { url } } }
}"""


def _staged_upload(creds: ShopifyCreds, image: Path) -> str:
    """Upload a local image to Shopify's staging bucket; returns its resourceUrl."""
    mime = mimetypes.guess_type(image.name)[0] or ""
    if not mime.startswith("image/"):
        raise ShopifyPublishError(f"{image.name} is not an image file.")
    data = image.read_bytes()
    target = _checked(creds, "stagedUploadsCreate", _STAGED, {"input": [{
        "filename": image.name, "mimeType": mime, "httpMethod": "POST",
        "resource": "IMAGE", "fileSize": str(len(data))}]})["stagedTargets"][0]
    try:
        r = httpx.post(target["url"],
                       data={p["name"]: p["value"] for p in target["parameters"]},
                       files={"file": (image.name, data, mime)}, timeout=120)
    except httpx.HTTPError as ex:
        raise ShopifyPublishError(f"Image upload failed: {ex}") from ex
    if r.status_code >= 300:
        raise ShopifyPublishError(f"Image upload failed: HTTP {r.status_code}")
    return target["resourceUrl"]


def upload_image(creds: ShopifyCreds, vault_root: Path, image: str,
                 product_note: str = "", alt: str = "") -> str:
    """Attach a vault image to a note's product, or (no product_note) add it to
    the store's Files and return its CDN URL for use in pages/articles/themes."""
    img = _resolve(vault_root, image)
    if not img.is_file():
        raise ShopifyPublishError(f"Image not found: {img}")
    if product_note:
        note = _resolve(vault_root, product_note)
        product_id = str(frontmatter.load(str(note)).metadata.get("shopify_product_id") or "")
        if not product_id:
            raise ShopifyPublishError(f"{note.name} has no shopify_product_id; publish it first.")
        resource = _staged_upload(creds, img)
        _checked(creds, "productUpdate", _ATTACH, {
            "product": {"id": product_id},
            "media": [{"originalSource": resource, "mediaContentType": "IMAGE",
                       "alt": alt or img.stem}]})
        return (f"Attached {img.name} to product {product_id} "
                f"(Shopify processes it in a few seconds).")
    resource = _staged_upload(creds, img)
    file_id = _checked(creds, "fileCreate", _FILE_CREATE, {"files": [{
        "originalSource": resource, "contentType": "IMAGE",
        "alt": alt or img.stem}]})["files"][0]["id"]
    for _ in range(10):
        node = _graphql(creds, _FILE_GET, {"id": file_id}).get("node") or {}
        url = (node.get("image") or {}).get("url")
        if url:
            return f"Uploaded {img.name} to Files: {url}"
        if node.get("fileStatus") == "FAILED":
            raise ShopifyPublishError(f"Shopify failed to process {img.name}.")
        time.sleep(1.5)
    return f"Uploaded {img.name} as {file_id}; still processing — check Files for its URL."


# --- Themes -------------------------------------------------------------------

_THEMES = "{ themes(first: 20) { nodes { id name role updatedAt } } }"
_THEME_FILES = """query($id: ID!, $names: [String!], $after: String) {
  theme(id: $id) {
    id name role
    files(filenames: $names, first: 250, after: $after) {
      nodes { filename size body { ... on OnlineStoreThemeFileBodyText { content } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""
_THEME_UPSERT = """mutation($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) {
  themeFilesUpsert(themeId: $themeId, files: $files) {
    upsertedThemeFiles { filename }
    userErrors { field message }
  }
}"""
_THEME_DUPLICATE = """mutation($id: ID!, $name: String) {
  themeDuplicate(id: $id, name: $name) {
    newTheme { id name role }
    userErrors { field message }
  }
}"""


def _themes(creds: ShopifyCreds) -> list[dict]:
    return (_graphql(creds, _THEMES).get("themes") or {}).get("nodes") or []


def _theme(creds: ShopifyCreds, theme_id: str = "") -> dict:
    themes = _themes(creds)
    if not theme_id:
        live = [t for t in themes if t["role"] == "MAIN"]
        if not live:
            raise ShopifyPublishError("No live theme found.")
        return live[0]
    if not theme_id.startswith("gid://"):
        theme_id = f"gid://shopify/OnlineStoreTheme/{theme_id}"
    for t in themes:
        if t["id"] == theme_id:
            return t
    raise ShopifyPublishError(f"Theme {theme_id} not found. Themes: "
                              + ", ".join(f"{t['name']} ({t['role']}) {t['id']}" for t in themes))


def _preview_url(creds: ShopifyCreds, theme: dict) -> str:
    return f"https://{creds.store}/?preview_theme_id={_numeric_id(theme['id'])}"


def theme_files(creds: ShopifyCreds, theme_id: str = "", filenames: Optional[list] = None) -> str:
    """List a theme's files (no filenames) or return the given files' contents.
    Filenames accept Shopify wildcards, e.g. 'sections/*.liquid'."""
    theme = _theme(creds, theme_id)
    names = [str(n) for n in (filenames or []) if str(n).strip()]
    head = f"Theme “{theme['name']}” ({theme['role']}) {theme['id']}"
    if not names:
        listed, after = [], None
        while True:
            data = (_graphql(creds, _THEME_FILES, {"id": theme["id"], "names": None,
                                                   "after": after}).get("theme") or {})
            files = data.get("files") or {}
            listed += [f"{f['filename']} ({f.get('size') or 0}b)" for f in files.get("nodes") or []]
            if not (files.get("pageInfo") or {}).get("hasNextPage"):
                break
            after = files["pageInfo"]["endCursor"]
        return head + f" · {len(listed)} files\n" + "\n".join(listed)
    data = (_graphql(creds, _THEME_FILES, {"id": theme["id"], "names": names,
                                           "after": None}).get("theme") or {})
    nodes = (data.get("files") or {}).get("nodes") or []
    if not nodes:
        return head + f"\nNo files matched {names}."
    out = [head]
    for f in nodes:
        content = (f.get("body") or {}).get("content")
        if content is None:
            content = "[binary file — not shown]"
        elif len(content) > THEME_FILE_CAP:
            content = content[:THEME_FILE_CAP] + "\n…[truncated]"
        out.append(f"\n=== {f['filename']} ===\n{content}")
    return "\n".join(out)


def theme_write(creds: ShopifyCreds, theme_id: str, filename: str, content: str) -> str:
    """Create or overwrite one text file in a NON-live theme."""
    if not theme_id:
        raise ShopifyPublishError("theme_id is required; duplicate the live theme first.")
    theme = _theme(creds, theme_id)
    if theme["role"] == "MAIN":
        raise ShopifyPublishError(
            "Refusing to edit the live theme. Duplicate it with shopify_theme_duplicate, "
            "edit the copy, and let the user preview and publish it in Shopify admin.")
    _checked(creds, "themeFilesUpsert", _THEME_UPSERT, {"themeId": theme["id"], "files": [
        {"filename": filename, "body": {"type": "TEXT", "value": content}}]})
    return (f"Wrote {filename} ({len(content)} chars) to “{theme['name']}”. "
            f"Preview: {_preview_url(creds, theme)}")


def theme_duplicate(creds: ShopifyCreds, theme_id: str = "", name: str = "") -> str:
    """Copy a theme (default: the live one) as a new unpublished theme."""
    source = _theme(creds, theme_id)
    new_name = name.strip() or f"{source['name']} — Workbench {date.today().isoformat()}"
    theme = _checked(creds, "themeDuplicate", _THEME_DUPLICATE,
                     {"id": source["id"], "name": new_name})["newTheme"]
    return (f"Duplicated “{source['name']}” as “{theme['name']}” ({theme['role']}) "
            f"{theme['id']}. It may take a minute to finish copying. "
            f"Preview: {_preview_url(creds, theme)}")


# --- Agent tool dispatch ------------------------------------------------------

def run_tool(name: str, args: dict, creds: ShopifyCreds, vault_root: Path) -> str:
    """Execute one shopify_* agent tool. Never raises — errors come back as text."""
    try:
        creds.validate()
        if name == "shopify_query":
            return run_query(creds, str(args.get("query") or ""), args.get("variables"))
        if name == "shopify_status":
            return sync_status(creds, vault_root, str(args.get("path") or ""))
        if name == "shopify_unpublish":
            return unpublish(creds, _resolve(vault_root, str(args["path"])),
                             bool(args.get("archive")))
        if name == "shopify_upload_image":
            return upload_image(creds, vault_root, str(args["image"]),
                                str(args.get("product_note") or ""), str(args.get("alt") or ""))
        if name == "shopify_theme_files":
            return theme_files(creds, str(args.get("theme_id") or ""), args.get("filenames"))
        if name == "shopify_theme_write":
            return theme_write(creds, str(args.get("theme_id") or ""),
                               str(args["filename"]), str(args.get("content") or ""))
        if name == "shopify_theme_duplicate":
            return theme_duplicate(creds, str(args.get("theme_id") or ""),
                                   str(args.get("name") or ""))
        return f"[unknown Shopify tool: {name}]"
    except ShopifyPublishError as ex:
        return f"[{name} failed: {ex}]"
    except (KeyError, ValueError) as ex:
        return f"[{name}: bad arguments: {ex}]"
    except Exception as ex:
        return f"[{name} error: {ex}]"
