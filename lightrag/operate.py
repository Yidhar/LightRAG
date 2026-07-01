from __future__ import annotations
from functools import partial
from pathlib import Path

import asyncio
import json
import json_repair
from typing import Any, AsyncIterator, overload, Literal
from collections import Counter, defaultdict

from lightrag.exceptions import (
    PipelineCancelledException,
    ChunkTokenLimitExceededError,
)
from lightrag.utils import (
    logger,
    compute_mdhash_id,
    Tokenizer,
    is_float_regex,
    sanitize_and_normalize_extracted_text,
    pack_user_ass_to_openai_messages,
    split_string_by_multi_markers,
    truncate_list_by_token_size,
    compute_args_hash,
    generate_cache_key,
    handle_cache,
    save_to_cache,
    CacheData,
    use_llm_func_with_cache,
    update_chunk_cache_list,
    remove_think_tags,
    pick_by_weighted_polling,
    pick_by_vector_similarity,
    process_chunks_unified,
    safe_vdb_operation_with_exception,
    create_prefixed_exception,
    fix_tuple_delimiter_corruption,
    convert_to_user_format,
    generate_reference_list_from_chunks,
    apply_source_ids_limit,
    merge_source_ids,
    make_relation_chunk_key,
    _cooperative_yield,
    performance_timing_log,
)
from lightrag.base import (
    BaseGraphStorage,
    BaseKVStorage,
    BaseVectorStorage,
    TextChunkSchema,
    QueryParam,
    QueryResult,
    QueryContextResult,
)
from lightrag.prompt import PROMPTS
from lightrag.constants import (
    GRAPH_FIELD_SEP,
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_RELATED_CHUNK_NUMBER,
    DEFAULT_KG_CHUNK_PICK_METHOD,
    DEFAULT_ENTITY_TYPES,
    DEFAULT_SUMMARY_LANGUAGE,
    SOURCE_IDS_LIMIT_METHOD_KEEP,
    SOURCE_IDS_LIMIT_METHOD_FIFO,
    DEFAULT_FILE_PATH_MORE_PLACEHOLDER,
    DEFAULT_MAX_FILE_PATHS,
    DEFAULT_ENTITY_NAME_MAX_LENGTH,
)
from lightrag.kg.shared_storage import get_storage_keyed_lock
from lightrag.llm.openai_batch import OpenAIBatchRequest, run_openai_chat_batch
import time
from dotenv import load_dotenv

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)


def _truncate_entity_identifier(
    identifier: str, limit: int, chunk_key: str, identifier_role: str
) -> str:
    """Truncate entity identifiers that exceed the configured length limit."""

    if len(identifier) <= limit:
        return identifier

    display_value = identifier[:limit]
    preview = identifier[:20]  # Show first 20 characters as preview
    logger.warning(
        "%s: %s len %d > %d chars (Name: '%s...')",
        chunk_key,
        identifier_role,
        len(identifier),
        limit,
        preview,
    )
    return display_value


def _normalize_vdb_compare_value(key: str, value: Any) -> Any:
    """Normalize VDB payload values so semantically-equal rows compare equal."""
    if value is None:
        return None

    if key in {"source_id", "file_path"} and isinstance(value, str):
        parts = [part.strip() for part in value.split(GRAPH_FIELD_SEP) if part.strip()]
        return tuple(sorted(dict.fromkeys(parts)))

    if key == "weight":
        try:
            return round(float(value), 8)
        except (TypeError, ValueError):
            return value

    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        return tuple(value)

    return value


async def _filter_unchanged_vdb_upsert_batch(
    storage: BaseVectorStorage | None,
    payload: dict[str, dict[str, Any]],
    *,
    label: str,
    min_probe_size: int = 64,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Skip VDB upserts whose persisted payload is unchanged.

    For rebuild / reprocess workflows, the KG layer often recomputes a large
    entity/relation set whose vector payload did not materially change. Those
    rows would otherwise be re-embedded and re-written, which is expensive.
    """
    stats = {
        "total": len(payload),
        "kept": len(payload),
        "skipped": 0,
    }
    if storage is None or not payload or len(payload) < min_probe_size:
        return payload, stats

    compare_keys = set(getattr(storage, "meta_fields", set()) or set()) | {"content"}
    if not compare_keys:
        return payload, stats

    payload_ids = list(payload.keys())
    fetch_start = time.perf_counter()
    try:
        existing_records = await storage.get_by_ids(payload_ids)
    except Exception as e:
        logger.warning(
            f"[vdb-diff] failed to probe existing {label} records before batch "
            f"upsert ({len(payload)} candidates): {type(e).__name__}: {e}"
        )
        return payload, stats

    performance_timing_log(
        "[vdb-diff] %s get_by_ids completed in %.4fs ids=%s",
        label,
        time.perf_counter() - fetch_start,
        len(payload_ids),
    )

    filtered_payload: dict[str, dict[str, Any]] = {}
    skipped = 0
    for record_id, new_record, existing_record in zip(
        payload_ids, payload.values(), existing_records
    ):
        if not isinstance(existing_record, dict):
            filtered_payload[record_id] = new_record
            continue

        unchanged = True
        for key in compare_keys:
            if _normalize_vdb_compare_value(
                key, new_record.get(key)
            ) != _normalize_vdb_compare_value(key, existing_record.get(key)):
                unchanged = False
                break

        if unchanged:
            skipped += 1
        else:
            filtered_payload[record_id] = new_record

    stats["kept"] = len(filtered_payload)
    stats["skipped"] = skipped

    if skipped:
        logger.info(
            f"[vdb-diff] {label}: skipped {skipped}/{len(payload)} unchanged "
            f"records before embedding/upsert"
        )

    return filtered_payload, stats


def chunking_by_token_size(
    tokenizer: Tokenizer,
    content: str,
    split_by_character: str | None = None,
    split_by_character_only: bool = False,
    chunk_overlap_token_size: int = 100,
    chunk_token_size: int = 1200,
) -> list[dict[str, Any]]:
    tokens = tokenizer.encode(content)
    results: list[dict[str, Any]] = []
    if split_by_character:
        raw_chunks = content.split(split_by_character)
        new_chunks = []
        if split_by_character_only:
            for chunk in raw_chunks:
                _tokens = tokenizer.encode(chunk)
                if len(_tokens) > chunk_token_size:
                    logger.warning(
                        "Chunk split_by_character exceeds token limit: len=%d limit=%d",
                        len(_tokens),
                        chunk_token_size,
                    )
                    raise ChunkTokenLimitExceededError(
                        chunk_tokens=len(_tokens),
                        chunk_token_limit=chunk_token_size,
                        chunk_preview=chunk[:120],
                    )
                new_chunks.append((len(_tokens), chunk))
        else:
            for chunk in raw_chunks:
                _tokens = tokenizer.encode(chunk)
                if len(_tokens) > chunk_token_size:
                    for start in range(
                        0, len(_tokens), chunk_token_size - chunk_overlap_token_size
                    ):
                        chunk_content = tokenizer.decode(
                            _tokens[start : start + chunk_token_size]
                        )
                        new_chunks.append(
                            (min(chunk_token_size, len(_tokens) - start), chunk_content)
                        )
                else:
                    new_chunks.append((len(_tokens), chunk))
        for index, (_len, chunk) in enumerate(new_chunks):
            results.append(
                {
                    "tokens": _len,
                    "content": chunk.strip(),
                    "chunk_order_index": index,
                }
            )
    else:
        for index, start in enumerate(
            range(0, len(tokens), chunk_token_size - chunk_overlap_token_size)
        ):
            chunk_content = tokenizer.decode(tokens[start : start + chunk_token_size])
            results.append(
                {
                    "tokens": min(chunk_token_size, len(tokens) - start),
                    "content": chunk_content.strip(),
                    "chunk_order_index": index,
                }
            )
    return results


async def _handle_entity_relation_summary(
    description_type: str,
    entity_or_relation_name: str,
    description_list: list[str],
    separator: str,
    global_config: dict,
    llm_response_cache: BaseKVStorage | None = None,
) -> tuple[str, bool]:
    """Handle entity relation description summary using map-reduce approach.

    This function summarizes a list of descriptions using a map-reduce strategy:
    1. If total tokens < summary_context_size and len(description_list) < force_llm_summary_on_merge, no need to summarize
    2. If total tokens < summary_max_tokens, summarize with LLM directly
    3. Otherwise, split descriptions into chunks that fit within token limits
    4. Summarize each chunk, then recursively process the summaries
    5. Continue until we get a final summary within token limits or num of descriptions is less than force_llm_summary_on_merge

    Args:
        entity_or_relation_name: Name of the entity or relation being summarized
        description_list: List of description strings to summarize
        global_config: Global configuration containing tokenizer and limits
        llm_response_cache: Optional cache for LLM responses

    Returns:
        Tuple of (final_summarized_description_string, llm_was_used_boolean)
    """
    # Handle empty input
    if not description_list:
        return "", False

    # If only one description, return it directly (no need for LLM call)
    if len(description_list) == 1:
        return description_list[0], False

    # Get configuration
    tokenizer: Tokenizer = global_config["tokenizer"]
    summary_context_size = global_config["summary_context_size"]
    summary_max_tokens = global_config["summary_max_tokens"]
    force_llm_summary_on_merge = global_config["force_llm_summary_on_merge"]

    current_list = description_list[:]  # Copy the list to avoid modifying original
    llm_was_used = False  # Track whether LLM was used during the entire process

    # Iterative map-reduce process
    while True:
        # Calculate total tokens in current list while periodically yielding so
        # a large merge does not monopolize the event loop in single-worker mode.
        total_tokens = 0
        for i, desc in enumerate(current_list, start=1):
            total_tokens += len(tokenizer.encode(desc))
            await _cooperative_yield(i, every=32)

        # If total length is within limits, perform final summarization
        if total_tokens <= summary_context_size or len(current_list) <= 2:
            if (
                len(current_list) < force_llm_summary_on_merge
                and total_tokens < summary_max_tokens
            ):
                # no LLM needed, just join the descriptions
                final_description = separator.join(current_list)
                return final_description if final_description else "", llm_was_used
            else:
                if total_tokens > summary_context_size and len(current_list) <= 2:
                    logger.warning(
                        f"Summarizing {entity_or_relation_name}: Oversize description found"
                    )
                # Final summarization of remaining descriptions - LLM will be used
                final_summary = await _summarize_descriptions(
                    description_type,
                    entity_or_relation_name,
                    current_list,
                    global_config,
                    llm_response_cache,
                )
                return final_summary, True  # LLM was used for final summarization

        # Need to split into chunks - Map phase
        # Ensure each chunk has minimum 2 descriptions to guarantee progress
        chunks = []
        current_chunk = []
        current_tokens = 0

        # Currently least 3 descriptions in current_list
        for i, desc in enumerate(current_list, start=1):
            desc_tokens = len(tokenizer.encode(desc))
            await _cooperative_yield(i, every=32)

            # If adding current description would exceed limit, finalize current chunk
            if current_tokens + desc_tokens > summary_context_size and current_chunk:
                # Ensure we have at least 2 descriptions in the chunk (when possible)
                if len(current_chunk) == 1:
                    # Force add one more description to ensure minimum 2 per chunk
                    current_chunk.append(desc)
                    chunks.append(current_chunk)
                    logger.warning(
                        f"Summarizing {entity_or_relation_name}: Oversize description found"
                    )
                    current_chunk = []  # next group is empty
                    current_tokens = 0
                else:  # curren_chunk is ready for summary in reduce phase
                    chunks.append(current_chunk)
                    current_chunk = [desc]  # leave it for next group
                    current_tokens = desc_tokens
            else:
                current_chunk.append(desc)
                current_tokens += desc_tokens

        # Add the last chunk if it exists
        if current_chunk:
            chunks.append(current_chunk)

        logger.info(
            f"   Summarizing {entity_or_relation_name}: Map {len(current_list)} descriptions into {len(chunks)} groups"
        )

        # Reduce phase: summarize each group from chunks.
        # Multi-description groups are dispatched concurrently via
        # asyncio.gather — each LLM call is independent and benefits
        # from the MAX_ASYNC worker pool. This turns the O(K) serial
        # chain of summary calls into O(1) wall time (bounded by the
        # slowest single call).
        async def _reduce_one(chunk_group):
            if len(chunk_group) == 1:
                return chunk_group[0], False
            summary = await _summarize_descriptions(
                description_type,
                entity_or_relation_name,
                chunk_group,
                global_config,
                llm_response_cache,
            )
            return summary, True

        reduce_results = await asyncio.gather(
            *[_reduce_one(chunk_group) for chunk_group in chunks]
        )
        new_summaries = []
        for summary, used_llm in reduce_results:
            new_summaries.append(summary)
            if used_llm:
                llm_was_used = True

        # Update current list with new summaries for next iteration
        current_list = new_summaries


async def _summarize_descriptions(
    description_type: str,
    description_name: str,
    description_list: list[str],
    global_config: dict,
    llm_response_cache: BaseKVStorage | None = None,
) -> str:
    """Helper function to summarize a list of descriptions using LLM.

    Args:
        entity_or_relation_name: Name of the entity or relation being summarized
        descriptions: List of description strings to summarize
        global_config: Global configuration containing LLM function and settings
        llm_response_cache: Optional cache for LLM responses

    Returns:
        Summarized description string
    """
    use_llm_func: callable = global_config["llm_model_func"]
    # Apply higher priority (8) to entity/relation summary tasks
    use_llm_func = partial(use_llm_func, _priority=8)

    language = global_config["addon_params"].get("language", DEFAULT_SUMMARY_LANGUAGE)

    summary_length_recommended = global_config["summary_length_recommended"]

    prompt_template = PROMPTS["summarize_entity_descriptions"]

    # Convert descriptions to JSONL format and apply token-based truncation
    tokenizer = global_config["tokenizer"]
    summary_context_size = global_config["summary_context_size"]

    # Create list of JSON objects with "Description" field
    json_descriptions = [{"Description": desc} for desc in description_list]

    # Use truncate_list_by_token_size for length truncation
    truncated_json_descriptions = truncate_list_by_token_size(
        json_descriptions,
        key=lambda x: json.dumps(x, ensure_ascii=False),
        max_token_size=summary_context_size,
        tokenizer=tokenizer,
    )

    # Convert to JSONL format (one JSON object per line)
    joined_descriptions = "\n".join(
        json.dumps(desc, ensure_ascii=False) for desc in truncated_json_descriptions
    )

    # Prepare context for the prompt
    context_base = dict(
        description_type=description_type,
        description_name=description_name,
        description_list=joined_descriptions,
        summary_length=summary_length_recommended,
        language=language,
    )
    use_prompt = prompt_template.format(**context_base)

    # Use LLM function with cache (higher priority for summary generation)
    summary, _ = await use_llm_func_with_cache(
        use_prompt,
        use_llm_func,
        llm_response_cache=llm_response_cache,
        cache_type="summary",
    )

    # Check summary token length against embedding limit
    embedding_token_limit = global_config.get("embedding_token_limit")
    if embedding_token_limit is not None and summary:
        tokenizer = global_config["tokenizer"]
        summary_token_count = len(tokenizer.encode(summary))
        threshold = int(embedding_token_limit)

        if summary_token_count > threshold:
            logger.warning(
                f"Summary tokens({summary_token_count}) exceeds embedding_token_limit({embedding_token_limit}) "
                f" for {description_type}: {description_name}"
            )

    return summary


def _handle_single_entity_extraction(
    record_attributes: list[str],
    chunk_key: str,
    timestamp: int,
    file_path: str = "unknown_source",
):
    if len(record_attributes) != 4 or "entity" not in record_attributes[0]:
        if len(record_attributes) > 1 and "entity" in record_attributes[0]:
            logger.warning(
                f"{chunk_key}: LLM output format error; found {len(record_attributes)}/4 fields on ENTITY `{record_attributes[1]}` @ `{record_attributes[2] if len(record_attributes) > 2 else 'N/A'}`"
            )
            logger.debug(record_attributes)
        return None

    try:
        entity_name = sanitize_and_normalize_extracted_text(
            record_attributes[1], remove_inner_quotes=True
        )

        # Validate entity name after all cleaning steps
        if not entity_name or not entity_name.strip():
            logger.info(
                f"Empty entity name found after sanitization. Original: '{record_attributes[1]}'"
            )
            return None

        # Process entity type with same cleaning pipeline
        entity_type = sanitize_and_normalize_extracted_text(
            record_attributes[2], remove_inner_quotes=True
        )

        if not entity_type.strip() or any(
            char in entity_type for char in ["'", "(", ")", "<", ">", "|", "/", "\\"]
        ):
            logger.warning(
                f"Entity extraction error: invalid entity type in: {record_attributes}"
            )
            return None

        # Handle comma-separated entity types by finding the first non-empty token
        if "," in entity_type:
            original = entity_type
            tokens = [t.strip() for t in entity_type.split(",")]
            non_empty = [t for t in tokens if t]
            if not non_empty:
                logger.warning(
                    f"Entity extraction error: all tokens empty after comma-split: '{original}'"
                )
                return None
            entity_type = non_empty[0]
            logger.warning(
                f"Entity type contains comma, taking first non-empty token: '{original}' -> '{entity_type}'"
            )

        # Remove spaces and convert to lowercase
        entity_type = entity_type.replace(" ", "").lower()

        # Process entity description with same cleaning pipeline
        entity_description = sanitize_and_normalize_extracted_text(record_attributes[3])

        if not entity_description.strip():
            logger.warning(
                f"Entity extraction error: empty description for entity '{entity_name}' of type '{entity_type}'"
            )
            return None

        return dict(
            entity_name=entity_name,
            entity_type=entity_type,
            description=entity_description,
            source_id=chunk_key,
            file_path=file_path,
            timestamp=timestamp,
        )

    except ValueError as e:
        logger.error(
            f"Entity extraction failed due to encoding issues in chunk {chunk_key}: {e}"
        )
        return None
    except Exception as e:
        logger.error(
            f"Entity extraction failed with unexpected error in chunk {chunk_key}: {e}"
        )
        return None


def _handle_single_relationship_extraction(
    record_attributes: list[str],
    chunk_key: str,
    timestamp: int,
    file_path: str = "unknown_source",
):
    if (
        len(record_attributes) != 5 or "relation" not in record_attributes[0]
    ):  # treat "relationship" and "relation" interchangeable
        if len(record_attributes) > 1 and "relation" in record_attributes[0]:
            logger.warning(
                f"{chunk_key}: LLM output format error; found {len(record_attributes)}/5 fields on RELATION `{record_attributes[1]}`~`{record_attributes[2] if len(record_attributes) > 2 else 'N/A'}`"
            )
            logger.debug(record_attributes)
        return None

    try:
        source = sanitize_and_normalize_extracted_text(
            record_attributes[1], remove_inner_quotes=True
        )
        target = sanitize_and_normalize_extracted_text(
            record_attributes[2], remove_inner_quotes=True
        )

        # Validate entity names after all cleaning steps
        if not source:
            logger.info(
                f"Empty source entity found after sanitization. Original: '{record_attributes[1]}'"
            )
            return None

        if not target:
            logger.info(
                f"Empty target entity found after sanitization. Original: '{record_attributes[2]}'"
            )
            return None

        if source == target:
            logger.debug(
                f"Relationship source and target are the same in: {record_attributes}"
            )
            return None

        # Process keywords with same cleaning pipeline
        edge_keywords = sanitize_and_normalize_extracted_text(
            record_attributes[3], remove_inner_quotes=True
        )
        edge_keywords = edge_keywords.replace("，", ",")

        # Process relationship description with same cleaning pipeline
        edge_description = sanitize_and_normalize_extracted_text(record_attributes[4])
        if not edge_description.strip():
            logger.warning(
                f"Relationship extraction error: empty description for relation '{source}'~'{target}' in chunk '{chunk_key}'"
            )
            return None

        edge_source_id = chunk_key
        weight = (
            float(record_attributes[-1].strip('"').strip("'"))
            if is_float_regex(record_attributes[-1].strip('"').strip("'"))
            else 1.0
        )

        return dict(
            src_id=source,
            tgt_id=target,
            weight=weight,
            description=edge_description,
            keywords=edge_keywords,
            source_id=edge_source_id,
            file_path=file_path,
            timestamp=timestamp,
        )

    except ValueError as e:
        logger.warning(
            f"Relationship extraction failed due to encoding issues in chunk {chunk_key}: {e}"
        )
        return None
    except Exception as e:
        logger.warning(
            f"Relationship extraction failed with unexpected error in chunk {chunk_key}: {e}"
        )
        return None


async def rebuild_knowledge_from_chunks(
    entities_to_rebuild: dict[str, list[str]],
    relationships_to_rebuild: dict[tuple[str, str], list[str]],
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_storage: BaseKVStorage,
    llm_response_cache: BaseKVStorage,
    global_config: dict[str, str],
    pipeline_status: dict | None = None,
    pipeline_status_lock=None,
    entity_chunks_storage: BaseKVStorage | None = None,
    relation_chunks_storage: BaseKVStorage | None = None,
) -> None:
    """Rebuild entity and relationship descriptions from cached extraction results with parallel processing

    This method uses cached LLM extraction results instead of calling LLM again,
    following the same approach as the insert process. Now with parallel processing
    controlled by llm_model_max_async and using get_storage_keyed_lock for data consistency.

    Args:
        entities_to_rebuild: Dict mapping entity_name -> list of remaining chunk_ids
        relationships_to_rebuild: Dict mapping (src, tgt) -> list of remaining chunk_ids
        knowledge_graph_inst: Knowledge graph storage
        entities_vdb: Entity vector database
        relationships_vdb: Relationship vector database
        text_chunks_storage: Text chunks storage
        llm_response_cache: LLM response cache
        global_config: Global configuration containing llm_model_max_async
        pipeline_status: Pipeline status dictionary
        pipeline_status_lock: Lock for pipeline status
        entity_chunks_storage: KV storage maintaining full chunk IDs per entity
        relation_chunks_storage: KV storage maintaining full chunk IDs per relation
    """
    if not entities_to_rebuild and not relationships_to_rebuild:
        return

    # Get all referenced chunk IDs
    all_referenced_chunk_ids = set()
    for chunk_ids in entities_to_rebuild.values():
        all_referenced_chunk_ids.update(chunk_ids)
    for chunk_ids in relationships_to_rebuild.values():
        all_referenced_chunk_ids.update(chunk_ids)

    status_message = f"Rebuilding knowledge from {len(all_referenced_chunk_ids)} cached chunk extractions (parallel processing)"
    logger.info(status_message)
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            pipeline_status["latest_message"] = status_message
            pipeline_status["history_messages"].append(status_message)

    # Get cached extraction results for these chunks using storage
    # cached_results： chunk_id -> [list of (extraction_result, create_time) from LLM cache sorted by create_time of the first extraction_result]
    cached_results = await _get_cached_extraction_results(
        llm_response_cache,
        all_referenced_chunk_ids,
        text_chunks_storage=text_chunks_storage,
    )

    if not cached_results:
        status_message = "No cached extraction results found, cannot rebuild"
        logger.warning(status_message)
        if pipeline_status is not None and pipeline_status_lock is not None:
            async with pipeline_status_lock:
                pipeline_status["latest_message"] = status_message
                pipeline_status["history_messages"].append(status_message)
        return

    # Process cached results to get entities and relationships for each chunk
    chunk_entities = {}  # chunk_id -> {entity_name: [entity_data]}
    chunk_relationships = {}  # chunk_id -> {(src, tgt): [relationship_data]}

    for chunk_id, results in cached_results.items():
        try:
            # Handle multiple extraction results per chunk
            chunk_entities[chunk_id] = defaultdict(list)
            chunk_relationships[chunk_id] = defaultdict(list)

            # process multiple LLM extraction results for a single chunk_id
            for result in results:
                entities, relationships = await _rebuild_from_extraction_result(
                    text_chunks_storage=text_chunks_storage,
                    chunk_id=chunk_id,
                    extraction_result=result[0],
                    timestamp=result[1],
                )

                # Merge entities and relationships from this extraction result
                # Compare description lengths and keep the better version for the same chunk_id
                for entity_name, entity_list in entities.items():
                    if entity_name not in chunk_entities[chunk_id]:
                        # New entity for this chunk_id
                        chunk_entities[chunk_id][entity_name].extend(entity_list)
                    elif len(chunk_entities[chunk_id][entity_name]) == 0:
                        # Empty list, add the new entities
                        chunk_entities[chunk_id][entity_name].extend(entity_list)
                    else:
                        # Compare description lengths and keep the better one
                        existing_desc_len = len(
                            chunk_entities[chunk_id][entity_name][0].get(
                                "description", ""
                            )
                            or ""
                        )
                        new_desc_len = len(entity_list[0].get("description", "") or "")

                        if new_desc_len > existing_desc_len:
                            # Replace with the new entity that has longer description
                            chunk_entities[chunk_id][entity_name] = list(entity_list)
                        # Otherwise keep existing version

                # Compare description lengths and keep the better version for the same chunk_id
                for rel_key, rel_list in relationships.items():
                    if rel_key not in chunk_relationships[chunk_id]:
                        # New relationship for this chunk_id
                        chunk_relationships[chunk_id][rel_key].extend(rel_list)
                    elif len(chunk_relationships[chunk_id][rel_key]) == 0:
                        # Empty list, add the new relationships
                        chunk_relationships[chunk_id][rel_key].extend(rel_list)
                    else:
                        # Compare description lengths and keep the better one
                        existing_desc_len = len(
                            chunk_relationships[chunk_id][rel_key][0].get(
                                "description", ""
                            )
                            or ""
                        )
                        new_desc_len = len(rel_list[0].get("description", "") or "")

                        if new_desc_len > existing_desc_len:
                            # Replace with the new relationship that has longer description
                            chunk_relationships[chunk_id][rel_key] = list(rel_list)
                        # Otherwise keep existing version

        except Exception as e:
            status_message = (
                f"Failed to parse cached extraction result for chunk {chunk_id}: {e}"
            )
            logger.info(status_message)  # Per requirement, change to info
            if pipeline_status is not None and pipeline_status_lock is not None:
                async with pipeline_status_lock:
                    pipeline_status["latest_message"] = status_message
                    pipeline_status["history_messages"].append(status_message)
            continue

    # Get max async tasks limit from global_config for semaphore control
    graph_max_async = global_config.get("llm_model_max_async", 4) * 2
    semaphore = asyncio.Semaphore(graph_max_async)

    # Counters for tracking progress
    rebuilt_entities_count = 0
    rebuilt_relationships_count = 0
    failed_entities_count = 0
    failed_relationships_count = 0

    async def _locked_rebuild_entity(entity_name, chunk_ids):
        nonlocal rebuilt_entities_count, failed_entities_count
        async with semaphore:
            workspace = global_config.get("workspace", "")
            namespace = f"{workspace}:GraphDB" if workspace else "GraphDB"
            async with get_storage_keyed_lock(
                [entity_name], namespace=namespace, enable_logging=False
            ):
                try:
                    await _rebuild_single_entity(
                        knowledge_graph_inst=knowledge_graph_inst,
                        entities_vdb=entities_vdb,
                        entity_name=entity_name,
                        chunk_ids=chunk_ids,
                        chunk_entities=chunk_entities,
                        llm_response_cache=llm_response_cache,
                        global_config=global_config,
                        entity_chunks_storage=entity_chunks_storage,
                    )
                    rebuilt_entities_count += 1
                except Exception as e:
                    failed_entities_count += 1
                    status_message = f"Failed to rebuild `{entity_name}`: {e}"
                    logger.info(status_message)  # Per requirement, change to info
                    if pipeline_status is not None and pipeline_status_lock is not None:
                        async with pipeline_status_lock:
                            pipeline_status["latest_message"] = status_message
                            pipeline_status["history_messages"].append(status_message)

    async def _locked_rebuild_relationship(src, tgt, chunk_ids):
        nonlocal rebuilt_relationships_count, failed_relationships_count
        async with semaphore:
            workspace = global_config.get("workspace", "")
            namespace = f"{workspace}:GraphDB" if workspace else "GraphDB"
            # Sort src and tgt to ensure order-independent lock key generation
            sorted_key_parts = sorted([src, tgt])
            async with get_storage_keyed_lock(
                sorted_key_parts,
                namespace=namespace,
                enable_logging=False,
            ):
                try:
                    await _rebuild_single_relationship(
                        knowledge_graph_inst=knowledge_graph_inst,
                        relationships_vdb=relationships_vdb,
                        entities_vdb=entities_vdb,
                        src=src,
                        tgt=tgt,
                        chunk_ids=chunk_ids,
                        chunk_relationships=chunk_relationships,
                        llm_response_cache=llm_response_cache,
                        global_config=global_config,
                        relation_chunks_storage=relation_chunks_storage,
                        entity_chunks_storage=entity_chunks_storage,
                        pipeline_status=pipeline_status,
                        pipeline_status_lock=pipeline_status_lock,
                    )
                    rebuilt_relationships_count += 1
                except Exception as e:
                    failed_relationships_count += 1
                    status_message = f"Failed to rebuild `{src}`~`{tgt}`: {e}"
                    logger.info(status_message)  # Per requirement, change to info
                    if pipeline_status is not None and pipeline_status_lock is not None:
                        async with pipeline_status_lock:
                            pipeline_status["latest_message"] = status_message
                            pipeline_status["history_messages"].append(status_message)

    # Create tasks for parallel processing
    tasks = []

    # Add entity rebuilding tasks
    for entity_name, chunk_ids in entities_to_rebuild.items():
        task = asyncio.create_task(_locked_rebuild_entity(entity_name, chunk_ids))
        tasks.append(task)

    # Add relationship rebuilding tasks
    for (src, tgt), chunk_ids in relationships_to_rebuild.items():
        task = asyncio.create_task(_locked_rebuild_relationship(src, tgt, chunk_ids))
        tasks.append(task)

    # Log parallel processing start
    status_message = f"Starting parallel rebuild of {len(entities_to_rebuild)} entities and {len(relationships_to_rebuild)} relationships (async: {graph_max_async})"
    logger.info(status_message)
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            pipeline_status["latest_message"] = status_message
            pipeline_status["history_messages"].append(status_message)

    # Execute all tasks in parallel with semaphore control and early failure detection
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    # Check if any task raised an exception and ensure all exceptions are retrieved
    first_exception = None

    for task in done:
        try:
            exception = task.exception()
            if exception is not None:
                if first_exception is None:
                    first_exception = exception
            else:
                # Task completed successfully, retrieve result to mark as processed
                task.result()
        except Exception as e:
            if first_exception is None:
                first_exception = e

    # If any task failed, cancel all pending tasks and raise the first exception
    if first_exception is not None:
        # Cancel all pending tasks
        for pending_task in pending:
            pending_task.cancel()

        # Wait for cancellation to complete
        if pending:
            await asyncio.wait(pending)

        # Re-raise the first exception to notify the caller
        raise first_exception

    # Final status report
    status_message = f"KG rebuild completed: {rebuilt_entities_count} entities and {rebuilt_relationships_count} relationships rebuilt successfully."
    if failed_entities_count > 0 or failed_relationships_count > 0:
        status_message += f" Failed: {failed_entities_count} entities, {failed_relationships_count} relationships."

    logger.info(status_message)
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            pipeline_status["latest_message"] = status_message
            pipeline_status["history_messages"].append(status_message)


async def _get_cached_extraction_results(
    llm_response_cache: BaseKVStorage,
    chunk_ids: set[str],
    text_chunks_storage: BaseKVStorage,
) -> dict[str, list[str]]:
    """Get cached extraction results for specific chunk IDs

    This function retrieves cached LLM extraction results for the given chunk IDs and returns
    them sorted by creation time. The results are sorted at two levels:
    1. Individual extraction results within each chunk are sorted by create_time (earliest first)
    2. Chunks themselves are sorted by the create_time of their earliest extraction result

    Args:
        llm_response_cache: LLM response cache storage
        chunk_ids: Set of chunk IDs to get cached results for
        text_chunks_storage: Text chunks storage for retrieving chunk data and LLM cache references

    Returns:
        Dict mapping chunk_id -> list of extraction_result_text, where:
        - Keys (chunk_ids) are ordered by the create_time of their first extraction result
        - Values (extraction results) are ordered by create_time within each chunk
    """
    cached_results = {}

    # Collect all LLM cache IDs from chunks
    all_cache_ids = set()

    # Read from storage
    chunk_data_list = await text_chunks_storage.get_by_ids(list(chunk_ids))
    for chunk_data in chunk_data_list:
        if chunk_data and isinstance(chunk_data, dict):
            llm_cache_list = chunk_data.get("llm_cache_list", [])
            if llm_cache_list:
                all_cache_ids.update(llm_cache_list)
        else:
            logger.warning(f"Chunk data is invalid or None: {chunk_data}")

    if not all_cache_ids:
        logger.warning(f"No LLM cache IDs found for {len(chunk_ids)} chunk IDs")
        return cached_results

    # Batch get LLM cache entries
    cache_data_list = await llm_response_cache.get_by_ids(list(all_cache_ids))

    # Process cache entries and group by chunk_id
    valid_entries = 0
    for cache_entry in cache_data_list:
        if (
            cache_entry is not None
            and isinstance(cache_entry, dict)
            and cache_entry.get("cache_type") == "extract"
            and cache_entry.get("chunk_id") in chunk_ids
        ):
            chunk_id = cache_entry["chunk_id"]
            extraction_result = cache_entry["return"]
            create_time = cache_entry.get(
                "create_time", 0
            )  # Get creation time, default to 0
            valid_entries += 1

            # Support multiple LLM caches per chunk
            if chunk_id not in cached_results:
                cached_results[chunk_id] = []
            # Store tuple with extraction result and creation time for sorting
            cached_results[chunk_id].append((extraction_result, create_time))

    # Sort extraction results by create_time for each chunk and collect earliest times
    chunk_earliest_times = {}
    for chunk_id in cached_results:
        # Sort by create_time (x[1]), then extract only extraction_result (x[0])
        cached_results[chunk_id].sort(key=lambda x: x[1])
        # Store the earliest create_time for this chunk (first item after sorting)
        chunk_earliest_times[chunk_id] = cached_results[chunk_id][0][1]

    # Sort cached_results by the earliest create_time of each chunk
    sorted_chunk_ids = sorted(
        chunk_earliest_times.keys(), key=lambda chunk_id: chunk_earliest_times[chunk_id]
    )

    # Rebuild cached_results in sorted order
    sorted_cached_results = {}
    for chunk_id in sorted_chunk_ids:
        sorted_cached_results[chunk_id] = cached_results[chunk_id]

    logger.info(
        f"Found {valid_entries} valid cache entries, {len(sorted_cached_results)} chunks with results"
    )
    return sorted_cached_results  # each item: list(extraction_result, create_time)


async def _process_extraction_result(
    result: str,
    chunk_key: str,
    timestamp: int,
    file_path: str = "unknown_source",
    tuple_delimiter: str = "<|#|>",
    completion_delimiter: str = "<|COMPLETE|>",
) -> tuple[dict, dict]:
    """Process a single extraction result (either initial or gleaning)
    Args:
        result (str): The extraction result to process
        chunk_key (str): The chunk key for source tracking
        file_path (str): The file path for citation
        tuple_delimiter (str): Delimiter for tuple fields
        record_delimiter (str): Delimiter for records
        completion_delimiter (str): Delimiter for completion
    Returns:
        tuple: (nodes_dict, edges_dict) containing the extracted entities and relationships
    """
    maybe_nodes = defaultdict(list)
    maybe_edges = defaultdict(list)

    if completion_delimiter not in result:
        logger.warning(
            f"{chunk_key}: Complete delimiter can not be found in extraction result"
        )

    # Split LLL output result to records by "\n"
    records = split_string_by_multi_markers(
        result,
        ["\n", completion_delimiter, completion_delimiter.lower()],
    )

    # Fix LLM output format error which use tuple_delimiter to separate record instead of "\n"
    fixed_records = []
    for i, record in enumerate(records, start=1):
        record = record.strip()
        if record is None:
            continue
        entity_records = split_string_by_multi_markers(
            record, [f"{tuple_delimiter}entity{tuple_delimiter}"]
        )
        for entity_record in entity_records:
            if not entity_record.startswith("entity") and not entity_record.startswith(
                "relation"
            ):
                entity_record = f"entity<|{entity_record}"
            entity_relation_records = split_string_by_multi_markers(
                # treat "relationship" and "relation" interchangeable
                entity_record,
                [
                    f"{tuple_delimiter}relationship{tuple_delimiter}",
                    f"{tuple_delimiter}relation{tuple_delimiter}",
                ],
            )
            for entity_relation_record in entity_relation_records:
                if not entity_relation_record.startswith(
                    "entity"
                ) and not entity_relation_record.startswith("relation"):
                    entity_relation_record = (
                        f"relation{tuple_delimiter}{entity_relation_record}"
                    )
                fixed_records.append(entity_relation_record)
        await _cooperative_yield(i, every=8)

    if len(fixed_records) != len(records):
        logger.warning(
            f"{chunk_key}: LLM output format error; find LLM use {tuple_delimiter} as record separators instead new-line"
        )

    delimiter_core = tuple_delimiter[2:-2]  # Extract "#" from "<|#|>"
    delimiter_core_lower = delimiter_core.lower()
    for i, record in enumerate(fixed_records, start=1):
        record = record.strip()
        if record is None:
            continue

        # Fix various forms of tuple_delimiter corruption from the LLM output using the dedicated function
        record = fix_tuple_delimiter_corruption(record, delimiter_core, tuple_delimiter)
        if delimiter_core != delimiter_core_lower:
            # change delimiter_core to lower case, and fix again
            record = fix_tuple_delimiter_corruption(
                record, delimiter_core_lower, tuple_delimiter
            )

        record_attributes = split_string_by_multi_markers(record, [tuple_delimiter])

        # Try to parse as entity
        entity_data = _handle_single_entity_extraction(
            record_attributes, chunk_key, timestamp, file_path
        )
        if entity_data is not None:
            truncated_name = _truncate_entity_identifier(
                entity_data["entity_name"],
                DEFAULT_ENTITY_NAME_MAX_LENGTH,
                chunk_key,
                "Entity name",
            )
            entity_data["entity_name"] = truncated_name
            maybe_nodes[truncated_name].append(entity_data)
            await _cooperative_yield(i, every=8)
            continue

        # Try to parse as relationship
        relationship_data = _handle_single_relationship_extraction(
            record_attributes, chunk_key, timestamp, file_path
        )
        if relationship_data is not None:
            truncated_source = _truncate_entity_identifier(
                relationship_data["src_id"],
                DEFAULT_ENTITY_NAME_MAX_LENGTH,
                chunk_key,
                "Relation entity",
            )
            truncated_target = _truncate_entity_identifier(
                relationship_data["tgt_id"],
                DEFAULT_ENTITY_NAME_MAX_LENGTH,
                chunk_key,
                "Relation entity",
            )
            relationship_data["src_id"] = truncated_source
            relationship_data["tgt_id"] = truncated_target
            maybe_edges[(truncated_source, truncated_target)].append(relationship_data)
        await _cooperative_yield(i, every=8)

    return dict(maybe_nodes), dict(maybe_edges)


async def _rebuild_from_extraction_result(
    text_chunks_storage: BaseKVStorage,
    extraction_result: str,
    chunk_id: str,
    timestamp: int,
) -> tuple[dict, dict]:
    """Parse cached extraction result using the same logic as extract_entities

    Args:
        text_chunks_storage: Text chunks storage to get chunk data
        extraction_result: The cached LLM extraction result
        chunk_id: The chunk ID for source tracking

    Returns:
        Tuple of (entities_dict, relationships_dict)
    """

    # Get chunk data for file_path from storage
    chunk_data = await text_chunks_storage.get_by_id(chunk_id)
    file_path = (
        chunk_data.get("file_path", "unknown_source")
        if chunk_data
        else "unknown_source"
    )

    # Call the shared processing function
    return await _process_extraction_result(
        extraction_result,
        chunk_id,
        timestamp,
        file_path,
        tuple_delimiter=PROMPTS["DEFAULT_TUPLE_DELIMITER"],
        completion_delimiter=PROMPTS["DEFAULT_COMPLETION_DELIMITER"],
    )


async def _rebuild_single_entity(
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    entity_name: str,
    chunk_ids: list[str],
    chunk_entities: dict,
    llm_response_cache: BaseKVStorage,
    global_config: dict[str, str],
    entity_chunks_storage: BaseKVStorage | None = None,
    pipeline_status: dict | None = None,
    pipeline_status_lock=None,
) -> None:
    """Rebuild a single entity from cached extraction results"""

    # Get current entity data
    current_entity = await knowledge_graph_inst.get_node(entity_name)
    if not current_entity:
        return

    # Helper function to update entity in both graph and vector storage
    async def _update_entity_storage(
        final_description: str,
        entity_type: str,
        file_paths: list[str],
        source_chunk_ids: list[str],
        truncation_info: str = "",
    ):
        try:
            # Update entity in graph storage (critical path)
            updated_entity_data = {
                **current_entity,
                "description": final_description,
                "entity_type": entity_type,
                "source_id": GRAPH_FIELD_SEP.join(source_chunk_ids),
                "file_path": GRAPH_FIELD_SEP.join(file_paths)
                if file_paths
                else current_entity.get("file_path", "unknown_source"),
                "created_at": int(time.time()),
                "truncate": truncation_info,
            }
            await knowledge_graph_inst.upsert_node(entity_name, updated_entity_data)

            # Update entity in vector database (equally critical)
            entity_vdb_id = compute_mdhash_id(entity_name, prefix="ent-")
            entity_content = f"{entity_name}\n{final_description}"

            vdb_data = {
                entity_vdb_id: {
                    "content": entity_content,
                    "entity_name": entity_name,
                    "source_id": updated_entity_data["source_id"],
                    "description": final_description,
                    "entity_type": entity_type,
                    "file_path": updated_entity_data["file_path"],
                }
            }

            # Use safe operation wrapper - VDB failure must throw exception
            await safe_vdb_operation_with_exception(
                operation=lambda: entities_vdb.upsert(vdb_data),
                operation_name="rebuild_entity_upsert",
                entity_name=entity_name,
                max_retries=3,
                retry_delay=0.1,
            )

        except Exception as e:
            error_msg = f"Failed to update entity storage for `{entity_name}`: {e}"
            logger.error(error_msg)
            raise  # Re-raise exception

    # normalized_chunk_ids = merge_source_ids([], chunk_ids)
    normalized_chunk_ids = chunk_ids

    if entity_chunks_storage is not None and normalized_chunk_ids:
        await entity_chunks_storage.upsert(
            {
                entity_name: {
                    "chunk_ids": normalized_chunk_ids,
                    "count": len(normalized_chunk_ids),
                }
            }
        )

    limit_method = (
        global_config.get("source_ids_limit_method") or SOURCE_IDS_LIMIT_METHOD_KEEP
    )

    limited_chunk_ids = apply_source_ids_limit(
        normalized_chunk_ids,
        global_config["max_source_ids_per_entity"],
        limit_method,
        identifier=f"`{entity_name}`",
    )

    # Collect all entity data from relevant (limited) chunks
    all_entity_data = []
    for chunk_id in limited_chunk_ids:
        if chunk_id in chunk_entities and entity_name in chunk_entities[chunk_id]:
            all_entity_data.extend(chunk_entities[chunk_id][entity_name])

    if not all_entity_data:
        logger.warning(
            f"No entity data found for `{entity_name}`, trying to rebuild from relationships"
        )

        # Get all edges connected to this entity
        edges = await knowledge_graph_inst.get_node_edges(entity_name)
        if not edges:
            logger.warning(f"No relations attached to entity `{entity_name}`")
            return

        # Collect relationship data to extract entity information
        relationship_descriptions = []
        file_paths = set()

        # Get edge data for all connected relationships
        for src_id, tgt_id in edges:
            edge_data = await knowledge_graph_inst.get_edge(src_id, tgt_id)
            if edge_data:
                if edge_data.get("description"):
                    relationship_descriptions.append(edge_data["description"])

                if edge_data.get("file_path"):
                    edge_file_paths = edge_data["file_path"].split(GRAPH_FIELD_SEP)
                    file_paths.update(edge_file_paths)

        # deduplicate descriptions
        description_list = list(dict.fromkeys(relationship_descriptions))

        # Generate final description from relationships or fallback to current
        if description_list:
            final_description, _ = await _handle_entity_relation_summary(
                "Entity",
                entity_name,
                description_list,
                GRAPH_FIELD_SEP,
                global_config,
                llm_response_cache=llm_response_cache,
            )
        else:
            final_description = current_entity.get("description", "")

        entity_type = current_entity.get("entity_type", "UNKNOWN")
        await _update_entity_storage(
            final_description,
            entity_type,
            file_paths,
            limited_chunk_ids,
        )
        return

    # Process cached entity data
    descriptions = []
    entity_types = []
    file_paths_list = []
    seen_paths = set()

    for entity_data in all_entity_data:
        if entity_data.get("description"):
            descriptions.append(entity_data["description"])
        if entity_data.get("entity_type"):
            entity_types.append(entity_data["entity_type"])
        if entity_data.get("file_path"):
            file_path = entity_data["file_path"]
            if file_path and file_path not in seen_paths:
                file_paths_list.append(file_path)
                seen_paths.add(file_path)

    # Apply MAX_FILE_PATHS limit
    max_file_paths = global_config.get("max_file_paths", DEFAULT_MAX_FILE_PATHS)
    file_path_placeholder = global_config.get(
        "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
    )
    limit_method = global_config.get("source_ids_limit_method")

    original_count = len(file_paths_list)
    if original_count > max_file_paths:
        if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
            # FIFO: keep tail (newest), discard head
            file_paths_list = file_paths_list[-max_file_paths:]
        else:
            # KEEP: keep head (earliest), discard tail
            file_paths_list = file_paths_list[:max_file_paths]

        file_paths_list.append(
            f"...{file_path_placeholder}...({limit_method} {max_file_paths}/{original_count})"
        )
        logger.info(
            f"Limited `{entity_name}`: file_path {original_count} -> {max_file_paths} ({limit_method})"
        )

    # Remove duplicates while preserving order
    description_list = list(dict.fromkeys(descriptions))
    entity_types = list(dict.fromkeys(entity_types))

    # Get most common entity type
    entity_type = (
        max(set(entity_types), key=entity_types.count)
        if entity_types
        else current_entity.get("entity_type", "UNKNOWN")
    )

    # Generate final description from entities or fallback to current
    if description_list:
        final_description, _ = await _handle_entity_relation_summary(
            "Entity",
            entity_name,
            description_list,
            GRAPH_FIELD_SEP,
            global_config,
            llm_response_cache=llm_response_cache,
        )
    else:
        final_description = current_entity.get("description", "")

    if len(limited_chunk_ids) < len(normalized_chunk_ids):
        truncation_info = (
            f"{limit_method} {len(limited_chunk_ids)}/{len(normalized_chunk_ids)}"
        )
    else:
        truncation_info = ""

    await _update_entity_storage(
        final_description,
        entity_type,
        file_paths_list,
        limited_chunk_ids,
        truncation_info,
    )

    # Log rebuild completion with truncation info
    status_message = f"Rebuild `{entity_name}` from {len(chunk_ids)} chunks"
    if truncation_info:
        status_message += f" ({truncation_info})"
    logger.info(status_message)
    # Update pipeline status
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            pipeline_status["latest_message"] = status_message
            pipeline_status["history_messages"].append(status_message)


async def _rebuild_single_relationship(
    knowledge_graph_inst: BaseGraphStorage,
    relationships_vdb: BaseVectorStorage,
    entities_vdb: BaseVectorStorage,
    src: str,
    tgt: str,
    chunk_ids: list[str],
    chunk_relationships: dict,
    llm_response_cache: BaseKVStorage,
    global_config: dict[str, str],
    relation_chunks_storage: BaseKVStorage | None = None,
    entity_chunks_storage: BaseKVStorage | None = None,
    pipeline_status: dict | None = None,
    pipeline_status_lock=None,
) -> None:
    """Rebuild a single relationship from cached extraction results

    Note: This function assumes the caller has already acquired the appropriate
    keyed lock for the relationship pair to ensure thread safety.
    """

    # Get current relationship data
    current_relationship = await knowledge_graph_inst.get_edge(src, tgt)
    if not current_relationship:
        return

    # normalized_chunk_ids = merge_source_ids([], chunk_ids)
    normalized_chunk_ids = chunk_ids

    if relation_chunks_storage is not None and normalized_chunk_ids:
        storage_key = make_relation_chunk_key(src, tgt)
        await relation_chunks_storage.upsert(
            {
                storage_key: {
                    "chunk_ids": normalized_chunk_ids,
                    "count": len(normalized_chunk_ids),
                }
            }
        )

    limit_method = (
        global_config.get("source_ids_limit_method") or SOURCE_IDS_LIMIT_METHOD_KEEP
    )
    limited_chunk_ids = apply_source_ids_limit(
        normalized_chunk_ids,
        global_config["max_source_ids_per_relation"],
        limit_method,
        identifier=f"`{src}`~`{tgt}`",
    )

    # Collect all relationship data from relevant chunks
    all_relationship_data = []
    for chunk_id in limited_chunk_ids:
        if chunk_id in chunk_relationships:
            # Check both (src, tgt) and (tgt, src) since relationships can be bidirectional
            for edge_key in [(src, tgt), (tgt, src)]:
                if edge_key in chunk_relationships[chunk_id]:
                    all_relationship_data.extend(
                        chunk_relationships[chunk_id][edge_key]
                    )

    if not all_relationship_data:
        logger.warning(f"No relation data found for `{src}-{tgt}`")
        return

    # Merge descriptions and keywords
    descriptions = []
    keywords = []
    weights = []
    file_paths_list = []
    seen_paths = set()

    for rel_data in all_relationship_data:
        if rel_data.get("description"):
            descriptions.append(rel_data["description"])
        if rel_data.get("keywords"):
            keywords.append(rel_data["keywords"])
        if rel_data.get("weight"):
            weights.append(rel_data["weight"])
        if rel_data.get("file_path"):
            file_path = rel_data["file_path"]
            if file_path and file_path not in seen_paths:
                file_paths_list.append(file_path)
                seen_paths.add(file_path)

    # Apply count limit
    max_file_paths = global_config.get("max_file_paths", DEFAULT_MAX_FILE_PATHS)
    file_path_placeholder = global_config.get(
        "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
    )
    limit_method = global_config.get("source_ids_limit_method")

    original_count = len(file_paths_list)
    if original_count > max_file_paths:
        if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
            # FIFO: keep tail (newest), discard head
            file_paths_list = file_paths_list[-max_file_paths:]
        else:
            # KEEP: keep head (earliest), discard tail
            file_paths_list = file_paths_list[:max_file_paths]

        file_paths_list.append(
            f"...{file_path_placeholder}...({limit_method} {max_file_paths}/{original_count})"
        )
        logger.info(
            f"Limited `{src}`~`{tgt}`: file_path {original_count} -> {max_file_paths} ({limit_method})"
        )

    # Remove duplicates while preserving order
    description_list = list(dict.fromkeys(descriptions))
    keywords = list(dict.fromkeys(keywords))

    combined_keywords = (
        ", ".join(set(keywords))
        if keywords
        else current_relationship.get("keywords", "")
    )

    weight = sum(weights) if weights else current_relationship.get("weight", 1.0)

    # Generate final description from relations or fallback to current
    if description_list:
        final_description, _ = await _handle_entity_relation_summary(
            "Relation",
            f"{src}-{tgt}",
            description_list,
            GRAPH_FIELD_SEP,
            global_config,
            llm_response_cache=llm_response_cache,
        )
    else:
        # fallback to keep current(unchanged)
        final_description = current_relationship.get("description", "")

    if len(limited_chunk_ids) < len(normalized_chunk_ids):
        truncation_info = (
            f"{limit_method} {len(limited_chunk_ids)}/{len(normalized_chunk_ids)}"
        )
    else:
        truncation_info = ""

    # Update relationship in graph storage
    updated_relationship_data = {
        **current_relationship,
        "description": final_description
        if final_description
        else current_relationship.get("description", ""),
        "keywords": combined_keywords,
        "weight": weight,
        "source_id": GRAPH_FIELD_SEP.join(limited_chunk_ids),
        "file_path": GRAPH_FIELD_SEP.join([fp for fp in file_paths_list if fp])
        if file_paths_list
        else current_relationship.get("file_path", "unknown_source"),
        "truncate": truncation_info,
    }

    # Ensure both endpoint nodes exist before writing the edge back
    # (certain storage backends require pre-existing nodes).
    node_description = (
        updated_relationship_data["description"]
        if updated_relationship_data.get("description")
        else current_relationship.get("description", "")
    )
    node_source_id = updated_relationship_data.get("source_id", "")
    node_file_path = updated_relationship_data.get("file_path", "unknown_source")

    for node_id in {src, tgt}:
        if not (await knowledge_graph_inst.has_node(node_id)):
            node_created_at = int(time.time())
            node_data = {
                "entity_id": node_id,
                "source_id": node_source_id,
                "description": node_description,
                "entity_type": "UNKNOWN",
                "file_path": node_file_path,
                "created_at": node_created_at,
                "truncate": "",
            }
            await knowledge_graph_inst.upsert_node(node_id, node_data=node_data)

            # Update entity_chunks_storage for the newly created entity
            if entity_chunks_storage is not None and limited_chunk_ids:
                await entity_chunks_storage.upsert(
                    {
                        node_id: {
                            "chunk_ids": limited_chunk_ids,
                            "count": len(limited_chunk_ids),
                        }
                    }
                )

            # Update entity_vdb for the newly created entity
            if entities_vdb is not None:
                entity_vdb_id = compute_mdhash_id(node_id, prefix="ent-")
                entity_content = f"{node_id}\n{node_description}"
                vdb_data = {
                    entity_vdb_id: {
                        "content": entity_content,
                        "entity_name": node_id,
                        "source_id": node_source_id,
                        "entity_type": "UNKNOWN",
                        "file_path": node_file_path,
                    }
                }
                await safe_vdb_operation_with_exception(
                    operation=lambda payload=vdb_data: entities_vdb.upsert(payload),
                    operation_name="rebuild_added_entity_upsert",
                    entity_name=node_id,
                    max_retries=3,
                    retry_delay=0.1,
                )

    await knowledge_graph_inst.upsert_edge(src, tgt, updated_relationship_data)

    # Update relationship in vector database
    # Sort src and tgt to ensure consistent ordering (smaller string first)
    if src > tgt:
        src, tgt = tgt, src
    try:
        rel_vdb_id = compute_mdhash_id(src + tgt, prefix="rel-")
        rel_vdb_id_reverse = compute_mdhash_id(tgt + src, prefix="rel-")

        # Delete old vector records first (both directions to be safe)
        try:
            await relationships_vdb.delete([rel_vdb_id, rel_vdb_id_reverse])
        except Exception as e:
            logger.debug(
                f"Could not delete old relationship vector records {rel_vdb_id}, {rel_vdb_id_reverse}: {e}"
            )

        # Insert new vector record
        rel_content = f"{combined_keywords}\t{src}\n{tgt}\n{final_description}"
        vdb_data = {
            rel_vdb_id: {
                "src_id": src,
                "tgt_id": tgt,
                "source_id": updated_relationship_data["source_id"],
                "content": rel_content,
                "keywords": combined_keywords,
                "description": final_description,
                "weight": weight,
                "file_path": updated_relationship_data["file_path"],
            }
        }

        # Use safe operation wrapper - VDB failure must throw exception
        await safe_vdb_operation_with_exception(
            operation=lambda: relationships_vdb.upsert(vdb_data),
            operation_name="rebuild_relationship_upsert",
            entity_name=f"{src}-{tgt}",
            max_retries=3,
            retry_delay=0.2,
        )

    except Exception as e:
        error_msg = f"Failed to rebuild relationship storage for `{src}-{tgt}`: {e}"
        logger.error(error_msg)
        raise  # Re-raise exception

    # Log rebuild completion with truncation info
    status_message = f"Rebuild `{src}`~`{tgt}` from {len(chunk_ids)} chunks"
    if truncation_info:
        status_message += f" ({truncation_info})"
    # Add truncation info from apply_source_ids_limit if truncation occurred
    if len(limited_chunk_ids) < len(normalized_chunk_ids):
        truncation_info = (
            f" ({limit_method}:{len(limited_chunk_ids)}/{len(normalized_chunk_ids)})"
        )
        status_message += truncation_info

    logger.info(status_message)

    # Update pipeline status
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            pipeline_status["latest_message"] = status_message
            pipeline_status["history_messages"].append(status_message)


async def _merge_nodes_then_upsert(
    entity_name: str,
    nodes_data: list[dict],
    knowledge_graph_inst: BaseGraphStorage,
    entity_vdb: BaseVectorStorage | None,
    global_config: dict,
    pipeline_status: dict = None,
    pipeline_status_lock=None,
    llm_response_cache: BaseKVStorage | None = None,
    entity_chunks_storage: BaseKVStorage | None = None,
):
    """Get existing nodes from knowledge graph use name,if exists, merge data, else create, then upsert."""
    timing_start = time.perf_counter()
    try:
        already_entity_types = []
        already_source_ids = []
        already_description = []
        already_file_paths = []

        # 1. Get existing node data from knowledge graph
        already_node = await knowledge_graph_inst.get_node(entity_name)
        if already_node:
            existing_entity_type = already_node.get("entity_type")
            # Coerce to str before any string operations: non-string values from
            # API/custom graph paths would otherwise raise TypeError on the comma check.
            if (
                not isinstance(existing_entity_type, str)
                or not existing_entity_type.strip()
            ):
                existing_entity_type = "UNKNOWN"
            # Sanitize entity_type read back from DB to prevent dirty data from propagating
            if "," in existing_entity_type:
                original = existing_entity_type
                tokens = [t.strip() for t in existing_entity_type.split(",")]
                non_empty = [t for t in tokens if t]
                existing_entity_type = non_empty[0] if non_empty else "UNKNOWN"
                logger.warning(
                    f"Entity type read from DB contains comma, taking first non-empty token: '{original}' -> '{existing_entity_type}'"
                )
            already_entity_types.append(existing_entity_type)

            existing_source_id = already_node.get("source_id") or ""
            already_source_ids.extend(existing_source_id.split(GRAPH_FIELD_SEP))

            existing_file_path = already_node.get("file_path") or "unknown_source"
            already_file_paths.extend(existing_file_path.split(GRAPH_FIELD_SEP))

            existing_desc = (already_node.get("description") or "").strip()
            if existing_desc:
                already_description.extend(existing_desc.split(GRAPH_FIELD_SEP))

        new_source_ids = [dp["source_id"] for dp in nodes_data if dp.get("source_id")]

        existing_full_source_ids = []
        if entity_chunks_storage is not None:
            stored_chunks = await entity_chunks_storage.get_by_id(entity_name)
            if stored_chunks and isinstance(stored_chunks, dict):
                existing_full_source_ids = [
                    chunk_id
                    for chunk_id in stored_chunks.get("chunk_ids", [])
                    if chunk_id
                ]

        if not existing_full_source_ids:
            existing_full_source_ids = [
                chunk_id for chunk_id in already_source_ids if chunk_id
            ]

        # 2. Merging new source ids with existing ones
        full_source_ids = merge_source_ids(existing_full_source_ids, new_source_ids)

        if entity_chunks_storage is not None and full_source_ids:
            await entity_chunks_storage.upsert(
                {
                    entity_name: {
                        "chunk_ids": full_source_ids,
                        "count": len(full_source_ids),
                    }
                }
            )

        # 3. Finalize source_id by applying source ids limit
        limit_method = global_config.get("source_ids_limit_method")
        max_source_limit = global_config.get("max_source_ids_per_entity")
        source_ids = apply_source_ids_limit(
            full_source_ids,
            max_source_limit,
            limit_method,
            identifier=f"`{entity_name}`",
        )

        # 4. Only keep nodes not filter by apply_source_ids_limit if limit_method is KEEP
        if limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP:
            allowed_source_ids = set(source_ids)
            filtered_nodes = []
            for dp in nodes_data:
                source_id = dp.get("source_id")
                # Skip descriptions sourced from chunks dropped by the limitation cap
                if (
                    source_id
                    and source_id not in allowed_source_ids
                    and source_id not in existing_full_source_ids
                ):
                    continue
                filtered_nodes.append(dp)
            nodes_data = filtered_nodes
        else:  # In FIFO mode, keep all nodes - truncation happens at source_ids level only
            nodes_data = list(nodes_data)

        # 5. Check if we need to skip summary due to source_ids limit
        if (
            limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP
            and len(existing_full_source_ids) >= max_source_limit
            and not nodes_data
        ):
            if already_node:
                logger.info(
                    f"Skipped `{entity_name}`: KEEP old chunks {already_source_ids}/{len(full_source_ids)}"
                )
                existing_node_data = dict(already_node)
                return existing_node_data, None  # No VDB payload for skipped entity
            else:
                logger.error(
                    f"Internal Error: already_node missing for `{entity_name}`"
                )
                raise ValueError(
                    f"Internal Error: already_node missing for `{entity_name}`"
                )

        # 6.1 Finalize source_id
        source_id = GRAPH_FIELD_SEP.join(source_ids)

        # 6.2 Finalize entity type by highest count
        entity_type = sorted(
            Counter(
                [dp["entity_type"] for dp in nodes_data] + already_entity_types
            ).items(),
            key=lambda x: x[1],
            reverse=True,
        )[0][0]

        # 7. Deduplicate nodes by description, keeping first occurrence in the same document
        unique_nodes = {}
        for i, dp in enumerate(nodes_data, start=1):
            desc = dp.get("description")
            if not desc:
                continue
            if desc not in unique_nodes:
                unique_nodes[desc] = dp
            await _cooperative_yield(i, every=32)

        # Sort description by timestamp, then by description length when timestamps are the same
        sorted_nodes = sorted(
            unique_nodes.values(),
            key=lambda x: (x.get("timestamp", 0), -len(x.get("description", ""))),
        )
        sorted_descriptions = [dp["description"] for dp in sorted_nodes]

        # Combine already_description with sorted new sorted descriptions
        description_list = already_description + sorted_descriptions
        if not description_list:
            fallback_description = f"Entity {entity_name}"
            logger.warning(
                f"Entity `{entity_name}` has no description; fallback to `{fallback_description}`"
            )
            description_list = [fallback_description]

        # Check for cancellation before LLM summary
        if pipeline_status is not None and pipeline_status_lock is not None:
            async with pipeline_status_lock:
                if pipeline_status.get("cancellation_requested", False):
                    raise PipelineCancelledException(
                        "User cancelled during entity summary"
                    )

        # 8. Get summary description an LLM usage status
        description, llm_was_used = await _handle_entity_relation_summary(
            "Entity",
            entity_name,
            description_list,
            GRAPH_FIELD_SEP,
            global_config,
            llm_response_cache,
        )

        # 9. Build file_path within MAX_FILE_PATHS
        file_paths_list = []
        seen_paths = set()
        has_placeholder = False  # Indicating file_path has been truncated before

        max_file_paths = global_config.get("max_file_paths", DEFAULT_MAX_FILE_PATHS)
        file_path_placeholder = global_config.get(
            "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
        )

        # Collect from already_file_paths, excluding placeholder
        for fp in already_file_paths:
            if fp and fp.startswith(f"...{file_path_placeholder}"):  # Skip placeholders
                has_placeholder = True
                continue
            if fp and fp not in seen_paths:
                file_paths_list.append(fp)
                seen_paths.add(fp)

        # Collect from new data
        for i, dp in enumerate(nodes_data, start=1):
            file_path_item = dp.get("file_path")
            if file_path_item and file_path_item not in seen_paths:
                file_paths_list.append(file_path_item)
                seen_paths.add(file_path_item)
            await _cooperative_yield(i, every=32)

        # Apply count limit
        if len(file_paths_list) > max_file_paths:
            limit_method = global_config.get(
                "source_ids_limit_method", SOURCE_IDS_LIMIT_METHOD_KEEP
            )
            file_path_placeholder = global_config.get(
                "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
            )
            # Add + sign to indicate actual file count is higher
            original_count_str = (
                f"{len(file_paths_list)}+"
                if has_placeholder
                else str(len(file_paths_list))
            )

            if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
                # FIFO: keep tail (newest), discard head
                file_paths_list = file_paths_list[-max_file_paths:]
                file_paths_list.append(f"...{file_path_placeholder}...(FIFO)")
            else:
                # KEEP: keep head (earliest), discard tail
                file_paths_list = file_paths_list[:max_file_paths]
                file_paths_list.append(f"...{file_path_placeholder}...(KEEP Old)")

            logger.info(
                f"Limited `{entity_name}`: file_path {original_count_str} -> {max_file_paths} ({limit_method})"
            )
        # Finalize file_path
        file_path = GRAPH_FIELD_SEP.join(file_paths_list)

        # 10.Log based on actual LLM usage
        num_fragment = len(description_list)
        already_fragment = len(already_description)
        if llm_was_used:
            status_message = f"LLMmrg: `{entity_name}` | {already_fragment}+{num_fragment - already_fragment}"
        else:
            status_message = f"Merged: `{entity_name}` | {already_fragment}+{num_fragment - already_fragment}"

        truncation_info = truncation_info_log = ""
        if len(source_ids) < len(full_source_ids):
            # Add truncation info from apply_source_ids_limit if truncation occurred
            truncation_info_log = (
                f"{limit_method} {len(source_ids)}/{len(full_source_ids)}"
            )
            if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
                truncation_info = truncation_info_log
            else:
                truncation_info = "KEEP Old"

        deduplicated_num = already_fragment + len(nodes_data) - num_fragment
        dd_message = ""
        if deduplicated_num > 0:
            # Duplicated description detected across multiple trucks for the same entity
            dd_message = f"dd {deduplicated_num}"

        if dd_message or truncation_info_log:
            status_message += (
                f" ({', '.join(filter(None, [truncation_info_log, dd_message]))})"
            )

        # Add message to pipeline satus when merge happens
        if already_fragment > 0 or llm_was_used:
            logger.info(status_message)
            if pipeline_status is not None and pipeline_status_lock is not None:
                async with pipeline_status_lock:
                    pipeline_status["latest_message"] = status_message
                    pipeline_status["history_messages"].append(status_message)
        else:
            logger.debug(status_message)

        # 11. Update graph (VDB upsert is deferred to batch — see Phase 3)
        node_data = dict(
            entity_id=entity_name,
            entity_type=entity_type,
            description=description,
            source_id=source_id,
            file_path=file_path,
            created_at=int(time.time()),
            truncate=truncation_info,
        )
        await knowledge_graph_inst.upsert_node(
            entity_name,
            node_data=node_data,
        )
        node_data["entity_name"] = entity_name

        # Build the VDB payload but do NOT upsert here. nano_vectordb's
        # upsert does a linear scan of all existing data on every call
        # (O(N) per call), so N individual upserts become O(N²). Instead,
        # we return the payload and the caller batches all entities into
        # ONE upsert call at the end of Phase 1 → O(N) total.
        vdb_payload = None
        if entity_vdb is not None:
            entity_vdb_id = compute_mdhash_id(str(entity_name), prefix="ent-")
            entity_content = f"{entity_name}\n{description}"
            vdb_payload = {
                entity_vdb_id: {
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "content": entity_content,
                    "source_id": source_id,
                    "file_path": file_path,
                }
            }
        return node_data, vdb_payload
    finally:
        performance_timing_log(
            "[_merge_nodes_then_upsert] `%s` completed in %.4fs",
            entity_name,
            time.perf_counter() - timing_start,
        )


async def _merge_edges_then_upsert(
    src_id: str,
    tgt_id: str,
    edges_data: list[dict],
    knowledge_graph_inst: BaseGraphStorage,
    relationships_vdb: BaseVectorStorage | None,
    entity_vdb: BaseVectorStorage | None,
    global_config: dict,
    pipeline_status: dict = None,
    pipeline_status_lock=None,
    llm_response_cache: BaseKVStorage | None = None,
    added_entities: list = None,  # New parameter to track entities added during edge processing
    relation_chunks_storage: BaseKVStorage | None = None,
    entity_chunks_storage: BaseKVStorage | None = None,
):
    timing_start = time.perf_counter()
    timing_relation = f"`{src_id}`~`{tgt_id}`"
    try:
        if src_id == tgt_id:
            return None, None, None  # Self-loop, skip

        already_edge = None
        already_weights = []
        already_source_ids = []
        already_description = []
        already_keywords = []
        already_file_paths = []

        # 1. Get existing edge data from graph storage.
        # Single call instead of has_edge() + get_edge() — avoids a
        # redundant O(degree) adjacency-list traversal per edge.
        already_edge = await knowledge_graph_inst.get_edge(src_id, tgt_id)
        if already_edge is not None:
                # Get weight with default 1.0 if missing
                already_weights.append(already_edge.get("weight", 1.0))

                # Get source_id with empty string default if missing or None
                if already_edge.get("source_id") is not None:
                    already_source_ids.extend(
                        already_edge["source_id"].split(GRAPH_FIELD_SEP)
                    )

                # Get file_path with empty string default if missing or None
                if already_edge.get("file_path") is not None:
                    already_file_paths.extend(
                        already_edge["file_path"].split(GRAPH_FIELD_SEP)
                    )

                # Get description with empty string default if missing or None
                if already_edge.get("description") is not None:
                    already_description.extend(
                        already_edge["description"].split(GRAPH_FIELD_SEP)
                    )

                # Get keywords with empty string default if missing or None
                if already_edge.get("keywords") is not None:
                    already_keywords.extend(
                        split_string_by_multi_markers(
                            already_edge["keywords"], [GRAPH_FIELD_SEP]
                        )
                    )

        new_source_ids = [dp["source_id"] for dp in edges_data if dp.get("source_id")]

        storage_key = make_relation_chunk_key(src_id, tgt_id)
        existing_full_source_ids = []
        if relation_chunks_storage is not None:
            stored_chunks = await relation_chunks_storage.get_by_id(storage_key)
            if stored_chunks and isinstance(stored_chunks, dict):
                existing_full_source_ids = [
                    chunk_id
                    for chunk_id in stored_chunks.get("chunk_ids", [])
                    if chunk_id
                ]

        if not existing_full_source_ids:
            existing_full_source_ids = [
                chunk_id for chunk_id in already_source_ids if chunk_id
            ]

        # 2. Merge new source ids with existing ones
        full_source_ids = merge_source_ids(existing_full_source_ids, new_source_ids)

        if relation_chunks_storage is not None and full_source_ids:
            await relation_chunks_storage.upsert(
                {
                    storage_key: {
                        "chunk_ids": full_source_ids,
                        "count": len(full_source_ids),
                    }
                }
            )

        # 3. Finalize source_id by applying source ids limit
        limit_method = global_config.get("source_ids_limit_method")
        max_source_limit = global_config.get("max_source_ids_per_relation")
        source_ids = apply_source_ids_limit(
            full_source_ids,
            max_source_limit,
            limit_method,
            identifier=f"`{src_id}`~`{tgt_id}`",
        )
        limit_method = (
            global_config.get("source_ids_limit_method") or SOURCE_IDS_LIMIT_METHOD_KEEP
        )

        # 4. Only keep edges with source_id in the final source_ids list if in KEEP mode
        if limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP:
            allowed_source_ids = set(source_ids)
            filtered_edges = []
            for dp in edges_data:
                source_id = dp.get("source_id")
                # Skip relationship fragments sourced from chunks dropped by keep oldest cap
                if (
                    source_id
                    and source_id not in allowed_source_ids
                    and source_id not in existing_full_source_ids
                ):
                    continue
                filtered_edges.append(dp)
            edges_data = filtered_edges
        else:  # In FIFO mode, keep all edges - truncation happens at source_ids level only
            edges_data = list(edges_data)

        # 5. Check if we need to skip summary due to source_ids limit
        if (
            limit_method == SOURCE_IDS_LIMIT_METHOD_KEEP
            and len(existing_full_source_ids) >= max_source_limit
            and not edges_data
        ):
            if already_edge:
                logger.info(
                    f"Skipped `{src_id}`~`{tgt_id}`: KEEP old chunks  {already_source_ids}/{len(full_source_ids)}"
                )
                existing_edge_data = dict(already_edge)
                return existing_edge_data, None, None  # No VDB payload for skipped edge
            else:
                logger.error(
                    f"Internal Error: already_node missing for `{src_id}`~`{tgt_id}`"
                )
                raise ValueError(
                    f"Internal Error: already_node missing for `{src_id}`~`{tgt_id}`"
                )

        # 6.1 Finalize source_id
        source_id = GRAPH_FIELD_SEP.join(source_ids)

        # 6.2 Finalize weight by summing new edges and existing weights
        weight = sum([dp["weight"] for dp in edges_data] + already_weights)

        # 6.2 Finalize keywords by merging existing and new keywords
        all_keywords = set()
        # Process already_keywords (which are comma-separated)
        for i, keyword_str in enumerate(already_keywords, start=1):
            if keyword_str:  # Skip empty strings
                all_keywords.update(
                    k.strip() for k in keyword_str.split(",") if k.strip()
                )
            await _cooperative_yield(i, every=32)
        # Process new keywords from edges_data
        for i, edge in enumerate(edges_data, start=1):
            if edge.get("keywords"):
                all_keywords.update(
                    k.strip() for k in edge["keywords"].split(",") if k.strip()
                )
            await _cooperative_yield(i, every=32)
        # Join all unique keywords with commas
        keywords = ",".join(sorted(all_keywords))

        # 7. Deduplicate by description, keeping first occurrence in the same document
        unique_edges = {}
        for i, dp in enumerate(edges_data, start=1):
            description_value = dp.get("description")
            if not description_value:
                continue
            if description_value not in unique_edges:
                unique_edges[description_value] = dp
            await _cooperative_yield(i, every=32)

        # Sort description by timestamp, then by description length (largest to smallest) when timestamps are the same
        sorted_edges = sorted(
            unique_edges.values(),
            key=lambda x: (x.get("timestamp", 0), -len(x.get("description", ""))),
        )
        sorted_descriptions = [dp["description"] for dp in sorted_edges]

        # Combine already_description with sorted new descriptions
        description_list = already_description + sorted_descriptions
        if not description_list:
            logger.error(f"Relation {src_id}~{tgt_id} has no description")
            raise ValueError(f"Relation {src_id}~{tgt_id} has no description")

        # Check for cancellation before LLM summary
        if pipeline_status is not None and pipeline_status_lock is not None:
            async with pipeline_status_lock:
                if pipeline_status.get("cancellation_requested", False):
                    raise PipelineCancelledException(
                        "User cancelled during relation summary"
                    )

        # 8. Get summary description an LLM usage status
        description, llm_was_used = await _handle_entity_relation_summary(
            "Relation",
            f"({src_id}, {tgt_id})",
            description_list,
            GRAPH_FIELD_SEP,
            global_config,
            llm_response_cache,
        )

        # 9. Build file_path within MAX_FILE_PATHS limit
        file_paths_list = []
        seen_paths = set()
        has_placeholder = False  # Track if already_file_paths contains placeholder

        max_file_paths = global_config.get("max_file_paths", DEFAULT_MAX_FILE_PATHS)
        file_path_placeholder = global_config.get(
            "file_path_more_placeholder", DEFAULT_FILE_PATH_MORE_PLACEHOLDER
        )

        # Collect from already_file_paths, excluding placeholder
        for fp in already_file_paths:
            # Check if this is a placeholder record
            if fp and fp.startswith(f"...{file_path_placeholder}"):  # Skip placeholders
                has_placeholder = True
                continue
            if fp and fp not in seen_paths:
                file_paths_list.append(fp)
                seen_paths.add(fp)

        # Collect from new data
        for i, dp in enumerate(edges_data, start=1):
            file_path_item = dp.get("file_path")
            if file_path_item and file_path_item not in seen_paths:
                file_paths_list.append(file_path_item)
                seen_paths.add(file_path_item)
            await _cooperative_yield(i, every=32)

        # Apply count limit
        if len(file_paths_list) > max_file_paths:
            limit_method = global_config.get(
                "source_ids_limit_method", SOURCE_IDS_LIMIT_METHOD_KEEP
            )

            # Add + sign to indicate actual file count is higher
            original_count_str = (
                f"{len(file_paths_list)}+"
                if has_placeholder
                else str(len(file_paths_list))
            )

            if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
                # FIFO: keep tail (newest), discard head
                file_paths_list = file_paths_list[-max_file_paths:]
                file_paths_list.append(f"...{file_path_placeholder}...(FIFO)")
            else:
                # KEEP: keep head (earliest), discard tail
                file_paths_list = file_paths_list[:max_file_paths]
                file_paths_list.append(f"...{file_path_placeholder}...(KEEP Old)")

            logger.info(
                f"Limited `{src_id}`~`{tgt_id}`: file_path {original_count_str} -> {max_file_paths} ({limit_method})"
            )
        # Finalize file_path
        file_path = GRAPH_FIELD_SEP.join(file_paths_list)

        # 10. Log based on actual LLM usage
        num_fragment = len(description_list)
        already_fragment = len(already_description)
        if llm_was_used:
            status_message = f"LLMmrg: `{src_id}`~`{tgt_id}` | {already_fragment}+{num_fragment - already_fragment}"
        else:
            status_message = f"Merged: `{src_id}`~`{tgt_id}` | {already_fragment}+{num_fragment - already_fragment}"

        truncation_info = truncation_info_log = ""
        if len(source_ids) < len(full_source_ids):
            # Add truncation info from apply_source_ids_limit if truncation occurred
            truncation_info_log = (
                f"{limit_method} {len(source_ids)}/{len(full_source_ids)}"
            )
            if limit_method == SOURCE_IDS_LIMIT_METHOD_FIFO:
                truncation_info = truncation_info_log
            else:
                truncation_info = "KEEP Old"

        deduplicated_num = already_fragment + len(edges_data) - num_fragment
        dd_message = ""
        if deduplicated_num > 0:
            # Duplicated description detected across multiple trucks for the same entity
            dd_message = f"dd {deduplicated_num}"

        if dd_message or truncation_info_log:
            status_message += (
                f" ({', '.join(filter(None, [truncation_info_log, dd_message]))})"
            )

        # Add message to pipeline satus when merge happens
        if already_fragment > 0 or llm_was_used:
            logger.info(status_message)
            if pipeline_status is not None and pipeline_status_lock is not None:
                async with pipeline_status_lock:
                    pipeline_status["latest_message"] = status_message
                    pipeline_status["history_messages"].append(status_message)
        else:
            logger.debug(status_message)

        # 11. Update both graph and vector db
        for need_insert_id in [src_id, tgt_id]:
            # Optimization: Use get_node instead of has_node + get_node
            existing_node = await knowledge_graph_inst.get_node(need_insert_id)

            if existing_node is None:
                # Node doesn't exist - create new node
                node_created_at = int(time.time())
                node_data = {
                    "entity_id": need_insert_id,
                    "source_id": source_id,
                    "description": description,
                    "entity_type": "UNKNOWN",
                    "file_path": file_path,
                    "created_at": node_created_at,
                    "truncate": "",
                }
                await knowledge_graph_inst.upsert_node(
                    need_insert_id, node_data=node_data
                )

                # Update entity_chunks_storage for the newly created entity
                if entity_chunks_storage is not None:
                    chunk_ids = [chunk_id for chunk_id in full_source_ids if chunk_id]
                    if chunk_ids:
                        await entity_chunks_storage.upsert(
                            {
                                need_insert_id: {
                                    "chunk_ids": chunk_ids,
                                    "count": len(chunk_ids),
                                }
                            }
                        )

                if entity_vdb is not None:
                    entity_vdb_id = compute_mdhash_id(need_insert_id, prefix="ent-")
                    entity_content = f"{need_insert_id}\n{description}"
                    vdb_data = {
                        entity_vdb_id: {
                            "content": entity_content,
                            "entity_name": need_insert_id,
                            "source_id": source_id,
                            "entity_type": "UNKNOWN",
                            "file_path": file_path,
                        }
                    }
                    await safe_vdb_operation_with_exception(
                        operation=lambda payload=vdb_data: entity_vdb.upsert(payload),
                        operation_name="added_entity_upsert",
                        entity_name=need_insert_id,
                        max_retries=3,
                        retry_delay=0.1,
                    )

                # Track entities added during edge processing
                if added_entities is not None:
                    entity_data = {
                        "entity_name": need_insert_id,
                        "entity_type": "UNKNOWN",
                        "description": description,
                        "source_id": source_id,
                        "file_path": file_path,
                        "created_at": node_created_at,
                    }
                    added_entities.append(entity_data)
            else:
                # Node exists - update its source_ids by merging with new source_ids
                updated = False  # Track if any update occurred

                # 1. Get existing full source_ids from entity_chunks_storage
                existing_full_source_ids = []
                if entity_chunks_storage is not None:
                    stored_chunks = await entity_chunks_storage.get_by_id(
                        need_insert_id
                    )
                    if stored_chunks and isinstance(stored_chunks, dict):
                        existing_full_source_ids = [
                            chunk_id
                            for chunk_id in stored_chunks.get("chunk_ids", [])
                            if chunk_id
                        ]

                # If not in entity_chunks_storage, get from graph database
                if not existing_full_source_ids:
                    if existing_node.get("source_id"):
                        existing_full_source_ids = existing_node["source_id"].split(
                            GRAPH_FIELD_SEP
                        )

                # 2. Merge with new source_ids from this relationship
                new_source_ids_from_relation = [
                    chunk_id for chunk_id in source_ids if chunk_id
                ]
                merged_full_source_ids = merge_source_ids(
                    existing_full_source_ids, new_source_ids_from_relation
                )

                # 3. Save merged full list to entity_chunks_storage (conditional)
                if (
                    entity_chunks_storage is not None
                    and merged_full_source_ids != existing_full_source_ids
                ):
                    updated = True
                    await entity_chunks_storage.upsert(
                        {
                            need_insert_id: {
                                "chunk_ids": merged_full_source_ids,
                                "count": len(merged_full_source_ids),
                            }
                        }
                    )

                # 4. Apply source_ids limit for graph and vector db
                limit_method = global_config.get(
                    "source_ids_limit_method", SOURCE_IDS_LIMIT_METHOD_KEEP
                )
                max_source_limit = global_config.get("max_source_ids_per_entity")
                limited_source_ids = apply_source_ids_limit(
                    merged_full_source_ids,
                    max_source_limit,
                    limit_method,
                    identifier=f"`{need_insert_id}`",
                )

                # 5. Update graph database and vector database with limited source_ids (conditional)
                limited_source_id_str = GRAPH_FIELD_SEP.join(limited_source_ids)

                if limited_source_id_str != existing_node.get("source_id", ""):
                    updated = True
                    updated_node_data = {
                        **existing_node,
                        "source_id": limited_source_id_str,
                    }
                    await knowledge_graph_inst.upsert_node(
                        need_insert_id, node_data=updated_node_data
                    )

                    # Update vector database
                    if entity_vdb is not None:
                        entity_vdb_id = compute_mdhash_id(need_insert_id, prefix="ent-")
                        entity_content = (
                            f"{need_insert_id}\n{existing_node.get('description', '')}"
                        )
                        vdb_data = {
                            entity_vdb_id: {
                                "content": entity_content,
                                "entity_name": need_insert_id,
                                "source_id": limited_source_id_str,
                                "entity_type": existing_node.get(
                                    "entity_type", "UNKNOWN"
                                ),
                                "file_path": existing_node.get(
                                    "file_path", "unknown_source"
                                ),
                            }
                        }
                        await safe_vdb_operation_with_exception(
                            operation=lambda payload=vdb_data: entity_vdb.upsert(
                                payload
                            ),
                            operation_name="existing_entity_update",
                            entity_name=need_insert_id,
                            max_retries=3,
                            retry_delay=0.1,
                        )

                # 6. Log once at the end if any update occurred
                if updated:
                    status_message = (
                        f"Chunks appended from relation: `{need_insert_id}`"
                    )
                    logger.info(status_message)
                    if pipeline_status is not None and pipeline_status_lock is not None:
                        async with pipeline_status_lock:
                            pipeline_status["latest_message"] = status_message
                            pipeline_status["history_messages"].append(status_message)

        edge_created_at = int(time.time())
        await knowledge_graph_inst.upsert_edge(
            src_id,
            tgt_id,
            edge_data=dict(
                weight=weight,
                description=description,
                keywords=keywords,
                source_id=source_id,
                file_path=file_path,
                created_at=edge_created_at,
                truncate=truncation_info,
            ),
        )

        edge_data = dict(
            src_id=src_id,
            tgt_id=tgt_id,
            description=description,
            keywords=keywords,
            source_id=source_id,
            file_path=file_path,
            created_at=edge_created_at,
            truncate=truncation_info,
            weight=weight,
        )

        # Sort src_id and tgt_id to ensure consistent ordering (smaller string first)
        if src_id > tgt_id:
            src_id, tgt_id = tgt_id, src_id

        # Build VDB payload but defer upsert to batch (Phase 3) to avoid
        # nano_vectordb's O(N) per-call linear scan → O(N²) total.
        rel_vdb_payload = None
        rel_vdb_delete_ids = None
        if relationships_vdb is not None:
            rel_vdb_id = compute_mdhash_id(src_id + tgt_id, prefix="rel-")
            rel_vdb_id_reverse = compute_mdhash_id(tgt_id + src_id, prefix="rel-")
            rel_vdb_delete_ids = [rel_vdb_id, rel_vdb_id_reverse]
            rel_content = f"{keywords}\t{src_id}\n{tgt_id}\n{description}"
            rel_vdb_payload = {
                rel_vdb_id: {
                    "src_id": src_id,
                    "tgt_id": tgt_id,
                    "source_id": source_id,
                    "content": rel_content,
                    "keywords": keywords,
                    "description": description,
                    "weight": weight,
                    "file_path": file_path,
                }
            }

        return edge_data, rel_vdb_payload, rel_vdb_delete_ids
    finally:
        performance_timing_log(
            "[_merge_edges_then_upsert] %s completed in %.4fs",
            timing_relation,
            time.perf_counter() - timing_start,
        )


async def merge_nodes_and_edges(
    chunk_results: list,
    knowledge_graph_inst: BaseGraphStorage,
    entity_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    global_config: dict[str, str],
    full_entities_storage: BaseKVStorage = None,
    full_relations_storage: BaseKVStorage = None,
    doc_id: str = None,
    pipeline_status: dict = None,
    pipeline_status_lock=None,
    llm_response_cache: BaseKVStorage | None = None,
    entity_chunks_storage: BaseKVStorage | None = None,
    relation_chunks_storage: BaseKVStorage | None = None,
    current_file_number: int = 0,
    total_files: int = 0,
    file_path: str = "unknown_source",
) -> None:
    """Two-phase merge: process all entities first, then all relationships

    This approach ensures data consistency by:
    1. Phase 1: Process all entities concurrently
    2. Phase 2: Process all relationships concurrently (may add missing entities)
    3. Phase 3: Update full_entities and full_relations storage with final results

    Args:
        chunk_results: List of tuples (maybe_nodes, maybe_edges) containing extracted entities and relationships
        knowledge_graph_inst: Knowledge graph storage
        entity_vdb: Entity vector database
        relationships_vdb: Relationship vector database
        global_config: Global configuration
        full_entities_storage: Storage for document entity lists
        full_relations_storage: Storage for document relation lists
        doc_id: Document ID for storage indexing
        pipeline_status: Pipeline status dictionary
        pipeline_status_lock: Lock for pipeline status
        llm_response_cache: LLM response cache
        entity_chunks_storage: Storage tracking full chunk lists per entity
        relation_chunks_storage: Storage tracking full chunk lists per relation
        current_file_number: Current file number for logging
        total_files: Total files for logging
        file_path: File path for logging
    """

    # Check for cancellation at the start of merge
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            if pipeline_status.get("cancellation_requested", False):
                raise PipelineCancelledException("User cancelled during merge phase")

    # Collect all nodes and edges from all chunks
    all_nodes = defaultdict(list)
    all_edges = defaultdict(list)

    for i, (maybe_nodes, maybe_edges) in enumerate(chunk_results, start=1):
        # Collect nodes
        for entity_name, entities in maybe_nodes.items():
            all_nodes[entity_name].extend(entities)

        # Collect edges with sorted keys for undirected graph
        for edge_key, edges in maybe_edges.items():
            sorted_edge_key = tuple(sorted(edge_key))
            all_edges[sorted_edge_key].extend(edges)
        await _cooperative_yield(i, every=32)

    total_entities_count = len(all_nodes)
    total_relations_count = len(all_edges)

    log_message = f"Merging stage {current_file_number}/{total_files}: {file_path}"
    logger.info(log_message)
    async with pipeline_status_lock:
        pipeline_status["latest_message"] = log_message
        pipeline_status["history_messages"].append(log_message)

    # Get max async tasks limit from global_config for semaphore control
    graph_max_async = global_config.get("llm_model_max_async", 4) * 2
    semaphore = asyncio.Semaphore(graph_max_async)

    # ===== Phase 1: Process all entities concurrently =====
    log_message = f"Phase 1: Processing {total_entities_count} entities from {doc_id} (async: {graph_max_async})"
    logger.info(log_message)
    async with pipeline_status_lock:
        pipeline_status["latest_message"] = log_message
        pipeline_status["history_messages"].append(log_message)

    async def _locked_process_entity_name(entity_name, entities):
        async with semaphore:
            # Check for cancellation before processing entity
            if pipeline_status is not None and pipeline_status_lock is not None:
                async with pipeline_status_lock:
                    if pipeline_status.get("cancellation_requested", False):
                        raise PipelineCancelledException(
                            "User cancelled during entity merge"
                        )

            workspace = global_config.get("workspace", "")
            namespace = f"{workspace}:GraphDB" if workspace else "GraphDB"
            async with get_storage_keyed_lock(
                [entity_name], namespace=namespace, enable_logging=False
            ):
                try:
                    logger.debug(f"Processing entity {entity_name}")
                    entity_data = await _merge_nodes_then_upsert(
                        entity_name,
                        entities,
                        knowledge_graph_inst,
                        entity_vdb,
                        global_config,
                        pipeline_status,
                        pipeline_status_lock,
                        llm_response_cache,
                        entity_chunks_storage,
                    )

                    return entity_data

                except Exception as e:
                    error_msg = f"Error processing entity `{entity_name}`: {e}"
                    logger.error(error_msg)

                    # Try to update pipeline status, but don't let status update failure affect main exception
                    try:
                        if (
                            pipeline_status is not None
                            and pipeline_status_lock is not None
                        ):
                            async with pipeline_status_lock:
                                pipeline_status["latest_message"] = error_msg
                                pipeline_status["history_messages"].append(error_msg)
                    except Exception as status_error:
                        logger.error(
                            f"Failed to update pipeline status: {status_error}"
                        )

                    # Re-raise the original exception with a prefix
                    prefixed_exception = create_prefixed_exception(
                        e, f"`{entity_name}`"
                    )
                    raise prefixed_exception from e

    # Create entity processing tasks
    entity_tasks = []
    for i, (entity_name, entities) in enumerate(all_nodes.items(), start=1):
        task = asyncio.create_task(_locked_process_entity_name(entity_name, entities))
        entity_tasks.append(task)
        await _cooperative_yield(i, every=16)

    # Execute entity tasks with error handling.
    # Each task returns (node_data, vdb_payload). We collect vdb_payloads
    # to do ONE batch upsert at Phase 3 instead of N individual upserts
    # (avoids nano_vectordb's O(N) per-call linear scan → O(N²) total).
    processed_entities = []
    entity_vdb_batch: dict[str, dict[str, Any]] = {}
    if entity_tasks:
        done, pending = await asyncio.wait(
            entity_tasks, return_when=asyncio.FIRST_EXCEPTION
        )

        first_exception = None
        processed_entities = []

        for i, task in enumerate(done, start=1):
            try:
                result = task.result()
            except BaseException as e:
                if first_exception is None:
                    first_exception = e
            else:
                node_data, vdb_payload = result
                processed_entities.append(node_data)
                if vdb_payload:
                    entity_vdb_batch.update(vdb_payload)
            await _cooperative_yield(i, every=32)

        if pending:
            for task in pending:
                task.cancel()
            pending_results = await asyncio.gather(*pending, return_exceptions=True)
            for result in pending_results:
                if isinstance(result, BaseException):
                    if first_exception is None:
                        first_exception = result
                else:
                    node_data, vdb_payload = result
                    processed_entities.append(node_data)
                    if vdb_payload:
                        entity_vdb_batch.update(vdb_payload)

        if first_exception is not None:
            raise first_exception

        await asyncio.sleep(0)

    # ===== Phase 2: Process all relationships concurrently =====
    log_message = f"Phase 2: Processing {total_relations_count} relations from {doc_id} (async: {graph_max_async})"
    logger.info(log_message)
    async with pipeline_status_lock:
        pipeline_status["latest_message"] = log_message
        pipeline_status["history_messages"].append(log_message)

    async def _locked_process_edges(edge_key, edges):
        async with semaphore:
            # Check for cancellation before processing edges
            if pipeline_status is not None and pipeline_status_lock is not None:
                async with pipeline_status_lock:
                    if pipeline_status.get("cancellation_requested", False):
                        raise PipelineCancelledException(
                            "User cancelled during relation merge"
                        )

            workspace = global_config.get("workspace", "")
            namespace = f"{workspace}:GraphDB" if workspace else "GraphDB"
            sorted_edge_key = sorted([edge_key[0], edge_key[1]])

            async with get_storage_keyed_lock(
                sorted_edge_key,
                namespace=namespace,
                enable_logging=False,
            ):
                try:
                    added_entities = []  # Track entities added during edge processing

                    logger.debug(f"Processing relation {sorted_edge_key}")
                    edge_data, rel_vdb_payload, rel_vdb_delete_ids = (
                        await _merge_edges_then_upsert(
                            edge_key[0],
                            edge_key[1],
                            edges,
                            knowledge_graph_inst,
                            relationships_vdb,
                            entity_vdb,
                            global_config,
                            pipeline_status,
                            pipeline_status_lock,
                            llm_response_cache,
                            added_entities,  # Pass list to collect added entities
                            relation_chunks_storage,
                            entity_chunks_storage,
                        )
                    )

                    if edge_data is None:
                        return None, [], None, None

                    return edge_data, added_entities, rel_vdb_payload, rel_vdb_delete_ids

                except Exception as e:
                    error_msg = f"Error processing relation `{sorted_edge_key}`: {e}"
                    logger.error(error_msg)

                    # Try to update pipeline status, but don't let status update failure affect main exception
                    try:
                        if (
                            pipeline_status is not None
                            and pipeline_status_lock is not None
                        ):
                            async with pipeline_status_lock:
                                pipeline_status["latest_message"] = error_msg
                                pipeline_status["history_messages"].append(error_msg)
                    except Exception as status_error:
                        logger.error(
                            f"Failed to update pipeline status: {status_error}"
                        )

                    # Re-raise the original exception with a prefix
                    prefixed_exception = create_prefixed_exception(
                        e, f"{sorted_edge_key}"
                    )
                    raise prefixed_exception from e

    # Create relationship processing tasks
    edge_tasks = []
    for i, (edge_key, edges) in enumerate(all_edges.items(), start=1):
        task = asyncio.create_task(_locked_process_edges(edge_key, edges))
        edge_tasks.append(task)
        await _cooperative_yield(i, every=16)

    # Execute relationship tasks with error handling.
    # Each task returns (edge_data, added_entities, rel_vdb_payload, rel_vdb_delete_ids).
    # VDB payloads are collected for batch upsert in Phase 3.
    processed_edges = []
    all_added_entities = []
    rel_vdb_batch: dict[str, dict[str, Any]] = {}
    rel_vdb_delete_batch: list[str] = []

    if edge_tasks:
        done, pending = await asyncio.wait(
            edge_tasks, return_when=asyncio.FIRST_EXCEPTION
        )

        first_exception = None

        for i, task in enumerate(done, start=1):
            try:
                edge_data, added_entities, rel_vdb_payload, rel_vdb_delete_ids = (
                    task.result()
                )
            except BaseException as e:
                if first_exception is None:
                    first_exception = e
            else:
                if edge_data is not None:
                    processed_edges.append(edge_data)
                all_added_entities.extend(added_entities or [])
                if rel_vdb_payload:
                    rel_vdb_batch.update(rel_vdb_payload)
                if rel_vdb_delete_ids:
                    rel_vdb_delete_batch.extend(rel_vdb_delete_ids)
            await _cooperative_yield(i, every=32)

        if pending:
            for task in pending:
                task.cancel()
            pending_results = await asyncio.gather(*pending, return_exceptions=True)
            for result in pending_results:
                if isinstance(result, BaseException):
                    if first_exception is None:
                        first_exception = result
                else:
                    edge_data, added_entities, rel_vdb_payload, rel_vdb_delete_ids = (
                        result
                    )
                    if edge_data is not None:
                        processed_edges.append(edge_data)
                    all_added_entities.extend(added_entities or [])
                    if rel_vdb_payload:
                        rel_vdb_batch.update(rel_vdb_payload)
                    if rel_vdb_delete_ids:
                        rel_vdb_delete_batch.extend(rel_vdb_delete_ids)

        if first_exception is not None:
            raise first_exception

        await asyncio.sleep(0)

    # ===== Phase 3: Update full_entities and full_relations storage =====
    if full_entities_storage and full_relations_storage and doc_id:
        try:
            # Merge all entities: original entities + entities added during edge processing
            final_entity_names = set()

            # Add original processed entities
            for i, entity_data in enumerate(processed_entities, start=1):
                if entity_data and entity_data.get("entity_name"):
                    final_entity_names.add(entity_data["entity_name"])
                await _cooperative_yield(i, every=32)

            # Add entities that were added during relationship processing
            for i, added_entity in enumerate(all_added_entities, start=1):
                if added_entity and added_entity.get("entity_name"):
                    final_entity_names.add(added_entity["entity_name"])
                await _cooperative_yield(i, every=32)

            # Collect all relation pairs
            final_relation_pairs = set()
            for i, edge_data in enumerate(processed_edges, start=1):
                if edge_data:
                    src_id = edge_data.get("src_id")
                    tgt_id = edge_data.get("tgt_id")
                    if src_id and tgt_id:
                        relation_pair = tuple(sorted([src_id, tgt_id]))
                        final_relation_pairs.add(relation_pair)
                await _cooperative_yield(i, every=32)

            log_message = f"Phase 3: Updating final {len(final_entity_names)}({len(processed_entities)}+{len(all_added_entities)}) entities and  {len(final_relation_pairs)} relations from {doc_id}"
            logger.info(log_message)
            async with pipeline_status_lock:
                pipeline_status["latest_message"] = log_message
                pipeline_status["history_messages"].append(log_message)

            # Update storage
            if final_entity_names:
                await full_entities_storage.upsert(
                    {
                        doc_id: {
                            "entity_names": list(final_entity_names),
                            "count": len(final_entity_names),
                        }
                    }
                )

            if final_relation_pairs:
                await full_relations_storage.upsert(
                    {
                        doc_id: {
                            "relation_pairs": [
                                list(pair) for pair in final_relation_pairs
                            ],
                            "count": len(final_relation_pairs),
                        }
                    }
                )

            logger.debug(
                f"Updated entity-relation index for document {doc_id}: {len(final_entity_names)} entities (original: {len(processed_entities)}, added: {len(all_added_entities)}), {len(final_relation_pairs)} relations"
            )

        except Exception as e:
            logger.error(
                f"Failed to update entity-relation index for document {doc_id}: {e}"
            )
            # Don't raise exception to avoid affecting main flow

    # ===== Phase 3b: Batch VDB upserts (O(N) instead of O(N²)) =====
    # nano_vectordb's upsert does a linear scan of all existing records on
    # every call. Upserting N items one-by-one is O(N²). Collecting all
    # payloads and doing ONE upsert call is O(N) — the single call still
    # scans the existing data, but only once.
    vdb_upsert_tasks = []
    if entity_vdb is not None and entity_vdb_batch:
        entity_vdb_batch, entity_vdb_diff_stats = await _filter_unchanged_vdb_upsert_batch(
            entity_vdb,
            entity_vdb_batch,
            label="entity_vdb",
        )
        if entity_vdb_diff_stats["skipped"] and not entity_vdb_batch:
            logger.info(
                "Batch entity upsert skipped entirely: all %s records unchanged",
                entity_vdb_diff_stats["total"],
            )
        if entity_vdb_diff_stats["skipped"] and entity_vdb_batch:
            logger.info(
                "Batch entity upsert reduced %s -> %s records after unchanged diff",
                entity_vdb_diff_stats["total"],
                len(entity_vdb_batch),
            )
    if entity_vdb is not None and entity_vdb_batch:
        logger.info(
            f"Batch upserting {len(entity_vdb_batch)} entities to entity_vdb"
        )
        vdb_upsert_tasks.append(
            safe_vdb_operation_with_exception(
                operation=lambda: entity_vdb.upsert(entity_vdb_batch),
                operation_name="batch_entity_upsert",
                entity_name=f"batch({len(entity_vdb_batch)})",
                max_retries=3,
                retry_delay=0.5,
            )
        )
    if relationships_vdb is not None:
        if rel_vdb_batch:
            rel_vdb_batch, rel_vdb_diff_stats = await _filter_unchanged_vdb_upsert_batch(
                relationships_vdb,
                rel_vdb_batch,
                label="relationships_vdb",
            )

            if rel_vdb_delete_batch:
                allowed_delete_ids: set[str] = set()
                for rel_vdb_id, payload in rel_vdb_batch.items():
                    allowed_delete_ids.add(rel_vdb_id)
                    src_id = str(payload.get("src_id") or "")
                    tgt_id = str(payload.get("tgt_id") or "")
                    if src_id and tgt_id:
                        allowed_delete_ids.add(
                            compute_mdhash_id(tgt_id + src_id, prefix="rel-")
                        )
                rel_vdb_delete_batch = list(
                    dict.fromkeys(
                        rel_id
                        for rel_id in rel_vdb_delete_batch
                        if rel_id in allowed_delete_ids
                    )
                )

            if rel_vdb_diff_stats["skipped"] and not rel_vdb_batch:
                logger.info(
                    "Batch relationship upsert skipped entirely: all %s records unchanged",
                    rel_vdb_diff_stats["total"],
                )
            if rel_vdb_diff_stats["skipped"] and rel_vdb_batch:
                logger.info(
                    "Batch relationship upsert reduced %s -> %s records after unchanged diff",
                    rel_vdb_diff_stats["total"],
                    len(rel_vdb_batch),
                )
        # Delete old relationship vectors first (stale forward+reverse ids)
        if rel_vdb_delete_batch:
            try:
                await relationships_vdb.delete(rel_vdb_delete_batch)
            except Exception as e:
                logger.warning(
                    f"Failed to batch-delete {len(rel_vdb_delete_batch)} "
                    f"stale relationship vectors: {e}"
                )
        if rel_vdb_batch:
            logger.info(
                f"Batch upserting {len(rel_vdb_batch)} relations to relationships_vdb"
            )
            vdb_upsert_tasks.append(
                safe_vdb_operation_with_exception(
                    operation=lambda: relationships_vdb.upsert(rel_vdb_batch),
                    operation_name="batch_relationship_upsert",
                    entity_name=f"batch({len(rel_vdb_batch)})",
                    max_retries=3,
                    retry_delay=0.5,
                )
            )
    if vdb_upsert_tasks:
        await asyncio.gather(*vdb_upsert_tasks)

    log_message = f"Completed merging: {len(processed_entities)} entities, {len(all_added_entities)} extra entities, {len(processed_edges)} relations"
    logger.info(log_message)
    async with pipeline_status_lock:
        pipeline_status["latest_message"] = log_message
        pipeline_status["history_messages"].append(log_message)


def _build_entity_extraction_context_base(
    global_config: dict[str, Any]
) -> dict[str, str]:
    language = global_config["addon_params"].get("language", DEFAULT_SUMMARY_LANGUAGE)
    entity_types = global_config["addon_params"].get(
        "entity_types", DEFAULT_ENTITY_TYPES
    )

    examples = "\n".join(PROMPTS["entity_extraction_examples"])
    example_context_base = dict(
        tuple_delimiter=PROMPTS["DEFAULT_TUPLE_DELIMITER"],
        completion_delimiter=PROMPTS["DEFAULT_COMPLETION_DELIMITER"],
        entity_types=", ".join(entity_types),
        language=language,
    )
    examples = examples.format(**example_context_base)

    return dict(
        tuple_delimiter=PROMPTS["DEFAULT_TUPLE_DELIMITER"],
        completion_delimiter=PROMPTS["DEFAULT_COMPLETION_DELIMITER"],
        entity_types=",".join(entity_types),
        examples=examples,
        language=language,
    )


def _build_entity_extraction_prompts(
    content: str, context_base: dict[str, str]
) -> tuple[str, str, str]:
    entity_extraction_system_prompt = PROMPTS["entity_extraction_system_prompt"].format(
        **context_base
    )
    entity_extraction_user_prompt = PROMPTS["entity_extraction_user_prompt"].format(
        **{**context_base, "input_text": content}
    )
    entity_continue_extraction_user_prompt = PROMPTS[
        "entity_continue_extraction_user_prompt"
    ].format(**{**context_base, "input_text": content})
    return (
        entity_extraction_system_prompt,
        entity_extraction_user_prompt,
        entity_continue_extraction_user_prompt,
    )


def _build_extract_cache_descriptor(
    user_prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    prompt_parts = []
    if user_prompt:
        prompt_parts.append(user_prompt)
    if system_prompt:
        prompt_parts.append(system_prompt)
    if history_messages:
        prompt_parts.append(json.dumps(history_messages, ensure_ascii=False))
    prompt = "\n".join(prompt_parts)
    args_hash = compute_args_hash(prompt)
    return {
        "prompt": prompt,
        "args_hash": args_hash,
        "cache_key": generate_cache_key("default", "extract", args_hash),
    }


def _normalize_entity_extraction_mode(global_config: dict[str, Any]) -> str:
    raw_mode = str(global_config.get("entity_extraction_mode", "auto") or "auto")
    normalized = raw_mode.strip().lower()
    if normalized not in {"auto", "realtime", "batch"}:
        return "auto"
    return normalized


def _can_use_openai_batch_entity_extraction(
    global_config: dict[str, Any], total_chunks: int
) -> tuple[bool, str]:
    mode = _normalize_entity_extraction_mode(global_config)
    if mode == "realtime":
        return False, "entity_extraction_mode=realtime"

    llm_binding = str(global_config.get("llm_binding", "") or "").strip().lower()
    if llm_binding != "openai":
        return False, f"unsupported binding={llm_binding or 'unknown'}"

    if not global_config.get("llm_binding_api_key"):
        return False, "missing llm_binding_api_key"

    if not global_config.get("llm_model_name"):
        return False, "missing llm_model_name"

    if mode == "batch":
        return True, "entity_extraction_mode=batch"

    min_chunks = max(
        int(global_config.get("entity_extraction_batch_min_chunks", 120) or 120),
        1,
    )
    if total_chunks < min_chunks:
        return False, f"chunk count {total_chunks} below threshold {min_chunks}"
    return True, f"auto batch enabled for {total_chunks} chunks"


def _extract_text_from_batch_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str):
                    parts.append(text_value)
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _extract_result_text_from_batch_output(
    output_line: dict[str, Any],
) -> tuple[str | None, str | None]:
    error = output_line.get("error")
    if error:
        return None, str(error)

    response = output_line.get("response") or {}
    status_code = response.get("status_code")
    body = response.get("body") or {}
    if status_code and int(status_code) >= 400:
        if isinstance(body, dict):
            body_error = body.get("error") or {}
            return None, body_error.get("message") or f"status_code={status_code}"
        return None, f"status_code={status_code}"

    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        return None, "batch response missing choices"

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None, "batch response missing message"

    content = _extract_text_from_batch_content(message.get("content"))
    if not content:
        return None, "batch response returned empty content"

    return remove_think_tags(content), None


async def _update_doc_processing_progress(
    doc_status_storage: BaseKVStorage | None,
    doc_id: str | None,
    *,
    stage: str,
    stage_label: str,
    processed: int,
    total: int,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    if doc_status_storage is None or not doc_id:
        return

    existing_record = await doc_status_storage.get_by_id(doc_id)
    if not existing_record:
        return

    record = dict(existing_record)
    metadata = dict(record.get("metadata", {}) or {})
    now_ts = int(time.time())
    processing_start_time = metadata.get("processing_start_time")
    if not isinstance(processing_start_time, int):
        processing_start_time = now_ts

    total_items = max(int(total or 0), 0)
    processed_items = max(int(processed or 0), 0)
    if total_items > 0:
        processed_items = min(processed_items, total_items)
    remaining_items = max(total_items - processed_items, 0)
    elapsed_seconds = max(now_ts - processing_start_time, 0)
    progress_percent: float | None = None
    eta_seconds: int | None = None
    if total_items > 0:
        progress_percent = round((processed_items / total_items) * 100, 2)
        if 0 < processed_items < total_items:
            eta_seconds = int(
                elapsed_seconds * (total_items - processed_items) / processed_items
            )
        elif processed_items >= total_items:
            eta_seconds = 0

    metadata.update(
        {
            "processing_stage": stage,
            "processing_stage_label": stage_label,
            "processing_progress_percent": progress_percent,
            "processing_items_processed": processed_items,
            "processing_items_total": total_items,
            "processing_items_remaining": remaining_items,
            "processing_elapsed_seconds": elapsed_seconds,
            "processing_eta_seconds": eta_seconds,
            "processing_last_updated_at": now_ts,
        }
    )
    if extra_metadata:
        metadata.update(extra_metadata)

    record["metadata"] = metadata
    await doc_status_storage.upsert({doc_id: record})


async def _update_entity_extraction_pipeline_progress(
    pipeline_status: dict | None,
    pipeline_status_lock: asyncio.Lock | None,
    *,
    total_chunks: int,
    processed_chunks: int,
    stage: str,
    stage_label: str,
    latest_message: str | None = None,
    append_history: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> None:
    if pipeline_status is None or pipeline_status_lock is None:
        return

    now_ts = int(time.time())
    async with pipeline_status_lock:
        stage_started_at = pipeline_status.get("stage_started_at")
        if pipeline_status.get("current_stage") != stage or not isinstance(
            stage_started_at, int
        ):
            stage_started_at = now_ts
            pipeline_status["stage_started_at"] = stage_started_at

        total_items = max(int(total_chunks or 0), 0)
        processed_items = max(int(processed_chunks or 0), 0)
        if total_items > 0:
            processed_items = min(processed_items, total_items)
        remaining_items = max(total_items - processed_items, 0)
        elapsed_seconds = max(now_ts - stage_started_at, 0)
        eta_seconds: int | None = None
        if 0 < processed_items < total_items:
            eta_seconds = int(elapsed_seconds * remaining_items / processed_items)
        elif total_items > 0 and processed_items >= total_items:
            eta_seconds = 0

        pipeline_status["total_chunks"] = max(
            int(pipeline_status.get("total_chunks", 0) or 0), total_items
        )
        pipeline_status["processed_chunks"] = max(
            int(pipeline_status.get("processed_chunks", 0) or 0), processed_items
        )
        pipeline_status["current_stage"] = stage
        pipeline_status["current_stage_label"] = stage_label
        pipeline_status["stage_unit"] = "chunks"
        pipeline_status["stage_total"] = total_items
        pipeline_status["stage_processed"] = processed_items
        pipeline_status["stage_remaining"] = remaining_items
        pipeline_status["stage_elapsed_seconds"] = elapsed_seconds
        pipeline_status["stage_eta_seconds"] = eta_seconds
        if extra_fields:
            for key, value in extra_fields.items():
                pipeline_status[key] = value
        if latest_message:
            pipeline_status["latest_message"] = latest_message
            if append_history:
                pipeline_status["history_messages"].append(latest_message)


async def _extract_entities_via_openai_batch(
    ordered_chunks: list[tuple[str, TextChunkSchema]],
    context_base: dict[str, str],
    global_config: dict[str, Any],
    pipeline_status: dict | None = None,
    pipeline_status_lock: asyncio.Lock | None = None,
    llm_response_cache: BaseKVStorage | None = None,
    text_chunks_storage: BaseKVStorage | None = None,
    doc_status_storage: BaseKVStorage | None = None,
) -> list[tuple[dict, dict]]:
    total_chunks = len(ordered_chunks)
    doc_id = next(
        (
            chunk_dp.get("full_doc_id")
            for _, chunk_dp in ordered_chunks
            if chunk_dp.get("full_doc_id")
        ),
        None,
    )

    batch_requests: list[OpenAIBatchRequest] = []
    batch_contexts: dict[str, dict[str, Any]] = {}
    cached_results: dict[str, tuple[dict, dict]] = {}

    for chunk_key, chunk_dp in ordered_chunks:
        content = chunk_dp["content"]
        file_path = chunk_dp.get("file_path", "unknown_source")
        (
            entity_extraction_system_prompt,
            entity_extraction_user_prompt,
            _entity_continue_extraction_user_prompt,
        ) = _build_entity_extraction_prompts(content, context_base)
        cache_descriptor = _build_extract_cache_descriptor(
            entity_extraction_user_prompt,
            entity_extraction_system_prompt,
        )

        cached_result = None
        if llm_response_cache is not None:
            cached_result = await handle_cache(
                llm_response_cache,
                cache_descriptor["args_hash"],
                cache_descriptor["prompt"],
                "default",
                cache_type="extract",
            )

        if cached_result:
            result_text, timestamp = cached_result
            maybe_nodes, maybe_edges = await _process_extraction_result(
                result_text,
                chunk_key,
                timestamp,
                file_path,
                tuple_delimiter=context_base["tuple_delimiter"],
                completion_delimiter=context_base["completion_delimiter"],
            )
            cached_results[chunk_key] = (maybe_nodes, maybe_edges)
            if text_chunks_storage:
                await update_chunk_cache_list(
                    chunk_key,
                    text_chunks_storage,
                    [cache_descriptor["cache_key"]],
                    "entity_extraction",
                )
            continue

        batch_requests.append(
            OpenAIBatchRequest(
                custom_id=chunk_key,
                body={
                    "model": global_config.get("llm_model_name"),
                    "messages": [
                        {
                            "role": "system",
                            "content": entity_extraction_system_prompt,
                        },
                        {"role": "user", "content": entity_extraction_user_prompt},
                    ],
                    "stream": False,
                    **dict(global_config.get("llm_openai_options") or {}),
                },
            )
        )
        batch_contexts[chunk_key] = {
            "file_path": file_path,
            "cache_descriptor": cache_descriptor,
        }

    processed_chunks = len(cached_results)
    await _update_entity_extraction_pipeline_progress(
        pipeline_status,
        pipeline_status_lock,
        total_chunks=total_chunks,
        processed_chunks=processed_chunks,
        stage="entity_extraction_batch",
        stage_label="Batch extracting entities",
        latest_message=(
            f"Submitting entity extraction batch for {len(batch_requests)} chunk(s)"
        ),
        append_history=True,
    )
    await _update_doc_processing_progress(
        doc_status_storage,
        doc_id,
        stage="entity_extraction_batch",
        stage_label="Batch extracting entities",
        processed=processed_chunks,
        total=total_chunks,
    )

    async def _check_batch_cancellation() -> bool:
        if pipeline_status is None or pipeline_status_lock is None:
            return False
        async with pipeline_status_lock:
            return bool(pipeline_status.get("cancellation_requested", False))

    async def _on_batch_status(status_payload: dict[str, Any]) -> None:
        request_counts = dict(status_payload.get("request_counts") or {})
        batch_completed = int(request_counts.get("completed", 0) or 0)
        batch_message = (
            f"Entity extraction batch {status_payload.get('batch_id')} "
            f"status={status_payload.get('status')} "
            f"completed={batch_completed}/{len(batch_requests)}"
        )
        extra_fields = {
            "batch_job_id": status_payload.get("batch_id"),
            "batch_job_status": status_payload.get("status"),
        }
        await _update_entity_extraction_pipeline_progress(
            pipeline_status,
            pipeline_status_lock,
            total_chunks=total_chunks,
            processed_chunks=processed_chunks + batch_completed,
            stage="entity_extraction_batch",
            stage_label="Batch extracting entities",
            latest_message=batch_message,
            append_history=True,
            extra_fields=extra_fields,
        )
        await _update_doc_processing_progress(
            doc_status_storage,
            doc_id,
            stage="entity_extraction_batch",
            stage_label="Batch extracting entities",
            processed=processed_chunks + batch_completed,
            total=total_chunks,
            extra_metadata=extra_fields,
        )

    batch_result = await run_openai_chat_batch(
        batch_requests,
        api_key=global_config.get("llm_binding_api_key"),
        base_url=global_config.get("llm_binding_host"),
        timeout=global_config.get("default_llm_timeout"),
        poll_interval_seconds=max(
            int(
                global_config.get("entity_extraction_batch_poll_interval_seconds", 5)
                or 5
            ),
            1,
        ),
        timeout_seconds=max(
            int(
                global_config.get("entity_extraction_batch_timeout_seconds", 3600)
                or 3600
            ),
            1,
        ),
        metadata={
            "workflow": "entity_extraction",
            "doc_id": doc_id or "",
            "chunks": len(batch_requests),
        },
        on_status=_on_batch_status,
        should_cancel=_check_batch_cancellation,
    )

    output_by_chunk = {
        line.get("custom_id"): line
        for line in batch_result.output_lines
        if isinstance(line, dict) and line.get("custom_id")
    }
    if len(output_by_chunk) != len(batch_requests):
        raise RuntimeError(
            f"Entity extraction batch returned {len(output_by_chunk)}/"
            f"{len(batch_requests)} output rows"
        )

    results_by_chunk = dict(cached_results)
    for chunk_key, chunk_dp in ordered_chunks:
        if chunk_key in results_by_chunk:
            continue

        output_line = output_by_chunk.get(chunk_key)
        if output_line is None:
            raise RuntimeError(f"Missing batch output for chunk {chunk_key}")

        result_text, failure_reason = _extract_result_text_from_batch_output(output_line)
        if not result_text:
            raise RuntimeError(
                f"Batch output for chunk {chunk_key} was unusable: {failure_reason}"
            )

        timestamp = int(time.time())
        context = batch_contexts[chunk_key]
        cache_descriptor = context["cache_descriptor"]
        if llm_response_cache and llm_response_cache.global_config.get(
            "enable_llm_cache_for_entity_extract"
        ):
            await save_to_cache(
                llm_response_cache,
                CacheData(
                    args_hash=cache_descriptor["args_hash"],
                    content=result_text,
                    prompt=cache_descriptor["prompt"],
                    cache_type="extract",
                    chunk_id=chunk_key,
                ),
            )
        if text_chunks_storage:
            await update_chunk_cache_list(
                chunk_key,
                text_chunks_storage,
                [cache_descriptor["cache_key"]],
                "entity_extraction",
            )

        maybe_nodes, maybe_edges = await _process_extraction_result(
            result_text,
            chunk_key,
            timestamp,
            chunk_dp.get("file_path", "unknown_source"),
            tuple_delimiter=context_base["tuple_delimiter"],
            completion_delimiter=context_base["completion_delimiter"],
        )
        results_by_chunk[chunk_key] = (maybe_nodes, maybe_edges)

    return [results_by_chunk[chunk_key] for chunk_key, _ in ordered_chunks]


async def extract_entities(
    chunks: dict[str, TextChunkSchema],
    global_config: dict[str, str],
    pipeline_status: dict = None,
    pipeline_status_lock=None,
    llm_response_cache: BaseKVStorage | None = None,
    text_chunks_storage: BaseKVStorage | None = None,
    doc_status_storage: BaseKVStorage | None = None,
) -> list:
    # Check for cancellation at the start of entity extraction
    if pipeline_status is not None and pipeline_status_lock is not None:
        async with pipeline_status_lock:
            if pipeline_status.get("cancellation_requested", False):
                raise PipelineCancelledException(
                    "User cancelled during entity extraction"
                )

    use_llm_func: callable = global_config["llm_model_func"]
    entity_extract_max_gleaning = global_config["entity_extract_max_gleaning"]

    ordered_chunks = list(chunks.items())
    total_chunks = len(ordered_chunks)
    doc_id = next(
        (
            chunk_dp.get("full_doc_id")
            for _, chunk_dp in ordered_chunks
            if chunk_dp.get("full_doc_id")
        ),
        None,
    )
    context_base = _build_entity_extraction_context_base(global_config)

    should_use_batch, batch_reason = _can_use_openai_batch_entity_extraction(
        global_config, total_chunks
    )
    if should_use_batch:
        logger.info("Entity extraction batch path enabled: %s", batch_reason)
        try:
            return await _extract_entities_via_openai_batch(
                ordered_chunks,
                context_base,
                global_config,
                pipeline_status=pipeline_status,
                pipeline_status_lock=pipeline_status_lock,
                llm_response_cache=llm_response_cache,
                text_chunks_storage=text_chunks_storage,
                doc_status_storage=doc_status_storage,
            )
        except asyncio.CancelledError as exc:
            raise PipelineCancelledException(
                "User cancelled during batch entity extraction"
            ) from exc
        except Exception as exc:
            fallback_enabled = bool(
                global_config.get("entity_extraction_batch_fallback_to_realtime", True)
            )
            if not fallback_enabled:
                raise
            logger.warning(
                "Entity extraction batch path failed (%s). Falling back to realtime: %s",
                batch_reason,
                exc,
            )
            await _update_entity_extraction_pipeline_progress(
                pipeline_status,
                pipeline_status_lock,
                total_chunks=total_chunks,
                processed_chunks=int(
                    (pipeline_status or {}).get("processed_chunks", 0) or 0
                ),
                stage="entity_extraction_fallback",
                stage_label="Fallback realtime entity extraction",
                latest_message=(
                    "Batch entity extraction failed, falling back to realtime: "
                    f"{exc}"
                ),
                append_history=True,
            )
            await _update_doc_processing_progress(
                doc_status_storage,
                doc_id,
                stage="entity_extraction_fallback",
                stage_label="Fallback realtime entity extraction",
                processed=int((pipeline_status or {}).get("processed_chunks", 0) or 0),
                total=total_chunks,
            )

    processed_chunks = 0
    await _update_entity_extraction_pipeline_progress(
        pipeline_status,
        pipeline_status_lock,
        total_chunks=total_chunks,
        processed_chunks=0,
        stage=(
            "entity_extraction_fallback"
            if should_use_batch
            else "entity_extraction_realtime"
        ),
        stage_label=(
            "Fallback realtime entity extraction"
            if should_use_batch
            else "Extracting entities"
        ),
        latest_message=f"Starting entity extraction for {total_chunks} chunk(s)",
        append_history=True,
    )
    await _update_doc_processing_progress(
        doc_status_storage,
        doc_id,
        stage=(
            "entity_extraction_fallback"
            if should_use_batch
            else "entity_extraction_realtime"
        ),
        stage_label=(
            "Fallback realtime entity extraction"
            if should_use_batch
            else "Extracting entities"
        ),
        processed=0,
        total=total_chunks,
    )

    async def _process_single_content(chunk_key_dp: tuple[str, TextChunkSchema]):
        """Process a single chunk
        Args:
            chunk_key_dp (tuple[str, TextChunkSchema]):
                ("chunk-xxxxxx", {"tokens": int, "content": str, "full_doc_id": str, "chunk_order_index": int})
        Returns:
            tuple: (maybe_nodes, maybe_edges) containing extracted entities and relationships
        """
        nonlocal processed_chunks
        chunk_key = chunk_key_dp[0]
        chunk_dp = chunk_key_dp[1]
        content = chunk_dp["content"]
        # Get file path from chunk data or use default
        file_path = chunk_dp.get("file_path", "unknown_source")

        # Create cache keys collector for batch processing
        cache_keys_collector = []

        (
            entity_extraction_system_prompt,
            entity_extraction_user_prompt,
            entity_continue_extraction_user_prompt,
        ) = _build_entity_extraction_prompts(content, context_base)

        final_result, timestamp = await use_llm_func_with_cache(
            entity_extraction_user_prompt,
            use_llm_func,
            system_prompt=entity_extraction_system_prompt,
            llm_response_cache=llm_response_cache,
            cache_type="extract",
            chunk_id=chunk_key,
            cache_keys_collector=cache_keys_collector,
        )

        history = pack_user_ass_to_openai_messages(
            entity_extraction_user_prompt, final_result
        )

        # Process initial extraction with file path
        maybe_nodes, maybe_edges = await _process_extraction_result(
            final_result,
            chunk_key,
            timestamp,
            file_path,
            tuple_delimiter=context_base["tuple_delimiter"],
            completion_delimiter=context_base["completion_delimiter"],
        )

        # Process additional gleaning results only 1 time when entity_extract_max_gleaning is greater than zero.
        if entity_extract_max_gleaning > 0:
            # Calculate total tokens for the gleaning request to prevent context window overflow
            tokenizer = global_config["tokenizer"]
            max_input_tokens = global_config["max_extract_input_tokens"]

            # Approximate total tokens: system prompt + history + user prompt.
            # This slightly underestimates actual API usage (missing role/framing tokens)
            # but is sufficient as a safety guard against context window overflow.
            history_str = json.dumps(history, ensure_ascii=False)
            full_context_str = (
                entity_extraction_system_prompt
                + history_str
                + entity_continue_extraction_user_prompt
            )
            token_count = len(tokenizer.encode(full_context_str))

            if token_count > max_input_tokens:
                logger.warning(
                    f"Gleaning stopped for chunk {chunk_key}: Input tokens ({token_count}) exceeded limit ({max_input_tokens})."
                )
            else:
                glean_result, timestamp = await use_llm_func_with_cache(
                    entity_continue_extraction_user_prompt,
                    use_llm_func,
                    system_prompt=entity_extraction_system_prompt,
                    llm_response_cache=llm_response_cache,
                    history_messages=history,
                    cache_type="extract",
                    chunk_id=chunk_key,
                    cache_keys_collector=cache_keys_collector,
                )

                # Process gleaning result separately with file path
                glean_nodes, glean_edges = await _process_extraction_result(
                    glean_result,
                    chunk_key,
                    timestamp,
                    file_path,
                    tuple_delimiter=context_base["tuple_delimiter"],
                    completion_delimiter=context_base["completion_delimiter"],
                )

                # Merge results - compare description lengths to choose better version
                for i, (entity_name, glean_entities) in enumerate(
                    glean_nodes.items(), start=1
                ):
                    if entity_name in maybe_nodes:
                        # Compare description lengths and keep the better one
                        original_desc_len = len(
                            maybe_nodes[entity_name][0].get("description", "") or ""
                        )
                        glean_desc_len = len(
                            glean_entities[0].get("description", "") or ""
                        )

                        if glean_desc_len > original_desc_len:
                            maybe_nodes[entity_name] = list(glean_entities)
                        # Otherwise keep original version
                    else:
                        # New entity from gleaning stage
                        maybe_nodes[entity_name] = list(glean_entities)
                    await _cooperative_yield(i, every=8)

                for i, (edge_key, glean_edge_list) in enumerate(
                    glean_edges.items(), start=1
                ):
                    if edge_key in maybe_edges:
                        # Compare description lengths and keep the better one
                        original_desc_len = len(
                            maybe_edges[edge_key][0].get("description", "") or ""
                        )
                        glean_desc_len = len(
                            glean_edge_list[0].get("description", "") or ""
                        )

                        if glean_desc_len > original_desc_len:
                            maybe_edges[edge_key] = list(glean_edge_list)
                        # Otherwise keep original version
                    else:
                        # New edge from gleaning stage
                        maybe_edges[edge_key] = list(glean_edge_list)
                    await _cooperative_yield(i, every=8)

        # Batch update chunk's llm_cache_list with all collected cache keys
        if cache_keys_collector and text_chunks_storage:
            await update_chunk_cache_list(
                chunk_key,
                text_chunks_storage,
                cache_keys_collector,
                "entity_extraction",
            )

        processed_chunks += 1
        entities_count = len(maybe_nodes)
        relations_count = len(maybe_edges)
        log_message = (
            f"Chunk {processed_chunks} of {total_chunks} extracted "
            f"{entities_count} Ent + {relations_count} Rel {chunk_key}"
        )
        logger.info(log_message)
        stage_name = (
            "entity_extraction_fallback"
            if should_use_batch
            else "entity_extraction_realtime"
        )
        stage_label = (
            "Fallback realtime entity extraction"
            if should_use_batch
            else "Extracting entities"
        )
        await _update_entity_extraction_pipeline_progress(
            pipeline_status,
            pipeline_status_lock,
            total_chunks=total_chunks,
            processed_chunks=processed_chunks,
            stage=stage_name,
            stage_label=stage_label,
            latest_message=log_message,
            append_history=True,
        )
        await _update_doc_processing_progress(
            doc_status_storage,
            doc_id,
            stage=stage_name,
            stage_label=stage_label,
            processed=processed_chunks,
            total=total_chunks,
        )

        # Return the extracted nodes and edges for centralized processing
        return maybe_nodes, maybe_edges

    # Get max async tasks limit from global_config
    chunk_max_async = global_config.get("llm_model_max_async", 4)
    semaphore = asyncio.Semaphore(chunk_max_async)

    async def _process_with_semaphore(chunk):
        async with semaphore:
            # Check for cancellation before processing chunk
            if pipeline_status is not None and pipeline_status_lock is not None:
                async with pipeline_status_lock:
                    if pipeline_status.get("cancellation_requested", False):
                        raise PipelineCancelledException(
                            "User cancelled during chunk processing"
                        )

            try:
                result = await _process_single_content(chunk)
                # Yield once between chunk completions so API coroutines can resume
                # even when many chunk tasks are hitting cache and finishing quickly.
                await asyncio.sleep(0)
                return result
            except Exception as e:
                chunk_id = chunk[0]  # Extract chunk_id from chunk[0]
                prefixed_exception = create_prefixed_exception(e, chunk_id)
                raise prefixed_exception from e

    tasks = []
    for c in ordered_chunks:
        task = asyncio.create_task(_process_with_semaphore(c))
        tasks.append(task)

    # Wait for tasks to complete or for the first exception to occur
    # This allows us to cancel remaining tasks if any task fails
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    # Check if any task raised an exception and ensure all exceptions are retrieved
    first_exception = None
    chunk_results = []

    for task in done:
        try:
            exception = task.exception()
            if exception is not None:
                if first_exception is None:
                    first_exception = exception
            else:
                chunk_results.append(task.result())
        except Exception as e:
            if first_exception is None:
                first_exception = e

    # If any task failed, cancel all pending tasks and raise the first exception
    if first_exception is not None:
        # Cancel all pending tasks
        for pending_task in pending:
            pending_task.cancel()

        # Wait for cancellation to complete
        if pending:
            await asyncio.wait(pending)

        # Add progress prefix to the exception message
        progress_prefix = f"C[{processed_chunks + 1}/{total_chunks}]"

        # Re-raise the original exception with a prefix
        prefixed_exception = create_prefixed_exception(first_exception, progress_prefix)
        raise prefixed_exception from first_exception

    # If all tasks completed successfully, chunk_results already contains the results
    # Return the chunk_results for later processing in merge_nodes_and_edges
    return chunk_results


async def kg_query(
    query: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_db: BaseKVStorage,
    query_param: QueryParam,
    global_config: dict[str, str],
    hashing_kv: BaseKVStorage | None = None,
    system_prompt: str | None = None,
    chunks_vdb: BaseVectorStorage = None,
    images_vdb: BaseVectorStorage = None,
    image_metadata: BaseKVStorage = None,
) -> QueryResult | None:
    """
    Execute knowledge graph query and return unified QueryResult object.

    Args:
        query: Query string
        knowledge_graph_inst: Knowledge graph storage instance
        entities_vdb: Entity vector database
        relationships_vdb: Relationship vector database
        text_chunks_db: Text chunks storage
        query_param: Query parameters
        global_config: Global configuration
        hashing_kv: Cache storage
        system_prompt: System prompt
        chunks_vdb: Document chunks vector database
        images_vdb: Optional multimodal image-side vector database. When set
            (along with ``image_metadata``), KG query modes can append
            cross-modal image hits alongside their normal retrieval path.
        image_metadata: Optional KV store holding image sidecar records
            (annotation text, blob refs). Required alongside ``images_vdb``
            for cross-modal retrieval to work.

    Returns:
        QueryResult | None: Unified query result object containing:
            - content: Non-streaming response text content
            - response_iterator: Streaming response iterator
            - raw_data: Complete structured data (including references and metadata)
            - is_streaming: Whether this is a streaming result

        Based on different query_param settings, different fields will be populated:
        - only_need_context=True: content contains context string
        - only_need_prompt=True: content contains complete prompt
        - stream=True: response_iterator contains streaming response, raw_data contains complete data
        - default: content contains LLM response text, raw_data contains complete data

        Returns None when no relevant context could be constructed for the query.
    """
    if not query:
        return QueryResult(content=PROMPTS["fail_response"])

    if query_param.model_func:
        use_model_func = query_param.model_func
    elif global_config.get("query_llm_model_func") is not None:
        # Use the query-specific LLM for answer generation (e.g.
        # enable_thinking=true for higher quality answers). Keyword
        # extraction below still uses llm_model_func (fast, no thinking).
        use_model_func = global_config["query_llm_model_func"]
        use_model_func = partial(use_model_func, _priority=5)
    else:
        use_model_func = global_config["llm_model_func"]
        # Apply higher priority (5) to query relation LLM function
        use_model_func = partial(use_model_func, _priority=5)

    # Keyword extraction always uses llm_model_func (fast, structured
    # output — thinking mode doesn't help here and just adds latency).
    hl_keywords, ll_keywords = await get_keywords_from_query(
        query, query_param, global_config, hashing_kv
    )

    logger.debug(f"High-level keywords: {hl_keywords}")
    logger.debug(f"Low-level  keywords: {ll_keywords}")

    # Handle empty keywords
    if ll_keywords == [] and query_param.mode in ["local", "hybrid", "mix"]:
        logger.warning("low_level_keywords is empty")
    if hl_keywords == [] and query_param.mode in ["global", "hybrid", "mix"]:
        logger.warning("high_level_keywords is empty")
    if hl_keywords == [] and ll_keywords == []:
        if len(query) < 50:
            logger.warning(f"Forced low_level_keywords to origin query: {query}")
            ll_keywords = [query]
        else:
            return QueryResult(content=PROMPTS["fail_response"])

    ll_keywords_str = ", ".join(ll_keywords) if ll_keywords else ""
    hl_keywords_str = ", ".join(hl_keywords) if hl_keywords else ""

    # Build query context (unified interface)
    context_result = await _build_query_context(
        query,
        ll_keywords_str,
        hl_keywords_str,
        knowledge_graph_inst,
        entities_vdb,
        relationships_vdb,
        text_chunks_db,
        query_param,
        chunks_vdb,
        images_vdb=images_vdb,
        image_metadata=image_metadata,
    )

    if context_result is None:
        logger.info("[kg_query] No query context could be built; returning no-result.")
        return None

    # Return different content based on query parameters
    if query_param.only_need_context and not query_param.only_need_prompt:
        return QueryResult(
            content=context_result.context, raw_data=context_result.raw_data
        )

    user_prompt = f"\n\n{query_param.user_prompt}" if query_param.user_prompt else "n/a"
    response_type = (
        query_param.response_type
        if query_param.response_type
        else "Multiple Paragraphs"
    )

    # Build system prompt
    sys_prompt_temp = system_prompt if system_prompt else PROMPTS["rag_response"]
    sys_prompt = sys_prompt_temp.format(
        response_type=response_type,
        user_prompt=user_prompt,
        context_data=context_result.context,
    )

    user_query = query

    if query_param.only_need_prompt:
        prompt_content = "\n\n".join([sys_prompt, "---User Query---", user_query])
        return QueryResult(content=prompt_content, raw_data=context_result.raw_data)

    # Call LLM
    tokenizer: Tokenizer = global_config["tokenizer"]
    len_of_prompts = len(tokenizer.encode(query + sys_prompt))
    logger.debug(
        f"[kg_query] Sending to LLM: {len_of_prompts:,} tokens (Query: {len(tokenizer.encode(query))}, System: {len(tokenizer.encode(sys_prompt))})"
    )

    # Handle cache
    args_hash = compute_args_hash(
        query_param.mode,
        query,
        query_param.response_type,
        query_param.top_k,
        query_param.chunk_top_k,
        query_param.max_entity_tokens,
        query_param.max_relation_tokens,
        query_param.max_total_tokens,
        hl_keywords_str,
        ll_keywords_str,
        query_param.user_prompt or "",
        query_param.enable_rerank,
    )

    cached_result = await handle_cache(
        hashing_kv, args_hash, user_query, query_param.mode, cache_type="query"
    )

    if cached_result is not None:
        cached_response, _ = cached_result  # Extract content, ignore timestamp
        logger.info(
            " == LLM cache == Query cache hit, using cached response as query result"
        )
        response = cached_response
    else:
        response = await use_model_func(
            user_query,
            system_prompt=sys_prompt,
            history_messages=query_param.conversation_history,
            enable_cot=True,
            stream=query_param.stream,
        )

        if hashing_kv and hashing_kv.global_config.get("enable_llm_cache"):
            queryparam_dict = {
                "mode": query_param.mode,
                "response_type": query_param.response_type,
                "top_k": query_param.top_k,
                "chunk_top_k": query_param.chunk_top_k,
                "max_entity_tokens": query_param.max_entity_tokens,
                "max_relation_tokens": query_param.max_relation_tokens,
                "max_total_tokens": query_param.max_total_tokens,
                "hl_keywords": hl_keywords_str,
                "ll_keywords": ll_keywords_str,
                "user_prompt": query_param.user_prompt or "",
                "enable_rerank": query_param.enable_rerank,
            }
            await save_to_cache(
                hashing_kv,
                CacheData(
                    args_hash=args_hash,
                    content=response,
                    prompt=query,
                    mode=query_param.mode,
                    cache_type="query",
                    queryparam=queryparam_dict,
                ),
            )

    # Return unified result based on actual response type
    if isinstance(response, str):
        # Non-streaming response (string)
        if len(response) > len(sys_prompt):
            response = (
                response.replace(sys_prompt, "")
                .replace("user", "")
                .replace("model", "")
                .replace(query, "")
                .replace("<system>", "")
                .replace("</system>", "")
                .strip()
            )

        return QueryResult(content=response, raw_data=context_result.raw_data)
    else:
        # Streaming response (AsyncIterator)
        return QueryResult(
            response_iterator=response,
            raw_data=context_result.raw_data,
            is_streaming=True,
        )


async def get_keywords_from_query(
    query: str,
    query_param: QueryParam,
    global_config: dict[str, str],
    hashing_kv: BaseKVStorage | None = None,
) -> tuple[list[str], list[str]]:
    """
    Retrieves high-level and low-level keywords for RAG operations.

    This function checks if keywords are already provided in query parameters,
    and if not, extracts them from the query text using LLM.

    Args:
        query: The user's query text
        query_param: Query parameters that may contain pre-defined keywords
        global_config: Global configuration dictionary
        hashing_kv: Optional key-value storage for caching results

    Returns:
        A tuple containing (high_level_keywords, low_level_keywords)
    """
    # Check if pre-defined keywords are already provided
    if query_param.hl_keywords or query_param.ll_keywords:
        return query_param.hl_keywords, query_param.ll_keywords

    # extract_keywords_only threads param.conversation_history into the
    # extraction prompt so anaphoric follow-ups resolve to real keywords.
    hl_keywords, ll_keywords = await extract_keywords_only(
        query, query_param, global_config, hashing_kv
    )
    return hl_keywords, ll_keywords


def _render_history_for_keyword_extraction(param: QueryParam) -> str:
    """Render recent conversation turns for the keyword-extraction prompt.

    Returns "" when there is no history (single-turn queries are
    unaffected — same prompt, same cache key as before). Otherwise returns
    a compact ``role: content`` transcript of the most recent turns, capped
    by ``param.history_turns`` (a "turn" = one user+assistant pair, so the
    cap is ``2 * history_turns`` messages). This is used ONLY to help the
    LLM resolve pronouns/ellipsis in the current query; the prompt
    instructs it not to mine the history for stale keywords.
    """
    conversation_history = getattr(param, "conversation_history", None) or []
    if not conversation_history:
        return ""

    turns = conversation_history
    history_turns = getattr(param, "history_turns", None)
    if isinstance(history_turns, int) and history_turns > 0:
        turns = conversation_history[-(history_turns * 2):]

    rendered: list[str] = []
    for msg in turns:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "") or "user").strip()
        content = str(msg.get("content", "") or "").strip()
        if content:
            rendered.append(f"{role}: {content}")

    if not rendered:
        return ""
    return "\n".join(rendered) + "\n"


async def extract_keywords_only(
    text: str,
    param: QueryParam,
    global_config: dict[str, str],
    hashing_kv: BaseKVStorage | None = None,
) -> tuple[list[str], list[str]]:
    """
    Extract high-level and low-level keywords from the given 'text' using the LLM.
    This method does NOT build the final RAG context or provide a final answer.
    It ONLY extracts keywords (hl_keywords, ll_keywords).

    When ``param.conversation_history`` is present, recent turns are rendered
    into the prompt so anaphoric follow-ups resolve to real keywords — this
    is what makes multi-turn ("连续对话") retrieval work.
    """

    # 1. Build the examples
    examples = "\n".join(PROMPTS["keywords_extraction_examples"])

    language = global_config["addon_params"].get("language", DEFAULT_SUMMARY_LANGUAGE)

    # 1b. Render recent conversation history so anaphoric follow-ups
    # ("他呢?", "再详细点") resolve to concrete keywords instead of
    # extracting nothing. Previously conversation_history reached only the
    # final answer LLM, never retrieval — so multi-turn follow-ups
    # retrieved no context and could not be answered ("连续对话回答不了").
    history_context = _render_history_for_keyword_extraction(param)

    # 2. Handle cache if needed - add cache type for keywords.
    # history_context is part of the key: the same follow-up text under a
    # different conversation must not collide on a stale keyword cache
    # entry (e.g. "他呢?" means different things in different threads).
    args_hash = compute_args_hash(
        param.mode,
        text,
        language,
        history_context,
    )
    cached_result = await handle_cache(
        hashing_kv, args_hash, text, param.mode, cache_type="keywords"
    )
    if cached_result is not None:
        cached_response, _ = cached_result  # Extract content, ignore timestamp
        try:
            keywords_data = json_repair.loads(cached_response)
            return keywords_data.get("high_level_keywords", []), keywords_data.get(
                "low_level_keywords", []
            )
        except (json.JSONDecodeError, KeyError):
            logger.warning(
                "Invalid cache format for keywords, proceeding with extraction"
            )

    # 3. Build the keyword-extraction prompt
    kw_prompt = PROMPTS["keywords_extraction"].format(
        query=text,
        examples=examples,
        language=language,
        history_context=history_context,
    )

    tokenizer: Tokenizer = global_config["tokenizer"]
    len_of_prompts = len(tokenizer.encode(kw_prompt))
    logger.debug(
        f"[extract_keywords] Sending to LLM: {len_of_prompts:,} tokens (Prompt: {len_of_prompts})"
    )

    # 4. Call the LLM for keyword extraction
    if param.model_func:
        use_model_func = param.model_func
    else:
        use_model_func = global_config["llm_model_func"]
        # Apply higher priority (5) to query relation LLM function
        use_model_func = partial(use_model_func, _priority=5)

    result = await use_model_func(kw_prompt, keyword_extraction=True)

    # 5. Parse out JSON from the LLM response
    result = remove_think_tags(result)
    try:
        keywords_data = json_repair.loads(result)
        if not keywords_data:
            logger.error("No JSON-like structure found in the LLM respond.")
            return [], []
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing error: {e}")
        logger.error(f"LLM respond: {result}")
        return [], []

    hl_keywords = keywords_data.get("high_level_keywords", [])
    ll_keywords = keywords_data.get("low_level_keywords", [])

    # 6. Cache only the processed keywords with cache type
    if hl_keywords or ll_keywords:
        cache_data = {
            "high_level_keywords": hl_keywords,
            "low_level_keywords": ll_keywords,
        }
        if hashing_kv.global_config.get("enable_llm_cache"):
            # Save to cache with query parameters
            queryparam_dict = {
                "mode": param.mode,
                "response_type": param.response_type,
                "top_k": param.top_k,
                "chunk_top_k": param.chunk_top_k,
                "max_entity_tokens": param.max_entity_tokens,
                "max_relation_tokens": param.max_relation_tokens,
                "max_total_tokens": param.max_total_tokens,
                "user_prompt": param.user_prompt or "",
                "enable_rerank": param.enable_rerank,
            }
            await save_to_cache(
                hashing_kv,
                CacheData(
                    args_hash=args_hash,
                    content=json.dumps(cache_data),
                    prompt=text,
                    mode=param.mode,
                    cache_type="keywords",
                    queryparam=queryparam_dict,
                ),
            )

    return hl_keywords, ll_keywords


async def _get_image_context(
    query: str,
    images_vdb: BaseVectorStorage,
    image_metadata: BaseKVStorage,
    query_param: QueryParam,
) -> list[dict]:
    """Retrieve multimodal image hits for a text query.

    Runs the query against ``images_vdb``. Because
    :class:`~lightrag.utils.MultimodalEmbeddingFunc`'s default ``__call__``
    routes to ``text_encode``, the query text is encoded into the SAME
    semantic space as the indexed *image* vectors — this is what enables
    cross-modal retrieval ("find images that match this description") at
    zero cost to the vector store internals.

    Each hit is resolved against the ``image_metadata`` KV store so the
    caller sees the full annotation text (captured during ``ainsert_image``)
    as the chunk content, not just the short caption stored in the VDB
    meta_fields.

    Returns a list of chunk-shaped dicts compatible with the downstream
    ``_merge_all_chunks`` / ``_build_context_str`` pipeline:

        {
            "content":       annotation_text,
            "file_path":     <source pdf / image path>,
            "chunk_id":      "img-<hash>",   # distinct from text chunks' "chunk-<hash>"
            "source_type":   "image_vector",
            "image_blob_id": "img-<hash>",
            "blob_ref":      <absolute path or URL>,
            "distance":      <cosine similarity>,
            "created_at":    <epoch>,
        }

    Args:
        query: Text query string.
        images_vdb: Vector store holding image-side embeddings.
        image_metadata: KV store holding image sidecar records.
        query_param: Query parameters (``chunk_top_k`` / ``top_k`` used).

    Returns:
        List of chunk-shaped dicts. Empty list when there are no hits or
        when either dependency is missing. Never raises — errors are logged
        and swallowed so a broken multimodal side never takes down the
        text-side retrieval path.
    """
    if images_vdb is None or image_metadata is None:
        return []
    try:
        def _build_image_locator(meta: dict[str, Any]) -> str:
            parts: list[str] = []
            page = meta.get("source_page")
            printed_page = meta.get("source_printed_page")
            page_label = str(meta.get("source_page_label") or "").strip()
            if page is not None:
                try:
                    parts.append(f"PDF物理第 {int(page)} 页")
                except (TypeError, ValueError):
                    parts.append(f"PDF物理第 {page} 页")
            printed_page_text = ""
            if printed_page is not None:
                try:
                    printed_page_text = str(int(printed_page))
                except (TypeError, ValueError):
                    printed_page_text = str(printed_page)
                if printed_page_text and printed_page_text != str(page):
                    parts.append(f"文档页码 {printed_page_text}")
            if page_label and page_label not in {str(page), printed_page_text}:
                parts.append(f"页标 {page_label}")
            bbox = meta.get("source_bbox")
            if isinstance(bbox, dict):
                x0 = bbox.get("x0", bbox.get("l"))
                y0 = bbox.get("y0", bbox.get("t"))
                width = bbox.get("width")
                height = bbox.get("height")
                if width is None:
                    x1 = bbox.get("x1", bbox.get("r"))
                    try:
                        if x0 is not None and x1 is not None:
                            width = float(x1) - float(x0)
                    except (TypeError, ValueError):
                        width = None
                if height is None:
                    y1 = bbox.get("y1", bbox.get("b"))
                    try:
                        if y0 is not None and y1 is not None:
                            height = float(y1) - float(y0)
                    except (TypeError, ValueError):
                        height = None
                locator_parts = []
                try:
                    if x0 is not None:
                        locator_parts.append(f"x={float(x0):.1f}")
                    if y0 is not None:
                        locator_parts.append(f"y={float(y0):.1f}")
                    if width is not None:
                        locator_parts.append(f"w={float(width):.1f}")
                    if height is not None:
                        locator_parts.append(f"h={float(height):.1f}")
                except (TypeError, ValueError):
                    locator_parts = []
                if locator_parts:
                    parts.append("原图位置: " + ", ".join(locator_parts))
            return " · ".join(parts)

        def _build_image_query_text(
            meta: dict[str, Any], fallback_annotation: str
        ) -> str:
            content = (fallback_annotation or "").strip()
            locator = _build_image_locator(meta)
            extra = meta.get("extra") if isinstance(meta.get("extra"), dict) else {}
            context_text = (
                meta.get("context_text")
                or extra.get("context_text")
                or extra.get("page_text_excerpt")
                or ""
            )
            context_text = str(context_text).strip()

            if locator and "【位置】" not in content:
                content = (
                    f"{content}\n【位置】 {locator}".strip() if content else f"【位置】 {locator}"
                )
            if context_text and context_text not in content:
                content = (
                    f"{content}\n【图像邻近上下文】\n{context_text}".strip()
                    if content
                    else f"【图像邻近上下文】\n{context_text}"
                )
            extraction_mode = str(
                meta.get("extraction_mode") or extra.get("extraction_mode") or ""
            ).strip()
            if extraction_mode and "【提取方式】" not in content:
                content = (
                    f"{content}\n【提取方式】 {extraction_mode}".strip()
                    if content
                    else f"【提取方式】 {extraction_mode}"
                )
            merged_modes_raw = meta.get("merged_extraction_modes") or extra.get(
                "merged_extraction_modes"
            )
            if isinstance(merged_modes_raw, list):
                merged_modes = [
                    str(mode).strip() for mode in merged_modes_raw if str(mode).strip()
                ]
            else:
                merged_modes = []
            if merged_modes and "【合并提取来源】" not in content:
                merged_modes_text = "、".join(merged_modes)
                content = (
                    f"{content}\n【合并提取来源】 {merged_modes_text}".strip()
                    if content
                    else f"【合并提取来源】 {merged_modes_text}"
                )
            native_xref = meta.get("native_xref") or extra.get("native_xref")
            if native_xref is not None and "【PDF原生图像】" not in content:
                content = (
                    f"{content}\n【PDF原生图像】 xref={native_xref}".strip()
                    if content
                    else f"【PDF原生图像】 xref={native_xref}"
                )
            context_chunks = meta.get("context_chunks") or extra.get("context_chunks") or []
            if isinstance(context_chunks, list) and context_chunks:
                chunk_lines: list[str] = []
                for chunk in context_chunks[:3]:
                    if not isinstance(chunk, dict):
                        continue
                    chunk_content = str(chunk.get("content") or "").strip()
                    if not chunk_content:
                        continue
                    chunk_order_index = chunk.get("chunk_order_index")
                    try:
                        chunk_label = f"Chunk #{int(chunk_order_index) + 1}"
                    except (TypeError, ValueError):
                        chunk_label = "Chunk"
                    chunk_lines.append(f"- {chunk_label}: {chunk_content[:500]}")
                if chunk_lines and "【图像邻近上下文块】" not in content:
                    chunk_block = "\n".join(chunk_lines)
                    content = (
                        f"{content}\n【图像邻近上下文块】\n{chunk_block}".strip()
                        if content
                        else f"【图像邻近上下文块】\n{chunk_block}"
                    )
            return content

        search_top_k = query_param.chunk_top_k or query_param.top_k
        cosine_threshold = images_vdb.cosine_better_than_threshold

        results = await images_vdb.query(query, top_k=search_top_k)
        if not results:
            logger.info(
                f"Image query: 0 hits "
                f"(top_k:{search_top_k} cosine:{cosine_threshold})"
            )
            return []

        # Batch-fetch annotation sidecars. Any missing entries are skipped
        # silently — a stale VDB record without its KV counterpart is a
        # corruption edge case, not a query-time failure.
        blob_ids = [r.get("blob_id") for r in results if r.get("blob_id")]
        metadata_records = await image_metadata.get_by_ids(blob_ids) if blob_ids else []
        metadata_by_id: dict[str, dict[str, Any]] = {}
        for bid, rec in zip(blob_ids, metadata_records):
            if rec is not None:
                metadata_by_id[bid] = rec

        image_chunks: list[dict] = []
        for r in results:
            blob_id = r.get("blob_id")
            if not blob_id:
                continue
            meta = metadata_by_id.get(blob_id) or {}
            extra = meta.get("extra") if isinstance(meta.get("extra"), dict) else {}
            # Prefer the full annotation text; fall back to caption if the
            # KV record is missing or has no annotation (e.g. cases where
            # vision captioning failed but embedding still ran).
            annotation_text = meta.get("annotation_text")
            if not annotation_text:
                annotation_text = r.get("caption") or ""
            annotation_text = _build_image_query_text(meta, annotation_text)
            if not annotation_text:
                # Nothing to feed the LLM — skip.
                continue
            image_chunks.append(
                {
                    "content": annotation_text,
                    "file_path": meta.get("source_file_path")
                    or r.get("file_path", "unknown_source"),
                    # Use blob_id as chunk_id. The "img-" prefix makes it
                    # naturally distinct from text chunks' "chunk-" prefix,
                    # so the seen-chunks dedup set in _merge_all_chunks
                    # can tell them apart and keep both when the same
                    # image also surfaces via the text-side path.
                    "chunk_id": blob_id,
                    "source_type": "image_vector",
                    "image_blob_id": blob_id,
                    "blob_ref": r.get("blob_ref"),
                    "distance": r.get("distance"),
                    "created_at": r.get("created_at"),
                    "source_doc_id": meta.get("source_doc_id"),
                    "source_page": meta.get("source_page", extra.get("source_page")),
                    "source_printed_page": meta.get(
                        "source_printed_page", extra.get("source_printed_page")
                    ),
                    "source_page_label": meta.get(
                        "source_page_label", extra.get("source_page_label")
                    ),
                    "source_bbox": meta.get("source_bbox", extra.get("source_bbox")),
                    "picture_index": meta.get(
                        "picture_index", extra.get("picture_index")
                    ),
                    "page_picture_index": meta.get(
                        "page_picture_index", extra.get("page_picture_index")
                    ),
                    "context_text": meta.get("context_text")
                    or extra.get("context_text")
                    or extra.get("page_text_excerpt"),
                    "context_chunk_ids": meta.get("context_chunk_ids")
                    or extra.get("context_chunk_ids"),
                    "context_chunks": meta.get("context_chunks")
                    or extra.get("context_chunks"),
                    "extraction_mode": meta.get("extraction_mode")
                    or extra.get("extraction_mode"),
                    "native_xref": meta.get("native_xref")
                    or extra.get("native_xref"),
                    "merged_extraction_modes": meta.get(
                        "merged_extraction_modes"
                    )
                    or extra.get("merged_extraction_modes"),
                    "extra": extra,
                }
            )

        logger.info(
            f"Image query: {len(image_chunks)} hits "
            f"(top_k:{search_top_k} cosine:{cosine_threshold})"
        )
        return image_chunks
    except Exception as e:
        logger.error(f"Error in _get_image_context: {e}")
        return []


async def _get_vector_context(
    query: str,
    chunks_vdb: BaseVectorStorage,
    query_param: QueryParam,
    query_embedding: list[float] = None,
) -> list[dict]:
    """
    Retrieve text chunks from the vector database without reranking or truncation.

    This function performs vector search to find relevant text chunks for a query.
    Reranking and truncation will be handled later in the unified processing.

    Args:
        query: The query string to search for
        chunks_vdb: Vector database containing document chunks
        query_param: Query parameters including chunk_top_k and ids
        query_embedding: Optional pre-computed query embedding to avoid redundant embedding calls

    Returns:
        List of text chunks with metadata
    """
    try:
        # Use chunk_top_k if specified, otherwise fall back to top_k
        search_top_k = query_param.chunk_top_k or query_param.top_k
        cosine_threshold = chunks_vdb.cosine_better_than_threshold

        results = await chunks_vdb.query(
            query, top_k=search_top_k, query_embedding=query_embedding
        )
        if not results:
            logger.info(
                f"Naive query: 0 chunks (chunk_top_k:{search_top_k} cosine:{cosine_threshold})"
            )
            return []

        valid_chunks = []
        for result in results:
            if "content" in result:
                chunk_with_metadata = {
                    "content": result["content"],
                    "created_at": result.get("created_at", None),
                    "file_path": result.get("file_path", "unknown_source"),
                    "source_type": "vector",  # Mark the source type
                    "chunk_id": result.get("id"),  # Add chunk_id for deduplication
                }
                valid_chunks.append(chunk_with_metadata)

        logger.info(
            f"Naive query: {len(valid_chunks)} chunks (chunk_top_k:{search_top_k} cosine:{cosine_threshold})"
        )
        return valid_chunks

    except Exception as e:
        logger.error(f"Error in _get_vector_context: {e}")
        return []


async def _perform_kg_search(
    query: str,
    ll_keywords: str,
    hl_keywords: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_db: BaseKVStorage,
    query_param: QueryParam,
    chunks_vdb: BaseVectorStorage = None,
    images_vdb: BaseVectorStorage = None,
    image_metadata: BaseKVStorage = None,
) -> dict[str, Any]:
    """
    Pure search logic that retrieves raw entities, relations, and vector chunks.
    No token truncation or formatting - just raw search results.

    When ``images_vdb`` and ``image_metadata`` are both provided, KG query
    modes can run an additional cross-modal retrieval pass against the
    image-side vector store. Image hits are resolved to their full annotation
    text (via the KV store) and merged into ``vector_chunks`` so they flow
    through the same truncation / merging / context-building pipeline as text
    chunks. Image chunks are distinguished by a ``chunk_id`` starting with
    ``img-`` (vs. ``chunk-`` for text) and carry
    ``source_type="image_vector"``.
    """

    # Initialize result containers
    local_entities = []
    local_relations = []
    global_entities = []
    global_relations = []
    vector_chunks = []
    chunk_tracking = {}

    # Handle different query modes

    # Track chunk sources and metadata for final logging
    chunk_tracking = {}  # chunk_id -> {source, frequency, order}

    # Pre-compute embeddings needed by the selected mode in a single batch call.
    # Only embed texts that the active retrieval branches will actually use:
    #   - query        → used by _get_vector_context (chunks VDB)
    #   - ll_keywords  → used by _get_node_data (entities VDB) in local/hybrid/mix
    #   - hl_keywords  → used by _get_edge_data (relationships VDB) in global/hybrid/mix
    # Batching avoids 2-3 sequential API round-trips.
    kg_chunk_pick_method = text_chunks_db.global_config.get(
        "kg_chunk_pick_method", DEFAULT_KG_CHUNK_PICK_METHOD
    )

    actual_embedding_func = text_chunks_db.embedding_func
    query_embedding = None
    ll_embedding = None
    hl_embedding = None

    mode = query_param.mode
    need_ll = mode in ("local", "hybrid", "mix") and bool(ll_keywords)
    need_hl = mode in ("global", "hybrid", "mix") and bool(hl_keywords)

    if actual_embedding_func:
        texts_to_embed: list[str] = []
        text_purposes: list[str] = []

        if query and (kg_chunk_pick_method == "VECTOR" or chunks_vdb or images_vdb):
            texts_to_embed.append(query)
            text_purposes.append("query")

        if need_ll:
            texts_to_embed.append(ll_keywords)
            text_purposes.append("ll")

        if need_hl:
            texts_to_embed.append(hl_keywords)
            text_purposes.append("hl")

        if texts_to_embed:
            try:
                all_embeddings = await actual_embedding_func(
                    texts_to_embed, _priority=5
                )
                for i, purpose in enumerate(text_purposes):
                    if purpose == "query":
                        query_embedding = all_embeddings[i]
                    elif purpose == "ll":
                        ll_embedding = all_embeddings[i]
                    elif purpose == "hl":
                        hl_embedding = all_embeddings[i]
                logger.debug(
                    "Pre-computed %d embeddings in single batch (purposes: %s)",
                    len(texts_to_embed),
                    ", ".join(text_purposes),
                )
            except Exception as e:
                logger.warning(f"Failed to batch pre-compute embeddings: {e}")

    # Handle local and global modes
    if query_param.mode == "local" and len(ll_keywords) > 0:
        local_entities, local_relations = await _get_node_data(
            ll_keywords,
            knowledge_graph_inst,
            entities_vdb,
            query_param,
            query_embedding=ll_embedding,
        )

    elif query_param.mode == "global" and len(hl_keywords) > 0:
        global_relations, global_entities = await _get_edge_data(
            hl_keywords,
            knowledge_graph_inst,
            relationships_vdb,
            query_param,
            query_embedding=hl_embedding,
        )

    else:  # hybrid or mix mode
        if len(ll_keywords) > 0:
            local_entities, local_relations = await _get_node_data(
                ll_keywords,
                knowledge_graph_inst,
                entities_vdb,
                query_param,
                query_embedding=ll_embedding,
            )
        if len(hl_keywords) > 0:
            global_relations, global_entities = await _get_edge_data(
                hl_keywords,
                knowledge_graph_inst,
                relationships_vdb,
                query_param,
                query_embedding=hl_embedding,
            )

        # Get vector chunks for mix mode
        if query_param.mode == "mix" and chunks_vdb:
            vector_chunks = await _get_vector_context(
                query,
                chunks_vdb,
                query_param,
                query_embedding,
            )
            # Track vector chunks with source metadata
            for i, chunk in enumerate(vector_chunks):
                chunk_id = chunk.get("chunk_id") or chunk.get("id")
                if chunk_id:
                    chunk_tracking[chunk_id] = {
                        "source": "C",
                        "frequency": 1,  # Vector chunks always have frequency 1
                        "order": i + 1,  # 1-based order in vector search results
                    }
                else:
                    logger.warning(f"Vector chunk missing chunk_id: {chunk}")

    # Multimodal: parallel cross-modal retrieval against images_vdb.
    # Runs in all KG modes with the multimodal pipeline fully wired so the
    # WebUI can surface retrieved PDF images regardless of the chosen KG mode.
    # Falls through silently otherwise — text-only workloads are unaffected.
    # Image hits are appended to vector_chunks so they flow through the
    # same merge / truncate / context-build path.
    if (
        query_param.mode in ("mix", "hybrid", "local", "global")
        and images_vdb is not None
        and image_metadata is not None
    ):
        image_chunks = await _get_image_context(
            query,
            images_vdb,
            image_metadata,
            query_param,
        )
        # Track with source "I" (image) so the chunk-source audit log
        # distinguishes them from text vector hits (source "C").
        for i, chunk in enumerate(image_chunks):
            chunk_id = chunk.get("chunk_id")
            if chunk_id:
                chunk_tracking[chunk_id] = {
                    "source": "I",
                    "frequency": 1,
                    "order": i + 1,
                }
        vector_chunks.extend(image_chunks)

    # Round-robin merge entities
    final_entities = []
    seen_entities = set()
    max_len = max(len(local_entities), len(global_entities))
    for i in range(max_len):
        # First from local
        if i < len(local_entities):
            entity = local_entities[i]
            entity_name = entity.get("entity_name")
            if entity_name and entity_name not in seen_entities:
                final_entities.append(entity)
                seen_entities.add(entity_name)

        # Then from global
        if i < len(global_entities):
            entity = global_entities[i]
            entity_name = entity.get("entity_name")
            if entity_name and entity_name not in seen_entities:
                final_entities.append(entity)
                seen_entities.add(entity_name)

    # Round-robin merge relations
    final_relations = []
    seen_relations = set()
    max_len = max(len(local_relations), len(global_relations))
    for i in range(max_len):
        # First from local
        if i < len(local_relations):
            relation = local_relations[i]
            # Build relation unique identifier
            if "src_tgt" in relation:
                rel_key = tuple(sorted(relation["src_tgt"]))
            else:
                rel_key = tuple(
                    sorted([relation.get("src_id"), relation.get("tgt_id")])
                )

            if rel_key not in seen_relations:
                final_relations.append(relation)
                seen_relations.add(rel_key)

        # Then from global
        if i < len(global_relations):
            relation = global_relations[i]
            # Build relation unique identifier
            if "src_tgt" in relation:
                rel_key = tuple(sorted(relation["src_tgt"]))
            else:
                rel_key = tuple(
                    sorted([relation.get("src_id"), relation.get("tgt_id")])
                )

            if rel_key not in seen_relations:
                final_relations.append(relation)
                seen_relations.add(rel_key)

    num_image_chunks = sum(
        1 for c in vector_chunks if c.get("source_type") == "image_vector"
    )
    num_text_vector_chunks = len(vector_chunks) - num_image_chunks
    logger.info(
        f"Raw search results: {len(final_entities)} entities, "
        f"{len(final_relations)} relations, "
        f"{num_text_vector_chunks} text vector chunks, "
        f"{num_image_chunks} image vector chunks"
    )

    return {
        "final_entities": final_entities,
        "final_relations": final_relations,
        "vector_chunks": vector_chunks,
        "chunk_tracking": chunk_tracking,
        "query_embedding": query_embedding,
    }


async def _apply_token_truncation(
    search_result: dict[str, Any],
    query_param: QueryParam,
    global_config: dict[str, str],
) -> dict[str, Any]:
    """
    Apply token-based truncation to entities and relations for LLM efficiency.
    """
    tokenizer = global_config.get("tokenizer")
    if not tokenizer:
        logger.warning("No tokenizer found, skipping truncation")
        return {
            "entities_context": [],
            "relations_context": [],
            "filtered_entities": search_result["final_entities"],
            "filtered_relations": search_result["final_relations"],
            "entity_id_to_original": {},
            "relation_id_to_original": {},
        }

    # Get token limits from query_param with fallbacks
    max_entity_tokens = getattr(
        query_param,
        "max_entity_tokens",
        global_config.get("max_entity_tokens", DEFAULT_MAX_ENTITY_TOKENS),
    )
    max_relation_tokens = getattr(
        query_param,
        "max_relation_tokens",
        global_config.get("max_relation_tokens", DEFAULT_MAX_RELATION_TOKENS),
    )

    final_entities = search_result["final_entities"]
    final_relations = search_result["final_relations"]

    # Create mappings from entity/relation identifiers to original data
    entity_id_to_original = {}
    relation_id_to_original = {}

    # Generate entities context for truncation
    entities_context = []
    for i, entity in enumerate(final_entities):
        entity_name = entity["entity_name"]
        created_at = entity.get("created_at", "UNKNOWN")
        if isinstance(created_at, (int, float)):
            created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))

        # Store mapping from entity name to original data
        entity_id_to_original[entity_name] = entity

        entities_context.append(
            {
                "entity": entity_name,
                "type": entity.get("entity_type", "UNKNOWN"),
                "description": entity.get("description", "UNKNOWN"),
                "created_at": created_at,
                "file_path": entity.get("file_path", "unknown_source"),
            }
        )

    # Generate relations context for truncation
    relations_context = []
    for i, relation in enumerate(final_relations):
        created_at = relation.get("created_at", "UNKNOWN")
        if isinstance(created_at, (int, float)):
            created_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))

        # Handle different relation data formats
        if "src_tgt" in relation:
            entity1, entity2 = relation["src_tgt"]
        else:
            entity1, entity2 = relation.get("src_id"), relation.get("tgt_id")

        # Store mapping from relation pair to original data
        relation_key = (entity1, entity2)
        relation_id_to_original[relation_key] = relation

        relations_context.append(
            {
                "entity1": entity1,
                "entity2": entity2,
                "description": relation.get("description", "UNKNOWN"),
                "created_at": created_at,
                "file_path": relation.get("file_path", "unknown_source"),
            }
        )

    logger.debug(
        f"Before truncation: {len(entities_context)} entities, {len(relations_context)} relations"
    )

    # Apply token-based truncation
    if entities_context:
        # Remove file_path and created_at for token calculation
        entities_context_for_truncation = []
        for entity in entities_context:
            entity_copy = entity.copy()
            entity_copy.pop("file_path", None)
            entity_copy.pop("created_at", None)
            entities_context_for_truncation.append(entity_copy)

        entities_context = truncate_list_by_token_size(
            entities_context_for_truncation,
            key=lambda x: "\n".join(
                json.dumps(item, ensure_ascii=False) for item in [x]
            ),
            max_token_size=max_entity_tokens,
            tokenizer=tokenizer,
        )

    if relations_context:
        # Remove file_path and created_at for token calculation
        relations_context_for_truncation = []
        for relation in relations_context:
            relation_copy = relation.copy()
            relation_copy.pop("file_path", None)
            relation_copy.pop("created_at", None)
            relations_context_for_truncation.append(relation_copy)

        relations_context = truncate_list_by_token_size(
            relations_context_for_truncation,
            key=lambda x: "\n".join(
                json.dumps(item, ensure_ascii=False) for item in [x]
            ),
            max_token_size=max_relation_tokens,
            tokenizer=tokenizer,
        )

    logger.info(
        f"After truncation: {len(entities_context)} entities, {len(relations_context)} relations"
    )

    # Create filtered original data based on truncated context
    filtered_entities = []
    filtered_entity_id_to_original = {}
    if entities_context:
        final_entity_names = {e["entity"] for e in entities_context}
        seen_nodes = set()
        for entity in final_entities:
            name = entity.get("entity_name")
            if name in final_entity_names and name not in seen_nodes:
                filtered_entities.append(entity)
                filtered_entity_id_to_original[name] = entity
                seen_nodes.add(name)

    filtered_relations = []
    filtered_relation_id_to_original = {}
    if relations_context:
        final_relation_pairs = {(r["entity1"], r["entity2"]) for r in relations_context}
        seen_edges = set()
        for relation in final_relations:
            src, tgt = relation.get("src_id"), relation.get("tgt_id")
            if src is None or tgt is None:
                src, tgt = relation.get("src_tgt", (None, None))

            pair = (src, tgt)
            if pair in final_relation_pairs and pair not in seen_edges:
                filtered_relations.append(relation)
                filtered_relation_id_to_original[pair] = relation
                seen_edges.add(pair)

    return {
        "entities_context": entities_context,
        "relations_context": relations_context,
        "filtered_entities": filtered_entities,
        "filtered_relations": filtered_relations,
        "entity_id_to_original": filtered_entity_id_to_original,
        "relation_id_to_original": filtered_relation_id_to_original,
    }


async def _merge_all_chunks(
    filtered_entities: list[dict],
    filtered_relations: list[dict],
    vector_chunks: list[dict],
    query: str = "",
    knowledge_graph_inst: BaseGraphStorage = None,
    text_chunks_db: BaseKVStorage = None,
    query_param: QueryParam = None,
    chunks_vdb: BaseVectorStorage = None,
    chunk_tracking: dict = None,
    query_embedding: list[float] = None,
) -> list[dict]:
    """
    Merge chunks from different sources: vector_chunks + entity_chunks + relation_chunks.
    """
    if chunk_tracking is None:
        chunk_tracking = {}

    # Get chunks from entities
    entity_chunks = []
    if filtered_entities and text_chunks_db:
        entity_chunks = await _find_related_text_unit_from_entities(
            filtered_entities,
            query_param,
            text_chunks_db,
            knowledge_graph_inst,
            query,
            chunks_vdb,
            chunk_tracking=chunk_tracking,
            query_embedding=query_embedding,
        )

    # Get chunks from relations
    relation_chunks = []
    if filtered_relations and text_chunks_db:
        relation_chunks = await _find_related_text_unit_from_relations(
            filtered_relations,
            query_param,
            text_chunks_db,
            entity_chunks,  # For deduplication
            query,
            chunks_vdb,
            chunk_tracking=chunk_tracking,
            query_embedding=query_embedding,
        )

    # Helper for creating a merged chunk entry with preserved multimodal markers
    def _create_merged_entry(chunk, chunk_id):
        entry = {
            "content": chunk["content"],
            "file_path": chunk.get("file_path", "unknown_source"),
            "chunk_id": chunk_id,
        }
        # Preserve multimodal markers from ANY source (vector, entity, relation).
        # While currently mainly image-vector hits carry these, preserving
        # them globally ensures future extensions (e.g. caption chunks
        # carrying their original blob IDs) work out of the box.
        if chunk.get("source_type") == "image_vector":
            entry["source_type"] = "image_vector"
            entry["image_blob_id"] = chunk.get("image_blob_id")
            if chunk.get("blob_ref"):
                entry["blob_ref"] = chunk.get("blob_ref")
            for field_name in (
                "source_doc_id",
                "source_page",
                "source_bbox",
                "picture_index",
                "context_text",
                "context_chunk_ids",
                "context_chunks",
                "extra",
            ):
                if chunk.get(field_name) is not None:
                    entry[field_name] = chunk.get(field_name)
        return entry

    # Round-robin merge chunks from different sources with deduplication
    merged_chunks = []
    seen_chunk_ids = set()
    max_len = max(len(vector_chunks), len(entity_chunks), len(relation_chunks))
    origin_len = len(vector_chunks) + len(entity_chunks) + len(relation_chunks)

    for i in range(max_len):
        # Add from vector chunks first (Naive mode / Multimodal hits)
        if i < len(vector_chunks):
            chunk = vector_chunks[i]
            chunk_id = chunk.get("chunk_id") or chunk.get("id")
            if chunk_id and chunk_id not in seen_chunk_ids:
                seen_chunk_ids.add(chunk_id)
                merged_chunks.append(_create_merged_entry(chunk, chunk_id))

        # Add from entity chunks (Local mode)
        if i < len(entity_chunks):
            chunk = entity_chunks[i]
            chunk_id = chunk.get("chunk_id") or chunk.get("id")
            if chunk_id and chunk_id not in seen_chunk_ids:
                seen_chunk_ids.add(chunk_id)
                merged_chunks.append(_create_merged_entry(chunk, chunk_id))

        # Add from relation chunks (Global mode)
        if i < len(relation_chunks):
            chunk = relation_chunks[i]
            chunk_id = chunk.get("chunk_id") or chunk.get("id")
            if chunk_id and chunk_id not in seen_chunk_ids:
                seen_chunk_ids.add(chunk_id)
                merged_chunks.append(_create_merged_entry(chunk, chunk_id))

    logger.info(
        f"Round-robin merged chunks: {origin_len} -> {len(merged_chunks)} (deduplicated {origin_len - len(merged_chunks)})"
    )

    return merged_chunks


async def _build_context_str(
    entities_context: list[dict],
    relations_context: list[dict],
    merged_chunks: list[dict],
    query: str,
    query_param: QueryParam,
    global_config: dict[str, str],
    chunk_tracking: dict = None,
    entity_id_to_original: dict = None,
    relation_id_to_original: dict = None,
) -> tuple[str, dict[str, Any]]:
    """
    Build the final LLM context string with token processing.
    This includes dynamic token calculation and final chunk truncation.
    """
    tokenizer = global_config.get("tokenizer")
    if not tokenizer:
        logger.error("Missing tokenizer, cannot build LLM context")
        # Return empty raw data structure when no tokenizer
        empty_raw_data = convert_to_user_format(
            [],
            [],
            [],
            [],
            query_param.mode,
        )
        empty_raw_data["status"] = "failure"
        empty_raw_data["message"] = "Missing tokenizer, cannot build LLM context."
        return "", empty_raw_data

    # Get token limits
    max_total_tokens = getattr(
        query_param,
        "max_total_tokens",
        global_config.get("max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS),
    )

    # Get the system prompt template from PROMPTS or global_config
    sys_prompt_template = global_config.get(
        "system_prompt_template", PROMPTS["rag_response"]
    )

    kg_context_template = PROMPTS["kg_query_context"]
    user_prompt = query_param.user_prompt if query_param.user_prompt else ""
    response_type = (
        query_param.response_type
        if query_param.response_type
        else "Multiple Paragraphs"
    )

    entities_str = "\n".join(
        json.dumps(entity, ensure_ascii=False) for entity in entities_context
    )
    relations_str = "\n".join(
        json.dumps(relation, ensure_ascii=False) for relation in relations_context
    )

    # Calculate preliminary kg context tokens
    pre_kg_context = kg_context_template.format(
        entities_str=entities_str,
        relations_str=relations_str,
        text_chunks_str="",
        reference_list_str="",
    )
    kg_context_tokens = len(tokenizer.encode(pre_kg_context))

    # Calculate preliminary system prompt tokens
    pre_sys_prompt = sys_prompt_template.format(
        context_data="",  # Empty for overhead calculation
        response_type=response_type,
        user_prompt=user_prompt,
    )
    sys_prompt_tokens = len(tokenizer.encode(pre_sys_prompt))

    # Calculate available tokens for text chunks
    query_tokens = len(tokenizer.encode(query))
    buffer_tokens = 200  # reserved for reference list and safety buffer
    available_chunk_tokens = max_total_tokens - (
        sys_prompt_tokens + kg_context_tokens + query_tokens + buffer_tokens
    )

    logger.debug(
        f"Token allocation - Total: {max_total_tokens}, SysPrompt: {sys_prompt_tokens}, Query: {query_tokens}, KG: {kg_context_tokens}, Buffer: {buffer_tokens}, Available for chunks: {available_chunk_tokens}"
    )

    # Apply token truncation to chunks using the dynamic limit
    truncated_chunks = await process_chunks_unified(
        query=query,
        unique_chunks=merged_chunks,
        query_param=query_param,
        global_config=global_config,
        source_type=query_param.mode,
        chunk_token_limit=available_chunk_tokens,  # Pass dynamic limit
    )

    # Generate reference list from truncated chunks using the new common function
    reference_list, truncated_chunks = generate_reference_list_from_chunks(
        truncated_chunks
    )

    # Rebuild chunks_context with truncated chunks
    # The actual tokens may be slightly less than available_chunk_tokens due to deduplication logic
    chunks_context = []
    for i, chunk in enumerate(truncated_chunks):
        chunks_context.append(
            {
                "reference_id": chunk["reference_id"],
                "content": chunk["content"],
            }
        )

    text_units_str = "\n".join(
        json.dumps(text_unit, ensure_ascii=False) for text_unit in chunks_context
    )
    reference_list_str = "\n".join(
        f"[{ref['reference_id']}] {ref['file_path']}"
        for ref in reference_list
        if ref["reference_id"]
    )

    logger.info(
        f"Final context: {len(entities_context)} entities, {len(relations_context)} relations, {len(chunks_context)} chunks"
    )

    # not necessary to use LLM to generate a response
    if not entities_context and not relations_context and not chunks_context:
        # Return empty raw data structure when no entities/relations
        empty_raw_data = convert_to_user_format(
            [],
            [],
            [],
            [],
            query_param.mode,
        )
        empty_raw_data["status"] = "failure"
        empty_raw_data["message"] = "Query returned empty dataset."
        return "", empty_raw_data

    # output chunks tracking infomations
    # format: <source><frequency>/<order> (e.g., E5/2 R2/1 C1/1)
    if truncated_chunks and chunk_tracking:
        chunk_tracking_log = []
        for chunk in truncated_chunks:
            chunk_id = chunk.get("chunk_id")
            if chunk_id and chunk_id in chunk_tracking:
                tracking_info = chunk_tracking[chunk_id]
                source = tracking_info["source"]
                frequency = tracking_info["frequency"]
                order = tracking_info["order"]
                chunk_tracking_log.append(f"{source}{frequency}/{order}")
            else:
                chunk_tracking_log.append("?0/0")

        if chunk_tracking_log:
            logger.info(f"Final chunks S+F/O: {' '.join(chunk_tracking_log)}")

    result = kg_context_template.format(
        entities_str=entities_str,
        relations_str=relations_str,
        text_chunks_str=text_units_str,
        reference_list_str=reference_list_str,
    )

    # Always return both context and complete data structure (unified approach)
    logger.debug(
        f"[_build_context_str] Converting to user format: {len(entities_context)} entities, {len(relations_context)} relations, {len(truncated_chunks)} chunks"
    )
    final_data = convert_to_user_format(
        entities_context,
        relations_context,
        truncated_chunks,
        reference_list,
        query_param.mode,
        entity_id_to_original,
        relation_id_to_original,
    )
    logger.debug(
        f"[_build_context_str] Final data after conversion: {len(final_data.get('entities', []))} entities, {len(final_data.get('relationships', []))} relationships, {len(final_data.get('chunks', []))} chunks"
    )
    return result, final_data


# Now let's update the old _build_query_context to use the new architecture
async def _build_query_context(
    query: str,
    ll_keywords: str,
    hl_keywords: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    relationships_vdb: BaseVectorStorage,
    text_chunks_db: BaseKVStorage,
    query_param: QueryParam,
    chunks_vdb: BaseVectorStorage = None,
    images_vdb: BaseVectorStorage = None,
    image_metadata: BaseKVStorage = None,
) -> QueryContextResult | None:
    """
    Main query context building function using the new 4-stage architecture:
    1. Search -> 2. Truncate -> 3. Merge chunks -> 4. Build LLM context

    Returns unified QueryContextResult containing both context and raw_data.

    When the multimodal pipeline is wired in (``images_vdb`` and
    ``image_metadata`` both non-None), KG query modes can also run a
    cross-modal pass against the image-side vector store. See
    ``_perform_kg_search`` for details.
    """

    if not query:
        logger.warning("Query is empty, skipping context building")
        return None

    # Stage 1: Pure search
    search_result = await _perform_kg_search(
        query,
        ll_keywords,
        hl_keywords,
        knowledge_graph_inst,
        entities_vdb,
        relationships_vdb,
        text_chunks_db,
        query_param,
        chunks_vdb,
        images_vdb=images_vdb,
        image_metadata=image_metadata,
    )

    # Stage 2: Perform deduplication and filtering if necessary
    if (
        not search_result["final_entities"]
        and not search_result["final_relations"]
        and not search_result["chunk_tracking"]
    ):
        return None

    # Stage 2: Apply token truncation for LLM efficiency
    truncation_result = await _apply_token_truncation(
        search_result,
        query_param,
        text_chunks_db.global_config,
    )

    # Stage 3: Merge chunks using filtered entities/relations
    merged_chunks = await _merge_all_chunks(
        filtered_entities=truncation_result["filtered_entities"],
        filtered_relations=truncation_result["filtered_relations"],
        vector_chunks=search_result["vector_chunks"],
        query=query,
        knowledge_graph_inst=knowledge_graph_inst,
        text_chunks_db=text_chunks_db,
        query_param=query_param,
        chunks_vdb=chunks_vdb,
        chunk_tracking=search_result["chunk_tracking"],
        query_embedding=search_result["query_embedding"],
    )

    if (
        not merged_chunks
        and not truncation_result["entities_context"]
        and not truncation_result["relations_context"]
    ):
        return None

    # Stage 4: Build final LLM context with dynamic token processing
    # _build_context_str now always returns tuple[str, dict]
    context, raw_data = await _build_context_str(
        entities_context=truncation_result["entities_context"],
        relations_context=truncation_result["relations_context"],
        merged_chunks=merged_chunks,
        query=query,
        query_param=query_param,
        global_config=text_chunks_db.global_config,
        chunk_tracking=search_result["chunk_tracking"],
        entity_id_to_original=truncation_result["entity_id_to_original"],
        relation_id_to_original=truncation_result["relation_id_to_original"],
    )

    # Convert keywords strings to lists and add complete metadata to raw_data
    hl_keywords_list = hl_keywords.split(", ") if hl_keywords else []
    ll_keywords_list = ll_keywords.split(", ") if ll_keywords else []

    # Add complete metadata to raw_data (preserve existing metadata including query_mode)
    if "metadata" not in raw_data:
        raw_data["metadata"] = {}

    # Update keywords while preserving existing metadata
    raw_data["metadata"]["keywords"] = {
        "high_level": hl_keywords_list,
        "low_level": ll_keywords_list,
    }
    raw_data["metadata"]["processing_info"] = {
        "total_entities_found": len(search_result.get("final_entities", [])),
        "total_relations_found": len(search_result.get("final_relations", [])),
        "entities_after_truncation": len(
            truncation_result.get("filtered_entities", [])
        ),
        "relations_after_truncation": len(
            truncation_result.get("filtered_relations", [])
        ),
        "merged_chunks_count": len(merged_chunks),
        "final_chunks_count": len(raw_data.get("data", {}).get("chunks", [])),
    }

    logger.debug(
        f"[_build_query_context] Context length: {len(context) if context else 0}"
    )
    logger.debug(
        f"[_build_query_context] Raw data entities: {len(raw_data.get('data', {}).get('entities', []))}, relationships: {len(raw_data.get('data', {}).get('relationships', []))}, chunks: {len(raw_data.get('data', {}).get('chunks', []))}"
    )

    return QueryContextResult(context=context, raw_data=raw_data)


async def _get_node_data(
    query: str,
    knowledge_graph_inst: BaseGraphStorage,
    entities_vdb: BaseVectorStorage,
    query_param: QueryParam,
    query_embedding=None,
):
    logger.info(
        f"Query nodes: {query} (top_k:{query_param.top_k}, cosine:{entities_vdb.cosine_better_than_threshold})"
    )

    results = await entities_vdb.query(
        query, top_k=query_param.top_k, query_embedding=query_embedding
    )

    if not len(results):
        return [], []

    # Extract all entity IDs from your results list
    node_ids = [r["entity_name"] for r in results]

    # Call the batch node retrieval and degree functions concurrently.
    nodes_dict, degrees_dict = await asyncio.gather(
        knowledge_graph_inst.get_nodes_batch(node_ids),
        knowledge_graph_inst.node_degrees_batch(node_ids),
    )

    # Now, if you need the node data and degree in order:
    node_datas = [nodes_dict.get(nid) for nid in node_ids]
    node_degrees = [degrees_dict.get(nid, 0) for nid in node_ids]

    if not all([n is not None for n in node_datas]):
        logger.warning("Some nodes are missing, maybe the storage is damaged")

    node_datas = [
        {
            **n,
            "entity_name": k["entity_name"],
            "rank": d,
            "created_at": k.get("created_at"),
        }
        for k, n, d in zip(results, node_datas, node_degrees)
        if n is not None
    ]

    use_relations = await _find_most_related_edges_from_entities(
        node_datas,
        query_param,
        knowledge_graph_inst,
    )

    logger.info(
        f"Local query: {len(node_datas)} entites, {len(use_relations)} relations"
    )

    # Entities are sorted by cosine similarity
    # Relations are sorted by rank + weight
    return node_datas, use_relations


async def _find_most_related_edges_from_entities(
    node_datas: list[dict],
    query_param: QueryParam,
    knowledge_graph_inst: BaseGraphStorage,
):
    node_names = [dp["entity_name"] for dp in node_datas]
    batch_edges_dict = await knowledge_graph_inst.get_nodes_edges_batch(node_names)

    all_edges = []
    seen = set()

    for node_name in node_names:
        this_edges = batch_edges_dict.get(node_name, [])
        for e in this_edges:
            sorted_edge = tuple(sorted(e))
            if sorted_edge not in seen:
                seen.add(sorted_edge)
                all_edges.append(sorted_edge)

    # Prepare edge pairs in two forms:
    # For the batch edge properties function, use dicts.
    edge_pairs_dicts = [{"src": e[0], "tgt": e[1]} for e in all_edges]
    # For edge degrees, use tuples.
    edge_pairs_tuples = list(all_edges)  # all_edges is already a list of tuples

    # Call the batched functions concurrently.
    edge_data_dict, edge_degrees_dict = await asyncio.gather(
        knowledge_graph_inst.get_edges_batch(edge_pairs_dicts),
        knowledge_graph_inst.edge_degrees_batch(edge_pairs_tuples),
    )

    # Reconstruct edge_datas list in the same order as the deduplicated results.
    all_edges_data = []
    for pair in all_edges:
        edge_props = edge_data_dict.get(pair)
        if edge_props is not None:
            if "weight" not in edge_props:
                logger.warning(
                    f"Edge {pair} missing 'weight' attribute, using default value 1.0"
                )
                edge_props["weight"] = 1.0

            combined = {
                "src_tgt": pair,
                "rank": edge_degrees_dict.get(pair, 0),
                **edge_props,
            }
            all_edges_data.append(combined)

    all_edges_data = sorted(
        all_edges_data, key=lambda x: (x["rank"], x["weight"]), reverse=True
    )

    return all_edges_data


async def _find_related_text_unit_from_entities(
    node_datas: list[dict],
    query_param: QueryParam,
    text_chunks_db: BaseKVStorage,
    knowledge_graph_inst: BaseGraphStorage,
    query: str = None,
    chunks_vdb: BaseVectorStorage = None,
    chunk_tracking: dict = None,
    query_embedding=None,
):
    """
    Find text chunks related to entities using configurable chunk selection method.

    This function supports two chunk selection strategies:
    1. WEIGHT: Linear gradient weighted polling based on chunk occurrence count
    2. VECTOR: Vector similarity-based selection using embedding cosine similarity
    """
    logger.debug(f"Finding text chunks from {len(node_datas)} entities")

    if not node_datas:
        return []

    # Step 1: Collect all text chunks for each entity
    entities_with_chunks = []
    for entity in node_datas:
        if entity.get("source_id"):
            chunks = split_string_by_multi_markers(
                entity["source_id"], [GRAPH_FIELD_SEP]
            )
            if chunks:
                entities_with_chunks.append(
                    {
                        "entity_name": entity["entity_name"],
                        "chunks": chunks,
                        "entity_data": entity,
                    }
                )

    if not entities_with_chunks:
        logger.warning("No entities with text chunks found")
        return []

    kg_chunk_pick_method = text_chunks_db.global_config.get(
        "kg_chunk_pick_method", DEFAULT_KG_CHUNK_PICK_METHOD
    )
    max_related_chunks = text_chunks_db.global_config.get(
        "related_chunk_number", DEFAULT_RELATED_CHUNK_NUMBER
    )

    # Step 2: Count chunk occurrences and deduplicate (keep chunks from earlier positioned entities)
    chunk_occurrence_count = {}
    for entity_info in entities_with_chunks:
        deduplicated_chunks = []
        for chunk_id in entity_info["chunks"]:
            chunk_occurrence_count[chunk_id] = (
                chunk_occurrence_count.get(chunk_id, 0) + 1
            )

            # If this is the first occurrence (count == 1), keep it; otherwise skip (duplicate from later position)
            if chunk_occurrence_count[chunk_id] == 1:
                deduplicated_chunks.append(chunk_id)
            # count > 1 means this chunk appeared in an earlier entity, so skip it

        # Update entity's chunks to deduplicated chunks
        entity_info["chunks"] = deduplicated_chunks

    # Step 3: Sort chunks for each entity by occurrence count (higher count = higher priority)
    total_entity_chunks = 0
    for entity_info in entities_with_chunks:
        sorted_chunks = sorted(
            entity_info["chunks"],
            key=lambda chunk_id: chunk_occurrence_count.get(chunk_id, 0),
            reverse=True,
        )
        entity_info["sorted_chunks"] = sorted_chunks
        total_entity_chunks += len(sorted_chunks)

    selected_chunk_ids = []  # Initialize to avoid UnboundLocalError

    # Step 4: Apply the selected chunk selection algorithm
    # Pick by vector similarity:
    #     The order of text chunks aligns with the naive retrieval's destination.
    #     When reranking is disabled, the text chunks delivered to the LLM tend to favor naive retrieval.
    if kg_chunk_pick_method == "VECTOR" and query and chunks_vdb:
        num_of_chunks = int(max_related_chunks * len(entities_with_chunks) / 2)

        # Get embedding function from global config
        actual_embedding_func = text_chunks_db.embedding_func
        if not actual_embedding_func:
            logger.warning("No embedding function found, falling back to WEIGHT method")
            kg_chunk_pick_method = "WEIGHT"
        else:
            try:
                selected_chunk_ids = await pick_by_vector_similarity(
                    query=query,
                    text_chunks_storage=text_chunks_db,
                    chunks_vdb=chunks_vdb,
                    num_of_chunks=num_of_chunks,
                    entity_info=entities_with_chunks,
                    embedding_func=actual_embedding_func,
                    query_embedding=query_embedding,
                )

                if selected_chunk_ids == []:
                    kg_chunk_pick_method = "WEIGHT"
                    logger.warning(
                        "No entity-related chunks selected by vector similarity, falling back to WEIGHT method"
                    )
                else:
                    logger.info(
                        f"Selecting {len(selected_chunk_ids)} from {total_entity_chunks} entity-related chunks by vector similarity"
                    )

            except Exception as e:
                logger.error(
                    f"Error in vector similarity sorting: {e}, falling back to WEIGHT method"
                )
                kg_chunk_pick_method = "WEIGHT"

    if kg_chunk_pick_method == "WEIGHT":
        # Pick by entity and chunk weight:
        #     When reranking is disabled, delivered more solely KG related chunks to the LLM
        selected_chunk_ids = pick_by_weighted_polling(
            entities_with_chunks, max_related_chunks, min_related_chunks=1
        )

        logger.info(
            f"Selecting {len(selected_chunk_ids)} from {total_entity_chunks} entity-related chunks by weighted polling"
        )

    if not selected_chunk_ids:
        return []

    # Step 5: Batch retrieve chunk data
    unique_chunk_ids = list(
        dict.fromkeys(selected_chunk_ids)
    )  # Remove duplicates while preserving order
    chunk_data_list = await text_chunks_db.get_by_ids(unique_chunk_ids)

    # Step 6: Build result chunks with valid data and update chunk tracking
    result_chunks = []
    for i, (chunk_id, chunk_data) in enumerate(zip(unique_chunk_ids, chunk_data_list)):
        if chunk_data is not None and "content" in chunk_data:
            chunk_data_copy = chunk_data.copy()
            chunk_data_copy["source_type"] = "entity"
            chunk_data_copy["chunk_id"] = chunk_id  # Add chunk_id for deduplication
            result_chunks.append(chunk_data_copy)

            # Update chunk tracking if provided
            if chunk_tracking is not None:
                chunk_tracking[chunk_id] = {
                    "source": "E",
                    "frequency": chunk_occurrence_count.get(chunk_id, 1),
                    "order": i + 1,  # 1-based order in final entity-related results
                }

    return result_chunks


async def _get_edge_data(
    keywords,
    knowledge_graph_inst: BaseGraphStorage,
    relationships_vdb: BaseVectorStorage,
    query_param: QueryParam,
    query_embedding=None,
):
    logger.info(
        f"Query edges: {keywords} (top_k:{query_param.top_k}, cosine:{relationships_vdb.cosine_better_than_threshold})"
    )

    results = await relationships_vdb.query(
        keywords, top_k=query_param.top_k, query_embedding=query_embedding
    )

    if not len(results):
        return [], []

    # Prepare edge pairs in two forms:
    # For the batch edge properties function, use dicts.
    edge_pairs_dicts = [{"src": r["src_id"], "tgt": r["tgt_id"]} for r in results]
    edge_data_dict = await knowledge_graph_inst.get_edges_batch(edge_pairs_dicts)

    # Reconstruct edge_datas list in the same order as results.
    edge_datas = []
    for k in results:
        pair = (k["src_id"], k["tgt_id"])
        edge_props = edge_data_dict.get(pair)
        if edge_props is not None:
            if "weight" not in edge_props:
                logger.warning(
                    f"Edge {pair} missing 'weight' attribute, using default value 1.0"
                )
                edge_props["weight"] = 1.0

            # Keep edge data without rank, maintain vector search order
            combined = {
                "src_id": k["src_id"],
                "tgt_id": k["tgt_id"],
                "created_at": k.get("created_at", None),
                **edge_props,
            }
            edge_datas.append(combined)

    # Relations maintain vector search order (sorted by similarity)

    use_entities = await _find_most_related_entities_from_relationships(
        edge_datas,
        query_param,
        knowledge_graph_inst,
    )

    logger.info(
        f"Global query: {len(use_entities)} entites, {len(edge_datas)} relations"
    )

    return edge_datas, use_entities


async def _find_most_related_entities_from_relationships(
    edge_datas: list[dict],
    query_param: QueryParam,
    knowledge_graph_inst: BaseGraphStorage,
):
    entity_names = []
    seen = set()

    for e in edge_datas:
        if e["src_id"] not in seen:
            entity_names.append(e["src_id"])
            seen.add(e["src_id"])
        if e["tgt_id"] not in seen:
            entity_names.append(e["tgt_id"])
            seen.add(e["tgt_id"])

    # Only get nodes data, no need for node degrees
    nodes_dict = await knowledge_graph_inst.get_nodes_batch(entity_names)

    # Rebuild the list in the same order as entity_names
    node_datas = []
    for entity_name in entity_names:
        node = nodes_dict.get(entity_name)
        if node is None:
            logger.warning(f"Node '{entity_name}' not found in batch retrieval.")
            continue
        # Combine the node data with the entity name, no rank needed
        combined = {**node, "entity_name": entity_name}
        node_datas.append(combined)

    return node_datas


async def _find_related_text_unit_from_relations(
    edge_datas: list[dict],
    query_param: QueryParam,
    text_chunks_db: BaseKVStorage,
    entity_chunks: list[dict] = None,
    query: str = None,
    chunks_vdb: BaseVectorStorage = None,
    chunk_tracking: dict = None,
    query_embedding=None,
):
    """
    Find text chunks related to relationships using configurable chunk selection method.

    This function supports two chunk selection strategies:
    1. WEIGHT: Linear gradient weighted polling based on chunk occurrence count
    2. VECTOR: Vector similarity-based selection using embedding cosine similarity
    """
    logger.debug(f"Finding text chunks from {len(edge_datas)} relations")

    if not edge_datas:
        return []

    # Step 1: Collect all text chunks for each relationship
    relations_with_chunks = []
    for relation in edge_datas:
        if relation.get("source_id"):
            chunks = split_string_by_multi_markers(
                relation["source_id"], [GRAPH_FIELD_SEP]
            )
            if chunks:
                # Build relation identifier
                if "src_tgt" in relation:
                    rel_key = tuple(sorted(relation["src_tgt"]))
                else:
                    rel_key = tuple(
                        sorted([relation.get("src_id"), relation.get("tgt_id")])
                    )

                relations_with_chunks.append(
                    {
                        "relation_key": rel_key,
                        "chunks": chunks,
                        "relation_data": relation,
                    }
                )

    if not relations_with_chunks:
        logger.warning("No relation-related chunks found")
        return []

    kg_chunk_pick_method = text_chunks_db.global_config.get(
        "kg_chunk_pick_method", DEFAULT_KG_CHUNK_PICK_METHOD
    )
    max_related_chunks = text_chunks_db.global_config.get(
        "related_chunk_number", DEFAULT_RELATED_CHUNK_NUMBER
    )

    # Step 2: Count chunk occurrences and deduplicate (keep chunks from earlier positioned relationships)
    # Also remove duplicates with entity_chunks

    # Extract chunk IDs from entity_chunks for deduplication
    entity_chunk_ids = set()
    if entity_chunks:
        for chunk in entity_chunks:
            chunk_id = chunk.get("chunk_id")
            if chunk_id:
                entity_chunk_ids.add(chunk_id)

    chunk_occurrence_count = {}
    # Track unique chunk_ids that have been removed to avoid double counting
    removed_entity_chunk_ids = set()

    for relation_info in relations_with_chunks:
        deduplicated_chunks = []
        for chunk_id in relation_info["chunks"]:
            # Skip chunks that already exist in entity_chunks
            if chunk_id in entity_chunk_ids:
                # Only count each unique chunk_id once
                removed_entity_chunk_ids.add(chunk_id)
                continue

            chunk_occurrence_count[chunk_id] = (
                chunk_occurrence_count.get(chunk_id, 0) + 1
            )

            # If this is the first occurrence (count == 1), keep it; otherwise skip (duplicate from later position)
            if chunk_occurrence_count[chunk_id] == 1:
                deduplicated_chunks.append(chunk_id)
            # count > 1 means this chunk appeared in an earlier relationship, so skip it

        # Update relationship's chunks to deduplicated chunks
        relation_info["chunks"] = deduplicated_chunks

    # Check if any relations still have chunks after deduplication
    relations_with_chunks = [
        relation_info
        for relation_info in relations_with_chunks
        if relation_info["chunks"]
    ]

    if not relations_with_chunks:
        logger.info(
            f"Find no additional relations-related chunks from {len(edge_datas)} relations"
        )
        return []

    # Step 3: Sort chunks for each relationship by occurrence count (higher count = higher priority)
    total_relation_chunks = 0
    for relation_info in relations_with_chunks:
        sorted_chunks = sorted(
            relation_info["chunks"],
            key=lambda chunk_id: chunk_occurrence_count.get(chunk_id, 0),
            reverse=True,
        )
        relation_info["sorted_chunks"] = sorted_chunks
        total_relation_chunks += len(sorted_chunks)

    logger.info(
        f"Find {total_relation_chunks} additional chunks in {len(relations_with_chunks)} relations (deduplicated {len(removed_entity_chunk_ids)})"
    )

    # Step 4: Apply the selected chunk selection algorithm
    selected_chunk_ids = []  # Initialize to avoid UnboundLocalError

    if kg_chunk_pick_method == "VECTOR" and query and chunks_vdb:
        num_of_chunks = int(max_related_chunks * len(relations_with_chunks) / 2)

        # Get embedding function from global config
        actual_embedding_func = text_chunks_db.embedding_func
        if not actual_embedding_func:
            logger.warning("No embedding function found, falling back to WEIGHT method")
            kg_chunk_pick_method = "WEIGHT"
        else:
            try:
                selected_chunk_ids = await pick_by_vector_similarity(
                    query=query,
                    text_chunks_storage=text_chunks_db,
                    chunks_vdb=chunks_vdb,
                    num_of_chunks=num_of_chunks,
                    entity_info=relations_with_chunks,
                    embedding_func=actual_embedding_func,
                    query_embedding=query_embedding,
                )

                if selected_chunk_ids == []:
                    kg_chunk_pick_method = "WEIGHT"
                    logger.warning(
                        "No relation-related chunks selected by vector similarity, falling back to WEIGHT method"
                    )
                else:
                    logger.info(
                        f"Selecting {len(selected_chunk_ids)} from {total_relation_chunks} relation-related chunks by vector similarity"
                    )

            except Exception as e:
                logger.error(
                    f"Error in vector similarity sorting: {e}, falling back to WEIGHT method"
                )
                kg_chunk_pick_method = "WEIGHT"

    if kg_chunk_pick_method == "WEIGHT":
        # Apply linear gradient weighted polling algorithm
        selected_chunk_ids = pick_by_weighted_polling(
            relations_with_chunks, max_related_chunks, min_related_chunks=1
        )

        logger.info(
            f"Selecting {len(selected_chunk_ids)} from {total_relation_chunks} relation-related chunks by weighted polling"
        )

    logger.debug(
        f"KG related chunks: {len(entity_chunks)} from entitys, {len(selected_chunk_ids)} from relations"
    )

    if not selected_chunk_ids:
        return []

    # Step 5: Batch retrieve chunk data
    unique_chunk_ids = list(
        dict.fromkeys(selected_chunk_ids)
    )  # Remove duplicates while preserving order
    chunk_data_list = await text_chunks_db.get_by_ids(unique_chunk_ids)

    # Step 6: Build result chunks with valid data and update chunk tracking
    result_chunks = []
    for i, (chunk_id, chunk_data) in enumerate(zip(unique_chunk_ids, chunk_data_list)):
        if chunk_data is not None and "content" in chunk_data:
            chunk_data_copy = chunk_data.copy()
            chunk_data_copy["source_type"] = "relationship"
            chunk_data_copy["chunk_id"] = chunk_id  # Add chunk_id for deduplication
            result_chunks.append(chunk_data_copy)

            # Update chunk tracking if provided
            if chunk_tracking is not None:
                chunk_tracking[chunk_id] = {
                    "source": "R",
                    "frequency": chunk_occurrence_count.get(chunk_id, 1),
                    "order": i + 1,  # 1-based order in final relation-related results
                }

    return result_chunks


@overload
async def naive_query(
    query: str,
    chunks_vdb: BaseVectorStorage,
    query_param: QueryParam,
    global_config: dict[str, str],
    hashing_kv: BaseKVStorage | None = None,
    system_prompt: str | None = None,
    return_raw_data: Literal[True] = True,
    images_vdb: BaseVectorStorage = None,
    image_metadata: BaseKVStorage = None,
) -> dict[str, Any]: ...


@overload
async def naive_query(
    query: str,
    chunks_vdb: BaseVectorStorage,
    query_param: QueryParam,
    global_config: dict[str, str],
    hashing_kv: BaseKVStorage | None = None,
    system_prompt: str | None = None,
    return_raw_data: Literal[False] = False,
    images_vdb: BaseVectorStorage = None,
    image_metadata: BaseKVStorage = None,
) -> str | AsyncIterator[str]: ...


async def naive_query(
    query: str,
    chunks_vdb: BaseVectorStorage,
    query_param: QueryParam,
    global_config: dict[str, str],
    hashing_kv: BaseKVStorage | None = None,
    system_prompt: str | None = None,
    images_vdb: BaseVectorStorage = None,
    image_metadata: BaseKVStorage = None,
) -> QueryResult | None:
    """
    Execute naive query and return unified QueryResult object.

    Args:
        query: Query string
        chunks_vdb: Document chunks vector database
        query_param: Query parameters
        global_config: Global configuration
        hashing_kv: Cache storage
        system_prompt: System prompt

    Returns:
        QueryResult | None: Unified query result object containing:
            - content: Non-streaming response text content
            - response_iterator: Streaming response iterator
            - raw_data: Complete structured data (including references and metadata)
            - is_streaming: Whether this is a streaming result

        Returns None when no relevant chunks are retrieved.
    """

    if not query:
        return QueryResult(content=PROMPTS["fail_response"])

    if query_param.model_func:
        use_model_func = query_param.model_func
    elif global_config.get("query_llm_model_func") is not None:
        use_model_func = global_config["query_llm_model_func"]
        use_model_func = partial(use_model_func, _priority=5)
    else:
        use_model_func = global_config["llm_model_func"]
        # Apply higher priority (5) to query relation LLM function
        use_model_func = partial(use_model_func, _priority=5)

    tokenizer: Tokenizer = global_config["tokenizer"]
    if not tokenizer:
        logger.error("Tokenizer not found in global configuration.")
        return QueryResult(content=PROMPTS["fail_response"])

    chunks = await _get_vector_context(query, chunks_vdb, query_param, None)

    # Multimodal: parallel cross-modal retrieval against images_vdb.
    # Falls through silently when images_vdb/image_metadata are not provided.
    if images_vdb is not None and image_metadata is not None:
        image_chunks = await _get_image_context(
            query,
            images_vdb,
            image_metadata,
            query_param,
        )
        if image_chunks:
            chunks.extend(image_chunks)

    if chunks is None or len(chunks) == 0:
        logger.info(
            "[naive_query] No relevant document chunks found; returning no-result."
        )
        return None

    # Calculate dynamic token limit for chunks
    max_total_tokens = getattr(
        query_param,
        "max_total_tokens",
        global_config.get("max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS),
    )

    # Calculate system prompt template tokens (excluding content_data)
    user_prompt = f"\n\n{query_param.user_prompt}" if query_param.user_prompt else "n/a"
    response_type = (
        query_param.response_type
        if query_param.response_type
        else "Multiple Paragraphs"
    )

    # Use the provided system prompt or default
    sys_prompt_template = (
        system_prompt if system_prompt else PROMPTS["naive_rag_response"]
    )

    # Create a preliminary system prompt with empty content_data to calculate overhead
    pre_sys_prompt = sys_prompt_template.format(
        response_type=response_type,
        user_prompt=user_prompt,
        content_data="",  # Empty for overhead calculation
    )

    # Calculate available tokens for chunks
    sys_prompt_tokens = len(tokenizer.encode(pre_sys_prompt))
    query_tokens = len(tokenizer.encode(query))
    buffer_tokens = 200  # reserved for reference list and safety buffer
    available_chunk_tokens = max_total_tokens - (
        sys_prompt_tokens + query_tokens + buffer_tokens
    )

    logger.debug(
        f"Naive query token allocation - Total: {max_total_tokens}, SysPrompt: {sys_prompt_tokens}, Query: {query_tokens}, Buffer: {buffer_tokens}, Available for chunks: {available_chunk_tokens}"
    )

    # Process chunks using unified processing with dynamic token limit
    processed_chunks = await process_chunks_unified(
        query=query,
        unique_chunks=chunks,
        query_param=query_param,
        global_config=global_config,
        source_type="vector",
        chunk_token_limit=available_chunk_tokens,  # Pass dynamic limit
    )

    # Generate reference list from processed chunks using the new common function
    reference_list, processed_chunks_with_ref_ids = generate_reference_list_from_chunks(
        processed_chunks
    )

    logger.info(f"Final context: {len(processed_chunks_with_ref_ids)} chunks")

    # Build raw data structure for naive mode using processed chunks with reference IDs
    raw_data = convert_to_user_format(
        [],  # naive mode has no entities
        [],  # naive mode has no relationships
        processed_chunks_with_ref_ids,
        reference_list,
        "naive",
    )

    # Add complete metadata for naive mode
    if "metadata" not in raw_data:
        raw_data["metadata"] = {}
    raw_data["metadata"]["keywords"] = {
        "high_level": [],  # naive mode has no keyword extraction
        "low_level": [],  # naive mode has no keyword extraction
    }
    raw_data["metadata"]["processing_info"] = {
        "total_chunks_found": len(chunks),
        "final_chunks_count": len(processed_chunks_with_ref_ids),
    }

    # Build chunks_context from processed chunks with reference IDs
    chunks_context = []
    for i, chunk in enumerate(processed_chunks_with_ref_ids):
        chunks_context.append(
            {
                "reference_id": chunk["reference_id"],
                "content": chunk["content"],
            }
        )

    text_units_str = "\n".join(
        json.dumps(text_unit, ensure_ascii=False) for text_unit in chunks_context
    )
    reference_list_str = "\n".join(
        f"[{ref['reference_id']}] {ref['file_path']}"
        for ref in reference_list
        if ref["reference_id"]
    )

    naive_context_template = PROMPTS["naive_query_context"]
    context_content = naive_context_template.format(
        text_chunks_str=text_units_str,
        reference_list_str=reference_list_str,
    )

    if query_param.only_need_context and not query_param.only_need_prompt:
        return QueryResult(content=context_content, raw_data=raw_data)

    sys_prompt = sys_prompt_template.format(
        response_type=query_param.response_type,
        user_prompt=user_prompt,
        content_data=context_content,
    )

    user_query = query

    if query_param.only_need_prompt:
        prompt_content = "\n\n".join([sys_prompt, "---User Query---", user_query])
        return QueryResult(content=prompt_content, raw_data=raw_data)

    # Handle cache
    args_hash = compute_args_hash(
        query_param.mode,
        query,
        query_param.response_type,
        query_param.top_k,
        query_param.chunk_top_k,
        query_param.max_entity_tokens,
        query_param.max_relation_tokens,
        query_param.max_total_tokens,
        query_param.user_prompt or "",
        query_param.enable_rerank,
    )
    cached_result = await handle_cache(
        hashing_kv, args_hash, user_query, query_param.mode, cache_type="query"
    )
    if cached_result is not None:
        cached_response, _ = cached_result  # Extract content, ignore timestamp
        logger.info(
            " == LLM cache == Query cache hit, using cached response as query result"
        )
        response = cached_response
    else:
        response = await use_model_func(
            user_query,
            system_prompt=sys_prompt,
            history_messages=query_param.conversation_history,
            enable_cot=True,
            stream=query_param.stream,
        )

        if hashing_kv and hashing_kv.global_config.get("enable_llm_cache"):
            queryparam_dict = {
                "mode": query_param.mode,
                "response_type": query_param.response_type,
                "top_k": query_param.top_k,
                "chunk_top_k": query_param.chunk_top_k,
                "max_entity_tokens": query_param.max_entity_tokens,
                "max_relation_tokens": query_param.max_relation_tokens,
                "max_total_tokens": query_param.max_total_tokens,
                "user_prompt": query_param.user_prompt or "",
                "enable_rerank": query_param.enable_rerank,
            }
            await save_to_cache(
                hashing_kv,
                CacheData(
                    args_hash=args_hash,
                    content=response,
                    prompt=query,
                    mode=query_param.mode,
                    cache_type="query",
                    queryparam=queryparam_dict,
                ),
            )

    # Return unified result based on actual response type
    if isinstance(response, str):
        # Non-streaming response (string)
        if len(response) > len(sys_prompt):
            response = (
                response[len(sys_prompt) :]
                .replace(sys_prompt, "")
                .replace("user", "")
                .replace("model", "")
                .replace(query, "")
                .replace("<system>", "")
                .replace("</system>", "")
                .strip()
            )

        return QueryResult(content=response, raw_data=raw_data)
    else:
        # Streaming response (AsyncIterator)
        return QueryResult(
            response_iterator=response, raw_data=raw_data, is_streaming=True
        )
