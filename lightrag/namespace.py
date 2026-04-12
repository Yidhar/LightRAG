from __future__ import annotations

from typing import Iterable


# All namespace should not be changed
class NameSpace:
    KV_STORE_FULL_DOCS = "full_docs"
    KV_STORE_TEXT_CHUNKS = "text_chunks"
    KV_STORE_LLM_RESPONSE_CACHE = "llm_response_cache"
    KV_STORE_FULL_ENTITIES = "full_entities"
    KV_STORE_FULL_RELATIONS = "full_relations"
    KV_STORE_ENTITY_CHUNKS = "entity_chunks"
    KV_STORE_RELATION_CHUNKS = "relation_chunks"
    # Multimodal: sidecar metadata for image blobs (caption JSON, source doc
    # backlink, page / bbox info when extracted from PDF, etc.). The blob
    # bytes themselves live in BLOB_STORE_IMAGES.
    KV_STORE_IMAGE_META = "image_metadata"

    VECTOR_STORE_ENTITIES = "entities"
    VECTOR_STORE_RELATIONSHIPS = "relationships"
    VECTOR_STORE_CHUNKS = "chunks"
    # Multimodal: image-side vectors produced by the multimodal embedding
    # model (tongyi-embedding-vision-plus / CLIP-family). Queried with
    # text-side encoded queries for cross-modal retrieval.
    VECTOR_STORE_IMAGES = "images"

    GRAPH_STORE_CHUNK_ENTITY_RELATION = "chunk_entity_relation"

    DOC_STATUS = "doc_status"

    # Multimodal: raw image bytes keyed by content-hashed blob id.
    BLOB_STORE_IMAGES = "image_blobs"


def is_namespace(namespace: str, base_namespace: str | Iterable[str]):
    if isinstance(base_namespace, str):
        return namespace.endswith(base_namespace)
    return any(is_namespace(namespace, ns) for ns in base_namespace)
