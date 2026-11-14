"""Developer access: SSH from Home Assistant to the TV over the home network, to add, check and
remove session keys. Imported only once developer access is on and asyncssh is installed.

Home Assistant logs in with its own management key, kept only in the config entry. The TV's host
key is pinned the first time it answers; a changed host key stops every operation until the owner
chooses Forget TV host key in the options.
"""

from __future__ import annotations

import asyncio
import logging
import time

import asyncssh

from . import dev_keys as keys

_LOGGER = logging.getLogger(__name__)
AUTHORIZED_KEYS = "/home/root/.ssh/authorized_keys"
CONNECT_TIMEOUT = 15


class TvError(Exception):
    """The TV could not be reached or refused the change."""


class HostKeyChanged(TvError):
    """The TV answered with a different host key than the pinned one."""


class Tv:
    """One TV. Calls are serialized so concurrent edits of authorized_keys cannot interleave."""

    def __init__(self, host: str, port: int, management_key: str, host_key: str | None) -> None:
        self.host = host
        self.port = port
        self._key = asyncssh.import_private_key(management_key)
        self.host_key = host_key  # OpenSSH public key, or None until first contact
        # Whether Dropbear accepted keys with OPTIONS; None until the first session key.
        self.options_supported: bool | None = None
        self._lock = asyncio.Lock()

    async def _connect(self, client_key: asyncssh.SSHKey | None = None):
        known_hosts = None
        if self.host_key:
            known_hosts = ([asyncssh.import_public_key(self.host_key)], [], [])
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    self.port,
                    username="root",
                    client_keys=[client_key or self._key],
                    known_hosts=known_hosts,
                    agent_path=None,
                    config=None,
                ),
                CONNECT_TIMEOUT,
            )
        except asyncssh.HostKeyNotVerifiable as err:
            raise HostKeyChanged("the TV's host key changed") from err
        except (OSError, asyncssh.Error, TimeoutError) as err:
            raise TvError(f"could not log in to the TV: {err}") from err
        if not self.host_key:
            server_key = conn.get_server_host_key()
            if server_key is not None:
                self.host_key = server_key.export_public_key().decode().strip()
                _LOGGER.info("Pinned the TV's host key")
        return conn

    async def _run(self, conn, command: str, stdin: str | None = None) -> str:
        try:
            result = await conn.run(command, input=stdin, check=True)
        except asyncssh.ProcessError as err:
            raise TvError(f"command failed on the TV: {err.stderr or err}") from err
        return str(result.stdout or "")

    async def _edit(self, conn, **changes) -> dict[str, int]:
        current = await self._run(conn, f"cat {AUTHORIZED_KEYS} 2>/dev/null || true")
        new, remaining = keys.rewrite(current, **changes)
        if new != current:
            tmp = f"{AUTHORIZED_KEYS}.luma-dev"
            await self._run(
                conn,
                f"umask 077; mkdir -p /home/root/.ssh && cat > {tmp} && mv {tmp} {AUTHORIZED_KEYS}",
                stdin=new,
            )
        return remaining

    async def _accepts(self, private_key: str) -> bool:
        try:
            conn = await self._connect(asyncssh.import_private_key(private_key))
        except HostKeyChanged:
            raise
        except TvError:
            return False
        async with conn:
            await self._run(conn, "true")
        return True

    async def install(self, generated: keys.KeyPair, request_id: str, expires_at: int) -> None:
        """Add a session key and check that the TV accepts it. Some Dropbear builds reject
        option-prefixed lines; then the key is added without options."""
        async with self._lock:
            attempts = [True, False] if self.options_supported is not False else [False]
            for options in attempts:
                line = keys.key_line(generated.public_key, request_id, expires_at, options=options)
                async with await self._connect() as conn:
                    await self._edit(conn, remove={request_id}, add=line)
                if await self._accepts(generated.private_key):
                    self.options_supported = options
                    return
                _LOGGER.warning("The TV refused a session key (options=%s)", options)
            async with await self._connect() as conn:
                await self._edit(conn, remove={request_id})
            raise TvError("the TV refused the session key")

    async def remove(self, request_ids: set[str]) -> dict[str, int]:
        async with self._lock, await self._connect() as conn:
            return await self._edit(conn, remove=request_ids)

    async def sweep(self) -> dict[str, int]:
        """Drop expired session keys; returns those still valid, id → expiry."""
        async with self._lock, await self._connect() as conn:
            return await self._edit(conn, now=time.time())
