"""Browser client for reading the first page of an X account's following list."""

import asyncio
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import BrowserContext, Page, Response, Route, async_playwright

from config.settings import settings
from scrapers.proxy_bridge import ensure_bridge, proxy_host_allowed, socks_needs_http_bridge

logger = logging.getLogger(__name__)

_TIMELINE_OPS = {
    "UserTweets",
    "UserTweetsAndReplies",
    "ListLatestTweetsTimeline",
    "HomeTimeline",
    "ForYouTimeline",
    "SearchTimeline",
}


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
        # Chromium accepts http(s) auth and SOCKS5 without auth. SOCKS5+user/pass
        # is forwarded through a local HTTP CONNECT bridge.
        scheme = {"socks5h": "socks5", "socks4a": "socks5"}.get(url.scheme.lower(), url.scheme)
        if scheme.lower().startswith("socks") and url.username:
            return None
        server = f"{scheme}://{url.netloc.rsplit('@', 1)[-1]}"
        proxy = {"server": server}
        if url.username is not None:
            proxy["username"] = unquote(url.username)
        if url.password is not None:
            proxy["password"] = unquote(url.password)
        return proxy

    async def _playwright_proxy(self) -> dict[str, str] | None:
        if socks_needs_http_bridge(settings.proxy_url):
            port = await ensure_bridge(settings.proxy_url)
            return {"server": f"http://127.0.0.1:{port}"}
        return self._proxy()

    async def __aenter__(self) -> "XScraper":
        if not settings.auth_token:
            raise ValueError("AUTH_TOKEN is required to browse X")
        if self._stack is not None:
            raise RuntimeError("XScraper is already open")
        stack = AsyncExitStack()
        try:
            playwright = await stack.enter_async_context(async_playwright())
            proxy = await self._playwright_proxy()
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-http2",
                    "--disable-quic",
                ],
                **({"proxy": proxy} if proxy else {}),
            )
            stack.push_async_callback(browser.close)
            self._context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            stack.push_async_callback(self._context.close)
            await self._context.route("**/*", self._filter_route)
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

    @staticmethod
    async def _filter_route(route: Route) -> None:
        host = urlsplit(route.request.url).hostname
        if proxy_host_allowed(host):
            await route.continue_()
        else:
            await route.abort()

    async def load_auth_cookie(self, auth_token_value: str) -> None:
        if self._context is None:
            raise RuntimeError("Open XScraper before loading cookies")
        if not auth_token_value:
            raise ValueError("auth_token cannot be empty")
        await self._context.add_cookies(
            [
                {
                    "name": "auth_token",
                    "value": auth_token_value,
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
                for domain in (".x.com", ".twitter.com")
            ]
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
            return await asyncio.wait_for(result, timeout=45)
        finally:
            page.remove_listener("response", on_response)
            await page.close()

    @staticmethod
    def _tweets(payload: object) -> list[Tweet]:
        tweets: dict[str, Tweet] = {}

        def handle_of(result: dict) -> str | None:
            user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
            legacy_user = user.get("legacy") or {}
            core_user = user.get("core") or {}
            name = legacy_user.get("screen_name") or core_user.get("screen_name")
            return name if isinstance(name, str) else None

        def text_of(result: dict) -> str | None:
            legacy = result.get("legacy") or {}
            note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
            text = legacy.get("full_text") or legacy.get("text") or note.get("text")
            return text.strip() if isinstance(text, str) and text.strip() else None

        def id_of(result: dict) -> str | None:
            raw = result.get("rest_id") or (result.get("legacy") or {}).get("id_str")
            if isinstance(raw, int):
                raw = str(raw)
            return raw if isinstance(raw, str) and raw.isdigit() else None

        def add(result: object) -> None:
            if not isinstance(result, dict):
                return
            result = result.get("tweet") or result
            handle = handle_of(result)
            tweet_id = id_of(result)
            text = text_of(result)
            if handle and re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) and tweet_id and text:
                url = f"https://x.com/{handle}/status/{tweet_id}"
                tweets[url] = Tweet(text=text, url=url)

        def visit(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    visit(item)
            elif isinstance(node, dict):
                packed = node.get("tweet_results") or node.get("tweet_result")
                if isinstance(packed, dict):
                    add(packed.get("result"))
                elif node.get("__typename") in {"Tweet", "TweetWithVisibilityResults"}:
                    add(node)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        visit(value)

        visit(payload)
        return list(tweets.values())[:20]

    @staticmethod
    async def _tweets_from_dom(page: Page) -> list[Tweet]:
        tweets: dict[str, Tweet] = {}
        articles = page.locator('article[data-testid="tweet"]')
        try:
            await articles.first.wait_for(timeout=8000)
        except Exception:
            return []
        n = min(await articles.count(), 20)
        for index in range(n):
            article = articles.nth(index)
            try:
                href = await article.locator('a[href*="/status/"]').first.get_attribute("href")
                text = (await article.locator('[data-testid="tweetText"]').first.inner_text()).strip()
            except Exception:
                continue
            if not href or not text:
                continue
            path = urlsplit(href if "://" in href else f"https://x.com{href}").path
            parts = path.strip("/").split("/")
            if (
                len(parts) >= 3
                and parts[1] == "status"
                and parts[2].isdigit()
                and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0])
            ):
                url = f"https://x.com/{parts[0]}/status/{parts[2]}"
                tweets[url] = Tweet(text=text, url=url)
        return list(tweets.values())

    async def _collect_tweets(
        self, url: str, for_you: bool = False, operations: tuple[str, ...] | None = None
    ) -> list[Tweet]:
        if self._context is None:
            raise RuntimeError("Open XScraper before requesting tweets")
        if for_you:
            operations = ("HomeTimeline", "ForYouTimeline")
        page = await self._context.new_page()
        tweets: dict[str, Tweet] = {}
        received = asyncio.Event()
        graphql_ops: list[str] = []
        timeline_ok = False

        async def on_response(response: Response) -> None:
            nonlocal timeline_ok
            path = urlsplit(response.url).path
            if "/graphql/" not in path:
                return
            op = path.rsplit("/", 1)[-1].split("?")[0]
            graphql_ops.append(op)
            if operations is not None and op not in operations:
                return
            try:
                for tweet in self._tweets(await response.json()):
                    tweets[tweet.url] = tweet
                if op in _TIMELINE_OPS or operations is not None:
                    timeline_ok = True
                if tweets:
                    received.set()
            except Exception:
                return

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if for_you:
                await page.get_by_role("tab", name="For you", exact=True).click()
            try:
                await asyncio.wait_for(received.wait(), timeout=25)
            except TimeoutError:
                pass
            if not tweets:
                for tweet in await self._tweets_from_dom(page):
                    tweets[tweet.url] = tweet
            if not tweets and not timeline_ok:
                logger.warning(
                    "No tweets at %s title=%r graphql=%s",
                    url,
                    await page.title(),
                    ",".join(graphql_ops) or "-",
                )
                raise TimeoutError(f"No tweets from {url}")
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

    async def get_user_tweets(self, handle: str) -> list[Tweet]:
        """Latest tweets authored by ``handle`` (retweeted/quoted tweets of others are dropped)."""
        handle = handle.removeprefix("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise ValueError("Invalid X handle")
        tweets = await self._collect_tweets(
            f"https://x.com/{handle}",
            operations=("UserTweets", "UserTweetsAndReplies"),
        )
        prefix = f"https://x.com/{handle.lower()}/status/"
        return [tweet for tweet in tweets if tweet.url.lower().startswith(prefix)]
