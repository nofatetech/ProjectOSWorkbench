"""Offline checks for the Shopify create/update and public-audience boundaries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter
import httpx
import shopify_publish as shopify


BLOG = "gid://shopify/Blog/123"
ARTICLE = "gid://shopify/Article/456"


class ShopifyPublishTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.note = Path(self.folder.name) / "post.md"
        self.note.write_text(
            "---\ntype: post\ntags: [news, private, News]\npublish: draft\n---\n"
            "# A field note\n\nBody with [[Internal|a link]].\n", encoding="utf-8")
        self.creds = shopify.ShopifyCreds(store="nofatetech.myshopify.com",
                                          access_token="test-token")

    def test_builds_markdown_and_filters_tags(self):
        payload, result, article_id = shopify.build_article(
            self.note, BLOG, "Owner", shopify.ShopifyOptions(
                project_tag="Project X", tag_exclude=["private"]))
        self.assertEqual(article_id, "")
        self.assertEqual(result.status, "draft")
        self.assertEqual(payload["title"], "A field note")
        self.assertEqual(payload["tags"], ["news", "Project X"])
        self.assertIn("<p>Body with a link.</p>", payload["body"])
        self.assertNotIn("<h1>", payload["body"])

    def test_create_then_update_same_article(self):
        calls = []

        def fake_graphql(_creds, query, variables=None, **_kwargs):
            calls.append((query, variables))
            if "query($id:" in query:
                return {"article": {"id": ARTICLE, "isPublished": False,
                                    "blog": {"id": BLOG, "handle": "news"}}}
            article = {"id": ARTICLE, "title": "A field note", "handle": "a-field-note",
                       "isPublished": bool(variables["article"]["isPublished"]),
                       "blog": {"id": BLOG, "handle": "news"}}
            if "articleCreate" in query:
                return {"articleCreate": {"article": article, "userErrors": []}}
            if "articleUpdate" in query:
                return {"articleUpdate": {"article": article, "userErrors": []}}
            raise AssertionError("Unexpected GraphQL operation")

        with patch.object(shopify, "_graphql", side_effect=fake_graphql):
            created = shopify.publish_note(self.creds, self.note, blog_id=BLOG, author="Owner")
            self.assertEqual(created.action, "created")
            self.assertEqual(created.status, "draft")
            meta = frontmatter.load(str(self.note)).metadata
            self.assertEqual(meta["shopify_article_id"], ARTICLE)
            self.assertEqual(meta["shopify_store"], "nofatetech.myshopify.com")

            updated = shopify.publish_note(
                self.creds, self.note, blog_id=BLOG, author="Owner",
                options=shopify.ShopifyOptions(status="publish"))
            self.assertEqual(updated.action, "updated")
            self.assertEqual(updated.status, "publish")
            self.assertEqual(updated.url,
                             "https://nofatetech.myshopify.com/blogs/news/a-field-note")
            self.assertEqual(frontmatter.load(str(self.note)).metadata["shopify_article_id"],
                             ARTICLE)
        self.assertEqual(sum("articleCreate" in q for q, _ in calls), 1)
        self.assertEqual(sum("articleUpdate" in q for q, _ in calls), 1)

    def test_restricted_note_cannot_go_live(self):
        self.note.write_text(self.note.read_text().replace("publish: draft",
                                                          "publish: publish\naudience: subscribers"))
        with self.assertRaisesRegex(shopify.ShopifyPublishError, "public"):
            shopify.build_article(self.note, BLOG, "Owner")

    def test_product_note_cannot_be_sent_as_blog_article(self):
        self.note.write_text(self.note.read_text().replace("type: post", "type: product"))
        with self.assertRaisesRegex(shopify.ShopifyPublishError, "product publisher"):
            shopify.build_article(self.note, BLOG, "Owner")

    def test_update_refuses_article_from_another_blog(self):
        self.note.write_text(self.note.read_text().replace(
            "type: post", f"type: post\nshopify_article_id: {ARTICLE}"))
        with patch.object(shopify, "get_article", return_value={
            "id": ARTICLE, "blog": {"id": "gid://shopify/Blog/other"}}), \
                patch.object(shopify, "_graphql") as graphql:
            with self.assertRaisesRegex(shopify.ShopifyPublishError, "different Shopify blog"):
                shopify.publish_note(self.creds, self.note, blog_id=BLOG, author="Owner")
            graphql.assert_not_called()

    def test_invalid_store_rejected_before_network(self):
        with self.assertRaises(shopify.ShopifyPublishError):
            shopify.ShopifyCreds(store="example.com", access_token="token").validate()

    def test_token_error_reports_plain_text_without_echoing_credentials(self):
        creds = shopify.ShopifyCreds(store="nofatetech.myshopify.com",
                                    client_id="test-client", client_secret="test-secret")
        response = httpx.Response(400, text=(
            "Oauth error shop_not_permitted: Client credentials cannot be performed "
            "on this shop (test-client, test-secret)."))
        with patch.object(shopify.httpx, "post", return_value=response):
            with self.assertRaises(shopify.ShopifyPublishError) as caught:
                shopify.list_blogs(creds)
        message = str(caught.exception)
        self.assertIn("Token request failed", message)
        self.assertIn("shop_not_permitted", message)
        self.assertIn("same Shopify organization", message)
        self.assertNotIn("test-secret", message)
        self.assertNotIn("test-client", message)

    def test_html_oauth_error_does_not_include_page_css(self):
        creds = shopify.ShopifyCreds(store="nofatetech.myshopify.com",
                                    client_id="test-client", client_secret="test-secret")
        response = httpx.Response(400, text=(
            "400 - Oauth error app_not_installed <style>* { border:0; margin:0; }</style>"))
        with patch.object(shopify.httpx, "post", return_value=response):
            with self.assertRaises(shopify.ShopifyPublishError) as caught:
                shopify.list_blogs(creds)
        message = str(caught.exception)
        self.assertIn("app_not_installed", message)
        self.assertIn("Install this app", message)
        self.assertNotIn("border:0", message)


if __name__ == "__main__":
    unittest.main()
