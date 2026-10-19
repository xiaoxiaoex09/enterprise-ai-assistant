
from knowledge_base import get_all_knowledge


def search_knowledge(question: str):
    docs = get_all_knowledge()

    keywords = [
        "ESP32", "STM32", "WiFi",
        "传感器", "温度", "湿度",
        "固件", "连接", "通信"
    ]

    results = []

    for doc in docs:
        score = 0

        for keyword in keywords:
            if (
                keyword.lower() in question.lower()
                and keyword.lower() in doc.lower()
            ):
                score += 1

        if score > 0:
            results.append((score, doc))

    results.sort(key=lambda item: item[0], reverse=True)

    return [doc for score, doc in results[:3]]
