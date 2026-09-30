"""Publish vault Markdown as Shopify blog articles via the Admin GraphQL API.

The note remains the source of truth. Shopify IDs are written back only after a
successful create/update, so the next push updates the same article.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import frontmatter
import httpx
import markdown


API_VERSION = "2026-07"
_STORE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_LIVE = {"publish", "published", "public", "live", "true", "yes", "1"}

_BLOGS = "query { blogs(first: 50) { nodes { id title handle } } }"
_ARTICLE = """query($id: ID!) {
  article(id: $id) { id title handle isPublished blog { id handle } }
}"""
_CREATE = """mutation($article: ArticleCreateInput!) {
  articleCreate(article: $article) {
    article { id title handle isPublished blog { id handle } }
    userErrors { field message }
  }
}"""
_UPDATE = """mutation($id: ID!, $article: ArticleUpdateInput!) {
  articleUpdate(id: $id, article: $article) {
    article { id title handle isPublished blog { id handle } }
    userErrors { field message }
  }
}"""


class ShopifyPublishError(Exception):
    """An actionable connection or publish failure."""


@dataclass
class ShopifyCreds:
    store: str = "nofatetech.myshopify.com"
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""  # existing installed app / OAuth offline token

    def validate(self) -> None:
        if not _STORE_RE.fullmatch(self.store.strip().lower()):
            raise ShopifyPublishError("Use a store domain like name.myshopify.com in Settings → SHOPIFY.")
        if not self.access_token and not (self.client_id and self.client_secret):
            raise ShopifyPublishError(
                "Add a Shopify app Client ID and secret, or an existing Admin API access token, "
                "in Settings → SHOPIFY.")


def creds_from_config(cfg) -> ShopifyCreds:
    return ShopifyCreds(
        store=(getattr(cfg, "shopify_store", "") or "").strip().lower(),
        client_id=(getattr(cfg, "shopify_client_id", "") or "").strip(),
        client_secret=(getattr(cfg, "shopify_client_secret", "") or "").strip(),
        access_token=(getattr(cfg, "shopify_access_token", "") or "").strip(),
    )


def _error_from_response(response: httpx.Response, creds: ShopifyCreds) -> str:
    try:
        data = response.json()
    except ValueError:
        message = response.text.strip()
        oauth_code = re.search(r"(?i)\boauth error\s+([a-z][a-z0-9_]+)", message)
        if oauth_code:
            message = f"OAuth error {oauth_code.group(1)}"
    else:
        if isinstance(data, dict):
            parts = [str(data[key]) for key in ("error", "error_description", "message", "errors")
                     if data.get(key)]
            message = ": ".join(parts)
        else:
            message = str(data)
    # Shopify OAuth errors can be plain text. Never echo app credentials or a
    # full HTML response into the Settings UI.
    message = re.sub(r"(?is)<(?:style|script)\b[^>]*>.*?</(?:style|script)>", " ", message)
    message = re.sub(r"<[^>]*>", " ", message)
    for secret in (creds.client_secret, creds.access_token, creds.client_id):
        if secret:
            message = message.replace(secret, "[redacted]")
    message = " ".join(message.split())[:250]
    return f"Shopify HTTP {response.status_code}: {message or 'No error details returned.'}"


def _token(creds: ShopifyCreds, *, timeout: float) -> str:
    creds.validate()
    if creds.access_token:
        return creds.access_token
    try:
        response = httpx.post(
            f"https://{creds.store}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": creds.client_id,
                  "client_secret": creds.client_secret}, timeout=timeout)
    except httpx.HTTPError as ex:
        raise ShopifyPublishError(f"Could not connect to Shopify: {ex}") from ex
    if response.status_code != 200:
        detail = _error_from_response(response, creds)
        if "shop_not_permitted" in detail.lower():
            detail += (" Client credentials require the app and store to be in the same "
                       "Shopify organization. Check the app installation and organization; "
                       "a different organization requires OAuth.")
        elif "app_not_installed" in detail.lower():
            detail += (" Install this app on the selected store from Shopify Dev Dashboard, "
                       "then retry. Verify that the store domain matches its installation.")
        raise ShopifyPublishError("Token request failed. " + detail)
    token = response.json().get("access_token")
    if not token:
        raise ShopifyPublishError("Shopify did not return an access token.")
    return str(token)


def _graphql(creds: ShopifyCreds, query: str, variables: Optional[dict] = None,
             *, timeout: float = 30.0) -> dict:
    token = _token(creds, timeout=timeout)
    try:
        response = httpx.post(
            f"https://{creds.store}/admin/api/{API_VERSION}/graphql.json",
            headers={"X-Shopify-Access-Token": token},
            json={"query": query, "variables": variables or {}}, timeout=timeout)
    except httpx.HTTPError as ex:
        raise ShopifyPublishError(f"Could not connect to Shopify: {ex}") from ex
    if response.status_code != 200:
        raise ShopifyPublishError("Admin API request failed. " + _error_from_response(response, creds))
    data = response.json()
    if data.get("errors"):
        raise ShopifyPublishError("; ".join(str(e.get("message", e)) for e in data["errors"]))
    return data.get("data") or {}


def list_blogs(creds: ShopifyCreds) -> list[dict]:
    return (_graphql(creds, _BLOGS).get("blogs") or {}).get("nodes") or []


def get_article(creds: ShopifyCreds, article_id: str) -> dict:
    article = _graphql(creds, _ARTICLE, {"id": article_id}).get("article")
    if not article:
        raise ShopifyPublishError("The Shopify article ID in this note was not found.")
    return article


@dataclass
class ShopifyOptions:
    status: Optional[str] = None  # draft | publish; None follows note frontmatter
    default_status: str = "draft"
    project_tag: str = ""
    tag_exclude: list[str] = field(default_factory=list)


@dataclass
class ShopifyResult:
    action: str
    status: str
    title: str
    url: str = ""
    article_id: str = ""
    tags: list[str] = field(default_factory=list)
    html: str = ""


def _tags(raw) -> list[str]:
    if isinstance(raw, str):
        return [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def build_article(path: Path, blog_id: str, author: str,
                  options: Optional[ShopifyOptions] = None) -> tuple[dict, ShopifyResult, str]:
    path = Path(path)
    if not path.is_file():
        raise ShopifyPublishError(f"Note not found: {path}")
    if not blog_id.startswith("gid://shopify/Blog/"):
        raise ShopifyPublishError("Choose a Shopify blog in Settings → SHOPIFY.")
    if not author.strip():
        raise ShopifyPublishError("Enter a Shopify article author in Settings → SHOPIFY.")
    opts = options or ShopifyOptions()
    post = frontmatter.load(str(path))
    meta, body = post.metadata, post.content or ""
    title = str(meta.get("title") or "").strip()
    h1 = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    if not title:
        title = h1.group(1).strip() if h1 else path.stem
        if h1:
            body = re.sub(r"^#\s+.+?\s*$\n?", "", body, count=1, flags=re.M)
    body = _WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1], body)
    html = markdown.markdown(body.strip(), extensions=["extra", "sane_lists", "smarty"])
    raw_status = opts.status if opts.status is not None else meta.get("publish", opts.default_status)
    status = "publish" if str(raw_status).strip().lower() in _LIVE else "draft"
    audience = str(meta.get("audience") or meta.get("visibility") or "").strip().lower()
    if status == "publish" and audience in {
        "private", "password", "subscribers", "subscriber-only", "members", "members-only",
        "paid", "paid-subscribers"}:
        raise ShopifyPublishError(
            "This note is marked for a restricted audience, but Shopify blog articles "
            "are public. Keep it as a draft until subscriber access is configured.")
    excluded = {tag.casefold() for tag in opts.tag_exclude}
    seen = set()
    tags = []
    for tag in _tags(meta.get("tags")) + _tags(opts.project_tag):
        key = tag.casefold()
        if key not in excluded and key not in seen:
            tags.append(tag)
            seen.add(key)
    payload = {"blogId": blog_id, "title": title, "body": html,
               "tags": tags, "isPublished": status == "publish"}
    summary = str(meta.get("summary") or meta.get("description") or "").strip()
    if summary:
        payload["summary"] = markdown.markdown(summary)
    article_id = str(meta.get("shopify_article_id") or "").strip()
    if not article_id:
        payload["author"] = {"name": author.strip()}
    return payload, ShopifyResult("dry-run", status, title, tags=tags, html=html), article_id


def _set_field(frontmatter_text: str, key: str, value: str) -> str:
    lines = frontmatter_text.split("\n")
    found = False
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}\s*:", line):
            lines[i] = f"{key}: {value}"
            found = True
            break
    if not found:
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _writeback(path: Path, fields: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8")
    match = _FM_RE.match(text)
    if match:
        fm, body = match.group(1), match.group(2)
        for key, value in fields.items():
            fm = _set_field(fm, key, value)
        path.write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")
    else:
        fm = "\n".join(f"{key}: {value}" for key, value in fields.items())
        path.write_text(f"---\n{fm}\n---\n\n{text}", encoding="utf-8")


def publish_note(creds: ShopifyCreds, path: Path, *, blog_id: str, author: str,
                 options: Optional[ShopifyOptions] = None, dry_run: bool = False
                 ) -> ShopifyResult:
    path = Path(path)
    payload, result, article_id = build_article(path, blog_id, author, options)
    if dry_run:
        return result
    creds.validate()
    existing_store = str(frontmatter.load(str(path)).metadata.get("shopify_store") or "").strip()
    if article_id and existing_store and existing_store != creds.store:
        raise ShopifyPublishError("This note belongs to a different Shopify store.")
    if article_id:
        # Verify the note's ID and blog before editing an existing remote article.
        remote = get_article(creds, article_id)
        if remote.get("blog", {}).get("id") != blog_id:
            raise ShopifyPublishError("This article belongs to a different Shopify blog.")
        data = _graphql(creds, _UPDATE, {"id": article_id, "article": payload})
        operation, action = "articleUpdate", "updated"
    else:
        data = _graphql(creds, _CREATE, {"article": payload})
        operation, action = "articleCreate", "created"
    node = data.get(operation) or {}
    if node.get("userErrors"):
        raise ShopifyPublishError("; ".join(e.get("message", "Unknown Shopify error")
                                             for e in node["userErrors"]))
    article = node.get("article") or {}
    if not article.get("id"):
        raise ShopifyPublishError("Shopify returned no article ID; the note was not changed.")
    result.action = action
    result.article_id = article["id"]
    result.status = "publish" if article.get("isPublished") else "draft"
    handle = article.get("handle") or ""
    blog_handle = (article.get("blog") or {}).get("handle") or ""
    result.url = (f"https://{creds.store}/blogs/{blog_handle}/{handle}"
                  if result.status == "publish" and blog_handle and handle else "")
    fields = {"shopify_article_id": result.article_id,
              "shopify_store": creds.store,
              "shopify_blog_id": blog_id,
              "shopify_published_url": result.url,
              "published_at": date.today().isoformat(),
              "publish": result.status}
    if not frontmatter.load(str(path)).metadata.get("wp_post_id"):
        fields["published_url"] = result.url
    _writeback(path, fields)
    result.html = ""
    return result
