import asyncio
import logging
import time
import warnings
import json
from asyncio import wait_for
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from playwright_captcha import CaptchaType

from src.consts import CHALLENGE_TITLES
from src.models import (
    HealthcheckResponse,
    LinkRequest,
    LinkResponse,
    Solution,
)
from src.utils import CamoufoxDepClass, TimeoutTimer, get_camoufox, logger

warnings.filterwarnings("ignore", category=SyntaxWarning)


router = APIRouter()

CamoufoxDep = Annotated[CamoufoxDepClass, Depends(get_camoufox)]


@router.get("/", include_in_schema=False)
def read_root():
    """Redirect to /docs."""
    logger.debug("Redirecting to /docs")
    return RedirectResponse(url="/docs", status_code=301)


@router.get("/health")
async def health_check(sb: CamoufoxDep):
    """Health check endpoint."""
    health_check_request = await read_item(
        LinkRequest.model_construct(url="https://google.com"),
        sb,
    )

    if health_check_request.solution.status != HTTPStatus.OK:
        raise HTTPException(
            status_code=500,
            detail="Health check failed",
        )

    return HealthcheckResponse(user_agent=health_check_request.solution.user_agent)


@router.post("/v1")
async def read_item(request: LinkRequest, dep: CamoufoxDep) -> LinkResponse:
    """Handle POST requests."""
    start_time = int(time.time() * 1000)

    timer = TimeoutTimer(duration=request.max_timeout)

    request.url = request.url.replace('"', "").strip()
    try:
        page_request = await dep.page.goto(
            request.url, timeout=timer.remaining() * 1000
        )
        status = page_request.status if page_request else HTTPStatus.OK
        await dep.page.wait_for_load_state(
            state="domcontentloaded", timeout=timer.remaining() * 1000
        )
        await dep.page.wait_for_load_state(
            "networkidle", timeout=timer.remaining() * 1000
        )
        
        # Smart Wait logic: Detect and wait for challenges to resolve
        if request.wait:
            logger.info(f"Waiting for {request.wait} seconds as requested")
            await asyncio.sleep(request.wait)
        else:
            # Check for common challenge indicators
            challenge_selectors = [
                "#ddg-l10n-title",          # DDoS-Guard
                "#cf-browser-verification",  # Cloudflare
                ".ray_id",                   # Cloudflare
                "text='Checking your browser'",
                "text='Verifying you are human'",
                "text='Please wait a few seconds'"
            ]
            
            # Check for selectors
            detected = False
            initial_status = status
            
            # 1. Selector-based detection
            for selector in challenge_selectors:
                try:
                    element = await dep.page.query_selector(selector)
                    if element and await element.is_visible():
                        logger.info(f"Challenge detected ({selector}), waiting for resolution...")
                        await dep.page.wait_for_selector(selector, state="hidden", timeout=30000)
                        detected = True
                        break
                except Exception:
                    continue

            # 2. Heuristic-based detection (Title or Spinner)
            if not detected:
                title = await dep.page.title()
                body = await dep.page.content()
                
                # Check for common challenge titles or the spinner heuristic
                is_challenge_title = any(kw in title for kw in CHALLENGE_TITLES)
                if is_challenge_title or (not title.strip() and "animation:" in body):
                    logger.info(f"Challenge suspected (Title: '{title}'), waiting for resolution...")
                    try:
                        # Wait for title to change to something non-challenge AND spinner to disappear
                        await dep.page.wait_for_function(
                            f"() => {{ \
                                const t = document.title.trim(); \
                                const b = document.body ? document.body.innerHTML : ''; \
                                const challengeKeywords = {json.dumps(CHALLENGE_TITLES)}; \
                                const isChallenge = challengeKeywords.some(kw => t.includes(kw)); \
                                return t.length > 0 && !isChallenge && !b.includes('animation:') && !b.includes('spinner'); \
                            }}", 
                            timeout=30000
                        )
                        detected = True
                    except Exception:
                        logger.warning("Timed out waiting for challenge resolution heuristic")

            # 3. Aggressive solving if still in challenge state
            curr_title = await dep.page.title()
            if any(kw in curr_title for kw in CHALLENGE_TITLES):
                logger.info("Challenge title still present, attempting captcha solver...")
                try:
                    await wait_for(
                        dep.solver.solve_captcha(
                            captcha_container=dep.page,
                            captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                            wait_checkbox_attempts=2,
                            wait_checkbox_delay=1.0,
                        ),
                        timeout=min(timer.remaining(), 30),
                    )
                    detected = True
                    status = HTTPStatus.OK
                except Exception as e:
                    logger.warning(f"Captcha solver failed or timed out: {e}")

            # 4. Final Status Re-verification
            if detected or initial_status != 200:
                if detected:
                    logger.info("Challenge resolved! Waiting 2s for page to settle...")
                    await asyncio.sleep(2)
                
                logger.info(f"Re-verifying status (Initial: {initial_status})...")
                try:
                    # 1. Try Performance API (Most accurate for current page status)
                    perf_status = await dep.page.evaluate("() => { \
                        const nav = performance.getEntriesByType('navigation')[0]; \
                        return nav ? nav.responseStatus : null; \
                    }")
                    
                    if perf_status and perf_status > 0:
                        status = perf_status
                        logger.info(f"Status re-verified via Performance API: {status}")
                    else:
                        # 2. Fallback to lightweight fetch
                        final_resp = await dep.page.request.get(dep.page.url)
                        status = final_resp.status
                        logger.info(f"Status re-verified via Fetch: {status}")
                except Exception as e:
                    logger.warning(f"Failed to re-verify status: {e}")

    except TimeoutError as e:
        logger.error("Timed out while solving the challenge")
        raise HTTPException(
            status_code=408,
            detail="Timed out while solving the challenge",
        ) from e

    cookies = await dep.context.cookies()

    screenshot_b64 = None
    try:
        screenshot_bytes = await dep.page.screenshot(full_page=True, type="png")
        import base64
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    except Exception as e:
        logger.warning("Failed to capture screenshot: %s", e)

    return LinkResponse(
        message="Success",
        solution=Solution(
            user_agent=await dep.page.evaluate("navigator.userAgent"),
            url=dep.page.url,
            status=status,
            cookies=cookies,
            headers=page_request.headers if page_request else {},
            response=await dep.page.content(),
            screenshot=screenshot_b64,
        ),
        start_timestamp=start_time,
    )
