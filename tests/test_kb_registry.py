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
def clear_kb_registry_modules():
    module_names = (
        "lightrag.api.kb_registry",
        "lightrag.api.models.kb",
    )
    for module_name in module_names:
        sys.modules.pop(module_name, None)

    yield

    for module_name in module_names:
        sys.modules.pop(module_name, None)


def test_kb_registry_persists_created_records(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    created = registry.create_kb(
        "team-alpha",
        kb_id="finance.primary",
        name="Finance KB",
        description="Quarterly finance documents",
        config_override={"rerank": True},
    )

    assert created.workspace_id == "team_alpha"
    assert created.id == "finance_primary"
    assert registry.exists("team_alpha", "finance_primary") is True

    reloaded = registry_module.KnowledgeBaseRegistry(tmp_path)
    restored = reloaded.get_kb("team_alpha", "finance_primary")

    assert restored is not None
    assert restored.name == "Finance KB"
    assert restored.description == "Quarterly finance documents"
    assert restored.config_override == {"rerank": True}


def test_kb_registry_lists_only_requested_workspace(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.create_kb("team-alpha", kb_id="sales")
    registry.create_kb("team-alpha", kb_id="support")
    registry.create_kb("ops", kb_id="runbooks")

    kb_ids = [kb.id for kb in registry.list_kbs("team-alpha")]

    assert kb_ids == ["sales", "support"]


def test_kb_registry_ensure_default_is_idempotent(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    first = registry.ensure_default_kb("team-alpha", "default")
    second = registry.ensure_default_kb("team-alpha", "default")

    assert first.id == "default"
    assert second.id == "default"
    assert [kb.id for kb in registry.list_kbs("team-alpha")] == ["default"]


def test_kb_registry_delete_removes_record(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.create_kb("team-alpha", kb_id="archive")

    assert registry.delete_kb("team-alpha", "archive") is True
    assert registry.exists("team-alpha", "archive") is False
    assert registry.delete_kb("team-alpha", "archive") is False


def test_kb_registry_update_mutates_selected_fields(tmp_path):
    registry_module = import_real_api_module("lightrag.api.kb_registry")

    registry = registry_module.KnowledgeBaseRegistry(tmp_path)
    registry.create_kb("team-alpha", kb_id="finance", name="Finance")

    updated = registry.update_kb(
        "team-alpha",
        "finance",
        name="Finance v2",
        description="Updated docs",
        config_override={"rerank": True},
        status="archived",
    )

    assert updated is not None
    assert updated.name == "Finance v2"
    assert updated.description == "Updated docs"
    assert updated.config_override == {"rerank": True}
    assert updated.status == "archived"
