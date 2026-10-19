from knowledge_base import add_knowledge


doc = """

产品：
ESP32环境监测设备


故障：
无法连接WiFi


原因：
1. WiFi密码错误
2. 不支持5GHz网络
3. ESP32固件异常


解决方法：
1. 确认2.4GHz WiFi
2. 检查网络密码
3. 重新烧录固件

"""


add_knowledge(doc)


print("知识添加成功")