#!/usr/bin/env python
"""Basic usage examples for the rate limiter library."""

import time
from rate_limiter.core import RateLimiter
from rate_limiter.config import RateLimitRule, RateLimiterConfig, Algorithm
from rate_limiter.algorithms.token_bucket import TokenBucketAlgorithm
from rate_limiter.algorithms.fixed_window import FixedWindowAlgorithm
from rate_limiter.storage.memory import MemoryStorage


def basic_in_memory_example():
    """Basic in-memory rate limiting example."""
    print("=== Basic In-Memory Rate Limiting ===\n")

    # Create configuration with token bucket algorithm
    rule = RateLimitRule(
        limit=5,  # 5 requests
        window=10,  # per 10 seconds
        algorithm=Algorithm.TOKEN_BUCKET
    )
    config = RateLimiterConfig(rules=[rule])

    # Create rate limiter
    limiter = RateLimiter(config)

    # Test basic rate limiting
    user_id = "user123"
    print(f"Testing rate limiting for user: {user_id}")

    for i in range(7):
        result = limiter.check_request(user_id, rules=[rule])
        status = "✅ ALLOWED" if result.allowed else "❌ DENIED"
        print(f"Request {i+1}: {status} - {result.remaining} remaining")

        # Wait a bit between requests
        time.sleep(0.5)


def multiple_algorithms_example():
    """Example showing different rate limiting algorithms."""
    print("\n=== Multiple Algorithms Comparison ===\n")

    # Test different algorithms
    algorithms = [
        ("Token Bucket", Algorithm.TOKEN_BUCKET),
        ("Fixed Window", Algorithm.FIXED_WINDOW),
        ("Sliding Window Counter", Algorithm.SLIDING_WINDOW_COUNTER),
        ("Sliding Window Log", Algorithm.SLIDING_WINDOW_LOG),
    ]

    for algo_name, algo in algorithms:
        print(f"\n--- {algo_name} ---")
        rule = RateLimitRule(limit=3, window=5, algorithm=algo)
        config = RateLimiterConfig(rules=[rule])
        limiter = RateLimiter(config)

        for i in range(5):
            result = limiter.check_request(f"test_user", rules=[rule])
            status = "✅" if result.allowed else "❌"
            print(f"  Request {i+1}: {status} - {result.remaining} remaining")
            time.sleep(0.3)


def burst_example():
    """Example showing burst behavior with token bucket."""
    print("\n=== Burst Behavior Example ===\n")

    # Token bucket allows bursts up to the limit
    rule = RateLimitRule(limit=10, window=60, algorithm=Algorithm.TOKEN_BUCKET)
    config = RateLimiterConfig(rules=[rule])
    limiter = RateLimiter(config)

    print("Making 10 rapid requests (token bucket should allow burst):")
    for i in range(10):
        result = limiter.check_request("burst_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")
    print("Expected: All 10 requests should be ALLOWED (burst capacity)")

    print("\nMaking 2 more requests:")
    for i in range(2):
        result = limiter.check_request("burst_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")
    print("Expected: Both should be DENIED (bucket empty)")


def fixed_window_example():
    """Example showing fixed window behavior."""
    print("\n=== Fixed Window Behavior ===\n")

    rule = RateLimitRule(limit=5, window=3, algorithm=Algorithm.FIXED_WINDOW)
    config = RateLimiterConfig(rules=[rule])
    limiter = RateLimiter(config)

    print("Making 5 requests within the window:")
    for i in range(5):
        result = limiter.check_request("window_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")

    print("\nWaiting for window to reset...")
    time.sleep(3.5)

    print("\nMaking 2 more requests after window reset:")
    for i in range(2):
        result = limiter.check_request("window_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")


def sliding_window_example():
    """Example showing sliding window behavior."""
    print("\n=== Sliding Window Behavior ===\n")

    rule = RateLimitRule(limit=5, window=5, algorithm=Algorithm.SLIDING_WINDOW_COUNTER)
    config = RateLimiterConfig(rules=[rule])
    limiter = RateLimiter(config)

    print("Making 3 requests at the start:")
    for i in range(3):
        result = limiter.check_request("sliding_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")

    print("\nWaiting 3 seconds (window sliding):")
    time.sleep(3)

    print("\nMaking 3 more requests:")
    for i in range(3):
        result = limiter.check_request("sliding_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")


def multiple_rules_example():
    """Example showing multiple rate limiting rules."""
    print("\n=== Multiple Rules Example ===\n")

    # Create multiple rules: strict and permissive
    strict_rule = RateLimitRule(
        limit=2,
        window=10,
        algorithm=Algorithm.TOKEN_BUCKET
    )
    permissive_rule = RateLimitRule(
        limit=10,
        window=10,
        algorithm=Algorithm.TOKEN_BUCKET
    )

    config = RateLimiterConfig(rules=[strict_rule, permissive_rule])
    limiter = RateLimiter(config)

    print("Making requests against both rules (strict=2, permissive=10):")
    for i in range(7):
        result = limiter.check_request("multi_user")
        status = "✅ ALLOWED" if result.allowed else "❌ DENIED"
        print(f"  Request {i+1}: {status} - {result.remaining} remaining")
        time.sleep(0.5)


def storage_backend_example():
    """Example showing different storage backends."""
    print("\n=== Storage Backend Example ===\n")

    # In-memory storage
    print("Using in-memory storage:")
    rule = RateLimitRule(limit=3, window=5, algorithm=Algorithm.TOKEN_BUCKET)
    config = RateLimiterConfig(storage_backend="memory", rules=[rule])
    limiter = RateLimiter(config)

    for i in range(4):
        result = limiter.check_request("storage_user", rules=[rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")
        time.sleep(0.5)


def dynamic_rules_example():
    """Example showing dynamic rule updates."""
    print("\n=== Dynamic Rules Example ===\n")

    # Start with strict rules
    initial_rule = RateLimitRule(limit=2, window=10, algorithm=Algorithm.TOKEN_BUCKET)
    config = RateLimiterConfig(rules=[initial_rule])
    limiter = RateLimiter(config)

    print("Initial strict rules (2 requests per 10 seconds):")
    for i in range(4):
        result = limiter.check_request("dynamic_user", rules=[initial_rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")
        time.sleep(0.5)

    # Update to more permissive rules
    print("\nUpdating to permissive rules (10 requests per 10 seconds):")
    new_rule = RateLimitRule(limit=10, window=10, algorithm=Algorithm.TOKEN_BUCKET)
    limiter.update_rules([new_rule])

    for i in range(4):
        result = limiter.check_request("dynamic_user", rules=[new_rule])
        status = "✅" if result.allowed else "❌"
        print(f"  Request {i+1}: {status}")
        time.sleep(0.5)


def main():
    """Run all examples."""
    print("Rate Limiter Library - Basic Usage Examples\n")
    print("=" * 50)

    basic_in_memory_example()
    multiple_algorithms_example()
    burst_example()
    fixed_window_example()
    sliding_window_example()
    multiple_rules_example()
    storage_backend_example()
    dynamic_rules_example()

    print("\n" + "=" * 50)
    print("All examples completed!")
    print("\nFor more advanced usage, see the FastAPI and Flask examples.")


if __name__ == "__main__":
    main()