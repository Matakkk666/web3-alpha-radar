"""Offline tests for Gemini tweet parsing."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from services.gemini_parser import ParsedTweet, parse_text_with_ai


class GeminiParserTests(unittest.TestCase):
    def test_schema_rejects_unknown_types_and_extra_fields(self):
        tweet = dict(
            is_spam=False,
            project_type="NFT",
            project_handle="@project",
            chain_or_algo="SOL",
            contract_or_link="contract",
            summary="New collection",
        )
        self.assertEqual(ParsedTweet.model_validate(tweet).project_type, "NFT")
        with self.assertRaises(ValidationError):
            ParsedTweet.model_validate({**tweet, "project_type": "DEFI"})
        with self.assertRaises(ValidationError):
            ParsedTweet.model_validate({**tweet, "extra": "not allowed"})

    def test_async_request_uses_schema_and_returns_parsed_tweet(self):
        parsed = ParsedTweet(
            is_spam=False,
            project_type="POW",
            project_handle="@miner",
            chain_or_algo="RandomX",
            contract_or_link="https://github.com/example/miner",
            summary="New PoW launch",
        )
        models = SimpleNamespace(generate_content=AsyncMock(return_value=SimpleNamespace(parsed=parsed)))
        async_client = MagicMock()
        async_client.__aenter__ = AsyncMock(return_value=SimpleNamespace(models=models))
        async_client.__aexit__ = AsyncMock(return_value=None)

        with patch("services.gemini_parser.settings.gemini_api_key", "test-key"), patch(
            "services.gemini_parser.genai.Client", return_value=SimpleNamespace(aio=async_client)
        ) as client_factory:
            result = asyncio.run(parse_text_with_ai("New miner @miner uses RandomX"))

        self.assertEqual(result, parsed)
        client_factory.assert_called_once_with(api_key="test-key")
        async_client.__aexit__.assert_awaited_once()
        kwargs = models.generate_content.await_args.kwargs
        self.assertEqual(kwargs["model"], "gemini-3.6-flash")
        self.assertEqual(kwargs["contents"], "New miner @miner uses RandomX")
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")
        self.assertIs(kwargs["config"].response_schema, ParsedTweet)
        self.assertIn("is_spam = true", kwargs["config"].system_instruction)
        self.assertIn("Bitcointalk", kwargs["config"].system_instruction)

    def test_missing_key_fails_before_request(self):
        with patch("services.gemini_parser.settings.gemini_api_key", None), patch(
            "services.gemini_parser.genai.Client"
        ) as client_factory:
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
                asyncio.run(parse_text_with_ai("tweet"))
            client_factory.assert_not_called()

    def test_empty_response_is_not_a_parsed_tweet(self):
        models = SimpleNamespace(generate_content=AsyncMock(return_value=SimpleNamespace(parsed=None)))
        async_client = MagicMock()
        async_client.__aenter__ = AsyncMock(return_value=SimpleNamespace(models=models))
        async_client.__aexit__ = AsyncMock(return_value=None)
        with patch("services.gemini_parser.settings.gemini_api_key", "test-key"), patch(
            "services.gemini_parser.genai.Client", return_value=SimpleNamespace(aio=async_client)
        ):
            with self.assertRaisesRegex(ValueError, "no valid ParsedTweet"):
                asyncio.run(parse_text_with_ai("tweet"))
