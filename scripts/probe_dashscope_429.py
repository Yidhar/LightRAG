"""Probe the real body of DashScope's 429 response.

The openai SDK's ``RateLimitError.__str__`` only prints the one-line
'message' field, which is what our LightRAG server log shows. DashScope's
full response body often carries much richer detail:
    - `code`: specific error code (Throttling.User, InvalidModel, ...)
    - `request_id`: for filing tickets
    - `param` / `type`: which request field tripped
    - extended message explaining WHICH limit (account TPM vs model TPM
      vs QPM vs concurrent-request cap)

This script:
    1. Hammers DashScope's chat/completions with small concurrent bursts
       at the exact model / host configured in .env
    2. Captures the first 429 and dumps its full response body, headers,
       and request id
    3. Also verifies whether the configured model id is valid by trying
       several fallbacks (qwen-plus, qwen-max, qwen-plus-latest, ...)

Usage:
    uv run python scripts/probe_dashscope_429.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time

from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

load_dotenv()

import aiohttp  # noqa: E402


HOST = os.environ.get("LLM_BINDING_HOST", "").rstrip("/")
KEY = os.environ.get("LLM_BINDING_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "")


def _dump_response(tag: str, status: int, headers: dict, body_text: str) -> None:
    print(f"\n{'=' * 72}")
    print(f" {tag}   HTTP {status}")
    print("=" * 72)
    print("Headers of interest:")
    interesting = [
        "content-type",
        "x-request-id",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
        "retry-after",
    ]
    for h in interesting:
        if h in {k.lower() for k in headers}:
            for k, v in headers.items():
                if k.lower() == h:
                    print(f"  {k}: {v}")
    print()
    print("Body:")
    try:
        parsed = json.loads(body_text)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(body_text[:4000])


async def one_call(
    session: aiohttp.ClientSession,
    model: str,
    index: int,
    verbose: bool = False,
) -> tuple[int, dict, str]:
    """Send a single small completion request and return (status, headers, body)."""
    url = f"{HOST}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": f"Reply with the single word 'ok-{index}' and nothing else.",
            }
        ],
        "max_tokens": 8,
        "temperature": 0.0,
    }
    req_headers = {
        "Authorization": f"Bearer {KEY[:8]}...{KEY[-4:]}",  # masked for print
        "Content-Type": "application/json",
    }
    if verbose:
        print(f"\n--- REQUEST ---")
        print(f"POST {url}")
        print(f"Headers: {json.dumps(req_headers, indent=2)}")
        print(f"Body:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    async with session.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        body = await resp.text()
        return resp.status, dict(resp.headers), body


async def probe_model_validity() -> None:
    """Send a single call with the configured model to see what DashScope says.

    Possible outcomes:
        HTTP 200 - model is valid, no rate limit
        HTTP 400 - model id not recognised / malformed
        HTTP 401 - api key wrong
        HTTP 404 - model not found
        HTTP 429 - rate limited (ironic on a single call)
    """
    print(f"\n[probe_model_validity] Single call to {MODEL} at {HOST}")
    async with aiohttp.ClientSession() as session:
        try:
            status, headers, body = await one_call(session, MODEL, 0, verbose=True)
        except Exception as e:
            print(f"  request raised: {type(e).__name__}: {e}")
            return
    _dump_response(f"single call, model={MODEL!r}", status, headers, body)


async def probe_burst(n: int = 4) -> None:
    """Fire N concurrent calls to mimic LightRAG's MAX_ASYNC=4 behaviour.

    Captures whichever 429 lands first (or all responses if they succeed).
    """
    print(f"\n[probe_burst] Firing {n} concurrent calls to {MODEL}")
    async with aiohttp.ClientSession() as session:
        t0 = time.time()
        results = await asyncio.gather(
            *(one_call(session, MODEL, i) for i in range(n)),
            return_exceptions=True,
        )
        elapsed = time.time() - t0
    print(f"  burst completed in {elapsed:.2f}s")

    any_429 = False
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            print(f"  [{i}] RAISED {type(r).__name__}: {r}")
            continue
        status, headers, body = r
        if status == 200:
            try:
                data = json.loads(body)
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                print(f"  [{i}] HTTP 200 -> {content!r}")
            except Exception:
                print(f"  [{i}] HTTP 200 (unparseable)")
        else:
            any_429 = True
            _dump_response(f"burst call [{i}]", status, headers, body)

    if not any_429:
        print("\n  No 429 in this burst — model appears healthy on a 4-way burst.")
        print("  The failures during indexing are probably hitting a DIFFERENT")
        print("  limit than this probe can exercise (e.g. per-key QPM that")
        print("  only trips after sustained load, or per-model cumulative TPM).")


async def probe_fallback_models() -> None:
    """Try several standard DashScope model ids to see which actually work."""
    candidates = [
        "qwen-plus",
        "qwen-plus-latest",
        "qwen-max",
        "qwen-max-latest",
        "qwen2.5-72b-instruct",
        "qwen3-plus",
        "qwen3-max",
    ]
    print(
        f"\n[probe_fallback_models] Testing {len(candidates)} fallback ids "
        "(to see which are live on this key)"
    )
    async with aiohttp.ClientSession() as session:
        for m in candidates:
            try:
                status, _, body = await one_call(session, m, 0)
            except Exception as e:
                print(f"  {m:35s} EXCEPTION  {type(e).__name__}")
                continue
            if status == 200:
                try:
                    data = json.loads(body)
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    print(f"  {m:35s} HTTP 200  -> {content!r}")
                except Exception:
                    print(f"  {m:35s} HTTP 200  (unparseable)")
            else:
                short = body[:150].replace("\n", " ")
                print(f"  {m:35s} HTTP {status}  {short}")


async def main() -> None:
    if not HOST or not KEY or not MODEL:
        print("Missing LLM_BINDING_HOST / LLM_BINDING_API_KEY / LLM_MODEL in .env")
        sys.exit(2)

    print(f"HOST  : {HOST}")
    print(f"MODEL : {MODEL}")
    print(f"KEY   : {KEY[:12]}...{KEY[-4:]}")

    await probe_model_validity()
    await probe_burst(4)
    await probe_fallback_models()


if __name__ == "__main__":
    asyncio.run(main())
