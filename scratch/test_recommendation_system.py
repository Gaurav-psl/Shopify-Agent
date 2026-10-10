import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import product_enrichment
import activity_tracker
import recommendation_engine
from types import SimpleNamespace


class TestRecommendationSystem(unittest.TestCase):

    def setUp(self):
        activity_tracker.clear_cache_for_testing()

    def test_product_enrichment_and_subcategorization(self):
        sample_products = [
            {
                "id": 101,
                "title": "Oversized Vintage Anime Graphic Tee",
                "product_type": "T-Shirts",
                "tags": "streetwear, anime, cotton, oversized, drop shoulder, black",
                "variants": [{"id": 1001, "price": "1299"}],
                "handle": "oversized-anime-tee",
            },
            {
                "id": 102,
                "title": "Heavyweight Pullover Fleece Hoodie",
                "product_type": "Sweatshirts",
                "tags": "winter, fleece, grey, minimal",
                "variants": [{"id": 1002, "price": "2499"}],
                "handle": "heavyweight-pullover-hoodie",
            },
            {
                "id": 103,
                "title": "Tactical Multi-Pocket Cargo Pants",
                "product_type": "Pants",
                "tags": "cargo, urban, green, olive, relaxed fit",
                "variants": [{"id": 1003, "price": "1899"}],
                "handle": "tactical-cargo-pants",
            },
            {
                "id": 104,
                "title": "Classic White Tennis Sneakers",
                "product_type": "Shoes",
                "tags": "sneakers, leather, white, casual",
                "variants": [{"id": 1004, "price": "3499"}],
                "handle": "classic-white-sneakers",
            },
        ]

        enriched = product_enrichment.enrich_catalog(sample_products)
        self.assertEqual(len(enriched), 4)

        # Check Product 101 (Graphic Tee)
        p1 = enriched[0]["_metadata"]
        self.assertEqual(p1["category"], "Apparel")
        self.assertEqual(p1["sub_category"], "Graphic Tees")
        self.assertEqual(p1["style"], "Streetwear")
        self.assertEqual(p1["fit"], "Oversized")
        self.assertEqual(p1["primary_color"], "Black")
        self.assertEqual(p1["price_bracket"], "Mid-range")

        # Check Product 102 (Hoodie)
        p2 = enriched[1]["_metadata"]
        self.assertEqual(p2["sub_category"], "Hoodies & Sweatshirts")
        self.assertEqual(p2["price_bracket"], "Premium")

        # Check Product 103 (Cargo Pants)
        p3 = enriched[2]["_metadata"]
        self.assertEqual(p3["sub_category"], "Cargo Pants")
        self.assertEqual(p3["primary_color"], "Green")

        # Check Product 104 (Footwear)
        p4 = enriched[3]["_metadata"]
        self.assertEqual(p4["category"], "Footwear")
        self.assertEqual(p4["sub_category"], "Sneakers")

    def test_user_activity_tracking_and_aggregation(self):
        shop = "test-store.myshopify.com"
        visitor_id = "vis_abc123"

        # Record 3 clicks on Product 101 (Graphic Tee)
        for _ in range(3):
            activity_tracker.track_activity(
                shop=shop,
                visitor_id=visitor_id,
                event_type="product_click",
                product_id="101",
                product_name="Oversized Anime Tee",
                category="Apparel",
                sub_category="Graphic Tees",
                style="Streetwear",
                price=1299.0,
            )

        # Record 1 view on Product 102 (Hoodie)
        activity_tracker.track_activity(
            shop=shop,
            visitor_id=visitor_id,
            event_type="product_view",
            product_id="102",
            product_name="Heavyweight Hoodie",
            category="Apparel",
            sub_category="Hoodies & Sweatshirts",
            style="Minimalist",
            price=2499.0,
        )

        summary = activity_tracker.get_user_activity_summary(shop, visitor_id)
        self.assertEqual(summary["total_clicks"], 3)
        self.assertEqual(summary["product_click_counts"].get("101"), 3)
        self.assertIn("101", summary["clicked_product_ids"])
        self.assertIn("102", summary["viewed_product_ids"])

        # Check top subcategories
        top_subcat_names = [s for s, _ in summary["top_subcategories"]]
        self.assertEqual(top_subcat_names[0], "Graphic Tees")

        # Check trending
        trending = activity_tracker.get_trending_product_ids(shop)
        self.assertIn("101", trending)

    async def _async_test_recommendations_click_affinity(self):
        shop = "test-store.myshopify.com"
        store = SimpleNamespace(shop_domain=shop)
        visitor_id = "vis_streetwear_fan"

        catalog = [
            {"id": "101", "title": "Tokyo Drifter Anime Tee", "tags": "graphic, tee, anime, black", "variants": [{"id": 1001, "price": "1200"}]},
            {"id": "102", "title": "Cyberpunk Oversized Tee", "tags": "graphic, tee, streetwear, black", "variants": [{"id": 1002, "price": "1100"}]},
            {"id": "103", "title": "Formal Oxford Button Down Shirt", "tags": "formal, shirt, white", "variants": [{"id": 1003, "price": "1500"}]},
            {"id": "104", "title": "Minimalist Crewneck Sweatshirt", "tags": "minimal, sweatshirt, grey", "variants": [{"id": 1004, "price": "2200"}]},
        ]

        # Simulate user having clicked "Cyberpunk Oversized Tee" twice
        for _ in range(2):
            activity_tracker.track_activity(
                shop=shop,
                visitor_id=visitor_id,
                event_type="product_click",
                product_id="102",
                product_name="Cyberpunk Oversized Tee",
                category="Apparel",
                sub_category="Graphic Tees",
                style="Streetwear",
            )

        with patch("shopify_actions.get_active_products_graphql", return_value=catalog):
            result = await recommendation_engine.recommend_products(
                store=store,
                visitor_id=visitor_id,
                limit=3,
            )

        results = result["results"]
        self.assertGreaterEqual(len(results), 2)
        # Top recommended product must be product 102 (directly clicked) or 101 (same subcategory)
        top_ids = [p["product_id"] for p in results[:2]]
        self.assertIn("102", top_ids)
        self.assertIn("101", top_ids)
        self.assertEqual(result["reason"], "personalized_activity")

    def test_recommendations_click_affinity_sync(self):
        import asyncio
        asyncio.run(self._async_test_recommendations_click_affinity())

    async def _async_test_cold_start_and_purchase_suppression(self):
        shop = "test-store.myshopify.com"
        store = SimpleNamespace(shop_domain=shop)
        
        catalog = [
            {"id": "201", "title": "Signature Heavyweight Hoodie", "tags": "hoodie, streetwear", "variants": [{"id": 2001, "price": "2500"}]},
            {"id": "202", "title": "Everyday Relaxed Sweatpants", "tags": "sweatpants, casual", "variants": [{"id": 2002, "price": "1800"}]},
            {"id": "203", "title": "Embroidered Beanie Hat", "tags": "beanie, hat, winter", "variants": [{"id": 2003, "price": "799"}]},
        ]

        # Customer already bought product 201
        customer_profile = {
            "customer_id": "cust_999",
            "purchased_product_ids": ["201"],
            "favorite_colors": ["black"],
        }

        with patch("shopify_actions.get_active_products_graphql", return_value=catalog):
            result = await recommendation_engine.recommend_products(
                store=store,
                customer_profile=customer_profile,
                customer_id="cust_999",
                limit=2,
            )

        top_pids = [p["product_id"] for p in result["results"]]
        # Product 201 should be suppressed because it was already purchased
        self.assertNotIn("201", top_pids)
        self.assertIn("202", top_pids)

    def test_cold_start_and_purchase_suppression_sync(self):
        import asyncio
        asyncio.run(self._async_test_cold_start_and_purchase_suppression())

    def test_tracking_api_endpoints(self):
        from fastapi.testclient import TestClient
        from chatbot_widget import router
        from fastapi import FastAPI
        test_app = FastAPI()
        test_app.include_router(router)
        client = TestClient(test_app)

        # POST /track-activity
        track_resp = client.post("/track-activity", json={
            "shop": "test-store.myshopify.com",
            "visitor_id": "test_visitor_99",
            "event_type": "product_click",
            "product_id": "555",
            "product_name": "Retro Neon Hoodie",
            "category": "Apparel",
            "sub_category": "Hoodies & Sweatshirts",
            "price": 2499.0
        })
        self.assertEqual(track_resp.status_code, 200)
        self.assertEqual(track_resp.json().get("status"), "ok")

        # GET /user-activity
        get_resp = client.get("/user-activity?shop=test-store.myshopify.com&visitor_id=test_visitor_99")
        self.assertEqual(get_resp.status_code, 200)
        data = get_resp.json()
        self.assertEqual(data.get("total_clicks"), 1)
        self.assertIn("555", data.get("clicked_product_ids", []))


if __name__ == "__main__":
    unittest.main()
