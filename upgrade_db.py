import sqlite3


conn = sqlite3.connect("tickets.db")


cursor = conn.cursor()


cursor.execute(
    """
    ALTER TABLE tickets
    ADD COLUMN ai_analysis TEXT
    """
)


cursor.execute(
    """
    ALTER TABLE tickets
    ADD COLUMN priority TEXT
    """
)


cursor.execute(
    """
    ALTER TABLE tickets
    ADD COLUMN category TEXT
    """
)


conn.commit()

conn.close()


print("数据库升级完成")