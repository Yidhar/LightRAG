import asyncio
import importlib
import sys

import pytest

pytestmark = pytest.mark.offline


def import_real_api_module(module_name: str):
    sys.modules.pop(module_name, None)

    package_name, _, child_name = module_name.rpartition(".")
    package = sys.modules.get(package_name)
    if package is not None and hasattr(package, child_name):
        delattr(package, child_name)

    return importlib.import_module(module_name)


@pytest.fixture(autouse=True)
def clear_rag_factory_modules():
    module_names = (
        "lightrag.api.kb_registry",
        "lightrag.api.models.kb",
        "lightrag.api.rag_factory",
    )
    for module_name in module_names:
        sys.modules.pop(module_name, None)

    yield

    for module_name in module_names:
        sys.modules.pop(module_name, None)


class FakeRag:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.finalized = False

    async def finalize_storages(self):
        self.finalized = True


@pytest.mark.asyncio
async def test_rag_factory_caches_by_workspace_and_kb(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")
    factory_module = import_real_api_module("lightrag.api.rag_factory")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.ensure_default_kb("team_alpha", "finance_primary")

    build_calls = []

    async def builder(workspace_id, kb_id, combined_workspace, knowledge_base):
        build_calls.append((workspace_id, kb_id, combined_workspace, knowledge_base.id))
        return FakeRag(combined_workspace)

    factory = factory_module.RagFactory(
        builder=builder,
        registry=registry,
        kb_separator="__",
    )

    first = await factory.get("team_alpha", "finance_primary")
    second = await factory.get("team_alpha", "finance_primary")

    assert first is second
    assert first.workspace == "team_alpha__finance_primary"
    assert build_calls == [
        (
            "team_alpha",
            "finance_primary",
            "team_alpha__finance_primary",
            "finance_primary",
        )
    ]


@pytest.mark.asyncio
async def test_rag_factory_concurrent_init_only_builds_once(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")
    factory_module = import_real_api_module("lightrag.api.rag_factory")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.ensure_default_kb("team_alpha", "support")

    build_calls = []

    async def builder(workspace_id, kb_id, combined_workspace, knowledge_base):
        build_calls.append((workspace_id, kb_id, combined_workspace))
        await asyncio.sleep(0.01)
        return FakeRag(combined_workspace)

    factory = factory_module.RagFactory(
        builder=builder,
        registry=registry,
        kb_separator="__",
    )

    results = await asyncio.gather(
        *[factory.get("team_alpha", "support") for _ in range(10)]
    )

    assert len({id(item) for item in results}) == 1
    assert build_calls == [("team_alpha", "support", "team_alpha__support")]


@pytest.mark.asyncio
async def test_rag_factory_finalize_all_closes_cached_instances(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")
    factory_module = import_real_api_module("lightrag.api.rag_factory")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.ensure_default_kb("team_alpha", "default")
    registry.ensure_default_kb("team_alpha", "sales")

    built_rags = []

    async def builder(workspace_id, kb_id, combined_workspace, knowledge_base):
        rag = FakeRag(combined_workspace)
        built_rags.append(rag)
        return rag

    factory = factory_module.RagFactory(
        builder=builder,
        registry=registry,
        kb_separator="__",
    )

    seeded = FakeRag("team_alpha__default")
    factory.prime("team_alpha", "default", seeded)
    await factory.get("team_alpha", "sales")
    await factory.finalize_all()

    assert seeded.finalized is True
    assert all(rag.finalized is True for rag in built_rags)


@pytest.mark.asyncio
async def test_rag_factory_evict_finalizes_one_cached_instance(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")
    factory_module = import_real_api_module("lightrag.api.rag_factory")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.ensure_default_kb("team_alpha", "default")

    async def builder(workspace_id, kb_id, combined_workspace, knowledge_base):
        return FakeRag(combined_workspace)

    factory = factory_module.RagFactory(
        builder=builder,
        registry=registry,
        kb_separator="__",
    )

    rag = await factory.get("team_alpha", "default")
    evicted = await factory.evict("team_alpha", "default")

    assert evicted is True
    assert rag.finalized is True

    rebuilt = await factory.get("team_alpha", "default")

    assert rebuilt is not rag
