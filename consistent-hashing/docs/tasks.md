# Tasks: Consistent Hashing

## Task 1: Project Setup and Hash Functions

- [x] 1.1 Create `consistent-hashing/pyproject.toml` with project metadata, dependencies (pytest, hypothesis), hatchling build config, and pytest configuration pointing to `tests/` with `src/` in pythonpath
- [x] 1.2 Create `consistent-hashing/src/consistent_hashing/__init__.py` with public API exports (ConsistentHashRing, HashFunction, sha1_hash, md5_hash, sha256_hash, DistributionStats, compute_distribution)
- [x] 1.3 Create `consistent-hashing/src/consistent_hashing/hash_functions.py` with `HashFunction` protocol, `sha1_hash`, `md5_hash`, and `sha256_hash` implementations returning integer hash values
- [x] 1.4 Create `consistent-hashing/tests/__init__.py` and `consistent-hashing/tests/test_hash_functions.py` with unit tests verifying: deterministic output, different inputs produce different hashes, output is a positive integer

## Task 2: Core Hash Ring Implementation

- [x] 2.1 Create `consistent-hashing/src/consistent_hashing/ring.py` with `ConsistentHashRing` class implementing `__init__`, `add_node`, `remove_node` methods -- uses sorted list of positions, position-to-node mapping, and virtual node generation with `"{node}#{index}"` format
- [x] 2.2 Implement `get_node(key)` method using `bisect_right` for O(log N) clockwise lookup with wrap-around
- [x] 2.3 Implement `get_nodes(key, count)` method that traverses clockwise collecting distinct physical servers, skipping virtual nodes of already-collected servers
- [x] 2.4 Implement `nodes` property, `total_virtual_nodes` property, and support for per-server virtual node counts (weighted nodes) via optional `num_virtual_nodes` parameter on `add_node`
- [x] 2.5 Create `consistent-hashing/tests/test_ring.py` with unit tests for: add/remove nodes, get_node determinism, get_nodes returns distinct servers, wrap-around behavior, empty ring raises RuntimeError, duplicate node raises ValueError, missing node raises KeyError

## Task 3: Statistics and Redistribution Analysis

- [x] 3.1 Create `consistent-hashing/src/consistent_hashing/stats.py` with `DistributionStats` dataclass and `compute_distribution(ring, keys)` function that computes per-server key counts, mean, standard deviation, min, max, and balance ratio
- [x] 3.2 Implement `compute_redistribution(keys, assignment_before, assignment_after)` function that identifies which keys moved between two server assignments and returns moved keys with source/destination
- [x] 3.3 Create `consistent-hashing/tests/test_stats.py` with unit tests for: correct key counting per server, standard deviation calculation, redistribution detection

## Task 4: Property-Based Tests

- [x] 4.1 Write property test: determinism -- for any stable ring and any key, `get_node(key)` always returns the same server (PBT)
- [x] 4.2 Write property test for minimal redistribution on server addition (PBT)
- [x] 4.3 Write property test: complete coverage -- every key maps to exactly one server, and all servers with virtual nodes receive at least some keys from a large random key set (PBT)
- [x] 4.4 Write property test: balanced distribution -- with >=100 virtual nodes per server and >=1000 keys, no server holds more than 2x the mean number of keys (PBT)

## Task 5: Demonstration and Documentation

- [x] 5.1 Create `consistent-hashing/examples/demo.py` demonstrating: ring creation with multiple servers, key assignment, adding a server and showing redistribution, removing a server and showing redistribution, printing distribution statistics before and after changes
- [x] 5.2 Create `consistent-hashing/README.md` with overview, installation instructions, quick start usage, algorithm explanation (hash ring, virtual nodes, clockwise lookup), API reference, and example output
- [x] 5.3 Copy requirements.md, design.md, and tasks.md into `consistent-hashing/docs/` directory
