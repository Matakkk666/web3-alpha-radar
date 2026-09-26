import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from scrapers.playwright_client import XScraper
from scrapers.proxy_bridge import normalized_upstream, proxy_host_allowed, socks_needs_http_bridge


def following_payload(names):
    return {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "user_results": {
                                                        "result": {"legacy": {"screen_name": name}}
                                                    }
                                                }
                                            }
                                        }
                                        for name in names
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    }


class XScraperTests(unittest.IsolatedAsyncioTestCase):
    def test_socks5h_with_auth_uses_bridge_not_chromium_socks(self):
        raw = "socks5h://user:p%40ss@proxy.test:1080"
        with patch("scrapers.playwright_client.settings.proxy_url", raw):
            self.assertTrue(socks_needs_http_bridge(raw))
            self.assertIsNone(XScraper._proxy())
        self.assertEqual(normalized_upstream(raw), "socks5://user:p%40ss@proxy.test:1080")

    def test_proxy_only_forwards_x_hosts(self):
        self.assertTrue(proxy_host_allowed("x.com"))
        self.assertTrue(proxy_host_allowed("abs.twimg.com"))
        self.assertTrue(proxy_host_allowed("api.twitter.com"))
        self.assertFalse(proxy_host_allowed("cm.g.doubleclick.net"))
        self.assertFalse(proxy_host_allowed("google.com"))

    def test_http_proxy_still_passed_to_chromium(self):
        with patch("scrapers.playwright_client.settings.proxy_url", "http://user:p%40ss@proxy.test:3128"):
            self.assertEqual(
                XScraper._proxy(),
                {"server": "http://proxy.test:3128", "username": "user", "password": "p@ss"},
            )

    def test_first_twenty_unique_names(self):
        names = [f"user{i}" for i in range(23)]
        payload = following_payload(["User0", "user0", *names[1:]])
        self.assertEqual(XScraper._usernames(payload), ["User0", *names[1:20]])

    def test_module_items_and_core_name(self):
        payload = following_payload([])
        entry = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][0]
        entry["entries"] = [
            {
                "content": {
                    "items": [
                        {
                            "item": {
                                "itemContent": {
                                    "user_results": {
                                        "result": {"core": {"screen_name": "module_user"}}
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        ]
        self.assertEqual(XScraper._usernames(payload), ["module_user"])

    async def test_cookie_before_navigation_and_graphql_response(self):
        events = []
        page = MagicMock()
        page.close = AsyncMock()
        response = MagicMock()
        response.url = "https://x.com/i/api/graphql/query-id/Following?variables=abc"
        response.json = AsyncMock(return_value=following_payload(["alpha", "beta"]))

        async def goto(url, **kwargs):
            events.append(("goto", url))
            other = MagicMock()
            other.url = "https://x.com/i/api/graphql/query-id/UserByScreenName"
            other.json = AsyncMock(return_value=following_payload(["wrong"]))
            await page.on.call_args.args[1](other)
            other.json.assert_not_awaited()
            await page.on.call_args.args[1](response)

        page.goto = AsyncMock(side_effect=goto)
        context = MagicMock()
        context.add_cookies = AsyncMock(side_effect=lambda cookies: events.append(("cookies", cookies)))
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        context.route = AsyncMock()
        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock()

        with patch("scrapers.playwright_client.async_playwright", return_value=manager), patch(
            "scrapers.playwright_client.settings.auth_token", "test-token"
        ), patch("scrapers.playwright_client.settings.proxy_url", "http://user:p%40ss@proxy.test:3128"):
            async with XScraper() as scraper:
                self.assertEqual(await scraper.get_following_list("@example"), ["alpha", "beta"])

        self.assertEqual(events[0], ("cookies", [
            {
                "name": "auth_token",
                "value": "test-token",
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
            {
                "name": "auth_token",
                "value": "test-token",
                "domain": ".twitter.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
        ]))
        self.assertEqual(events[1], ("goto", "https://x.com/example/following"))
        playwright.chromium.launch.assert_awaited_once_with(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-http2",
                "--disable-quic",
            ],
            proxy={"server": "http://proxy.test:3128", "username": "user", "password": "p@ss"},
        )
        page.close.assert_awaited_once()
        context.close.assert_awaited_once()
        browser.close.assert_awaited_once()

    async def test_auth_token_required(self):
        with patch("scrapers.playwright_client.settings.auth_token", None):
            with self.assertRaisesRegex(ValueError, "AUTH_TOKEN"):
                async with XScraper():
                    pass

    async def test_invalid_handle(self):
        scraper = XScraper()
        scraper._context = MagicMock()
        with self.assertRaisesRegex(ValueError, "Invalid X handle"):
            await scraper.get_following_list("example/followers")
        scraper._context.new_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
