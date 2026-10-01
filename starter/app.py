from flask import Flask, abort, jsonify, request, send_file
from pathlib import Path

HTML_FILE = Path(__file__).resolve().parent / "boogle.html"

app = Flask(__name__)

@app.get('/')
def index():
    if request.method != "GET":
        abort(405)
    return send_file(HTML_FILE)

@app.get('/jsontest')
def whatclass():
    if request.method != "GET":
        abort(405)
    return jsonify({"data": "I am in CSE190/CSE291!"})
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port="8000")
