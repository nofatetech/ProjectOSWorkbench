"""Publish a project Markdown note as a contact-first Shopify product.

Products stay draft until explicitly made live. Live listings have one variant,
zero tracked inventory, and no shipping, so the storefront's normal purchase
button is disabled. The Markdown note retains the Shopify product ID for updates.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import frontmatter
import markdown

from shopify_publish import (ShopifyCreds, ShopifyPublishError, _graphql, _tags,
                             _writeback)


_LIVE = {"publish", "published", "public", "live", "true", "yes", "1"}
_DRAFT = {"draft", "false", "no", "0"}
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_PRODUCT = """query($id: ID!) {
  product(id: $id) {
    id title handle status
    variants(first: 2) { nodes {
      id price inventoryPolicy inventoryQuantity
      inventoryItem { id tracked requiresShipping }
    } }
  }
}"""
_CREATE = """mutation($product: ProductCreateInput!) {
  productCreate(product: $product) {
    product { id handle status }
    userErrors { field message }
  }
}"""
_UPDATE = """mutation($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id handle status }
    userErrors { field message }
  }
}"""
_VARIANT = """mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price inventoryPolicy }
    userErrors { field message }
  }
}"""
_INVENTORY = """mutation($id: ID!, $input: InventoryItemInput!) {
  inventoryItemUpdate(id: $id, input: $input) {
    inventoryItem { id tracked requiresShipping }
    userErrors { field message }
  }
}"""
_PUBLICATION = """query {
  shop { currencyCode }
  publications(first: 50) { nodes { id name } }
}"""
_PUBLISH = """mutation($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    userErrors { field message }
  }
}"""
_COLLECTION_BY_HANDLE = """query($handle: String!) {
  collectionByIdentifier(identifier: {handle: $handle}) { id title }
}"""
_COLLECTION_CREATE = """mutation($collection: CollectionCreateInput!) {
  collectionCreate(collection: $collection) {
    collection { id handle }
    userErrors { field message }
  }
}"""
_FINAL = """query($id: ID!, $publicationId: ID!) {
  product(id: $id) {
    id handle status publishedOnPublication(publicationId: $publicationId)
  }
}"""


@dataclass
class ProductOptions:
    status: Optional[str] = None  # draft | publish; None follows frontmatter
    project_tag: str = ""
    tag_exclude: list[str] = field(default_factory=list)


@dataclass
class ProductResult:
    action: str
    status: str
    title: str
    price: str
    url: str = ""
    product_id: str = ""
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)


def _checked(creds: ShopifyCreds, operation: str, query: str, variables: dict) -> dict:
    node = _graphql(creds, query, variables).get(operation)
    if not isinstance(node, dict):
        raise ShopifyPublishError(f"Shopify returned no {operation} result.")
    errors = node.get("userErrors") or []
    if errors:
        raise ShopifyPublishError("; ".join(e.get("message", "Shopify rejected the product")
                                            for e in errors))
    return node


def get_product(creds: ShopifyCreds, product_id: str) -> dict:
    product = _graphql(creds, _PRODUCT, {"id": product_id}).get("product")
    if not product:
        raise ShopifyPublishError("The Shopify product ID in this note was not found.")
    return product


def _price(value) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ShopifyPublishError("Use a numeric price_usd such as 1.00.") from None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise ShopifyPublishError("price_usd must be a positive USD amount in cents.")
    return f"{amount:.2f}"


def build_product(path: Path, options: Optional[ProductOptions] = None
                  ) -> tuple[dict, ProductResult, str]:
    path = Path(path)
    if not path.is_file():
        raise ShopifyPublishError(f"Note not found: {path}")
    post = frontmatter.load(str(path))
    meta, body = post.metadata, post.content or ""
    if str(meta.get("type") or "").strip().lower() != "product":
        raise ShopifyPublishError("Product publishing requires `type: product` in frontmatter.")
    if meta.get("shopify_article_id"):
        raise ShopifyPublishError("This note is linked to a Shopify blog article, not a product.")
    title = str(meta.get("title") or "").strip()
    h1 = re.search(r"^#\s+(.+?)\s*$", body, re.M)
    if not title:
        title = h1.group(1).strip() if h1 else path.stem
        if h1:
            body = re.sub(r"^#\s+.+?\s*$\n?", "", body, count=1, flags=re.M)
    opts = options or ProductOptions()
    raw_status = opts.status if opts.status is not None else meta.get("publish", "draft")
    status = str(raw_status).strip().lower()
    if status in _LIVE:
        status = "publish"
    elif status in _DRAFT:
        status = "draft"
    else:
        raise ShopifyPublishError("Product status must be draft or publish.")
    audience = str(meta.get("audience") or meta.get("visibility") or "").strip().lower()
    if status == "publish" and audience in {
        "private", "password", "subscribers", "subscriber-only", "members", "members-only",
        "paid", "paid-subscribers"}:
        raise ShopifyPublishError(
            "This note is marked for a restricted audience, but Shopify products are public.")
    description_text = re.sub(r"^#\s+.+?\s*$\n?", "", body, count=1, flags=re.M)
    if status == "publish" and not description_text.strip():
        raise ShopifyPublishError("Write a product description before making it live.")
    body = _WIKILINK_RE.sub(lambda m: m.group(1).split("|")[-1], body)
    html = markdown.markdown(body.strip(), extensions=["extra", "sane_lists", "smarty"])
    html = ('<p><a href="/pages/contact">Contact us about this offer</a></p>'
            '<p>This is an inquiry listing. The displayed price is a catalog '
            'placeholder, not an agreed service fee.</p>' + html)
    excluded = {tag.casefold() for tag in opts.tag_exclude}
    seen: set[str] = set()
    tags = []
    for tag in _tags(meta.get("tags")) + _tags(opts.project_tag):
        key = tag.casefold()
        if key not in excluded and key not in seen:
            tags.append(tag)
            seen.add(key)
    price = _price(meta.get("price_usd", "1.00"))
    payload = {"title": title, "descriptionHtml": html,
               "productType": str(meta.get("product_type") or "Service").strip(),
               "vendor": str(meta.get("vendor") or "NoFate Technology").strip(),
               "tags": tags}
    handle = str(meta.get("handle") or "").strip()
    if handle:
        payload["handle"] = handle
    seo = {k: str(meta[f"seo_{k}"]).strip() for k in ("title", "description")
           if str(meta.get(f"seo_{k}") or "").strip()}
    if seo:
        payload["seo"] = seo
    product_id = str(meta.get("shopify_product_id") or "").strip()
    if product_id and not re.fullmatch(r"gid://shopify/Product/\d+", product_id):
        raise ShopifyPublishError("shopify_product_id must be a Shopify Product GID.")
    return payload, ProductResult("dry-run", status, title, price, tags=tags), product_id


def _publication_id(creds: ShopifyCreds) -> str:
    data = _graphql(creds, _PUBLICATION)
    if (data.get("shop") or {}).get("currencyCode") != "USD":
        raise ShopifyPublishError("The Shopify store must use USD for price_usd notes.")
    publications = (data.get("publications") or {}).get("nodes") or []
    matches = [p["id"] for p in publications if p.get("name") == "Online Store"]
    if len(matches) != 1:
        raise ShopifyPublishError("Could not find one Online Store publication in Shopify.")
    return matches[0]


def _handleize(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "collection"


def _ensure_collection(creds: ShopifyCreds, title: str, publication_id: str) -> str:
    """ID of the manual collection with this title's handle, creating and
    publishing it to the Online Store if missing. Join-only: a product is never
    removed from collections the note no longer lists."""
    handle = _handleize(title)
    found = _graphql(creds, _COLLECTION_BY_HANDLE, {"handle": handle}).get(
        "collectionByIdentifier") or {}
    if found.get("id"):
        return found["id"]
    created = _checked(creds, "collectionCreate", _COLLECTION_CREATE,
                       {"collection": {"title": title, "handle": handle}})
    collection_id = (created.get("collection") or {}).get("id") or ""
    if not collection_id:
        raise ShopifyPublishError(f"Shopify returned no ID for collection {title!r}.")
    _checked(creds, "publishablePublish", _PUBLISH,
             {"id": collection_id, "input": [{"publicationId": publication_id}]})
    return collection_id


def _single_variant(product: dict) -> dict:
    variants = (product.get("variants") or {}).get("nodes") or []
    if len(variants) != 1:
        raise ShopifyPublishError("This MVP supports products with exactly one variant.")
    variant = variants[0]
    if variant.get("inventoryQuantity") != 0:
        raise ShopifyPublishError(
            "This product has inventory. Clear it in Shopify before using contact-only publishing.")
    return variant


def publish_product(creds: ShopifyCreds, path: Path, *,
                    options: Optional[ProductOptions] = None,
                    dry_run: bool = False) -> ProductResult:
    path = Path(path)
    payload, result, product_id = build_product(path, options)
    if dry_run:
        return result
    creds.validate()
    publication_id = _publication_id(creds)
    meta = frontmatter.load(str(path)).metadata
    existing_store = str(meta.get("shopify_store") or "").strip()
    if product_id and existing_store and existing_store != creds.store:
        raise ShopifyPublishError("This product note belongs to a different Shopify store.")

    if product_id:
        current = get_product(creds, product_id)
        current_variant = _single_variant(current)  # validate before remote edits
        current_item = current_variant.get("inventoryItem") or {}
        if (current.get("status") == "ACTIVE" and
                (current_variant.get("inventoryPolicy") != "DENY" or
                 not current_item.get("tracked"))):
            # Avoid an active, purchasable window while making it contact-only.
            _checked(creds, "productUpdate", _UPDATE,
                     {"product": {"id": product_id, "status": "DRAFT"}})
            current["status"] = "DRAFT"
        _checked(creds, "productUpdate", _UPDATE,
                 {"product": {"id": product_id, **payload}})
        result.action = "updated"
    else:
        created = _checked(creds, "productCreate", _CREATE,
                           {"product": {**payload, "status": "DRAFT"}})
        product_id = (created.get("product") or {}).get("id") or ""
        if not product_id:
            raise ShopifyPublishError("Shopify returned no product ID.")
        # Save the ID before further mutations so retrying cannot duplicate a draft.
        _writeback(path, {"shopify_product_id": product_id, "shopify_store": creds.store})
        current = get_product(creds, product_id)
        result.action = "created"

    variant = _single_variant(current)
    _checked(creds, "productVariantsBulkUpdate", _VARIANT,
             {"productId": product_id, "variants": [
                 {"id": variant["id"], "price": result.price, "inventoryPolicy": "DENY"}]})
    item = variant.get("inventoryItem") or {}
    if not item.get("id"):
        raise ShopifyPublishError("Shopify returned no inventory item for this product.")
    _checked(creds, "inventoryItemUpdate", _INVENTORY,
             {"id": item["id"], "input": {"tracked": True, "requiresShipping": False}})
    checked = get_product(creds, product_id)
    checked_variant = _single_variant(checked)
    checked_item = checked_variant.get("inventoryItem") or {}
    if (checked_variant.get("price") != result.price or
            checked_variant.get("inventoryPolicy") != "DENY" or
            not checked_item.get("tracked") or checked_item.get("requiresShipping")):
        raise ShopifyPublishError("Shopify did not confirm the contact-only product settings.")

    collections = _tags(meta.get("collections"))
    if collections:
        ids = [_ensure_collection(creds, title, publication_id) for title in collections]
        _checked(creds, "productUpdate", _UPDATE,
                 {"product": {"id": product_id, "collectionsToJoin": ids}})
        result.collections = collections

    if result.status == "publish":
        _checked(creds, "publishablePublish", _PUBLISH,
                 {"id": product_id, "input": [{"publicationId": publication_id}]})
        if checked.get("status") != "ACTIVE":
            _checked(creds, "productUpdate", _UPDATE,
                     {"product": {"id": product_id, "status": "ACTIVE"}})
        result.url = f"https://{creds.store}/products/{checked['handle']}"
    elif checked.get("status") != "DRAFT":
        _checked(creds, "productUpdate", _UPDATE,
                 {"product": {"id": product_id, "status": "DRAFT"}})
    final = _graphql(creds, _FINAL, {"id": product_id,
                                    "publicationId": publication_id}).get("product") or {}
    expected_status = "ACTIVE" if result.status == "publish" else "DRAFT"
    if (final.get("status") != expected_status or
            (result.status == "publish" and not final.get("publishedOnPublication"))):
        raise ShopifyPublishError("Shopify did not confirm the requested product visibility.")
    result.product_id = product_id
    _writeback(path, {"shopify_product_id": product_id, "shopify_store": creds.store,
                      "shopify_published_url": result.url,
                      "published_at": date.today().isoformat(), "publish": result.status})
    return result
