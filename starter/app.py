from flask import Flask, abort, jsonify, request, send_file, redirect, g
from pathlib import Path
from uuid import uuid4, UUID
from datetime import timezone
import os
import sqlite3
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
import logging
import time

import joblib
import urllib3


HTML_FILE = Path(__file__).resolve().parent / "boogle.html"

DB_FILE = Path(__file__).resolve().parent / "boogle.db"
MODEL_FILE = Path(__file__).resolve().parent / "models.joblib"
SEGMENTS_FILE = Path(__file__).resolve().parent / "segments.json"
RTB_URL = os.environ.get("RTB_URL", "https://webtracking.sysnet.ucsd.edu/pa1/rtb").rstrip("/")
DSP_NAMES = ("steady", "niche", "shady", "glitch")
BID_FLOOR = Decimal("0.50")
TMAX = 250

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
http = urllib3.PoolManager(num_pools=4, maxsize=8, retries=False)

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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_auctions (
                auction_id TEXT PRIMARY KEY,
                bid_id TEXT NOT NULL,
                creative_id TEXT,
                advertiser_domains TEXT NOT NULL,
                price TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ad_complaints (
                auction_id TEXT PRIMARY KEY REFERENCES ad_auctions(auction_id),
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
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


@lru_cache(maxsize=1)
def load_ad_models():
    return joblib.load(MODEL_FILE)


@lru_cache(maxsize=1)
def load_segment_ids():
    with SEGMENTS_FILE.open(encoding="utf-8") as file:
        taxonomy = json.load(file)
    return {(s["attribute"], s["value"]): s["id"] for s in taxonomy["segments"]}


def get_segments_for_client(client_id):
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT query FROM searches WHERE client_id = ? ORDER BY ts DESC, search_id DESC",
            (client_id,),
        ).fetchall()

    text = " ".join(query.strip() for (query,) in rows if query.strip())
    if not text:
        return []

    segment_ids = load_segment_ids()
    segments = []
    for attribute, model in load_ad_models().items():
        if model[0].transform([text]).nnz == 0:
            continue
        value = model.predict([text])[0]
        segment_id = segment_ids.get((attribute, value))
        if segment_id is not None:
            segments.append({"id": segment_id})
    return segments


def request_dsp(name, payload, deadline):
    auction_id = json.loads(payload)["id"]
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        app.logger.info("Auction %s DSP %s: deadline expired before request", auction_id, name)
        return None
    try:
        response = http.request(
            "POST", f"{RTB_URL}/{name}/bid", body=payload,
            headers={"Content-Type": "application/json"},
            timeout=urllib3.Timeout(total=remaining), retries=False, redirect=False,
        )
        if time.monotonic() > deadline:
            app.logger.info("Auction %s DSP %s: response arrived after deadline", auction_id, name)
            return None
        if response.status != 200:
            app.logger.info("Auction %s DSP %s: HTTP %s", auction_id, name, response.status)
            return None
        return json.loads(response.data)
    except (OSError, urllib3.exceptions.HTTPError, ValueError) as error:
        app.logger.info("Auction %s DSP %s: request or response failed: %s", auction_id, name, error)
        return None


def collect_bids(bid_request):
    payload = json.dumps(bid_request).encode("utf-8")
    deadline = time.monotonic() + TMAX / 1000
    executor = ThreadPoolExecutor(max_workers=len(DSP_NAMES))
    try:
        futures = [executor.submit(request_dsp, name, payload, deadline) for name in DSP_NAMES]
        done, _ = wait(futures, timeout=max(0, deadline - time.monotonic()))
        return [future.result() for future in futures if future in done and future.result() is not None]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def valid_bids(responses, auction_id):
    for response in responses:
        if not isinstance(response, dict) or response.get("id") != auction_id or response.get("cur") != "USD":
            continue
        seatbids = response.get("seatbid")
        if not isinstance(seatbids, list):
            continue
        for seatbid in seatbids:
            if not isinstance(seatbid, dict) or not isinstance(seatbid.get("bid"), list):
                continue
            for bid in seatbid["bid"]:
                if not isinstance(bid, dict):
                    continue
                price = bid.get("price")
                if isinstance(price, bool) or not isinstance(price, (int, float)):
                    continue
                try:
                    price = Decimal(str(price))
                    if not price.is_finite() or price < BID_FLOOR:
                        continue
                except InvalidOperation:
                    continue
                if bid.get("impid") != "1" or bid.get("w") != 300 or bid.get("h") != 250:
                    continue
                if not all(isinstance(bid.get(key), str) and bid[key] for key in ("id", "adm", "burl")):
                    continue
                yield price, bid


def blocked_advertisers():
    """Block domains and creatives associated with a reported auction."""
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT a.advertiser_domains, a.creative_id
            FROM ad_auctions AS a
            JOIN ad_complaints AS c ON c.auction_id = a.auction_id
            """
        ).fetchall()

    domains = set()
    creatives = set()
    for domains_json, creative_id in rows:
        advertised_domains = json.loads(domains_json)
        if isinstance(advertised_domains, list):
            domains.update(
                domain.strip().lower() for domain in advertised_domains
                if isinstance(domain, str) and domain.strip()
            )
        if creative_id:
            creatives.add(creative_id)
    return domains, creatives


def is_blocked_bid(bid, domains, creatives):
    creative_id = bid.get("crid")
    if isinstance(creative_id, str) and creative_id in creatives:
        return True
    advertised_domains = bid.get("adomain")
    return isinstance(advertised_domains, list) and any(
        isinstance(domain, str) and domain.strip().lower() in domains
        for domain in advertised_domains
    )


@app.get("/ad")
def ad():
    auction_id = uuid4().hex
    blocked_domains, _ = blocked_advertisers()
    bid_request = {
        "id": auction_id,
        "imp": [{"id": "1", "banner": {"format": [{"w": 300, "h": 250}]}, "bidfloor": float(BID_FLOOR)}],
        "device": {"ua": request.headers.get("User-Agent", ""), "ip": request.remote_addr},
        "user": {"id": g.client_id, "data": [{"id": "boogle", "segment": get_segments_for_client(g.client_id), "ext": {"segtax": 500}}]},
        "tmax": TMAX,
        "badv": sorted(blocked_domains),
    }
    responses = collect_bids(bid_request)
    # Recheck after bidding in case feedback arrived during the auction.
    blocked_domains, blocked_creatives = blocked_advertisers()
    valid = list(valid_bids(responses, auction_id))
    bids = sorted(
        (item for item in valid
         if not is_blocked_bid(item[1], blocked_domains, blocked_creatives)),
        key=lambda item: item[0], reverse=True,
    )
    if not bids:
        app.logger.info("Auction %s: responses=%d valid_prices=%s eligible_prices=[]",
                        auction_id, len(responses), [str(price) for price, _ in valid])
        return "", 200

    winning_price, winner = bids[0]
    second_price = bids[1][0] if len(bids) > 1 else BID_FLOOR
    price = min(winning_price, max(second_price, BID_FLOOR) + Decimal("0.01"))
    app.logger.info("Auction %s: responses=%d valid_prices=%s eligible_prices=%s clearing_price=%s",
                    auction_id, len(responses), [str(amount) for amount, _ in valid],
                    [str(amount) for amount, _ in bids], price)

    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO ad_auctions (auction_id, bid_id, creative_id, advertiser_domains, price) VALUES (?, ?, ?, ?, ?)",
            (auction_id, winner["id"], winner.get("crid") if isinstance(winner.get("crid"), str) else None,
             json.dumps(winner.get("adomain", [])), str(price)),
        )

    billing_url = winner["burl"].replace("${AUCTION_PRICE}", str(price))
    try:
        response = http.request("GET", billing_url, timeout=5, retries=False, redirect=False)
        if not 200 <= response.status < 300:
            app.logger.warning("Billing notice failed for auction %s: HTTP %s", auction_id, response.status)
            return "", 200
    except (OSError, urllib3.exceptions.HTTPError, ValueError) as error:
        app.logger.warning("Billing notice failed for auction %s: %s", auction_id, error)
        return "", 200

    return winner["adm"], 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/ad/feedback")
def ad_feedback():
    feedback = request.get_json(silent=True)
    if not isinstance(feedback, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    auction_id = feedback.get("auction_id")
    if not isinstance(auction_id, str) or not auction_id.strip():
        return jsonify({"error": "auction_id must be a nonempty string"}), 400
    if feedback.get("event") != "complaint":
        return jsonify({"error": "event must be complaint"}), 400

    with get_db_connection() as conn:
        auction = conn.execute(
            "SELECT 1 FROM ad_auctions WHERE auction_id = ?", (auction_id,)
        ).fetchone()
        if auction is None:
            return jsonify({"error": "Unknown auction_id"}), 404
        conn.execute(
            "INSERT OR IGNORE INTO ad_complaints (auction_id) VALUES (?)",
            (auction_id,),
        )

    return "", 204

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
