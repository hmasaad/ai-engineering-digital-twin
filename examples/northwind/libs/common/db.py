import psycopg


def connect():
    return psycopg.connect("dbname=northwind")


def find_user(conn, name: str):
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return conn.execute(query).fetchone()


def recent_orders(conn):
    return conn.execute("SELECT * FROM orders").fetchall()
