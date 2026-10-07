"""Minimal stand-in backend for exercising the framework end-to-end.

Not part of the framework itself -- a real project points config/specs/*
and TIBB_BASE_URL at its actual services instead of this.
"""

import itertools
import os

from flask import Flask, jsonify, request

app = Flask(__name__)

TESTING_API_KEY = os.environ.get("TIBB_TESTING_API_KEY", "test-key-123")
_order_ids = itertools.count(1)
_orders: dict[str, dict] = {}


@app.post("/auth/login")
def login():
    body = request.get_json(force=True)
    if not body.get("username") or not body.get("password"):
        return jsonify({"error": "username and password required"}), 400
    return jsonify({"access_token": f"token-for-{body['username']}"}), 201


@app.post("/orders")
def create_order():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "missing bearer token"}), 401
    body = request.get_json(force=True)
    order_id = str(next(_order_ids))
    order = {"id": order_id, "status": "created", "items": body.get("items", [])}
    _orders[order_id] = order
    return jsonify(order), 200


@app.get("/tester/orders/<order_id>")
def get_order_by_id(order_id):
    if request.headers.get("X-Testing-Api-Key") != TESTING_API_KEY:
        return jsonify({"error": "invalid testing api key"}), 403
    order = _orders.get(order_id)
    if not order:
        return jsonify({"error": "not found"}), 404
    return jsonify(order), 200


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 8000)))
