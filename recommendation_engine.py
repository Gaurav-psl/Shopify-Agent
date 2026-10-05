"""Personalized product recommendations for authenticated Shopify customers."""
from __future__ import annotations
import re
import shopify_actions


def _words(value: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _product_words(product: dict) -> set[str]:
    values = [product.get("title", ""), product.get("product_type", ""), " ".join(product.get("tags", []) or [])]
    out=set()
    for v in values:
        out |= _words(v)
    return out


def _card(product: dict, store) -> dict:
    variant=(product.get("variants") or [{}])[0] or {}
    price=variant.get("price")
    try:
        price=float(price)
        price=int(price) if price.is_integer() else price
    except (TypeError,ValueError,AttributeError):
        pass
    card={"id":str(variant.get("id") or ""),"name":product.get("title","Unnamed product"),"price":price,"image":product.get("image","")}
    if product.get("handle"):
        card["url"]=f"https://{store.shop_domain}/products/{product['handle']}"
    return card


async def recommend_products(store, customer_profile: dict | None, limit: int=6) -> dict:
    catalog=await shopify_actions.get_active_products_graphql(store, first=100)
    if not catalog:
        return {"results":[],"reason":"catalog_unavailable"}
    profile=customer_profile or {}
    preferred=set()
    for field in ("top_types","top_tags"):
        for value in profile.get(field) or []:
            preferred |= _words(str(value))
    purchased={str(x) for x in (profile.get("purchased_product_ids") or [])}
    scored=[]
    for product in catalog:
        words=_product_words(product)
        score=0.0
        overlap=preferred & words
        if overlap: score += 10*len(overlap)
        if str(product.get("id") or "") in purchased: score -= 8
        else: score += 2
        if words & {"new","new-arrival","new-arrivals"}: score += 2
        scored.append((score,product))
    scored.sort(key=lambda x:x[0],reverse=True)
    return {"results":[_card(p,store) for _,p in scored[:limit]],"reason":"personalized" if preferred or purchased else "catalog"}
