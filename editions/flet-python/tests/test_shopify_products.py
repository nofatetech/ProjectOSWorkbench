"""Offline checks for product note publishing and contact-only safeguards."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter
import shopify_products as products
from shopify_publish import ShopifyCreds, ShopifyPublishError
from models import Project
from vault import create_project_note, scan_project_content


PRODUCT = "gid://shopify/Product/123"
VARIANT = "gid://shopify/ProductVariant/456"
INVENTORY = "gid://shopify/InventoryItem/789"
PUBLICATION = "gid://shopify/Publication/101"


class ShopifyProductTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.note = Path(self.folder.name) / "offer.md"
        self.note.write_text(
            "---\ntype: product\npublish: draft\nprice_usd: 1.00\n"
            "tags: [systems, private, Systems]\n---\n"
            "# A useful offer\n\nSolve a real workflow with [[NoFate|our team]].\n",
            encoding="utf-8")
        self.creds = ShopifyCreds(store="nofatetech.myshopify.com",
                                  access_token="test-token")

    def test_builds_contact_first_product_from_markdown(self):
        payload, result, product_id = products.build_product(
            self.note, products.ProductOptions(
                project_tag="NoFate Media", tag_exclude=["private"]))
        self.assertEqual(product_id, "")
        self.assertEqual(result.status, "draft")
        self.assertEqual(result.price, "1.00")
        self.assertEqual(payload["title"], "A useful offer")
        self.assertEqual(payload["tags"], ["systems", "NoFate Media"])
        self.assertIn('href="/pages/contact"', payload["descriptionHtml"])
        self.assertIn("<p>Solve a real workflow with our team.</p>",
                      payload["descriptionHtml"])

    def test_new_product_note_appears_in_project_posts(self):
        root = Path(self.folder.name)
        folder = root / "10_Projects" / "Demo"
        folder.mkdir(parents=True)
        (folder / "Demo.md").write_text("---\ntype: project\n---\n# Demo\n")
        path = create_project_note(folder, "product", "A useful offer")
        self.assertEqual(frontmatter.load(str(path)).metadata["price_usd"], 1.0)
        project = Project(id="p_demo", name="Demo", vault_folder="10_Projects/Demo/")
        content = scan_project_content(project, root)
        self.assertEqual([note.path for note in content.posts], [str(path)])

    def test_create_then_update_same_product(self):
        state = {"exists": False, "status": "DRAFT", "price": "0.00",
                 "tracked": False, "shipping": True, "policy": "DENY",
                 "quantity": 0, "published": False}
        calls = []

        def product():
            return {"id": PRODUCT, "title": "A useful offer", "handle": "a-useful-offer",
                    "status": state["status"],
                    "publishedOnPublication": state["published"], "variants": {"nodes": [{
                        "id": VARIANT, "price": state["price"],
                        "inventoryPolicy": state["policy"],
                        "inventoryQuantity": state["quantity"],
                        "inventoryItem": {"id": INVENTORY, "tracked": state["tracked"],
                                          "requiresShipping": state["shipping"]}}]}}

        def fake_graphql(_creds, query, variables=None, **_kwargs):
            calls.append(query)
            if "publications(first:" in query:
                return {"shop": {"currencyCode": "USD"}, "publications": {"nodes": [
                    {"id": PUBLICATION, "name": "Online Store"}]}}
            if "query($id:" in query:
                return {"product": product() if state["exists"] else None}
            if "productCreate(" in query:
                state["exists"] = True
                return {"productCreate": {"product": product(), "userErrors": []}}
            if "productVariantsBulkUpdate(" in query:
                state["price"] = variables["variants"][0]["price"]
                state["policy"] = variables["variants"][0]["inventoryPolicy"]
                return {"productVariantsBulkUpdate": {"userErrors": []}}
            if "inventoryItemUpdate(" in query:
                state["tracked"] = variables["input"]["tracked"]
                state["shipping"] = variables["input"]["requiresShipping"]
                return {"inventoryItemUpdate": {"userErrors": []}}
            if "publishablePublish(" in query:
                state["published"] = True
                return {"publishablePublish": {"userErrors": []}}
            if "productUpdate(" in query:
                state["status"] = variables["product"].get("status", state["status"])
                return {"productUpdate": {"product": product(), "userErrors": []}}
            raise AssertionError("Unexpected GraphQL operation")

        with patch.object(products, "_graphql", side_effect=fake_graphql):
            created = products.publish_product(self.creds, self.note)
            self.assertEqual((created.action, created.status), ("created", "draft"))
            self.assertEqual(frontmatter.load(str(self.note)).metadata["shopify_product_id"],
                             PRODUCT)
            self.assertTrue(state["tracked"])
            self.assertFalse(state["shipping"])
            self.assertEqual(state["price"], "1.00")
            self.assertFalse(state["published"])

            updated = products.publish_product(
                self.creds, self.note, options=products.ProductOptions(status="publish"))
            self.assertEqual((updated.action, updated.status), ("updated", "publish"))
            self.assertEqual(updated.url,
                             "https://nofatetech.myshopify.com/products/a-useful-offer")
            self.assertTrue(state["published"])
            self.assertEqual(state["status"], "ACTIVE")
            self.assertEqual(frontmatter.load(str(self.note)).metadata["publish"],
                             "publish")
        self.assertEqual(sum("productCreate(" in query for query in calls), 1)

    def test_stocked_product_is_rejected_before_update(self):
        self.note.write_text(self.note.read_text().replace(
            "type: product", f"type: product\nshopify_product_id: {PRODUCT}"))
        stocked = {"id": PRODUCT, "variants": {"nodes": [{
            "id": VARIANT, "inventoryQuantity": 2}]}}
        with patch.object(products, "_publication_id", return_value=PUBLICATION), \
                patch.object(products, "get_product", return_value=stocked), \
                patch.object(products, "_checked") as checked:
            with self.assertRaisesRegex(ShopifyPublishError, "inventory"):
                products.publish_product(self.creds, self.note)
            checked.assert_not_called()

    def test_rejects_wrong_note_type_and_invalid_price(self):
        self.note.write_text(self.note.read_text().replace("type: product", "type: post"))
        with self.assertRaisesRegex(ShopifyPublishError, "type: product"):
            products.build_product(self.note)
        self.note.write_text(self.note.read_text().replace("type: post", "type: product")
                             .replace("price_usd: 1.00", "price_usd: 1.001"))
        with self.assertRaisesRegex(ShopifyPublishError, "price_usd"):
            products.build_product(self.note)

    def test_blank_product_can_be_draft_but_not_live(self):
        self.note.write_text("---\ntype: product\npublish: draft\n---\n\n# Empty\n")
        self.assertEqual(products.build_product(self.note)[1].status, "draft")
        with self.assertRaisesRegex(ShopifyPublishError, "description"):
            products.build_product(self.note, products.ProductOptions(status="publish"))

    def test_restricted_product_cannot_go_live(self):
        self.note.write_text(self.note.read_text().replace(
            "publish: draft", "publish: publish\naudience: subscribers"))
        with self.assertRaisesRegex(ShopifyPublishError, "public"):
            products.build_product(self.note)

    def test_rejects_product_from_another_store(self):
        self.note.write_text(self.note.read_text().replace(
            "type: product", f"type: product\nshopify_product_id: {PRODUCT}\n"
            "shopify_store: another.myshopify.com"))
        with patch.object(products, "_publication_id", return_value=PUBLICATION), \
                patch.object(products, "get_product") as get_product:
            with self.assertRaisesRegex(ShopifyPublishError, "different Shopify store"):
                products.publish_product(self.creds, self.note)
            get_product.assert_not_called()


if __name__ == "__main__":
    unittest.main()
