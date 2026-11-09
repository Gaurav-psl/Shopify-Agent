"""Qdrant access. One collection holds every store ('tenant'); every read and write is filtered by tenant.
Each point has a dense vector (semantic) and a sparse vector (keywords, IDF applied by Qdrant)."""
import asyncio
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from . import config
from .errors import ServiceError

_client: AsyncQdrantClient | None = None
_ready = False      # collection exists AND its vector size was verified (set by ensure_collection)
_known = False      # collection is known to exist (cached so searches don't pay an extra round trip)
_lock = asyncio.Lock()
_NS = uuid.UUID("6f1c2d52-8f0e-4f0b-9a57-3d0e5c1b7a11")


def client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        _client = (AsyncQdrantClient(url=config.QDRANT_URL, api_key=config.QDRANT_API_KEY, timeout=15)
                   if config.QDRANT_URL else AsyncQdrantClient(path=config.QDRANT_PATH))
    return _client


async def close() -> None:
    global _client, _ready, _known
    if _client is not None:
        await _client.close()
    _client, _ready, _known = None, False, False


def point_id(tenant: str, doc_id: str, idx: int) -> str:
    return str(uuid.uuid5(_NS, f"{tenant}|{doc_id}|{idx}"))


def _tenant_filter(tenant: str, *extra: qm.Condition) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key="tenant", match=qm.MatchValue(value=tenant)), *extra])


async def collection_exists() -> bool:
    global _known
    if _known:
        return True
    _known = await client().collection_exists(config.COLLECTION)
    return _known


async def ensure_collection(dim: int) -> None:
    """Creates the collection on first ingest; refuses to run if its vector size no longer matches the embedder."""
    global _ready
    if _ready:
        return
    async with _lock:
        c = client()
        if not await c.collection_exists(config.COLLECTION):
            await c.create_collection(
                config.COLLECTION,
                vectors_config={"dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)},
                sparse_vectors_config={"sparse": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
            )
            for field in ("tenant", "doc_id", "kind", "policy_type"):
                try:
                    schema = (qm.KeywordIndexParams(type="keyword", is_tenant=True) if field == "tenant"
                              else qm.PayloadSchemaType.KEYWORD)
                    await c.create_payload_index(config.COLLECTION, field, schema)
                except Exception as e:  # noqa: BLE001  (embedded mode ignores indexes)
                    print(f"store: payload index {field}: {e!r}")
        else:
            info = await c.get_collection(config.COLLECTION)
            vecs = info.config.params.vectors
            size = vecs["dense"].size if isinstance(vecs, dict) else getattr(vecs, "size", None)
            if size != dim:
                raise ServiceError(500, f"collection '{config.COLLECTION}' has vector size {size} but the embedder "
                                        f"returns {dim}. Use a new QDRANT_COLLECTION and re-ingest.")
        _ready = _known = True


# ----------------------------------------------------------------------------- write
async def get_doc_hash(tenant: str, doc_id: str) -> str | None:
    if not await collection_exists():
        return None
    got = await client().retrieve(config.COLLECTION, [point_id(tenant, doc_id, 0)], with_payload=["doc_hash"])
    return (got[0].payload or {}).get("doc_hash") if got else None


async def upsert_chunks(tenant: str, doc_id: str, rows: list[dict]) -> None:
    """rows: {payload, dense, sparse(indices, values)|None}. Upserts first, then deletes chunks left over from a
    longer previous version, so a document is never briefly empty while it is being replaced."""
    points = []
    for i, r in enumerate(rows):
        vec: dict = {"dense": r["dense"]}
        if r.get("sparse"):
            vec["sparse"] = qm.SparseVector(indices=r["sparse"][0], values=r["sparse"][1])
        points.append(qm.PointStruct(id=point_id(tenant, doc_id, i), vector=vec,
                                     payload={**r["payload"], "tenant": tenant, "doc_id": doc_id, "chunk_idx": i}))
    c = client()
    for i in range(0, len(points), 64):
        await c.upsert(config.COLLECTION, points[i:i + 64], wait=True)
    await c.delete(config.COLLECTION, points_selector=qm.FilterSelector(filter=_tenant_filter(
        tenant, qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id)),
        qm.FieldCondition(key="chunk_idx", range=qm.Range(gte=len(rows))))), wait=True)


async def delete_doc(tenant: str, doc_id: str) -> None:
    if await collection_exists():
        await client().delete(config.COLLECTION, points_selector=qm.FilterSelector(filter=_tenant_filter(
            tenant, qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id)))), wait=True)


async def delete_tenant(tenant: str) -> None:
    if await collection_exists():
        await client().delete(config.COLLECTION, points_selector=qm.FilterSelector(filter=_tenant_filter(tenant)),
                              wait=True)


# ----------------------------------------------------------------------------- read
async def list_docs(tenant: str) -> list[dict]:
    if not await collection_exists():
        return []
    docs, offset = [], None
    flt = _tenant_filter(tenant, qm.FieldCondition(key="chunk_idx", match=qm.MatchValue(value=0)))
    while True:
        pts, offset = await client().scroll(config.COLLECTION, scroll_filter=flt, limit=256, offset=offset,
                                            with_payload=["doc_id", "title", "kind", "policy_type", "url",
                                                          "n_chunks", "updated_at"])
        docs += [p.payload for p in pts]
        if offset is None:
            return sorted(docs, key=lambda d: d.get("doc_id", ""))


async def count(tenant: str) -> int:
    if not await collection_exists():
        return 0
    return (await client().count(config.COLLECTION, count_filter=_tenant_filter(tenant), exact=True)).count


def _search_filter(tenant: str, policy_types: list[str] | None, kinds: list[str] | None) -> qm.Filter:
    extra = []
    if policy_types:
        extra.append(qm.FieldCondition(key="policy_type", match=qm.MatchAny(any=policy_types)))
    if kinds:
        extra.append(qm.FieldCondition(key="kind", match=qm.MatchAny(any=kinds)))
    return _tenant_filter(tenant, *extra)


async def search(tenant: str, using: str, query, limit: int, policy_types=None, kinds=None) -> list[dict]:
    """One ranked list: [{id, score, payload}]. `using` is 'dense' (cosine) or 'sparse' (keyword)."""
    res = await client().query_points(
        config.COLLECTION, query=query, using=using, limit=limit, with_payload=True,
        query_filter=_search_filter(tenant, policy_types, kinds))
    return [{"id": str(p.id), "score": float(p.score), "payload": p.payload or {}} for p in res.points]


def sparse_query(indices: list[int], values: list[float]) -> qm.SparseVector:
    return qm.SparseVector(indices=indices, values=values)
