
import json
import math
import os
import urllib.error
import urllib.request

EMBEDDING_MODEL = "BAAI/bge-m3"


def get_embedding(text: str) -> list[float]:
    api_key = os.getenv("SILICONFLOW_API_KEY")

    if not api_key:
        raise RuntimeError(
            "未配置 SILICONFLOW_API_KEY"
        )

    payload = {
        "model": EMBEDDING_MODEL,
        "input": text,
        "encoding_format": "float"
    }

    request = urllib.request.Request(
        "https://api.siliconflow.cn/v1/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(
            request, timeout=60
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Embedding API 请求失败（HTTP {error.code}）") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("Embedding API 网络连接或响应超时") from error
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("Embedding API 返回了无效数据") from error

    try:
        vector = result["data"][0]["embedding"]
        if not isinstance(vector, list) or not vector:
            raise ValueError("empty embedding")
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector):
            raise ValueError("invalid embedding values")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("Embedding API 返回内容不完整") from error
    return vector


def cosine_similarity(a, b):
    if len(a) != len(b):
        return 0.0

    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)
