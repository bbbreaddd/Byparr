import logging
import time
import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated, NamedTuple, cast

from camoufox import AsyncCamoufox
from fastapi import Header
from playwright.async_api import Browser, BrowserContext, Page
from playwright_captcha import (
    ClickSolver,
    FrameworkType,
)
from pydantic import BaseModel, Field

from src.consts import (
    ADDON_PATH,
    LOG_LEVEL,
    MAX_ATTEMPTS,
    PROXY_PASSWORD,
    PROXY_SERVER,
    PROXY_USERNAME,
)

solver_logger = logging.getLogger("playwright_captcha")
solver_logger.handlers.clear()
if LOG_LEVEL == logging.DEBUG:
    solver_logger.addHandler(logging.StreamHandler())
    solver_logger.setLevel(LOG_LEVEL)
else:
    solver_logger.handlers.append(logging.NullHandler())

logger = logging.getLogger("uvicorn.error")
logger.setLevel(LOG_LEVEL)
if len(logger.handlers) == 0:
    logger.addHandler(logging.StreamHandler())

# Global browser instance for singleton reuse
_GLOBAL_BROWSER: Browser | None = None
_BROWSER_LOCK = asyncio.Lock()
_REQUEST_COUNT = 0
_MAX_REQUESTS_PER_BROWSER = 50


class TimeoutTimer(BaseModel):
    duration: int  # in seconds
    start_time: float = Field(default_factory=time.perf_counter)

    def remaining(self) -> float:
        """Get remaining time in seconds."""
        return max(0, self.duration - (time.perf_counter() - self.start_time))


class CamoufoxDepClass(NamedTuple):
    page: Page
    solver: ClickSolver
    context: BrowserContext


async def get_browser() -> Browser:
    """Gets or creates the global browser instance with rotation logic."""
    global _GLOBAL_BROWSER, _REQUEST_COUNT
    async with _BROWSER_LOCK:
        # Check if browser needs rotation
        if _GLOBAL_BROWSER is not None and _REQUEST_COUNT >= _MAX_REQUESTS_PER_BROWSER:
            logger.info("Browser rotation limit reached. Restarting Camoufox...")
            await _GLOBAL_BROWSER.close()
            _GLOBAL_BROWSER = None
            _REQUEST_COUNT = 0

        if _GLOBAL_BROWSER is None:
            logger.info("Initializing global Camoufox browser instance...")
            _GLOBAL_BROWSER = await AsyncCamoufox(
                main_world_eval=True,
                addons=[ADDON_PATH],
                geoip=True,
                locale="en-US",
                headless=True,
                humanize=True,
                i_know_what_im_doing=True,
                config={"forceScopeAccess": True},
                disable_coop=True,
            ).start()
        return _GLOBAL_BROWSER

async def close_browser():
    """Closes the global browser instance."""
    global _GLOBAL_BROWSER
    async with _BROWSER_LOCK:
        if _GLOBAL_BROWSER is not None:
            logger.info("Closing global browser instance...")
            await _GLOBAL_BROWSER.close()
            _GLOBAL_BROWSER = None


async def get_camoufox(
    x_proxy_server: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Server",
            description="Override proxy server for this request in protocol://host:port format.",
        ),
    ] = None,
    x_proxy_username: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Username",
        ),
    ] = None,
    x_proxy_password: Annotated[
        str | None,
        Header(
            alias="X-Proxy-Password",
        ),
    ] = None,
    x_user_agent: Annotated[
        str | None,
        Header(
            alias="X-User-Agent",
            description="Override the default User-Agent.",
        ),
    ] = None,
) -> AsyncGenerator[CamoufoxDepClass]:
    """Yields a fresh page and context from the global browser instance."""
    global _REQUEST_COUNT
    browser = await get_browser()
    
    async with _BROWSER_LOCK:
        _REQUEST_COUNT += 1
        logger.debug(f"Request count: {_REQUEST_COUNT}/{_MAX_REQUESTS_PER_BROWSER}")
    
    proxy_config = None
    if x_proxy_server:
        proxy_config = {
            "server": x_proxy_server,
            "username": x_proxy_username,
            "password": x_proxy_password,
        }
    elif PROXY_SERVER:
        proxy_config = {
            "server": PROXY_SERVER,
            "username": PROXY_USERNAME,
            "password": PROXY_PASSWORD,
        }

    # Use a fresh context for each request for isolation and proxy support
    context = await browser.new_context(
        user_agent=x_user_agent,
        viewport={"width": 1920, "height": 1080},
        ignore_https_errors=True,
        proxy=proxy_config
    )
    
    try:
        page = await context.new_page()

        # Optimization: Block unnecessary resources to save CPU/Memory
        async def block_resources(route):
            if route.request.resource_type in ["media", "font"]:
                await route.abort()
            else:
                await route.continue_()
        
        await page.route("**/*", block_resources)

        async with ClickSolver(
            framework=FrameworkType.CAMOUFOX,
            page=page,
            max_attempts=MAX_ATTEMPTS,
            attempt_delay=1,
        ) as solver:
            yield CamoufoxDepClass(page, solver, context)
            
    finally:
        # Always close the context to free up RAM, but keep the browser running
        await context.close()
