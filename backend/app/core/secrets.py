"""Secret material resolution: HashiCorp Vault, then env/settings, then encrypted DB rows."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decrypt_value, encrypt_value

log = get_logger("secrets")


class SecretManager:
    def __init__(self) -> None:
        self._vault: Any | None = None
        self._cache: dict[str, str] = {}
        self.backend = "settings"

    async def connect(self) -> None:
        if not (settings.vault_addr and settings.vault_token):
            log.info("secret_backend", backend="settings+db")
            return
        try:
            import hvac

            client = hvac.Client(url=settings.vault_addr, token=settings.vault_token)
            if await asyncio.to_thread(client.is_authenticated):
                self._vault = client
                self.backend = "vault"
                log.info("secret_backend", backend="vault", addr=settings.vault_addr)
            else:
                log.warning("vault_auth_failed")
        except Exception as exc:
            log.warning("vault_unavailable", error=str(exc))

    @property
    def healthy(self) -> bool:
        return self._vault is not None

    async def get(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        if self._vault is not None:
            try:
                resp = await asyncio.to_thread(
                    self._vault.secrets.kv.v2.read_secret_version,
                    path=name,
                    mount_point=settings.vault_mount,
                    raise_on_deleted_version=False,
                )
                value = resp["data"]["data"].get("value")
                if value:
                    self._cache[name] = value
                    return value
            except Exception as exc:
                log.warning("vault_read_failed", secret=name, error=str(exc))
        value = getattr(settings, name.replace("-", "_").lower(), None)
        if isinstance(value, str) and value:
            return value
        return None

    async def put(self, name: str, value: str) -> str:
        """Store a secret; returns the storage backend used."""
        if self._vault is not None:
            await asyncio.to_thread(
                self._vault.secrets.kv.v2.create_or_update_secret,
                path=name,
                secret={"value": value},
                mount_point=settings.vault_mount,
            )
            self._cache[name] = value
            return "vault"
        return "database"

    @staticmethod
    def seal(value: str) -> str:
        return encrypt_value(value)

    @staticmethod
    def unseal(value: str) -> str:
        return decrypt_value(value)

    def invalidate(self, name: str | None = None) -> None:
        if name:
            self._cache.pop(name, None)
        else:
            self._cache.clear()


secret_manager = SecretManager()
