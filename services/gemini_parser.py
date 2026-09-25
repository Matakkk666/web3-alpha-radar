"""Structured classification of project tweets with Gemini."""

from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict

from config.settings import settings


class ParsedTweet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_spam: bool
    project_type: Literal["NFT", "POW", "UNKNOWN"]
    project_handle: str
    chain_or_algo: str
    contract_or_link: str
    summary: str


SYSTEM_INSTRUCTION = (
    "Анализируй текст твита о криптопроекте. "
    "is_spam = true для розыгрышей, спама, рекламы, щиткоинов. "
    "Если это NFT, ищи сеть (Robinhood, SOL, EVM) и контракты. "
    "Если это POW, ищи алгоритм хеширования, ссылки на Github/Bitcointalk. "
    "Укажи project_handle проекта, а не автора репоста. "
    "Не выдумывай отсутствующие данные: используй пустую строку для неизвестных "
    "project_handle, chain_or_algo и contract_or_link; UNKNOWN для неизвестного типа. "
    "Возвращай строгий JSON по схеме без дополнительных полей и текста."
)


async def parse_text_with_ai(text: str) -> ParsedTweet:
    """Parse a tweet into a validated project classification."""
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY is required to parse tweets")

    async with genai.Client(api_key=settings.gemini_api_key).aio as client:
        response = await client.models.generate_content(
            model="gemini-3.6-flash",
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=ParsedTweet,
            ),
        )

    if response.parsed is None:
        raise ValueError("Gemini returned no valid ParsedTweet")
    return ParsedTweet.model_validate(response.parsed)
