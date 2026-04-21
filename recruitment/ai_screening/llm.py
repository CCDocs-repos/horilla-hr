"""DeepSeek client + response parser.

Wraps the OpenAI SDK pointed at DeepSeek's base URL — identical config to
/opt/workspace/job-applicant-platform/src/lib/ai-screening.ts callLLM().
"""

import json
import logging
import os
import re
from typing import Any, Dict

from openai import OpenAI

logger = logging.getLogger(__name__)

MODEL = "deepseek-reasoner"
BASE_URL = "https://api.deepseek.com"
MAX_TOKENS = 4096


def call_llm(prompt: str) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    client = OpenAI(api_key=api_key, base_url=BASE_URL)
    response = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    message = response.choices[0].message
    content = getattr(message, "content", None)
    if not content:
        content = getattr(message, "reasoning_content", None)
    if not content:
        raise RuntimeError("No content in DeepSeek API response")
    return content


def parse_report(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return {
        "score": 0,
        "summary": "AI screening did not return structured output. Manual review required.",
        "greenFlags": [],
        "redFlags": ["Raw AI output: " + raw[:400]],
    }
