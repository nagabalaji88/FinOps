"""Operator-set configuration that overrides the environment, applied without a restart.

`settings` is read once at import, so a value in `.env` cannot be changed on a running
deployment. That is the right behaviour for infrastructure -- a database URL should not move
under a live process -- and the wrong behaviour for the two things an operator actually needs
to change while the platform is up: which model the agents use, and a provider key that has
just been rotated or newly issued.

Precedence is operator, then environment. A row set here wins because someone deliberately
set it through the admin surface; nothing here is *required*, so a deployment that configures
everything through `.env` behaves exactly as it did before.

The cache is per-process and reloaded on write. A multi-process deployment picks up another
worker's change on its next `refresh()`, which the admin endpoints call after every write --
so within one process it is immediate, and across processes it is eventually consistent
rather than instant. Anything needing strict cross-worker immediacy belongs in Vault.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decrypt_value, encrypt_value

log = get_logger("runtime_config")

#: Settings an operator may change at runtime, and the `settings` attribute each falls back to.
MODEL_KEYS: dict[str, str] = {
    "default_model": "default_model",
    "guardrails_model": "guardrails_model",
}

#: Provider -> the environment variable it has always read. A stored key shadows this.
PROVIDER_ENV: dict[str, str] = {
    "openai": "openai_api_key",
    "azure_openai": "azure_openai_api_key",
    "anthropic": "anthropic_api_key",
    "google": "google_api_key",
    "mistral": "mistral_api_key",
    "deepseek": "deepseek_api_key",
    "together": "together_api_key",
    "groq": "groq_api_key",
    "openrouter": "openrouter_api_key",
}


def _secret_name(provider: str) -> str:
    return f"{provider.upper()}_API_KEY"


class RuntimeConfig:
    def __init__(self) -> None:
        self._models: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self._loaded = False

    # --- reads (hot path; must not touch the database) ----------------------
    def api_key(self, provider: str) -> str | None:
        """The key to use for a provider, operator value first.

        Read on every call rather than captured at construction, so a rotated key is picked
        up without a restart and a provider object can be built, logged and serialised
        without ever holding a secret. A blank value is a *missing* credential, not an empty
        one: passing "" reads as no auth to some providers and as a malformed header to
        others, and both fail further downstream with a worse error than "it is unset".
        """
        stored = self._keys.get(provider, "").strip()
        if stored:
            return stored
        attribute = PROVIDER_ENV.get(provider)
        value = getattr(settings, attribute, None) if attribute else None
        return value.strip() or None if isinstance(value, str) else None

    def model(self, name: str) -> str:
        stored = self._models.get(name, "").strip()
        if stored:
            return stored
        return str(getattr(settings, MODEL_KEYS.get(name, name), "") or "")

    @property
    def default_model(self) -> str:
        return self.model("default_model")

    @property
    def guardrails_model(self) -> str:
        return self.model("guardrails_model")

    def key_source(self, provider: str) -> str:
        """Where the key in force came from, for the admin view. Never the value."""
        if self._keys.get(provider, "").strip():
            return "stored"
        return "environment" if self.api_key(provider) else "unset"

    def overrides(self) -> dict[str, Any]:
        return {
            "models": dict(self._models),
            "providers_with_stored_keys": sorted(k for k, v in self._keys.items() if v.strip()),
        }

    # --- writes -------------------------------------------------------------
    async def refresh(self, session: AsyncSession) -> None:
        from app.db.models.identity import PlatformSetting, StoredSecret

        rows = (await session.execute(select(PlatformSetting))).scalars().all()
        self._models = {r.key: r.value for r in rows if r.key in MODEL_KEYS}

        secrets = (
            (await session.execute(select(StoredSecret).where(StoredSecret.category == "provider_key")))
            .scalars()
            .all()
        )
        keys: dict[str, str] = {}
        for row in secrets:
            provider = row.name.removesuffix("_API_KEY").lower()
            try:
                keys[provider] = decrypt_value(row.sealed_value)
            except Exception as exc:  # a key sealed under a rotated JWT_SECRET cannot be read
                log.error("stored_key_unreadable", provider=provider, error=str(exc))
        self._keys = keys
        self._loaded = True

    async def set_model(self, session: AsyncSession, name: str, value: str, *, actor: str) -> None:
        from app.db.models.identity import PlatformSetting

        if name not in MODEL_KEYS:
            raise ValueError(f"unknown model setting '{name}'")
        row = (
            await session.execute(select(PlatformSetting).where(PlatformSetting.key == name))
        ).scalar_one_or_none()
        if row is None:
            session.add(PlatformSetting(key=name, value=value, updated_by=actor))
        else:
            row.value = value
            row.updated_by = actor
        await session.flush()
        self._models[name] = value
        log.info("model_setting_changed", setting=name, value=value or "(cleared)", actor=actor)

    async def set_api_key(self, session: AsyncSession, provider: str, value: str, *, actor: str) -> None:
        from app.db.models.identity import StoredSecret

        if provider not in PROVIDER_ENV:
            raise ValueError(f"unknown provider '{provider}'")
        name = _secret_name(provider)
        row = (
            await session.execute(select(StoredSecret).where(StoredSecret.name == name))
        ).scalar_one_or_none()
        sealed = encrypt_value(value)
        hint = value[-4:] if len(value) >= 4 else ""
        if row is None:
            session.add(
                StoredSecret(
                    name=name,
                    category="provider_key",
                    sealed_value=sealed,
                    hint=hint,
                    backend="database",
                    created_by=actor,
                )
            )
        else:
            row.sealed_value = sealed
            row.hint = hint
            row.category = "provider_key"
        await session.flush()
        self._keys[provider] = value
        # The value never appears here, and the hint is only the last four characters.
        log.info("provider_key_stored", provider=provider, hint=hint, actor=actor)

    async def clear_api_key(self, session: AsyncSession, provider: str, *, actor: str) -> None:
        """Remove the stored key so the environment variable applies again."""
        from app.db.models.identity import StoredSecret

        row = (
            await session.execute(select(StoredSecret).where(StoredSecret.name == _secret_name(provider)))
        ).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
            await session.flush()
        self._keys.pop(provider, None)
        log.info("provider_key_cleared", provider=provider, actor=actor)


runtime_config = RuntimeConfig()
