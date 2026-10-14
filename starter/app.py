from flask import Flask, abort, jsonify, request, send_file, redirect, g
from pathlib import Path
from uuid import uuid4, UUID
from datetime import timezone
import os
import psycopg
import sqlite3
from contextlib import contextmanager


HTML_FILE = Path(__file__).resolve().parent / "boogle.html"

DB_FILE = Path(__file__).resolve().parent / "boogle.db"

app = Flask(__name__)

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                search_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                query TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                )
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS searches_by_client
            ON searches (client_id, ts DESC, search_id DESC)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS requests (
                request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                ts TEXT NOT NULL DEFAULT (
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ),
                ip TEXT,
                path TEXT,
                referer TEXT
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS requests_by_client
            ON requests (client_id, ts DESC, request_id DESC)
        """)

init_db()

@app.before_request
def record_visit():
    try:
        g.client_id = str(UUID(request.cookies.get("boogle_id", "")))
        g.needs_cookie = False
    except ValueError:
        g.client_id = str(uuid4())
        g.needs_cookie = True

    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO requests (client_id, ip, path, referer)
            VALUES (?, ?, ?, ?)
            """,
            (
                g.client_id,
                request.remote_addr,
                request.path,
                request.referrer,
            ),
        )


@app.after_request
def attach_client_cookie(response):
    if g.get("needs_cookie", False):
        response.set_cookie(
            "boogle_id",
            g.client_id,
            path="/",
            max_age=60 * 60 * 24 * 365,
            samesite="Lax",
            secure=False,
        )

    return response

@app.get('/')
def index():
    if request.method != "GET":
        abort(405)
    response = send_file(HTML_FILE)

    return response

@app.get("/search")
def search():
    query = request.args.get("q", "")

    if query.strip():
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO searches (client_id, query) VALUES (?, ?)",
                (g.client_id, query),
            )

    return redirect("/", code=303)

@app.get('/api/history')
def history():
    client_id = request.cookies.get("boogle_id")

    if not client_id:
        return jsonify({
            "client_id": None,
            "searches": [],
        })

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT query, ts
            FROM searches
            WHERE client_id = ?
            ORDER BY ts DESC, search_id DESC
            """,
            (client_id,),
        ).fetchall()

    searches = [
        {"query": query, "ts": ts}
        for query, ts in rows
    ]

    return jsonify({
        "client_id": client_id,
        "searches": searches,
    })

@app.get("/dump")
def dump():
    clients = []

    with get_db_connection() as conn:
        client_rows = conn.execute("""
            SELECT client_id, MIN(ts), MAX(ts)
            FROM requests
            GROUP BY client_id
            ORDER BY MIN(ts), client_id
        """).fetchall()

        for client_id, first_seen, last_seen in client_rows:
            latest_ip = conn.execute(
                """
                SELECT ip
                FROM requests
                WHERE client_id = ?
                ORDER BY ts DESC, request_id DESC
                LIMIT 1
                """,
                (client_id,),
            ).fetchone()[0]

            ip_rows = conn.execute(
                """
                SELECT DISTINCT ip
                FROM requests
                WHERE client_id = ? AND ip IS NOT NULL
                ORDER BY ip
                """,
                (client_id,),
            ).fetchall()

            search_rows = conn.execute(
                """
                SELECT query, ts
                FROM searches
                WHERE client_id = ?
                ORDER BY ts DESC, search_id DESC
                """,
                (client_id,),
            ).fetchall()

            clients.append({
                "client_id": client_id,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "latest_ip": latest_ip,
                "ips": [row[0] for row in ip_rows],
                "searches": [
                    {"query": query, "ts": ts}
                    for query, ts in search_rows
                ],
            })

    return jsonify({"clients": clients})

@app.get('/jsontest')
def whatclass():
    if request.method != "GET":
        abort(405)
    return jsonify({"data": "I am in CSE190/CSE291!"})
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port="8000")
