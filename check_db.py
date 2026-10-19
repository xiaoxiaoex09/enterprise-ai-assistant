import sqlite3


conn = sqlite3.connect("tickets.db")


result = conn.execute(
    "PRAGMA table_info(knowledge_chunks)"
).fetchall()


for row in result:
    print(row)


conn.close()