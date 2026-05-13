#!/usr/bin/env python
"""FastAPI integration example for the rate limiter library."""

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from rate_limiter.core import RateLimiter
from rate_limiter.config import RateLimitRule, RateLimiterConfig, Algorithm
from rate_limiter.middleware.fastapi import (
    RateLimitMiddleware,
    RateLimitConfig,
    create_rate_limited_app,
)
from rate_limiter.decorators import rate_limit


class Item(BaseModel):
    name: str
    price: float


app = FastAPI(title="Rate Limiter API Example", version="1.0.0")

# Global rate limiting middleware (applies to all endpoints)
# Option 1: Using RateLimitConfig
rate_limit_config = RateLimitConfig()
rate_limit_config.add_rule(
    limit=100, window=60, algorithm="token_bucket", key_type="ip"
)
rate_limit_config.add_rule(
    limit=1000, window=3600, algorithm="token_bucket", key_type="user_id"
)

app.add_middleware(RateLimitMiddleware, config=rate_limit_config.get_config())

# Option 2: Using create_rate_limited_app (simpler)
# app.add_middleware(create_rate_limited_app, limit=100, window=60)


# Custom rate limiter for specific routes
custom_limiter = RateLimiter(
    RateLimiterConfig(
        rules=[
            RateLimitRule(limit=5, window=60, algorithm=Algorithm.TOKEN_BUCKET)
        ]
    )
)


# Endpoint with global rate limiting only
@app.get("/")
async def root():
    return {"message": "Welcome to the Rate Limiter API"}


# Endpoint with custom rate limiting decorator
@app.get("/strict")
@rate_limit(limit=3, window=60, algorithm="token_bucket", raise_exception=True)
async def strict_endpoint(request: Request):
    return {
        "message": "This endpoint has stricter rate limiting (3 requests per minute)",
        "client_ip": request.client.host if request.client else None,
    }


# Endpoint with custom rate limiting (return 429 response instead of exception)
@app.get("/custom")
@rate_limit(
    limit=2,
    window=60,
    algorithm="fixed_window",
    raise_exception=False,
    key_func=lambda request: request.client.host if request.client else "anonymous",
)
async def custom_endpoint(request: Request):
    return {"message": "This endpoint has custom rate limiting"}


# POST endpoint with rate limiting
@app.post("/items/")
async def create_item(item: Item, request: Request):
    # The global middleware handles rate limiting for this endpoint
    return {
        "item_name": item.name,
        "item_price": item.price,
        "client_ip": request.client.host if request.client else None,
    }


# Endpoint with no rate limiting (bypasses global middleware)
@app.get("/unlimited")
async def unlimited_endpoint(request: Request):
    # This endpoint is not rate limited
    return {
        "message": "This endpoint has no rate limiting",
        "client_ip": request.client.host if request.client else None,
    }


# Endpoint demonstrating different algorithms
@app.get("/algorithm/{algo_name}")
async def algorithm_demo(algo_name: str, request: Request):
    algorithms = {
        "token_bucket": Algorithm.TOKEN_BUCKET,
        "fixed_window": Algorithm.FIXED_WINDOW,
        "sliding_counter": Algorithm.SLIDING_WINDOW_COUNTER,
        "sliding_log": Algorithm.SLIDING_WINDOW_LOG,
    }

    if algo_name not in algorithms:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown algorithm: {algo_name}. Valid options: {', '.join(algorithms.keys())}",
        )

    # Create a temporary limiter with the selected algorithm
    rule = RateLimitRule(
        limit=5, window=30, algorithm=algorithms[algo_name]
    )
    config = RateLimiterConfig(rules=[rule])
    limiter = RateLimiter(config)

    # Check rate limit manually
    result = limiter.check_request(request)

    return {
        "algorithm": algo_name,
        "allowed": result.allowed,
        "remaining": result.remaining,
        "limit": result.limit,
        "retry_after": result.retry_after,
    }


# Error handler for rate limit exceeded (when using raise_exception=True)
@app.exception_handler(Exception)
async def rate_limit_exception_handler(request: Request, exc: Exception):
    if "rate limit exceeded" in str(exc).lower():
        return JSONResponse(
            status_code=429,
            content={
                "error": "Rate Limit Exceeded",
                "message": str(exc),
            },
        )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal Server Error", "message": str(exc)},
    )


# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


if __name__ == "__main__":
    print("Starting FastAPI rate limiter example...")
    print("Available endpoints:")
    print("  GET / - Welcome message (global rate limiting)")
    print("  GET /strict - Strict rate limiting (3 req/min)")
    print("  GET /custom - Custom rate limiting (2 req/min)")
    print("  POST /items/ - Item creation (global rate limiting)")
    print("  GET /unlimited - No rate limiting")
    print("  GET /algorithm/{token_bucket|fixed_window|sliding_counter|sliding_log} - Algorithm demo")
    print("  GET /health - Health check")
    print("\nRate limiting headers are automatically added to all responses.")
    print("\nStarting server on http://localhost:8000")
    uvicorn.run(app, host="localhost", port=8000)