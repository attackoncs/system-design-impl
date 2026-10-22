"""Network layer package for the distributed key-value store.

Provides gRPC server and client implementations for inter-node communication,
including connection pooling and error handling.
"""

from kv_store.network.grpc_client import GRPCClient, NodeUnavailableError
from kv_store.network.grpc_server import GRPCServer

__all__ = [
    "GRPCServer",
    "GRPCClient",
    "NodeUnavailableError",
]
