"""Probe different ways to disable Qwen3 reasoning / thinking mode on DashScope.

We saw in probe_dashscope_429.py that qwen3.6-plus returns 217 reasoning
tokens even for a 5-char response. This script tries each documented
switch and prints the resulting token usage so we can pick the one that
actually works on DashScope's OpenAI-compat endpoint.

Run:
    uv run python scripts/probe_qwen3_no_thinking.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys

from dotenv import load_dotenv

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, io.UnsupportedOperation):
        pass

load_dotenv()
import aiohttp  # noqa: E402

HOST = os.environ["LLM_BINDING_HOST"].rstrip("/")
KEY = os.environ["LLM_BINDING_API_KEY"]
MODEL = os.environ["LLM_MODEL"]

# A prompt similar in shape to LightRAG's entity extraction: multi-line,
# asks for a structured answer. Small but should trigger the same
# reasoning behaviour.
TEST_PROMPT = (
    "Extract the two key entities from the text and output them as a "
    "JSON array of strings, nothing else.\n\n"
    "Text: The Eiffel Tower is in Paris, designed by Gustave Eiffel."
)


async def probe(
    tag: str,
    extra_payload: dict | None,
) -> tuple[int, int, int, str]:
    """Send one call with the given extra payload, return token usage."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 256,
        "temperature": 0.0,
    }
    if extra_payload:
        payload.update(extra_payload)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HOST}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            status = resp.status
            body_text = await resp.text()

    print(f"\n{'=' * 66}")
    print(f"  {tag}")
    print(f"  extra: {json.dumps(extra_payload) if extra_payload else 'none'}")
    print(f"  HTTP {status}")

    if status != 200:
        print(f"  body: {body_text[:500]}")
        return status, 0, 0, ""

    data = json.loads(body_text)
    usage = data.get("usage", {})
    prompt_tok = usage.get("prompt_tokens", 0)
    completion_tok = usage.get("completion_tokens", 0)
    details = usage.get("completion_tokens_details", {}) or {}
    reasoning_tok = details.get("reasoning_tokens", 0)
    text_tok = details.get("text_tokens", completion_tok)

    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "")
    )
    reasoning = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("reasoning_content", "") or ""
    )

    print(f"  prompt_tokens     : {prompt_tok}")
    print(f"  completion_tokens : {completion_tok}")
    print(f"     reasoning      : {reasoning_tok}")
    print(f"     text           : {text_tok}")
    print(f"  content   : {content[:120]!r}")
    print(f"  reasoning_content : {'(empty)' if not reasoning else reasoning[:120] + '...'}")
    return status, completion_tok, reasoning_tok, content


async def main() -> None:
    print(f"HOST  : {HOST}")
    print(f"MODEL : {MODEL}")
    print(f"PROMPT (test): {TEST_PROMPT[:80]}...")

    # Baseline: no knobs, thinking should run
    await probe("baseline (no extra args)", None)

    # Variant A: top-level enable_thinking
    await probe("enable_thinking=False (top level)", {"enable_thinking": False})

    # Variant B: chat_template_kwargs (vLLM-style)
    await probe(
        "chat_template_kwargs.enable_thinking=False (vLLM style)",
        {"chat_template_kwargs": {"enable_thinking": False}},
    )

    # Variant C: DashScope 'parameters' field (native DashScope style)
    await probe(
        "parameters.enable_thinking=False (DashScope native style)",
        {"parameters": {"enable_thinking": False}},
    )

    # Variant D: extra_body (OpenAI SDK convention)
    await probe(
        "extra_body.enable_thinking=False (OpenAI SDK pass-through)",
        {"extra_body": {"enable_thinking": False}},
    )

    # Variant E: thinking_budget=0
    await probe("thinking_budget=0 (top level)", {"thinking_budget": 0})

    print("\n" + "=" * 66)
    print("  Take the variant with reasoning_tokens=0 and a non-empty")
    print("  content. That is the knob to wire into LightRAG via")
    print("  OPENAI_LLM_EXTRA_BODY in .env.")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
