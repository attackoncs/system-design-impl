#!/usr/bin/env python
"""FastAPI integration example for the rate limiter library."""

import fastapi
import fastapi.responses
import pydantic
import rate_limiter.config
import rate_limiter.core
import rate_limiter.decorators
import rate_limiter.middleware.fastapi
import uvicorn


class Item(pydantic.BaseModel):
    name: str
    price: float


app = fastapi.FastAPI(title="Rate Limiter API Example", version="1.0.0")

# Global rate limiting middleware (applies to all endpoints)
# Option 1: Using RateLimitConfig
rate_limit_config = rate_limiter.middleware.fastapi.RateLimitConfig()
rate_limit_config.add_rule(
    limit=100, window=60, algorithm="token_bucket", key_type="ip"
)
rate_limit_config.add_rule(
    limit=1000, window=3600, algorithm="token_bucket", key_type="user_id"
)

app.add_middleware(rate_limiter.middleware.fastapi.RateLimitMiddleware, config=rate_limit_config.get_config())

# Option 2: Using create_rate_limited_app (simpler)
# app.add_middleware(create_rate_limited_app, limit=100, window=60)


# Custom rate limiter for specific routes
custom_limiter = rate_limiter.core.RateLimiter(
    rate_limiter.config.RateLimiterConfig(
        rules=[
            rate_limiter.config.RateLimitRule(limit=5, window=60, algorithm=rate_limiter.config.Algorithm.TOKEN_BUCKET)
        ]
    )
)


# Endpoint with global rate limiting only
@app.get("/")
async def root():
    return {"message": "Welcome to the Rate Limiter API"}


# Endpoint with custom rate limiting decorator
@app.get("/strict")
@rate_limiter.decorators.rate_limit(limit=3, window=60, algorithm="token_bucket", raise_exception=True)
async def strict_endpoint(request: fastapi.Request):
    return {
        "message": (
            "This endpoint has stricter rate limiting (3 requests per minute)"
        ),
        "client_ip": request.client.host if request.client else None,
    }


# Endpoint with custom rate limiting (return 429 response instead of exception)
@app.get("/custom")
@rate_limiter.decorators.rate_limit(
    limit=2,
    window=60,
    algorithm="fixed_window",
    raise_exception=False,
    key_func=lambda request: (
        request.client.host if request.client else "anonymous"
    ),
)
async def custom_endpoint(request: fastapi.Request):
    return {"message": "This endpoint has custom rate limiting"}


# POST endpoint with rate limiting
@app.post("/items/")
async def create_item(item: Item, request: fastapi.Request):
    # The global middleware handles rate limiting for this endpoint
    return {
        "item_name": item.name,
        "item_price": item.price,
        "client_ip": request.client.host if request.client else None,
    }


# Endpoint with no rate limiting (bypasses global middleware)
@app.get("/unlimited")
async def unlimited_endpoint(request: fastapi.Request):
    # This endpoint is not rate limited
    return {
        "message": "This endpoint has no rate limiting",
        "client_ip": request.client.host if request.client else None,
    }


# Endpoint demonstrating different algorithms
@app.get("/algorithm/{algo_name}")
async def algorithm_demo(algo_name: str, request: fastapi.Request):
    algorithms = {
        "token_bucket": rate_limiter.config.Algorithm.TOKEN_BUCKET,
        "fixed_window": rate_limiter.config.Algorithm.FIXED_WINDOW,
        "sliding_counter": rate_limiter.config.Algorithm.SLIDING_WINDOW_COUNTER,
        "sliding_log": rate_limiter.config.Algorithm.SLIDING_WINDOW_LOG,
    }

    if algo_name not in algorithms:
        raise fastapi.HTTPException(
            status_code=400,
            detail=(
                f"Unknown algorithm: {algo_name}. Valid options:"
                f" {', '.join(algorithms.keys())}"
            ),
        )

    # Create a temporary limiter with the selected algorithm
    rule = rate_limiter.config.RateLimitRule(limit=5, window=30, algorithm=algorithms[algo_name])
    config = rate_limiter.config.RateLimiterConfig(rules=[rule])
    limiter = rate_limiter.core.RateLimiter(config)

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
async def rate_limit_exception_handler(request: fastapi.Request, exc: Exception):
    if "rate limit exceeded" in str(exc).lower():
        return fastapi.responses.JSONResponse(
            status_code=429,
            content={
                "error": "Rate Limit Exceeded",
                "message": str(exc),
            },
        )
    return fastapi.responses.JSONResponse(
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
    print(
        "  GET"
        " /algorithm/{token_bucket|fixed_window|sliding_counter|sliding_log} -"
        " Algorithm demo"
    )
    print("  GET /health - Health check")
    print("\nRate limiting headers are automatically added to all responses.")
    print("\nStarting server on http://localhost:8000")
    uvicorn.run(app, host="localhost", port=8000)
