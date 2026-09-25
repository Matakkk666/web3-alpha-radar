"""Browser client for reading the first page of an X account's following list."""

import asyncio
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import BrowserContext, Page, Response, async_playwright

from config.settings import settings


@dataclass(frozen=True)
class Tweet:
    text: str
    url: str


class XScraper:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._context: BrowserContext | None = None

    @staticmethod
    def _proxy() -> dict[str, str] | None:
        if not settings.proxy_url:
            return None
        url = urlsplit(settings.proxy_url)
        if not url.scheme or not url.hostname:
            raise ValueError("PROXY_URL must include a scheme and hostname")
        server = f"{url.scheme}://{url.netloc.rsplit('@', 1)[-1]}"
        proxy = {"server": server}
        if url.username is not None:
            proxy["username"] = unquote(url.username)
        if url.password is not None:
            proxy["password"] = unquote(url.password)
        return proxy

    async def __aenter__(self) -> "XScraper":
        if not settings.auth_token:
            raise ValueError("AUTH_TOKEN is required to browse X")
        if self._stack is not None:
            raise RuntimeError("XScraper is already open")
        stack = AsyncExitStack()
        try:
            playwright = await stack.enter_async_context(async_playwright())
            proxy = self._proxy()
            browser = await playwright.chromium.launch(
                headless=True, **({"proxy": proxy} if proxy else {})
            )
            stack.push_async_callback(browser.close)
            self._context = await browser.new_context()
            stack.push_async_callback(self._context.close)
            await self.load_auth_cookie(settings.auth_token)
        except BaseException:
            await stack.aclose()
            self._context = None
            raise
        self._stack = stack
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._context = None

    async def load_auth_cookie(self, auth_token_value: str) -> None:
        if self._context is None:
            raise RuntimeError("Open XScraper before loading cookies")
        if not auth_token_value:
            raise ValueError("auth_token cannot be empty")
        await self._context.add_cookies(
            [{"name": "auth_token", "value": auth_token_value, "domain": ".x.com", "path": "/"}]
        )

    @staticmethod
    def _usernames(payload: dict) -> list[str]:
        user = payload.get("data", {}).get("user", {}).get("result", {})
        timeline = user.get("timeline_v2") or user.get("timeline") or {}
        instructions = timeline.get("timeline", {}).get("instructions", [])
        names: list[str] = []
        seen: set[str] = set()

        def visit(node: object) -> None:
            if len(names) >= 20:
                return
            if isinstance(node, dict):
                user_results = node.get("user_results")
                result = user_results.get("result") if isinstance(user_results, dict) else None
                if isinstance(result, dict):
                    name = (result.get("legacy") or {}).get("screen_name") or (
                        result.get("core") or {}
                    ).get("screen_name")
                    if isinstance(name, str) and name and name.lower() not in seen:
                        seen.add(name.lower())
                        names.append(name)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        for instruction in instructions:
            for entry in instruction.get("entries", []):
                visit(entry.get("content", {}))
            if "entry" in instruction:
                visit(instruction["entry"].get("content", {}))
        return names

    async def get_following_list(self, handle: str) -> list[str]:
        if self._context is None:
            raise RuntimeError("Open XScraper before requesting the following list")
        handle = handle.removeprefix("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise ValueError("Invalid X handle")

        page = await self._context.new_page()
        result: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()

        async def on_response(response: Response) -> None:
            path = urlsplit(response.url).path
            if result.done() or "/graphql/" not in path or path.rsplit("/", 1)[-1] not in (
                "Following", "FollowingLight"
            ):
                return
            try:
                payload = await response.json()
                names = self._usernames(payload)
            except Exception as error:
                if not result.done():
                    result.set_exception(error)
            else:
                if not result.done():
                    result.set_result(names)

        page.on("response", on_response)
        try:
            await page.goto(f"https://x.com/{handle}/following", wait_until="domcontentloaded")
            return await asyncio.wait_for(result, timeout=15)
        finally:
            page.remove_listener("response", on_response)
            await page.close()

    @staticmethod
    def _tweets(payload: object) -> list[Tweet]:
        tweets: dict[str, Tweet] = {}

        def visit(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    visit(item)
            elif isinstance(node, dict):
                result = node.get("tweet_results", {}).get("result") if isinstance(
                    node.get("tweet_results"), dict
                ) else None
                if isinstance(result, dict):
                    result = result.get("tweet") or result
                    legacy = result.get("legacy") or {}
                    user = (result.get("core") or {}).get("user_results") or {}
                    author = (user.get("result") or {}).get("legacy") or {}
                    handle = author.get("screen_name") or (user.get("result") or {}).get(
                        "core", {}
                    ).get("screen_name")
                    tweet_id = result.get("rest_id") or legacy.get("id_str")
                    text = legacy.get("full_text")
                    if (
                        isinstance(handle, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle)
                        and isinstance(tweet_id, str) and tweet_id.isdigit()
                        and isinstance(text, str) and text.strip()
                    ):
                        url = f"https://x.com/{handle}/status/{tweet_id}"
                        tweets[url] = Tweet(text=text, url=url)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        visit(value)

        visit(payload)
        return list(tweets.values())[:20]

    async def _collect_tweets(self, url: str, for_you: bool = False) -> list[Tweet]:
        if self._context is None:
            raise RuntimeError("Open XScraper before requesting tweets")
        page = await self._context.new_page()
        tweets: dict[str, Tweet] = {}
        received = asyncio.Event()

        async def on_response(response: Response) -> None:
            path = urlsplit(response.url).path
            if "/graphql/" not in path or (for_you and path.rsplit("/", 1)[-1] not in (
                "HomeTimeline", "ForYouTimeline"
            )):
                return
            try:
                for tweet in self._tweets(await response.json()):
                    tweets[tweet.url] = tweet
                if tweets:
                    received.set()
            except Exception:
                return

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if for_you:
                await page.get_by_role("tab", name="For you", exact=True).click()
            await asyncio.wait_for(received.wait(), timeout=15)
            return list(tweets.values())[:20]
        finally:
            page.remove_listener("response", on_response)
            await page.close()

    async def get_tweets(self, list_url: str) -> list[Tweet]:
        url = urlsplit(list_url)
        if url.scheme != "https" or url.hostname not in ("x.com", "twitter.com") or not re.fullmatch(
            r"/(?:i/)?lists/\d+/?", url.path
        ):
            raise ValueError("TWITTER_LIST_URL must be an X list URL")
        return await self._collect_tweets(list_url.replace("https://twitter.com/", "https://x.com/", 1))

    async def search_and_bookmark(self, query: str, count: int) -> int:
        if self._context is None:
            raise RuntimeError("Open XScraper before searching")
        page: Page = await self._context.new_page()
        clicked = 0
        try:
            await page.goto(f"https://x.com/search?q={quote(query)}&src=typed_query&f=live")
            articles = page.locator('article[data-testid="tweet"]')
            await articles.first.wait_for(timeout=15000)
            for index in range(await articles.count()):
                if clicked == count:
                    break
                await articles.nth(index).locator('[data-testid="caret"]').click()
                bookmark = page.get_by_role("menuitem", name="Bookmark", exact=True)
                if await bookmark.count():
                    await bookmark.click()
                    clicked += 1
                else:
                    await page.keyboard.press("Escape")
            return clicked
        finally:
            await page.close()

    async def get_for_you_tweets(self) -> list[Tweet]:
        return await self._collect_tweets("https://x.com/home", for_you=True)
