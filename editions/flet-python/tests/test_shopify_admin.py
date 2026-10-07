"""Offline checks for the agent's Shopify management tools (no network)."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter
import shopify_admin as admin
import shopify_products as products
from shopify_publish import ShopifyCreds, ShopifyPublishError
from tools import SHOPIFY_SCHEMAS, ToolContext, execute_tool, schemas_for


STORE = "nofatetech.myshopify.com"
LIVE_THEME = {"id": "gid://shopify/OnlineStoreTheme/1", "name": "Ride", "role": "MAIN"}
COPY_THEME = {"id": "gid://shopify/OnlineStoreTheme/2", "name": "Ride copy",
              "role": "UNPUBLISHED"}


class ShopifyAdminTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.creds = ShopifyCreds(store=STORE, access_token="test-token")

    def note(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_query_guard_refuses_mutations_but_not_strings(self):
        self.assertTrue(admin.is_read_only('{ products(first: 1) { nodes { id } } }'))
        self.assertTrue(admin.is_read_only('{ products(query: "title:mutation") { nodes { id } } }'))
        self.assertFalse(admin.is_read_only('mutation { productDelete(input: {id: "x"}) { deletedProductId } }'))
        self.assertFalse(admin.is_read_only('# hi\n  mutation M { x }'))
        with patch.object(admin, "_graphql") as gql:
            out = admin.run_tool("shopify_query", {"query": "mutation { x }"}, self.creds, self.root)
        gql.assert_not_called()
        self.assertIn("read-only", out)

    def test_query_returns_json_and_parses_string_variables(self):
        with patch.object(admin, "_graphql", return_value={"shop": {"name": "NoFate"}}) as gql:
            out = admin.run_query(self.creds, "query($n: Int) { shop { name } }", '{"n": 1}')
        self.assertIn('"NoFate"', out)
        self.assertEqual(gql.call_args.args[2], {"n": 1})

    def test_build_page_from_markdown(self):
        path = self.note("about.md", "---\ntype: page\npublish: publish\nhandle: about\n"
                                     "template: landing\n---\n# About Us\n\nWe help [[Teams|teams]].\n")
        payload, state, page_id = admin.build_page(path)
        self.assertEqual((state, page_id), ("publish", ""))
        self.assertEqual(payload["title"], "About Us")
        self.assertEqual(payload["handle"], "about")
        self.assertEqual(payload["templateSuffix"], "landing")
        self.assertTrue(payload["isPublished"])
        self.assertIn("<p>We help teams.</p>", payload["body"])
        self.assertNotIn("About Us", payload["body"])

    def test_page_rules(self):
        empty = self.note("empty.md", "---\ntype: page\n---\n# Title only\n")
        with self.assertRaisesRegex(ShopifyPublishError, "body"):
            admin.build_page(empty, status="publish")
        other = self.note("other.md", "---\ntype: note\n---\nx\n")
        with self.assertRaisesRegex(ShopifyPublishError, "type: page"):
            admin.build_page(other)

    def test_publish_page_creates_and_writes_back(self):
        path = self.note("svc.md", "---\ntype: page\npublish: draft\n---\n# Services\n\nBody.\n")
        page = {"id": "gid://shopify/Page/9", "handle": "services", "isPublished": False}
        with patch.object(admin, "_graphql", return_value={"pageCreate": {"page": page}}) as gql:
            out = admin.publish_page(self.creds, path)
        self.assertIn("created page (draft)", out)
        self.assertEqual(gql.call_args.args[2]["page"]["isPublished"], False)
        meta = frontmatter.load(str(path)).metadata
        self.assertEqual(meta["shopify_page_id"], "gid://shopify/Page/9")
        self.assertEqual(meta["shopify_store"], STORE)
        self.assertEqual(meta["publish"], "draft")

    def test_unpublish_product_and_page(self):
        prod = self.note("p.md", "---\ntype: product\npublish: publish\n"
                                 "shopify_product_id: gid://shopify/Product/1\n"
                                 f"shopify_store: {STORE}\n---\nx\n")
        with patch.object(admin, "_graphql",
                          return_value={"productUpdate": {"product": {}}}) as gql:
            admin.unpublish(self.creds, prod, archive=True)
        self.assertEqual(gql.call_args.args[2]["product"]["status"], "ARCHIVED")
        self.assertEqual(frontmatter.load(str(prod)).metadata["publish"], "archived")

        page = self.note("pg.md", "---\ntype: page\npublish: publish\n"
                                  "shopify_page_id: gid://shopify/Page/2\n---\nx\n")
        with patch.object(admin, "_graphql", return_value={"pageUpdate": {"page": {}}}):
            out = admin.unpublish(self.creds, page)
        self.assertIn("page → hidden", out)
        self.assertEqual(frontmatter.load(str(page)).metadata["publish"], "draft")

    def test_unpublish_refuses_other_store_and_unlinked(self):
        other = self.note("o.md", "---\nshopify_product_id: gid://shopify/Product/1\n"
                                  "shopify_store: other.myshopify.com\n---\nx\n")
        with self.assertRaisesRegex(ShopifyPublishError, "different Shopify store"):
            admin.unpublish(self.creds, other)
        plain = self.note("plain.md", "---\ntype: note\n---\nx\n")
        with self.assertRaisesRegex(ShopifyPublishError, "isn't linked"):
            admin.unpublish(self.creds, plain)

    def test_theme_write_refuses_live_theme(self):
        with patch.object(admin, "_graphql",
                          return_value={"themes": {"nodes": [LIVE_THEME, COPY_THEME]}}) as gql:
            with self.assertRaisesRegex(ShopifyPublishError, "live theme"):
                admin.theme_write(self.creds, "1", "sections/x.liquid", "hi")
        self.assertEqual(gql.call_count, 1)  # only the theme lookup, no upsert

    def test_theme_write_to_copy_returns_preview(self):
        responses = [{"themes": {"nodes": [LIVE_THEME, COPY_THEME]}},
                     {"themeFilesUpsert": {"upsertedThemeFiles": [{"filename": "a"}]}}]
        with patch.object(admin, "_graphql", side_effect=responses) as gql:
            out = admin.theme_write(self.creds, COPY_THEME["id"], "sections/a.liquid", "<p>x</p>")
        files = gql.call_args.args[2]["files"]
        self.assertEqual(files[0]["body"], {"type": "TEXT", "value": "<p>x</p>"})
        self.assertIn("preview_theme_id=2", out)

    def test_theme_files_reads_live_theme_by_default(self):
        responses = [{"themes": {"nodes": [COPY_THEME, LIVE_THEME]}},
                     {"theme": {"files": {"nodes": [
                         {"filename": "layout/theme.liquid", "size": 5,
                          "body": {"content": "hello"}}]}}}]
        with patch.object(admin, "_graphql", side_effect=responses) as gql:
            out = admin.theme_files(self.creds, filenames=["layout/theme.liquid"])
        self.assertEqual(gql.call_args.args[2]["id"], LIVE_THEME["id"])
        self.assertIn("=== layout/theme.liquid ===\nhello", out)

    def test_sync_status_flags_drift_and_unlinked_items(self):
        path = self.note("offer.md", "---\ntype: product\npublish: draft\n"
                                     "shopify_product_id: gid://shopify/Product/1\n---\nx\n")
        old = time.time() - 3600
        os.utime(path, (old, old))
        responses = [
            {"nodes": [{"id": "gid://shopify/Product/1", "title": "Offer", "status": "ACTIVE",
                        "handle": "offer", "updatedAt": "2020-01-01T00:00:00Z"}]},
            {"products": {"nodes": [{"id": "gid://shopify/Product/2", "title": "Shirt",
                                     "status": "ACTIVE"}]},
             "pages": {"nodes": []}, "articles": {"nodes": []}}]
        with patch.object(admin, "_graphql", side_effect=responses):
            out = admin.sync_status(self.creds, self.root)
        self.assertIn("note edited after last push", out)
        self.assertIn("note says publish: draft", out)
        self.assertIn("“Shirt”", out)

    def test_upload_image_attaches_to_product(self):
        img = self.root / "hero.png"
        img.write_bytes(b"\x89PNG fake")
        self.note("offer.md", "---\ntype: product\nshopify_product_id: gid://shopify/Product/1\n---\nx\n")
        staged = {"stagedUploadsCreate": {"stagedTargets": [{
            "url": "https://upload.example", "resourceUrl": "https://upload.example/r",
            "parameters": [{"name": "key", "value": "k"}]}]}}
        with patch.object(admin, "_graphql",
                          side_effect=[staged, {"productUpdate": {"product": {}}}]) as gql, \
                patch.object(admin.httpx, "post", return_value=MagicMock(status_code=201)) as post:
            out = admin.upload_image(self.creds, self.root, "hero.png", "offer.md", "Hero")
        self.assertEqual(post.call_args.kwargs["data"], {"key": "k"})
        media = gql.call_args.args[2]["media"][0]
        self.assertEqual(media, {"originalSource": "https://upload.example/r",
                                 "mediaContentType": "IMAGE", "alt": "Hero"})
        self.assertIn("Attached hero.png", out)

    def test_upload_rejects_non_images(self):
        doc = self.note("notes.md", "x")
        out = admin.run_tool("shopify_upload_image", {"image": str(doc)}, self.creds, self.root)
        self.assertIn("not an image", out)

    def test_tools_advertised_only_with_shopify_fn(self):
        names = {s["function"]["name"] for s in SHOPIFY_SCHEMAS}
        ctx = ToolContext(vault_root=self.root)
        self.assertFalse(names & {s["function"]["name"] for s in schemas_for(ctx)})
        self.assertIn("unavailable", execute_tool("shopify_query", {"query": "{x}"}, ctx))
        ctx.shopify_fn = lambda name, args: f"ran {name}"
        self.assertTrue(names <= {s["function"]["name"] for s in schemas_for(ctx)})
        self.assertEqual(execute_tool("shopify_status", {}, ctx), "ran shopify_status")


class ProductExtrasTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.creds = ShopifyCreds(store=STORE, access_token="test-token")

    def test_handle_and_seo_from_frontmatter(self):
        path = Path(self.folder.name) / "o.md"
        path.write_text("---\ntype: product\nhandle: diag\nseo_title: Diagnose\n"
                        "seo_description: Find the bottleneck\n---\n# Offer\n\nBody\n")
        payload, _result, _id = products.build_product(path)
        self.assertEqual(payload["handle"], "diag")
        self.assertEqual(payload["seo"], {"title": "Diagnose",
                                          "description": "Find the bottleneck"})

    def test_ensure_collection_reuses_or_creates_and_publishes(self):
        with patch.object(products, "_graphql",
                          return_value={"collectionByIdentifier": {"id": "gid://c/1"}}) as gql:
            self.assertEqual(products._ensure_collection(self.creds, "Consulting", "pub"), "gid://c/1")
        self.assertEqual(gql.call_count, 1)
        responses = [{"collectionByIdentifier": None},
                     {"collectionCreate": {"collection": {"id": "gid://c/2"}}},
                     {"publishablePublish": {}}]
        with patch.object(products, "_graphql", side_effect=responses) as gql:
            self.assertEqual(products._ensure_collection(self.creds, "Civic Work!", "pub"), "gid://c/2")
        self.assertEqual(gql.call_args_list[1].args[2]["collection"]["handle"], "civic-work")
        self.assertEqual(gql.call_args_list[2].args[2]["input"], [{"publicationId": "pub"}])


if __name__ == "__main__":
    unittest.main()
