from pdf_chat_service import ask_pdf


def run_agent(question):

    question_lower = question.lower()


    # 简单设备识别

    if "esp32" in question_lower:
        device = "ESP32"

    elif "stm32" in question_lower:
        device = "STM32"

    else:
        device = "未知设备"


    # 问题分类

    if (
        "wifi" in question_lower
        or "网络" in question
        or "连接" in question
    ):
        category = "网络故障"

    elif (
        "温度" in question
        or "传感器" in question
    ):
        category = "传感器故障"

    else:
        category = "其他"


    # 调用RAG

    knowledge_answer = ask_pdf(question)


    return {

        "device": device,

        "category": category,

        "knowledge": knowledge_answer

    }