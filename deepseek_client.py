"""Small, testable adapter for DeepSeek chat completions."""

import json
import os
import urllib.error
import urllib.request


def chat_completion(messages: list[dict[str, str]], timeout: int = 60) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    payload = {
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": messages,
        "stream": False,
    }
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"DeepSeek API 请求失败（HTTP {error.code}）") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("DeepSeek API 网络连接或响应超时") from error
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("DeepSeek API 返回了无效数据") from error

    try:
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty content")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("DeepSeek API 返回内容不完整") from error
    return content
