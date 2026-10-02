"""Server-only configuration; never serialize Settings to the browser."""

import os
from dataclasses import dataclass, field
from decimal import Decimal

from dotenv import load_dotenv


@dataclass
class Settings:
    database_url: str = field(default="", repr=False)
    show_source_links: bool = True
    host: str = "127.0.0.1"
    port: int = 8050
    monthly_budget_usd: Decimal = Decimal("100")
    generation_model: str = "gpt-5.6-luna"
    embedding_model: str = "text-embedding-3-small"
    max_output_tokens: int = 1600
    requests_per_minute: int = 5
    requests_per_day: int = 30
    concurrency: int = 2
    cookie_secret: str = field(default="", repr=False)
    # Hosted HTTPS deployments send session cookies only over TLS.
    secure_cookies: bool = False
    trusted_proxy: str = ""
    # Explicit opt-in preserves the deterministic baseline for reproducible checks.
    research_agent_enabled: bool = False
    # Paid source discovery is confined to the separate maintenance MCP server.
    claims_source_search_enabled: bool = False
    # Public semantic questions may search the web after a validated corpus miss.
    web_search_enabled: bool = False
    # Private curated attachments can be mounted independently of application code.
    record_asset_root: str = field(default="", repr=False)
    record_asset_manifest_sha256: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        return cls(
            database_url=os.getenv("OBS_DATABASE_URL", ""),
            show_source_links=os.getenv("OBS_SHOW_SOURCE_LINKS", "true").lower()
            == "true",
            host=os.getenv("OBS_HOST", "127.0.0.1"),
            # Platforms such as Railway assign the listening port through PORT.
            port=int(os.getenv("OBS_PORT") or os.getenv("PORT") or "8050"),
            monthly_budget_usd=Decimal(os.getenv("OBS_MONTHLY_BUDGET_USD", "100")),
            generation_model=os.getenv("OBS_GENERATION_MODEL", "gpt-5.6-luna"),
            embedding_model=os.getenv("OBS_EMBEDDING_MODEL", "text-embedding-3-small"),
            requests_per_minute=int(os.getenv("OBS_REQUESTS_PER_MINUTE", "5")),
            requests_per_day=int(os.getenv("OBS_REQUESTS_PER_DAY", "30")),
            concurrency=int(os.getenv("OBS_CONCURRENCY", "2")),
            cookie_secret=os.getenv("OBS_COOKIE_SECRET", ""),
            secure_cookies=os.getenv("OBS_SECURE_COOKIES", "false").lower() == "true",
            trusted_proxy=os.getenv("OBS_TRUSTED_PROXY", ""),
            research_agent_enabled=os.getenv("OBS_RESEARCH_AGENT_ENABLED", "false").lower() == "true",
            claims_source_search_enabled=os.getenv("OBS_CLAIMS_SOURCE_SEARCH_ENABLED", "false").lower() == "true",
            web_search_enabled=os.getenv("OBS_WEB_SEARCH_ENABLED", "true").lower() == "true",
            record_asset_root=os.getenv("OBS_RECORD_ASSET_ROOT", ""),
            record_asset_manifest_sha256=os.getenv("OBS_RECORD_ASSET_MANIFEST_SHA256", ""),
        )
