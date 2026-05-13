#!/usr/bin/env python
"""Flask integration example for the rate limiter library.

Demonstrates how to use the rate limiter with Flask, including:
- Global rate limiting via the RateLimitExtension
- Per-route rate limiting with the @rate_limit decorator
- Multiple rules (per-minute and per-hour)
- Different algorithms on different endpoints
- Custom key functions
- Health check endpoint
"""

from flask import Flask, request, jsonify

from rate_limiter.core import RateLimiter
from rate_limiter.config import RateLimitRule, RateLimiterConfig, Algorithm
from rate_limiter.middleware.flask import (
    RateLimitExtension,
    RateLimitConfig,
    create_rate_limited_app,
    rate_limit,
)
from rate_limiter.keys import ip_key, composite_key, path_key


# ---------------------------------------------------------------------------
# App Setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Configure global rate limiting with multiple rules.
# These apply to ALL routes via the extension's before_request hook.
config = RateLimitConfig()
config.add_rule(limit=100, window=60, algorithm="token_bucket", key_type="ip")   # 100 req/min per IP
config.add_rule(limit=1000, window=3600, algorithm="token_bucket", key_type="ip")  # 1000 req/hour per IP

# Initialize the extension — this registers before/after request hooks
rate_limiter = RateLimitExtension(app, config=config.get_config(), auto_headers=True)


# ---------------------------------------------------------------------------
# Basic Endpoints (protected by global rate limiting)
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Welcome endpoint — protected by global rate limits only."""
    return jsonify({
        "message": "Welcome to the Flask Rate Limiter Example",
        "tip": "Check the X-RateLimit-* headers in the response.",
    })


@app.route("/api/data")
def get_data():
    """Standard API endpoint — protected by global rate limits."""
    return jsonify({
        "data": [1, 2, 3, 4, 5],
        "client_ip": request.remote_addr,
    })


# ---------------------------------------------------------------------------
# Per-Route Rate Limiting with @rate_limit decorator
# ---------------------------------------------------------------------------

@app.route("/api/strict")
@rate_limit(limit=5, window=60, algorithm="token_bucket", key_type="ip")
def strict_endpoint():
    """Strict endpoint — only 5 requests per minute per IP.

    The @rate_limit decorator adds an additional check on top of the global
    limits. A request must pass both the global and per-route checks.
    """
    return jsonify({
        "message": "This endpoint allows only 5 requests per minute.",
        "client_ip": request.remote_addr,
    })


@app.route("/api/upload", methods=["POST"])
@rate_limit(limit=3, window=60, algorithm="fixed_window", key_type="ip")
def upload_endpoint():
    """Upload endpoint — very strict limit using fixed window algorithm.

    POST endpoints that are expensive to process often need tighter limits.
    """
    return jsonify({
        "message": "Upload accepted (3 uploads per minute max).",
        "client_ip": request.remote_addr,
    })


# ---------------------------------------------------------------------------
# Different Algorithms
# ---------------------------------------------------------------------------

@app.route("/api/sliding")
@rate_limit(limit=10, window=60, algorithm="sliding_window_log", key_type="ip")
def sliding_window_endpoint():
    """Endpoint using sliding window log algorithm.

    The sliding window log provides the most accurate rate limiting by
    tracking individual request timestamps, but uses more memory.
    """
    return jsonify({
        "message": "Sliding window log: 10 requests per minute.",
        "algorithm": "sliding_window_log",
    })


@app.route("/api/leaky")
@rate_limit(limit=8, window=60, algorithm="leaking_bucket", key_type="ip")
def leaky_bucket_endpoint():
    """Endpoint using leaking bucket algorithm.

    The leaking bucket smooths out bursts by processing requests at a
    fixed rate, making it ideal for APIs that need steady throughput.
    """
    return jsonify({
        "message": "Leaking bucket: 8 requests per minute.",
        "algorithm": "leaking_bucket",
    })


# ---------------------------------------------------------------------------
# Custom Key Functions
# ---------------------------------------------------------------------------

def api_key_extractor(request_obj) -> str:
    """Custom key function that uses the X-API-Key header.

    Falls back to IP address if no API key is provided.
    """
    api_key = request_obj.headers.get("X-API-Key")
    if api_key:
        return f"apikey:{api_key}"
    return f"ip:{request_obj.remote_addr or '127.0.0.1'}"


# Create a limiter with a custom key function
custom_key_rule = RateLimitRule(
    limit=20,
    window=60,
    algorithm=Algorithm.TOKEN_BUCKET,
    key_func=api_key_extractor,
)
custom_key_limiter = RateLimiter(RateLimiterConfig(rules=[custom_key_rule]))


@app.route("/api/custom-key")
@rate_limit(limit=20, window=60, algorithm="token_bucket", key_type="ip")
def custom_key_endpoint():
    """Endpoint demonstrating custom key-based rate limiting.

    Rate limits are tracked per API key (via X-API-Key header).
    Different API keys get independent rate limit counters.
    """
    api_key = request.headers.get("X-API-Key", "none")
    return jsonify({
        "message": "Rate limited by API key (or IP as fallback).",
        "api_key": api_key,
        "client_ip": request.remote_addr,
    })


@app.route("/api/composite-key")
@rate_limit(limit=15, window=60, algorithm="sliding_window_counter", key_type="path")
def composite_key_endpoint():
    """Endpoint using path-based key for rate limiting.

    This demonstrates using the path key type, which means the rate limit
    counter is shared across all clients for this specific path.
    """
    return jsonify({
        "message": "Rate limited by path (shared counter for this endpoint).",
        "path": request.path,
        "client_ip": request.remote_addr,
    })


# ---------------------------------------------------------------------------
# Health Check (lightweight, still subject to global limits)
# ---------------------------------------------------------------------------

@app.route("/health")
def health_check():
    """Health check endpoint.

    Returns service status. Protected by global rate limits but has no
    additional per-route limits.
    """
    return jsonify({
        "status": "healthy",
        "version": "1.0.0",
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting Flask rate limiter example...")
    print()
    print("Available endpoints:")
    print("  GET  /                  - Welcome (global rate limiting)")
    print("  GET  /api/data          - Data endpoint (global rate limiting)")
    print("  GET  /api/strict        - Strict limit (5 req/min, token bucket)")
    print("  POST /api/upload        - Upload limit (3 req/min, fixed window)")
    print("  GET  /api/sliding       - Sliding window log (10 req/min)")
    print("  GET  /api/leaky         - Leaking bucket (8 req/min)")
    print("  GET  /api/custom-key    - Custom key function (20 req/min per API key)")
    print("  GET  /api/composite-key - Path-based key (15 req/min, sliding window counter)")
    print("  GET  /health            - Health check")
    print()
    print("Global limits: 100 req/min and 1000 req/hour per IP")
    print("Rate limit headers (X-RateLimit-*) are added to all responses.")
    print()
    print("Starting server on http://localhost:5000")
    app.run(host="localhost", port=5000, debug=True)
