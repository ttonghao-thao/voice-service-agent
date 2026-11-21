"""Connection options for the official GPT-Live SDK against Sophon."""

from contextlib import asynccontextmanager

import aiohttp
from livekit.agents import APIConnectOptions
from livekit.plugins.openai.realtime import GPTLiveModel


async def without_authorization(request, handler):
    request.headers.pop("Authorization", None)
    return await handler(request)


@asynccontextmanager
async def sophon_gpt_live(url, api_key):
    # The SDK requires an API key. Sophon with anonymous access may reject a
    # placeholder bearer. This HTTP session is used only for this connection.
    middlewares = () if api_key else (without_authorization,)
    async with aiohttp.ClientSession(middlewares=middlewares) as client:
        model = GPTLiveModel(
            base_url=url, api_key=api_key or "unused", http_session=client,
            voice="chelsie", delegation="client",
            conn_options=APIConnectOptions(max_retry=0, timeout=8),
        )
        try:
            yield model
        finally:
            await model.aclose()
