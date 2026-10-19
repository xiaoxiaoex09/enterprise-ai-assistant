from database import get_connection


def add_knowledge(content):

    with get_connection() as conn:

        conn.execute(
            """
            INSERT INTO knowledge(content)
            VALUES(?)
            """,
            (content,)
        )

        conn.commit()


def get_all_knowledge():

    with get_connection() as conn:

        rows = conn.execute(
            """
            SELECT content FROM knowledge
            """
        ).fetchall()

        return [
            row["content"]
            for row in rows
        ]