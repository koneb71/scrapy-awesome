"""scrapy-playwright browser providers.

`PatchrightBrowserProvider` swaps vanilla Playwright for Patchright (a stealth Chromium build that
patches the CDP leaks bot detectors look for). Select it with::

    PLAYWRIGHT_BROWSER_PROVIDER = "scrapy_awesome.crawl.providers.PatchrightBrowserProvider"
    PLAYWRIGHT_BROWSER_TYPE = "chromium"

Patchright objects are Playwright-compatible, so the rest of scrapy-playwright (PageMethods,
contexts, storage_state) works unchanged.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from scrapy_playwright.handler import Config

logger = logging.getLogger(__name__)


class PatchrightBrowserProvider:
    """Browser provider backed by ``patchright.async_api``."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pw_cm: Any = None
        self._pw: Any = None
        self._browser_type: Any = None

    async def start(self) -> None:
        from patchright.async_api import async_playwright

        self._pw_cm = async_playwright()
        self._pw = await self._pw_cm.start()
        name = self.config.browser_type_name
        if name != "chromium":
            logger.warning(
                "Patchright only ships chromium; ignoring PLAYWRIGHT_BROWSER_TYPE=%s", name
            )
            name = "chromium"
        self._browser_type = getattr(self._pw, name)

    async def launch_browser(self) -> Any:
        if self._browser_type is None:
            raise RuntimeError("start() must be awaited before launch_browser()")
        if self.config.cdp_url:
            logger.info("patchright: connecting over CDP %s", self.config.cdp_url)
            return await self._browser_type.connect_over_cdp(
                self.config.cdp_url, **self.config.cdp_kwargs
            )
        if self.config.connect_url:
            logger.info("patchright: connecting to remote %s", self.config.connect_url)
            return await self._browser_type.connect(
                self.config.connect_url, **self.config.connect_kwargs
            )
        logger.info("patchright: launching chromium")
        return await self._browser_type.launch(**self.config.launch_options)

    async def launch_persistent_context(self, context_kwargs: dict) -> Any:
        if self._browser_type is None:
            raise RuntimeError("start() must be awaited before launch_persistent_context()")
        return await self._browser_type.launch_persistent_context(**context_kwargs)

    async def close(self) -> None:
        if self._pw_cm is not None:
            try:
                await self._pw_cm.__aexit__()
            except Exception:  # pragma: no cover - best effort teardown
                logger.debug("patchright: error during teardown", exc_info=True)
        if self._pw is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                await self._pw.stop()
