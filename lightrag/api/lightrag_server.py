"""
LightRAG FastAPI Server
"""

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
import os
import re
import logging
import logging.config
import sys
import uvicorn
import pipmaster as pm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pathlib import Path
from ascii_colors import ASCIIColors
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from lightrag.api.utils_api import (
    get_combined_auth_dependency,
    display_splash_screen,
    check_env_file,
)
from lightrag.api.dependencies import (
    get_request_context,
    install_platform_state,
    resolve_kb_id,
    resolve_workspace_id,
)
from lightrag.api.kb_registry import KnowledgeBaseRegistry
from lightrag.api.rag_factory import RagFactory
from .config import (
    collect_manual_kb_isolation_migration_backends,
    global_args,
    update_uvicorn_mode_config,
    get_default_host,
)
from lightrag.utils import get_env_value
from lightrag import LightRAG, __version__ as core_version
from lightrag.api import __api_version__
from lightrag.types import GPTKeywordExtractionFormat
from lightrag.utils import EmbeddingFunc
from lightrag.constants import (
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILENAME,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_EMBEDDING_TIMEOUT,
)
from lightrag.api.routers.document_routes import (
    DocumentManager,
    create_document_routes,
)
from lightrag.api.routers.auth_routes import create_auth_routes, issue_login_tokens
from lightrag.api.auth_provider import get_auth_provider
from lightrag.api.routers.audit_routes import create_audit_routes
from lightrag.api.routers.kb_routes import create_kb_routes
from lightrag.api.routers.membership_routes import create_membership_routes
from lightrag.api.routers.workspace_routes import create_workspace_routes
from lightrag.api.routers.query_routes import create_query_routes
from lightrag.api.routers.graph_routes import create_graph_routes
from lightrag.api.routers.image_routes import create_image_routes
from lightrag.api.routers.ollama_api import OllamaAPI

from lightrag.utils import logger, set_verbose_debug
from lightrag.kg.shared_storage import (
    get_namespace_data,
    get_default_workspace,
    # set_default_workspace,
    cleanup_keyed_lock,
    finalize_share_data,
)
from fastapi.security import OAuth2PasswordRequestForm

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)


webui_title = os.getenv("WEBUI_TITLE")
webui_description = os.getenv("WEBUI_DESCRIPTION")

class LLMConfigCache:
    """Smart LLM and Embedding configuration cache class"""

    def __init__(self, args):
        self.args = args

        # Initialize configurations based on binding conditions
        self.openai_llm_options = None
        self.gemini_llm_options = None
        self.gemini_embedding_options = None
        self.ollama_llm_options = None
        self.ollama_embedding_options = None

        # Only initialize and log OpenAI options when using OpenAI-related bindings
        if args.llm_binding in ["openai", "azure_openai"]:
            from lightrag.llm.binding_options import OpenAILLMOptions

            self.openai_llm_options = OpenAILLMOptions.options_dict(args)
            logger.info(f"OpenAI LLM Options: {self.openai_llm_options}")

        if args.llm_binding == "gemini":
            from lightrag.llm.binding_options import GeminiLLMOptions

            self.gemini_llm_options = GeminiLLMOptions.options_dict(args)
            logger.info(f"Gemini LLM Options: {self.gemini_llm_options}")

        # Only initialize and log Ollama LLM options when using Ollama LLM binding
        if args.llm_binding == "ollama":
            try:
                from lightrag.llm.binding_options import OllamaLLMOptions

                self.ollama_llm_options = OllamaLLMOptions.options_dict(args)
                logger.info(f"Ollama LLM Options: {self.ollama_llm_options}")
            except ImportError:
                logger.warning(
                    "OllamaLLMOptions not available, using default configuration"
                )
                self.ollama_llm_options = {}

        # Only initialize and log Ollama Embedding options when using Ollama Embedding binding
        if args.embedding_binding == "ollama":
            try:
                from lightrag.llm.binding_options import OllamaEmbeddingOptions

                self.ollama_embedding_options = OllamaEmbeddingOptions.options_dict(
                    args
                )
                logger.info(
                    f"Ollama Embedding Options: {self.ollama_embedding_options}"
                )
            except ImportError:
                logger.warning(
                    "OllamaEmbeddingOptions not available, using default configuration"
                )
                self.ollama_embedding_options = {}

        # Only initialize and log Gemini Embedding options when using Gemini Embedding binding
        if args.embedding_binding == "gemini":
            try:
                from lightrag.llm.binding_options import GeminiEmbeddingOptions

                self.gemini_embedding_options = GeminiEmbeddingOptions.options_dict(
                    args
                )
                logger.info(
                    f"Gemini Embedding Options: {self.gemini_embedding_options}"
                )
            except ImportError:
                logger.warning(
                    "GeminiEmbeddingOptions not available, using default configuration"
                )
                self.gemini_embedding_options = {}


def check_frontend_build():
    """Check if frontend is built and optionally check if source is up-to-date

    Returns:
        tuple: (assets_exist: bool, is_outdated: bool)
            - assets_exist: True if WebUI build files exist
            - is_outdated: True if source is newer than build (only in dev environment)
    """
    webui_dir = Path(__file__).parent / "webui"
    index_html = webui_dir / "index.html"

    # 1. Check if build files exist
    if not index_html.exists():
        ASCIIColors.yellow("\n" + "=" * 80)
        ASCIIColors.yellow("WARNING: Frontend Not Built")
        ASCIIColors.yellow("=" * 80)
        ASCIIColors.yellow("The WebUI frontend has not been built yet.")
        ASCIIColors.yellow("The API server will start without the WebUI interface.")
        ASCIIColors.yellow(
            "\nTo enable WebUI, build the frontend using these commands:\n"
        )
        ASCIIColors.cyan("    cd lightrag_webui")
        ASCIIColors.cyan("    bun install --frozen-lockfile")
        ASCIIColors.cyan("    bun run build")
        ASCIIColors.cyan("    cd ..")
        ASCIIColors.yellow("\nThen restart the service.\n")
        ASCIIColors.cyan(
            "Note: Make sure you have Bun installed. Visit https://bun.sh for installation."
        )
        ASCIIColors.yellow("=" * 80 + "\n")
        return (False, False)  # Assets don't exist, not outdated

    # 2. Check if this is a development environment (source directory exists)
    try:
        source_dir = Path(__file__).parent.parent.parent / "lightrag_webui"
        src_dir = source_dir / "src"

        # Determine if this is a development environment: source directory exists and contains src directory
        if not source_dir.exists() or not src_dir.exists():
            # Production environment, skip source code check
            logger.debug(
                "Production environment detected, skipping source freshness check"
            )
            return (True, False)  # Assets exist, not outdated (prod environment)

        # Development environment, perform source code timestamp check
        logger.debug("Development environment detected, checking source freshness")

        # Source code file extensions (files to check)
        source_extensions = {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",  # TypeScript/JavaScript
            ".css",
            ".scss",
            ".sass",
            ".less",  # Style files
            ".json",
            ".jsonc",  # Configuration/data files
            ".html",
            ".htm",  # Template files
            ".md",
            ".mdx",  # Markdown
        }

        # Key configuration files (in lightrag_webui root directory)
        key_files = [
            source_dir / "package.json",
            source_dir / "bun.lock",
            source_dir / "vite.config.ts",
            source_dir / "tsconfig.json",
            source_dir / "tailraid.config.js",
            source_dir / "index.html",
        ]

        # Get the latest modification time of source code
        latest_source_time = 0

        # Check source code files in src directory
        for file_path in src_dir.rglob("*"):
            if file_path.is_file():
                # Only check source code files, ignore temporary files and logs
                if file_path.suffix.lower() in source_extensions:
                    mtime = file_path.stat().st_mtime
                    latest_source_time = max(latest_source_time, mtime)

        # Check key configuration files
        for key_file in key_files:
            if key_file.exists():
                mtime = key_file.stat().st_mtime
                latest_source_time = max(latest_source_time, mtime)

        # Get build time
        build_time = index_html.stat().st_mtime

        # Compare timestamps (5 second tolerance to avoid file system time precision issues)
        if latest_source_time > build_time + 5:
            ASCIIColors.yellow("\n" + "=" * 80)
            ASCIIColors.yellow("WARNING: Frontend Source Code Has Been Updated")
            ASCIIColors.yellow("=" * 80)
            ASCIIColors.yellow(
                "The frontend source code is newer than the current build."
            )
            ASCIIColors.yellow(
                "This might happen after 'git pull' or manual code changes.\n"
            )
            ASCIIColors.cyan(
                "Recommended: Rebuild the frontend to use the latest changes:"
            )
            ASCIIColors.cyan("    cd lightrag_webui")
            ASCIIColors.cyan("    bun install --frozen-lockfile")
            ASCIIColors.cyan("    bun run build")
            ASCIIColors.cyan("    cd ..")
            ASCIIColors.yellow("\nThe server will continue with the current build.")
            ASCIIColors.yellow("=" * 80 + "\n")
            return (True, True)  # Assets exist, outdated
        else:
            logger.info("Frontend build is up-to-date")
            return (True, False)  # Assets exist, up-to-date

    except Exception as e:
        # If check fails, log warning but don't affect startup
        logger.warning(f"Failed to check frontend source freshness: {e}")
        return (True, False)  # Assume assets exist and up-to-date on error


def create_app(args):
    # Check frontend build first and get status
    webui_assets_exist, is_frontend_outdated = check_frontend_build()

    # Create unified API version display with warning symbol if frontend is outdated
    api_version_display = (
        f"{__api_version__}⚠️" if is_frontend_outdated else __api_version__
    )

    # Setup logging
    logger.setLevel(args.log_level)
    set_verbose_debug(args.verbose)

    # Create configuration cache (this will output configuration logs)
    config_cache = LLMConfigCache(args)

    # Verify that bindings are correctly setup
    if args.llm_binding not in [
        "lollms",
        "ollama",
        "openai",
        "azure_openai",
        "aws_bedrock",
        "gemini",
    ]:
        raise Exception("llm binding not supported")

    if args.embedding_binding not in [
        "lollms",
        "ollama",
        "openai",
        "azure_openai",
        "aws_bedrock",
        "jina",
        "gemini",
    ]:
        raise Exception("embedding binding not supported")

    # Set default hosts if not provided
    if args.llm_binding_host is None:
        args.llm_binding_host = get_default_host(args.llm_binding)

    if args.embedding_binding_host is None:
        args.embedding_binding_host = get_default_host(args.embedding_binding)

    # Add SSL validation
    if args.ssl:
        if not args.ssl_certfile or not args.ssl_keyfile:
            raise Exception(
                "SSL certificate and key files must be provided when SSL is enabled"
            )
        if not os.path.exists(args.ssl_certfile):
            raise Exception(f"SSL certificate file not found: {args.ssl_certfile}")
        if not os.path.exists(args.ssl_keyfile):
            raise Exception(f"SSL key file not found: {args.ssl_keyfile}")

    # Check if API key is provided either through env var or args
    api_key = os.getenv("LIGHTRAG_API_KEY") or args.key

    kb_separator = (getattr(args, "kb_separator", None) or "__").strip() or "__"
    default_workspace_id = getattr(
        args, "default_workspace_id", None
    ) or args.workspace or "default"
    default_kb_id = getattr(args, "default_kb_id", None) or "default"
    default_runtime_workspace = args.workspace
    if args.enable_kb_isolation:
        default_runtime_workspace = (
            f"{default_workspace_id}{kb_separator}{default_kb_id}"
        )
        if getattr(args, "workers", 1) > 1:
            logger.warning(
                "KB isolation is enabled while workers=%s. The current KB registry is file-backed and "
                "best operated as a single writer during migration/initial rollout. Prefer one worker "
                "or avoid concurrent KB metadata mutations across workers.",
                args.workers,
            )
        manual_migration_backends = collect_manual_kb_isolation_migration_backends(
            args
        )
        if manual_migration_backends:
            logger.warning(
                "KB isolation is enabled with backend(s) that do not yet have scripted "
                "WS5 migration coverage: %s. New deployments are fine, but existing "
                "deployments should follow docs/platform-v2/ws5-migration-validation-rollout.md "
                "and complete backend-specific migration planning before enabling the flag.",
                ", ".join(manual_migration_backends),
            )

    # Initialize document manager with workspace support for data isolation
    doc_manager = DocumentManager(
        args.input_dir,
        workspace=default_runtime_workspace,
    )

    async def initialize_rag_runtime(rag_instance: LightRAG) -> None:
        """Initialize the storage/runtime state for one LightRAG instance."""
        await rag_instance.initialize_storages()
        await rag_instance.check_and_migrate_data()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Lifespan context manager for startup and shutdown events"""
        # Store background tasks
        app.state.background_tasks = set()
        app.state.db_ready = False

        try:
            if args.use_db_auth:
                from lightrag.api.db import init_db
                from lightrag.api.identity_store import bootstrap_identity_store

                try:
                    await init_db(args.db_url)
                    seed_summary = await bootstrap_identity_store(
                        args.auth_accounts,
                        default_workspace_id=app.state.default_workspace_id,
                        default_workspace_name="Default workspace",
                    )
                    app.state.db_ready = True
                    app.state.identity_schema_ready = True
                    app.state.env_account_seed_summary = seed_summary.to_dict()
                except Exception as exc:
                    raise RuntimeError(
                        "Failed to initialize the Platform V2 DB/auth scaffold. "
                        "Check DB_URL, AUTH_ACCOUNTS formatting, and async DB dependencies."
                    ) from exc

            # Initialize database connections
            # Note: initialize_storages() now auto-initializes pipeline_status for rag.workspace
            await initialize_rag_runtime(rag)

            ASCIIColors.green("\nServer is ready to accept connections! 🚀\n")

            yield

        finally:
            if args.use_db_auth:
                from lightrag.api.db import close_db

                await close_db()
                app.state.db_ready = False
                app.state.identity_schema_ready = False

            # Clean up database connections
            if args.enable_kb_isolation and getattr(app.state, "rag_factory", None):
                await app.state.rag_factory.finalize_all()
            else:
                await rag.finalize_storages()

            if "LIGHTRAG_GUNICORN_MODE" not in os.environ:
                # Only perform cleanup in Uvicorn single-process mode
                logger.debug("Unvicorn Mode: finalizing shared storage...")
                finalize_share_data()
            else:
                # In Gunicorn mode with preload_app=True, cleanup is handled by on_exit hooks
                logger.debug(
                    "Gunicorn Mode: postpone shared storage finalization to master process"
                )

    # Initialize FastAPI
    base_description = (
        "Providing API for LightRAG core, Web UI and Ollama Model Emulation"
    )
    swagger_description = (
        base_description
        + (" (API-Key Enabled)" if api_key else "")
        + "\n\n[View ReDoc documentation](/redoc)"
    )
    app_kwargs = {
        "title": "LightRAG Server API",
        "description": swagger_description,
        "version": __api_version__,
        "openapi_url": "/openapi.json",  # Explicitly set OpenAPI schema URL
        "docs_url": None,  # Disable default docs, we'll create custom endpoint
        "redoc_url": "/redoc",  # Explicitly set redoc URL
        "lifespan": lifespan,
    }

    # Configure Swagger UI parameters
    # Enable persistAuthorization and tryItOutEnabled for better user experience
    app_kwargs["swagger_ui_parameters"] = {
        "persistAuthorization": True,
        "tryItOutEnabled": True,
    }

    app = FastAPI(**app_kwargs)
    install_platform_state(app, args)

    # Add custom validation error handler for /query/data endpoint
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        # Check if this is a request to /query/data endpoint
        if request.url.path.endswith("/query/data"):
            # Extract error details
            error_details = []
            for error in exc.errors():
                field_path = " -> ".join(str(loc) for loc in error["loc"])
                error_details.append(f"{field_path}: {error['msg']}")

            error_message = "; ".join(error_details)

            # Return in the expected format for /query/data
            return JSONResponse(
                status_code=400,
                content={
                    "status": "failure",
                    "message": f"Validation error: {error_message}",
                    "data": {},
                    "metadata": {},
                },
            )
        else:
            # For other endpoints, return the default FastAPI validation error
            return JSONResponse(status_code=422, content={"detail": exc.errors()})

    def get_cors_origins():
        """Get allowed origins from global_args
        Returns a list of allowed origins, defaults to ["*"] if not set
        """
        origins_str = global_args.cors_origins
        if origins_str == "*":
            return ["*"]
        return [origin.strip() for origin in origins_str.split(",")]

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-New-Token"
        ],  # Expose token renewal header for cross-origin requests
    )

    # Create combined auth dependency for all endpoints
    combined_auth = get_combined_auth_dependency(api_key)

    @app.middleware("http")
    async def attach_request_context(request: Request, call_next):
        # Initialise the auth fields. We MUST NOT build the full
        # RequestContext here: it would cache with db_user_id=None, and
        # the subsequent ``combined_auth`` dep that populates
        # request.state.db_user_id would be ignored by later
        # ``get_request_context(request)`` calls (the cache wins).
        # Leave context construction to the first consumer after auth.
        request.state.db_user_id = None
        request.state.token_info = None
        # Pre-resolve the workspace / kb ids purely for downstream
        # observability hooks that read request.state directly; this
        # does not build or cache a RequestContext.
        request.state.workspace_id = resolve_workspace_id(request)
        request.state.kb_id = resolve_kb_id(request)
        response = await call_next(request)
        return response

    def get_workspace_from_request(request: Request) -> str | None:
        """
        Extract workspace from HTTP request header or use default.

        This enables multi-workspace API support by checking the custom
        'LIGHTRAG-WORKSPACE' header. If not present, falls back to the
        server's default workspace configuration.

        Args:
            request: FastAPI Request object

        Returns:
            Workspace identifier (may be empty string for global namespace)
        """
        # Check custom header first
        workspace = request.headers.get("LIGHTRAG-WORKSPACE", "").strip()

        if not workspace:
            workspace = None
        else:
            sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", workspace)
            if sanitized != workspace:
                logger.warning(
                    f"Workspace header '{workspace}' contains invalid characters. "
                    f"Sanitized to '{sanitized}'."
                )
                workspace = sanitized

        return workspace

    # Create working directory if it doesn't exist
    Path(args.working_dir).mkdir(parents=True, exist_ok=True)

    def create_optimized_openai_llm_func(
        config_cache: LLMConfigCache, args, llm_timeout: int
    ):
        """Create optimized OpenAI LLM function with pre-processed configuration"""

        async def optimized_openai_alike_model_complete(
            prompt,
            system_prompt=None,
            history_messages=None,
            keyword_extraction=False,
            **kwargs,
        ) -> str:
            from lightrag.llm.openai import openai_complete_if_cache

            keyword_extraction = kwargs.pop("keyword_extraction", None)
            if keyword_extraction:
                kwargs["response_format"] = GPTKeywordExtractionFormat
            if history_messages is None:
                history_messages = []

            # Use pre-processed configuration to avoid repeated parsing
            kwargs["timeout"] = llm_timeout
            if config_cache.openai_llm_options:
                kwargs.update(config_cache.openai_llm_options)

            return await openai_complete_if_cache(
                args.llm_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                base_url=args.llm_binding_host,
                api_key=args.llm_binding_api_key,
                **kwargs,
            )

        return optimized_openai_alike_model_complete

    def create_optimized_azure_openai_llm_func(
        config_cache: LLMConfigCache, args, llm_timeout: int
    ):
        """Create optimized Azure OpenAI LLM function with pre-processed configuration"""

        async def optimized_azure_openai_model_complete(
            prompt,
            system_prompt=None,
            history_messages=None,
            keyword_extraction=False,
            **kwargs,
        ) -> str:
            from lightrag.llm.azure_openai import azure_openai_complete_if_cache

            keyword_extraction = kwargs.pop("keyword_extraction", None)
            if keyword_extraction:
                kwargs["response_format"] = GPTKeywordExtractionFormat
            if history_messages is None:
                history_messages = []

            # Use pre-processed configuration to avoid repeated parsing
            kwargs["timeout"] = llm_timeout
            if config_cache.openai_llm_options:
                kwargs.update(config_cache.openai_llm_options)

            return await azure_openai_complete_if_cache(
                args.llm_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                base_url=args.llm_binding_host,
                api_key=os.getenv("AZURE_OPENAI_API_KEY", args.llm_binding_api_key),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
                **kwargs,
            )

        return optimized_azure_openai_model_complete

    def create_optimized_gemini_llm_func(
        config_cache: LLMConfigCache, args, llm_timeout: int
    ):
        """Create optimized Gemini LLM function with cached configuration"""

        async def optimized_gemini_model_complete(
            prompt,
            system_prompt=None,
            history_messages=None,
            keyword_extraction=False,
            **kwargs,
        ) -> str:
            from lightrag.llm.gemini import gemini_complete_if_cache

            if history_messages is None:
                history_messages = []

            # Use pre-processed configuration to avoid repeated parsing
            kwargs["timeout"] = llm_timeout
            if (
                config_cache.gemini_llm_options is not None
                and "generation_config" not in kwargs
            ):
                kwargs["generation_config"] = dict(config_cache.gemini_llm_options)

            return await gemini_complete_if_cache(
                args.llm_model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages,
                api_key=args.llm_binding_api_key,
                base_url=args.llm_binding_host,
                keyword_extraction=keyword_extraction,
                **kwargs,
            )

        return optimized_gemini_model_complete

    def create_llm_model_func(binding: str):
        """
        Create LLM model function based on binding type.
        Uses optimized functions for OpenAI bindings and lazy import for others.
        """
        try:
            if binding == "lollms":
                from lightrag.llm.lollms import lollms_model_complete

                return lollms_model_complete
            elif binding == "ollama":
                from lightrag.llm.ollama import ollama_model_complete

                return ollama_model_complete
            elif binding == "aws_bedrock":
                return bedrock_model_complete  # Already defined locally
            elif binding == "azure_openai":
                # Use optimized function with pre-processed configuration
                return create_optimized_azure_openai_llm_func(
                    config_cache, args, llm_timeout
                )
            elif binding == "gemini":
                return create_optimized_gemini_llm_func(config_cache, args, llm_timeout)
            else:  # openai and compatible
                # Use optimized function with pre-processed configuration
                return create_optimized_openai_llm_func(config_cache, args, llm_timeout)
        except ImportError as e:
            raise Exception(f"Failed to import {binding} LLM binding: {e}")

    def create_llm_model_kwargs(binding: str, args, llm_timeout: int) -> dict:
        """
        Create LLM model kwargs based on binding type.
        Uses lazy import for binding-specific options.
        """
        if binding in ["lollms", "ollama"]:
            try:
                from lightrag.llm.binding_options import OllamaLLMOptions

                return {
                    "host": args.llm_binding_host,
                    "timeout": llm_timeout,
                    "options": OllamaLLMOptions.options_dict(args),
                    "api_key": args.llm_binding_api_key,
                }
            except ImportError as e:
                raise Exception(f"Failed to import {binding} options: {e}")
        return {}

    def create_optimized_embedding_function(
        config_cache: LLMConfigCache, binding, model, host, api_key, args
    ) -> EmbeddingFunc:
        """
        Create optimized embedding function and return an EmbeddingFunc instance
        with proper max_token_size inheritance from provider defaults.

        This function:
        1. Imports the provider embedding function
        2. Extracts max_token_size and embedding_dim from provider if it's an EmbeddingFunc
        3. Creates an optimized wrapper that calls the underlying function directly (avoiding double-wrapping)
        4. Returns a properly configured EmbeddingFunc instance

        Configuration Rules:
        - When EMBEDDING_MODEL is not set: Uses provider's default model and dimension
          (e.g., jina-embeddings-v4 with 2048 dims, text-embedding-3-small with 1536 dims)
        - When EMBEDDING_MODEL is set to a custom model: User MUST also set EMBEDDING_DIM
          to match the custom model's dimension (e.g., for jina-embeddings-v3, set EMBEDDING_DIM=1024)

        Note: The embedding_dim parameter is automatically injected by EmbeddingFunc wrapper
        when send_dimensions=True (enabled for Jina and Gemini bindings). This wrapper calls
        the underlying provider function directly (.func) to avoid double-wrapping, so we must
        explicitly pass embedding_dim to the provider's underlying function.
        """

        # Step 1: Import provider function and extract default attributes
        provider_func = None
        provider_max_token_size = None
        provider_embedding_dim = None

        try:
            if binding == "openai":
                from lightrag.llm.openai import openai_embed

                provider_func = openai_embed
            elif binding == "ollama":
                from lightrag.llm.ollama import ollama_embed

                provider_func = ollama_embed
            elif binding == "gemini":
                from lightrag.llm.gemini import gemini_embed

                provider_func = gemini_embed
            elif binding == "jina":
                from lightrag.llm.jina import jina_embed

                provider_func = jina_embed
            elif binding == "azure_openai":
                from lightrag.llm.azure_openai import azure_openai_embed

                provider_func = azure_openai_embed
            elif binding == "aws_bedrock":
                from lightrag.llm.bedrock import bedrock_embed

                provider_func = bedrock_embed
            elif binding == "lollms":
                from lightrag.llm.lollms import lollms_embed

                provider_func = lollms_embed

            # Extract attributes if provider is an EmbeddingFunc
            if provider_func and isinstance(provider_func, EmbeddingFunc):
                provider_max_token_size = provider_func.max_token_size
                provider_embedding_dim = provider_func.embedding_dim
                logger.debug(
                    f"Extracted from {binding} provider: "
                    f"max_token_size={provider_max_token_size}, "
                    f"embedding_dim={provider_embedding_dim}"
                )
        except ImportError as e:
            logger.warning(f"Could not import provider function for {binding}: {e}")

        # Step 2: Apply priority (user config > provider default)
        # For max_token_size: explicit env var > provider default > None
        final_max_token_size = args.embedding_token_limit or provider_max_token_size
        # For embedding_dim: user config (always has value) takes priority
        # Only use provider default if user config is explicitly None (which shouldn't happen)
        final_embedding_dim = (
            args.embedding_dim if args.embedding_dim else provider_embedding_dim
        )

        # Step 3: Create optimized embedding function (calls underlying function directly)
        # Note: When model is None, each binding will use its own default model
        async def optimized_embedding_function(texts, embedding_dim=None):
            try:
                if binding == "lollms":
                    from lightrag.llm.lollms import lollms_embed

                    # Get real function, skip EmbeddingFunc wrapper if present
                    actual_func = (
                        lollms_embed.func
                        if isinstance(lollms_embed, EmbeddingFunc)
                        else lollms_embed
                    )
                    # lollms embed_model is not used (server uses configured vectorizer)
                    # Only pass base_url and api_key
                    return await actual_func(texts, base_url=host, api_key=api_key)
                elif binding == "ollama":
                    from lightrag.llm.ollama import ollama_embed

                    # Get real function, skip EmbeddingFunc wrapper if present
                    actual_func = (
                        ollama_embed.func
                        if isinstance(ollama_embed, EmbeddingFunc)
                        else ollama_embed
                    )

                    # Use pre-processed configuration if available
                    if config_cache.ollama_embedding_options is not None:
                        ollama_options = config_cache.ollama_embedding_options
                    else:
                        from lightrag.llm.binding_options import OllamaEmbeddingOptions

                        ollama_options = OllamaEmbeddingOptions.options_dict(args)

                    # Pass embed_model only if provided, let function use its default (bge-m3:latest)
                    kwargs = {
                        "texts": texts,
                        "host": host,
                        "api_key": api_key,
                        "options": ollama_options,
                    }
                    if model:
                        kwargs["embed_model"] = model
                    return await actual_func(**kwargs)
                elif binding == "azure_openai":
                    from lightrag.llm.azure_openai import azure_openai_embed

                    actual_func = (
                        azure_openai_embed.func
                        if isinstance(azure_openai_embed, EmbeddingFunc)
                        else azure_openai_embed
                    )
                    # Pass model only if provided, let function use its default otherwise
                    kwargs = {
                        "texts": texts,
                        "api_key": api_key,
                        "embedding_dim": embedding_dim,
                    }
                    if model:
                        kwargs["model"] = model
                    return await actual_func(**kwargs)
                elif binding == "aws_bedrock":
                    from lightrag.llm.bedrock import bedrock_embed

                    actual_func = (
                        bedrock_embed.func
                        if isinstance(bedrock_embed, EmbeddingFunc)
                        else bedrock_embed
                    )
                    # Pass model only if provided, let function use its default otherwise
                    kwargs = {"texts": texts}
                    if model:
                        kwargs["model"] = model
                    return await actual_func(**kwargs)
                elif binding == "jina":
                    from lightrag.llm.jina import jina_embed

                    actual_func = (
                        jina_embed.func
                        if isinstance(jina_embed, EmbeddingFunc)
                        else jina_embed
                    )
                    # Pass model only if provided, let function use its default (jina-embeddings-v4)
                    kwargs = {
                        "texts": texts,
                        "embedding_dim": embedding_dim,
                        "base_url": host,
                        "api_key": api_key,
                    }
                    if model:
                        kwargs["model"] = model
                    return await actual_func(**kwargs)
                elif binding == "gemini":
                    from lightrag.llm.gemini import gemini_embed

                    actual_func = (
                        gemini_embed.func
                        if isinstance(gemini_embed, EmbeddingFunc)
                        else gemini_embed
                    )

                    # Use pre-processed configuration if available
                    if config_cache.gemini_embedding_options is not None:
                        gemini_options = config_cache.gemini_embedding_options
                    else:
                        from lightrag.llm.binding_options import GeminiEmbeddingOptions

                        gemini_options = GeminiEmbeddingOptions.options_dict(args)

                    # Pass model only if provided, let function use its default (gemini-embedding-001)
                    kwargs = {
                        "texts": texts,
                        "base_url": host,
                        "api_key": api_key,
                        "embedding_dim": embedding_dim,
                        "task_type": gemini_options.get(
                            "task_type", "RETRIEVAL_DOCUMENT"
                        ),
                    }
                    if model:
                        kwargs["model"] = model
                    return await actual_func(**kwargs)
                else:  # openai and compatible
                    from lightrag.llm.openai import openai_embed

                    actual_func = (
                        openai_embed.func
                        if isinstance(openai_embed, EmbeddingFunc)
                        else openai_embed
                    )
                    # Pass model only if provided, let function use its default (text-embedding-3-small)
                    kwargs = {
                        "texts": texts,
                        "base_url": host,
                        "api_key": api_key,
                        "embedding_dim": embedding_dim,
                    }
                    if model:
                        kwargs["model"] = model
                    return await actual_func(**kwargs)
            except ImportError as e:
                raise Exception(f"Failed to import {binding} embedding: {e}")

        # Step 4: Wrap in EmbeddingFunc and return
        embedding_func_instance = EmbeddingFunc(
            embedding_dim=final_embedding_dim,
            func=optimized_embedding_function,
            max_token_size=final_max_token_size,
            send_dimensions=False,  # Will be set later based on binding requirements
            model_name=model,
        )

        # Log final embedding configuration
        logger.info(
            f"Embedding config: binding={binding} model={model} "
            f"embedding_dim={final_embedding_dim} max_token_size={final_max_token_size}"
        )

        return embedding_func_instance

    llm_timeout = get_env_value("LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT, int)
    embedding_timeout = get_env_value(
        "EMBEDDING_TIMEOUT", DEFAULT_EMBEDDING_TIMEOUT, int
    )

    async def bedrock_model_complete(
        prompt,
        system_prompt=None,
        history_messages=None,
        keyword_extraction=False,
        **kwargs,
    ) -> str:
        # Lazy import
        from lightrag.llm.bedrock import bedrock_complete_if_cache

        keyword_extraction = kwargs.pop("keyword_extraction", None)
        if keyword_extraction:
            kwargs["response_format"] = GPTKeywordExtractionFormat
        if history_messages is None:
            history_messages = []

        # Use global temperature for Bedrock
        kwargs["temperature"] = get_env_value("BEDROCK_LLM_TEMPERATURE", 1.0, float)

        return await bedrock_complete_if_cache(
            args.llm_model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages,
            **kwargs,
        )

    # Create embedding function with optimized configuration and max_token_size inheritance
    import inspect

    # Create the EmbeddingFunc instance (now returns complete EmbeddingFunc with max_token_size)
    embedding_func = create_optimized_embedding_function(
        config_cache=config_cache,
        binding=args.embedding_binding,
        model=args.embedding_model,
        host=args.embedding_binding_host,
        api_key=args.embedding_binding_api_key,
        args=args,
    )

    # Get embedding_send_dim from centralized configuration
    embedding_send_dim = args.embedding_send_dim

    # Check if the underlying function signature has embedding_dim parameter
    sig = inspect.signature(embedding_func.func)
    has_embedding_dim_param = "embedding_dim" in sig.parameters

    # Determine send_dimensions value based on binding type
    # Jina and Gemini REQUIRE dimension parameter (forced to True)
    # OpenAI and others: controlled by EMBEDDING_SEND_DIM environment variable
    if args.embedding_binding in ["jina", "gemini"]:
        # Jina and Gemini APIs require dimension parameter - always send it
        send_dimensions = has_embedding_dim_param
        dimension_control = f"forced by {args.embedding_binding.title()} API"
    else:
        # For OpenAI and other bindings, respect EMBEDDING_SEND_DIM setting
        send_dimensions = embedding_send_dim and has_embedding_dim_param
        if send_dimensions or not embedding_send_dim:
            dimension_control = "by env var"
        else:
            dimension_control = "by not hasparam"

    # Set send_dimensions on the EmbeddingFunc instance
    embedding_func.send_dimensions = send_dimensions

    logger.info(
        f"Send embedding dimension: {send_dimensions} {dimension_control} "
        f"(dimensions={embedding_func.embedding_dim}, has_param={has_embedding_dim_param}, "
        f"binding={args.embedding_binding})"
    )

    # Log max_token_size source
    if embedding_func.max_token_size:
        source = (
            "env variable"
            if args.embedding_token_limit
            else f"{args.embedding_binding} provider default"
        )
        logger.info(
            f"Embedding max_token_size: {embedding_func.max_token_size} (from {source})"
        )
    else:
        logger.info(
            "Embedding max_token_size: None (Embedding token limit is disabled)."
        )

    # Configure rerank function based on args.rerank_bindingparameter
    rerank_model_func = None
    if args.rerank_binding != "null":
        from lightrag.rerank import cohere_rerank, jina_rerank, ali_rerank

        # Map rerank binding to corresponding function
        rerank_functions = {
            "cohere": cohere_rerank,
            "jina": jina_rerank,
            "aliyun": ali_rerank,
        }

        # Select the appropriate rerank function based on binding
        selected_rerank_func = rerank_functions.get(args.rerank_binding)
        if not selected_rerank_func:
            logger.error(f"Unsupported rerank binding: {args.rerank_binding}")
            raise ValueError(f"Unsupported rerank binding: {args.rerank_binding}")

        # Get default values from selected_rerank_func if args values are None
        if args.rerank_model is None or args.rerank_binding_host is None:
            sig = inspect.signature(selected_rerank_func)

            # Set default model if args.rerank_model is None
            if args.rerank_model is None and "model" in sig.parameters:
                default_model = sig.parameters["model"].default
                if default_model != inspect.Parameter.empty:
                    args.rerank_model = default_model

            # Set default base_url if args.rerank_binding_host is None
            if args.rerank_binding_host is None and "base_url" in sig.parameters:
                default_base_url = sig.parameters["base_url"].default
                if default_base_url != inspect.Parameter.empty:
                    args.rerank_binding_host = default_base_url

        async def server_rerank_func(
            query: str, documents: list, top_n: int = None, extra_body: dict = None
        ):
            """Server rerank function with configuration from environment variables"""
            # Prepare kwargs for rerank function
            kwargs = {
                "query": query,
                "documents": documents,
                "top_n": top_n,
                "api_key": args.rerank_binding_api_key,
                "model": args.rerank_model,
                "base_url": args.rerank_binding_host,
            }

            # Add Cohere-specific parameters if using cohere binding
            if args.rerank_binding == "cohere":
                # Enable chunking if configured (useful for models with token limits like ColBERT)
                kwargs["enable_chunking"] = (
                    os.getenv("RERANK_ENABLE_CHUNKING", "false").lower() == "true"
                )
                kwargs["max_tokens_per_doc"] = int(
                    os.getenv("RERANK_MAX_TOKENS_PER_DOC", "4096")
                )

            return await selected_rerank_func(**kwargs, extra_body=extra_body)

        rerank_model_func = server_rerank_func
        logger.info(
            f"Reranking is enabled: {args.rerank_model or 'default model'} using {args.rerank_binding} provider"
        )
    else:
        logger.info("Reranking is disabled")

    # Create ollama_server_infos from command line arguments
    from lightrag.api.config import OllamaServerInfos

    ollama_server_infos = OllamaServerInfos(
        name=args.simulated_model_name, tag=args.simulated_model_tag
    )

    # Build a query-time LLM function with enable_thinking=true when the
    # indexing function has enable_thinking=false. This lets qwen3.6-plus
    # (and similar reasoning models) use fast no-thinking mode during
    # entity extraction (high volume, cost-sensitive) and full reasoning
    # during answer generation (low volume, quality-sensitive).
    #
    # Only creates a separate function when OPENAI_LLM_EXTRA_BODY contains
    # enable_thinking=false AND QUERY_ENABLE_THINKING is explicitly set to
    # true. Otherwise query_llm_model_func stays None and the query path
    # falls back to the same llm_model_func used by indexing.
    query_llm_model_func = None
    query_enable_thinking = get_env_value("QUERY_ENABLE_THINKING", False, bool)
    if query_enable_thinking and args.llm_binding in ["openai", "azure_openai"]:
        # Deep-copy the config_cache's openai_llm_options and override
        # extra_body to enable thinking for the query path.
        import copy

        query_config_cache = copy.copy(config_cache)
        query_options = dict(config_cache.openai_llm_options or {})
        indexing_extra_body = query_options.get("extra_body") or {}
        query_extra_body = dict(indexing_extra_body)
        query_extra_body["enable_thinking"] = True
        query_options["extra_body"] = query_extra_body
        query_config_cache.openai_llm_options = query_options
        logger.info(
            f"Query LLM will use enable_thinking=true "
            f"(indexing uses {indexing_extra_body.get('enable_thinking', 'default')})"
        )

        if args.llm_binding == "azure_openai":
            query_llm_model_func = create_optimized_azure_openai_llm_func(
                query_config_cache, args, llm_timeout
            )
        else:
            query_llm_model_func = create_optimized_openai_llm_func(
                query_config_cache, args, llm_timeout
            )

    # --- Multimodal pipeline: build image_embedding_func + vision_model_func
    # from .env when configured. These are optional — when None, the LightRAG
    # instance operates in pure-text mode identically to before Phase 1-6.
    image_embedding_func = None
    vision_model_func = None

    mm_emb_model = get_env_value("MM_EMB_MODEL", "", str)
    mm_emb_dim = get_env_value("MM_EMB_DIM", 0, int)
    mm_emb_key = get_env_value("MM_EMB_BINDING_API_KEY", "", str)
    vision_model = get_env_value("VISION_MODEL", "", str)
    vision_host = get_env_value("VISION_BINDING_HOST", "", str)
    vision_key = get_env_value("VISION_BINDING_API_KEY", "", str)

    if mm_emb_model and mm_emb_dim and mm_emb_key:
        try:
            from lightrag.llm.tongyi import tongyi_multimodal_embedding

            mm_emb_host = get_env_value("MM_EMB_BINDING_HOST", "", str) or None
            image_embedding_func = tongyi_multimodal_embedding(
                model=mm_emb_model,
                embedding_dim=mm_emb_dim,
                api_key=mm_emb_key,
                base_url=mm_emb_host,
            )
            logger.info(
                f"Multimodal image embedding enabled: model={mm_emb_model} "
                f"dim={mm_emb_dim}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to initialize multimodal embedding ({mm_emb_model}): {e}. "
                "Image pipeline will be disabled."
            )
            image_embedding_func = None

    if vision_model and vision_host and vision_key:
        try:
            from lightrag.llm.openai import openai_complete_if_cache
            from functools import partial as _partial

            vision_model_func = _partial(
                openai_complete_if_cache,
                model=vision_model,
                base_url=vision_host,
                api_key=vision_key,
            )
            logger.info(
                f"Multimodal vision model enabled: model={vision_model} "
                f"host={vision_host}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to initialize vision model ({vision_model}): {e}. "
                "Image captioning will be disabled."
            )
            vision_model_func = None

    def build_rag_instance(workspace_value: str | None) -> LightRAG:
        """Create a configured LightRAG instance for one logical runtime."""
        try:
            return LightRAG(
                working_dir=args.working_dir,
                workspace=workspace_value,
                llm_model_func=create_llm_model_func(args.llm_binding),
                query_llm_model_func=query_llm_model_func,
                llm_model_name=args.llm_model,
                llm_model_max_async=args.max_async,
                summary_max_tokens=args.summary_max_tokens,
                summary_context_size=args.summary_context_size,
                chunk_token_size=int(args.chunk_size),
                chunk_overlap_token_size=int(args.chunk_overlap_size),
                llm_model_kwargs=create_llm_model_kwargs(
                    args.llm_binding, args, llm_timeout
                ),
                llm_binding=args.llm_binding,
                llm_binding_host=args.llm_binding_host,
                llm_binding_api_key=args.llm_binding_api_key,
                llm_openai_options=dict(config_cache.openai_llm_options or {}),
                embedding_func=embedding_func,
                image_embedding_func=image_embedding_func,
                vision_model_func=vision_model_func,
                default_llm_timeout=llm_timeout,
                default_embedding_timeout=embedding_timeout,
                kv_storage=args.kv_storage,
                graph_storage=args.graph_storage,
                vector_storage=args.vector_storage,
                doc_status_storage=args.doc_status_storage,
                vector_db_storage_cls_kwargs={
                    "cosine_better_than_threshold": args.cosine_threshold
                },
                enable_llm_cache_for_entity_extract=args.enable_llm_cache_for_extract,
                enable_llm_cache=args.enable_llm_cache,
                rerank_model_func=rerank_model_func,
                max_parallel_insert=args.max_parallel_insert,
                max_graph_nodes=args.max_graph_nodes,
                entity_extraction_mode=args.entity_extraction_mode,
                entity_extraction_batch_min_chunks=args.entity_extraction_batch_min_chunks,
                entity_extraction_batch_poll_interval_seconds=args.entity_extraction_batch_poll_interval_seconds,
                entity_extraction_batch_timeout_seconds=args.entity_extraction_batch_timeout_seconds,
                entity_extraction_batch_fallback_to_realtime=args.entity_extraction_batch_fallback_to_realtime,
                addon_params={
                    "language": args.summary_language,
                    "entity_types": args.entity_types,
                },
                ollama_server_infos=ollama_server_infos,
            )
        except Exception as e:
            logger.error(f"Failed to initialize LightRAG: {e}")
            raise

    kb_registry = None
    if args.enable_kb_isolation:
        kb_registry = KnowledgeBaseRegistry(args.working_dir)
        kb_registry.ensure_default_kb(default_workspace_id, default_kb_id)

    # Initialize RAG with unified configuration
    rag = build_rag_instance(default_runtime_workspace)

    rag_factory = None
    if args.enable_kb_isolation:

        async def build_isolated_rag(
            workspace_id: str,
            kb_id: str,
            combined_workspace: str,
            knowledge_base,
        ) -> LightRAG:
            isolated_rag = build_rag_instance(combined_workspace)
            await initialize_rag_runtime(isolated_rag)
            return isolated_rag

        rag_factory = RagFactory(
            builder=build_isolated_rag,
            registry=kb_registry,
            kb_separator=kb_separator,
        )
        rag_factory.prime(default_workspace_id, default_kb_id, rag)

    app.state.kb_registry = kb_registry
    app.state.rag_factory = rag_factory
    app.state.default_rag = rag
    app.state.doc_manager_base_input_dir = args.input_dir
    app.state.default_runtime_workspace = default_runtime_workspace
    app.state.default_doc_manager = doc_manager
    app.state.doc_manager_cache = {default_runtime_workspace: doc_manager}

    # Add routes
    app.include_router(create_document_routes(api_key=api_key))
    app.include_router(create_auth_routes())
    app.include_router(create_workspace_routes(api_key))
    app.include_router(create_membership_routes(api_key))
    app.include_router(create_kb_routes(api_key))
    app.include_router(create_audit_routes(api_key))
    app.include_router(create_query_routes(api_key=api_key, top_k=args.top_k))
    app.include_router(create_graph_routes(api_key=api_key))

    # Multimodal image routes — only registered when the multimodal
    # pipeline is enabled on this LightRAG instance. create_image_routes
    # returns None on text-only deployments, so FastAPI never sees any
    # new endpoints unless image ingestion is actually wired up.
    image_router = None
    if rag.image_blob_store is not None and rag.image_metadata is not None:
        image_router = create_image_routes(api_key=api_key)
    if image_router is not None:
        app.include_router(image_router)
        logger.info("Registered multimodal image routes (/images/*)")

    # Add Ollama API routes
    ollama_api = OllamaAPI(
        top_k=args.top_k,
        api_key=api_key,
        ollama_server_infos=rag.ollama_server_infos,
    )
    app.include_router(ollama_api.router, prefix="/api")

    # Custom Swagger UI endpoint for offline support
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        """Custom Swagger UI HTML with local static files"""
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=app.title + " - Swagger UI",
            oauth2_redirect_url="/docs/oauth2-redirect",
            swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
            swagger_ui_parameters=app.swagger_ui_parameters,
        )

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect():
        """OAuth2 redirect for Swagger UI"""
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/")
    async def redirect_to_webui():
        """Redirect root path based on WebUI availability"""
        if webui_assets_exist:
            return RedirectResponse(url="/webui")
        else:
            return RedirectResponse(url="/docs")

    @app.get("/auth-status")
    async def get_auth_status(request: Request):
        """
        Return authentication capabilities without issuing implicit guest credentials.

        This response is intentionally provider-shaped so future remote identity
        integrations can keep the same login bootstrap contract.
        """
        provider_status = await get_auth_provider(request).get_auth_status(request)
        return {
            **provider_status.to_dict(),
            "core_version": core_version,
            "api_version": api_version_display,
            "webui_title": webui_title,
            "webui_description": webui_description,
        }

    @app.post("/login")
    async def login(
        request: Request,
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
    ):
        from lightrag.api.audit import emit_audit_event

        principal = await get_auth_provider(request).authenticate_password(
            request,
            form_data.username,
            form_data.password,
        )
        if principal is None:
            # Record the failed attempt so operators can spot brute-force
            # patterns. No user_id / role context yet — just the supplied
            # username, which may be garbage.
            await emit_audit_event(
                request,
                action="user:login",
                resource_type="user",
                outcome="denied",
                status_code=401,
                actor_username=str(form_data.username)[:128] or None,
                metadata={"reason": "invalid_credentials"},
            )
            raise HTTPException(status_code=401, detail="Incorrect credentials")

        login_tokens = await issue_login_tokens(
            request,
            response,
            username=principal.username,
            user_id=principal.user_id,
            role=principal.role,
            memberships=principal.memberships,
            metadata=principal.metadata,
        )
        await emit_audit_event(
            request,
            action="user:login",
            resource_type="user",
            resource_id=principal.user_id,
            outcome="success",
            status_code=200,
            actor_user_id=principal.user_id,
            actor_username=principal.username,
            actor_role=principal.role,
            metadata={
                "auth_provider": (principal.metadata or {}).get("auth_provider"),
                "account_source": (principal.metadata or {}).get("account_source"),
            },
        )
        return {
            **login_tokens,
            "auth_mode": "local",
            "core_version": core_version,
            "api_version": api_version_display,
            "webui_title": webui_title,
            "webui_description": webui_description,
        }

    @app.get(
        "/health",
        dependencies=[Depends(combined_auth)],
        summary="Get system health and configuration status",
        description="Returns comprehensive system status including WebUI availability, configuration, and operational metrics",
        response_description="System health status with configuration details",
        responses={
            200: {
                "description": "Successful response with system status",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "healthy",
                            "webui_available": True,
                            "working_directory": "/path/to/working/dir",
                            "input_directory": "/path/to/input/dir",
                            "configuration": {
                                "llm_binding": "openai",
                                "llm_model": "gpt-4",
                                "embedding_binding": "openai",
                                "embedding_model": "text-embedding-ada-002",
                                "workspace": "default",
                            },
                            "auth_mode": "enabled",
                            "pipeline_busy": False,
                            "core_version": "0.0.1",
                            "api_version": "0.0.1",
                        }
                    }
                },
            }
        },
    )
    async def get_status(request: Request):
        """Get current system status including WebUI availability"""
        try:
            request_context = get_request_context(request)
            runtime_default_workspace = get_default_workspace()
            if args.enable_kb_isolation:
                workspace = request_context.workspace_id or default_workspace_id
                pipeline_workspace = (
                    f"{workspace}{kb_separator}{request_context.kb_id or default_kb_id}"
                )
            else:
                default_rag = getattr(request.app.state, "default_rag", None)
                pipeline_workspace = getattr(
                    default_rag, "workspace", runtime_default_workspace
                )

            try:
                pipeline_status = await get_namespace_data(
                    "pipeline_status", workspace=pipeline_workspace
                )
            except Exception as exc:
                from lightrag.exceptions import PipelineNotInitializedError

                if not isinstance(exc, PipelineNotInitializedError):
                    raise

                from lightrag.kg.shared_storage import initialize_pipeline_status

                logger.warning(
                    "Pipeline status namespace '%s' was missing during /health; "
                    "initializing on demand.",
                    pipeline_workspace,
                )
                await initialize_pipeline_status(workspace=pipeline_workspace)
                pipeline_status = await get_namespace_data(
                    "pipeline_status", workspace=pipeline_workspace
                )

            auth_mode = "local"

            # Cleanup expired keyed locks and get status
            keyed_lock_info = cleanup_keyed_lock()

            return {
                "status": "healthy",
                "webui_available": webui_assets_exist,
                "working_directory": str(args.working_dir),
                "input_directory": str(args.input_dir),
                "configuration": {
                    # LLM configuration binding/host address (if applicable)/model (if applicable)
                    "llm_binding": args.llm_binding,
                    "llm_binding_host": args.llm_binding_host,
                    "llm_model": args.llm_model,
                    # embedding model configuration binding/host address (if applicable)/model (if applicable)
                    "embedding_binding": args.embedding_binding,
                    "embedding_binding_host": args.embedding_binding_host,
                    "embedding_model": args.embedding_model,
                    "summary_max_tokens": args.summary_max_tokens,
                    "summary_context_size": args.summary_context_size,
                    "kv_storage": args.kv_storage,
                    "doc_status_storage": args.doc_status_storage,
                    "graph_storage": args.graph_storage,
                    "vector_storage": args.vector_storage,
                    "enable_llm_cache_for_extract": args.enable_llm_cache_for_extract,
                    "enable_llm_cache": args.enable_llm_cache,
                    "workspace": runtime_default_workspace,
                    "max_graph_nodes": args.max_graph_nodes,
                    # Rerank configuration
                    "enable_rerank": rerank_model_func is not None,
                    "rerank_binding": args.rerank_binding,
                    "rerank_model": args.rerank_model if rerank_model_func else None,
                    "rerank_binding_host": args.rerank_binding_host
                    if rerank_model_func
                    else None,
                    # Environment variable status (requested configuration)
                    "summary_language": args.summary_language,
                    "force_llm_summary_on_merge": args.force_llm_summary_on_merge,
                    "max_parallel_insert": args.max_parallel_insert,
                    "cosine_threshold": args.cosine_threshold,
                    "min_rerank_score": args.min_rerank_score,
                    "related_chunk_number": args.related_chunk_number,
                    "max_async": args.max_async,
                    "embedding_func_max_async": args.embedding_func_max_async,
                    "embedding_batch_num": args.embedding_batch_num,
                },
                "auth_mode": auth_mode,
                "platform_features": {
                    "use_db_auth": bool(args.use_db_auth),
                    "enable_kb_isolation": bool(args.enable_kb_isolation),
                    "db_ready": bool(getattr(app.state, "db_ready", False)),
                    "identity_schema_ready": bool(
                        getattr(app.state, "identity_schema_ready", False)
                    ),
                    "env_account_seed_summary": getattr(
                        app.state, "env_account_seed_summary", {}
                    ),
                    "default_workspace_id": getattr(
                        app.state, "default_workspace_id", None
                    ),
                    "default_kb_id": getattr(app.state, "default_kb_id", None),
                    "kb_separator": getattr(app.state, "kb_separator", None),
                    "kb_registry_ready": getattr(app.state, "kb_registry", None)
                    is not None,
                    "rag_factory_ready": getattr(app.state, "rag_factory", None)
                    is not None,
                },
                "pipeline_busy": pipeline_status.get("busy", False),
                "keyed_locks": keyed_lock_info,
                "core_version": core_version,
                "api_version": api_version_display,
                "webui_title": webui_title,
                "webui_description": webui_description,
            }
        except Exception as e:
            logger.error(f"Error getting health status: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    # Custom StaticFiles class for smart caching
    class SmartStaticFiles(StaticFiles):  # Renamed from NoCacheStaticFiles
        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)

            is_html = path.endswith(".html") or response.media_type == "text/html"

            if is_html:
                response.headers["Cache-Control"] = (
                    "no-cache, no-store, must-revalidate"
                )
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            elif (
                "/assets/" in path
            ):  # Assets (JS, CSS, images, fonts) generated by Vite with hash in filename
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            # Add other rules here if needed for non-HTML, non-asset files

            # Ensure correct Content-Type
            if path.endswith(".js"):
                response.headers["Content-Type"] = "application/javascript"
            elif path.endswith(".css"):
                response.headers["Content-Type"] = "text/css"

            return response

    # Mount Swagger UI static files for offline support
    swagger_static_dir = Path(__file__).parent / "static" / "swagger-ui"
    if swagger_static_dir.exists():
        app.mount(
            "/static/swagger-ui",
            StaticFiles(directory=swagger_static_dir),
            name="swagger-ui-static",
        )

    # Conditionally mount WebUI only if assets exist
    if webui_assets_exist:
        static_dir = Path(__file__).parent / "webui"
        static_dir.mkdir(exist_ok=True)
        app.mount(
            "/webui",
            SmartStaticFiles(
                directory=static_dir, html=True, check_dir=True
            ),  # Use SmartStaticFiles
            name="webui",
        )
        logger.info("WebUI assets mounted at /webui")
    else:
        logger.info("WebUI assets not available, /webui route not mounted")

        # Add redirect for /webui when assets are not available
        @app.get("/webui")
        @app.get("/webui/")
        async def webui_redirect_to_docs():
            """Redirect /webui to /docs when WebUI is not available"""
            return RedirectResponse(url="/docs")

    return app


def get_application(args=None):
    """Factory function for creating the FastAPI application"""
    if args is None:
        args = global_args
    return create_app(args)


def configure_logging():
    """Configure logging for uvicorn startup"""

    # Reset any existing handlers to ensure clean configuration
    for logger_name in ["uvicorn", "uvicorn.access", "uvicorn.error", "lightrag"]:
        logger = logging.getLogger(logger_name)
        logger.handlers = []
        logger.filters = []

    # Get log directory path from environment variable
    log_dir = os.getenv("LOG_DIR", os.getcwd())
    log_file_path = os.path.abspath(os.path.join(log_dir, DEFAULT_LOG_FILENAME))

    print(f"\nLightRAG log file: {log_file_path}\n")
    os.makedirs(os.path.dirname(log_dir), exist_ok=True)

    # Get log file max size and backup count from environment variables
    log_max_bytes = get_env_value("LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES, int)
    log_backup_count = get_env_value("LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT, int)

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(levelname)s: %(message)s",
                },
                "detailed": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                },
                "file": {
                    "formatter": "detailed",
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": log_file_path,
                    "maxBytes": log_max_bytes,
                    "backupCount": log_backup_count,
                    "encoding": "utf-8",
                },
            },
            "loggers": {
                # Configure all uvicorn related loggers
                "uvicorn": {
                    "handlers": ["console", "file"],
                    "level": "INFO",
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["console", "file"],
                    "level": "INFO",
                    "propagate": False,
                    "filters": ["path_filter"],
                },
                "uvicorn.error": {
                    "handlers": ["console", "file"],
                    "level": "INFO",
                    "propagate": False,
                },
                "lightrag": {
                    "handlers": ["console", "file"],
                    "level": "INFO",
                    "propagate": False,
                    "filters": ["path_filter"],
                },
            },
            "filters": {
                "path_filter": {
                    "()": "lightrag.utils.LightragPathFilter",
                },
            },
        }
    )


def check_and_install_dependencies():
    """Check and install required dependencies"""
    required_packages = [
        "uvicorn",
        "tiktoken",
        "fastapi",
        # Add other required packages here
    ]

    for package in required_packages:
        if not pm.is_installed(package):
            print(f"Installing {package}...")
            pm.install(package)
            print(f"{package} installed successfully")


def main():
    # On Windows, ProactorEventLoop (default since Python 3.8) has known
    # race conditions with uvicorn's socket binding that can cause the server
    # to report it's running while the port is never actually bound.
    # Using SelectorEventLoop resolves this issue.
    # See: https://github.com/HKUDS/LightRAG/issues/2438
    if sys.platform == "win32":
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Explicitly initialize configuration for clarity
    # (The proxy will auto-initialize anyway, but this makes intent clear)
    from .config import initialize_config

    initialize_config()

    # Check if running under Gunicorn
    if "GUNICORN_CMD_ARGS" in os.environ:
        # If started with Gunicorn, return directly as Gunicorn will call get_application
        print("Running under Gunicorn - worker management handled by Gunicorn")
        return

    # Check .env file
    if not check_env_file():
        sys.exit(1)

    # Check and install dependencies
    check_and_install_dependencies()

    from multiprocessing import freeze_support

    freeze_support()

    # Configure logging before parsing args
    configure_logging()
    update_uvicorn_mode_config()
    display_splash_screen(global_args)

    # Note: Signal handlers are NOT registered here because:
    # - Uvicorn has built-in signal handling that properly calls lifespan shutdown
    # - Custom signal handlers can interfere with uvicorn's graceful shutdown
    # - Cleanup is handled by the lifespan context manager's finally block

    # Create application instance directly instead of using factory function
    app = create_app(global_args)

    # Start Uvicorn in single process mode
    uvicorn_config = {
        "app": app,  # Pass application instance directly instead of string path
        "host": global_args.host,
        "port": global_args.port,
        "log_config": None,  # Disable default config
    }

    if global_args.ssl:
        uvicorn_config.update(
            {
                "ssl_certfile": global_args.ssl_certfile,
                "ssl_keyfile": global_args.ssl_keyfile,
            }
        )

    print(
        f"Starting Uvicorn server in single-process mode on {global_args.host}:{global_args.port}"
    )
    uvicorn.run(**uvicorn_config)


if __name__ == "__main__":
    main()
