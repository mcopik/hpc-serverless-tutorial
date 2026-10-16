# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.

size_generators = {
    'test': {'workload': 'compute', 'size': 100_000},
    'small': {'workload': 'compute', 'size': 5_000_000},
    'large': {'workload': 'memory', 'size': 200},
}

def buckets_count():
    return (0, 0)

def generate_input(data_dir, size, benchmarks_bucket, input_paths, output_paths, upload_func, nosql_func):
    return dict(size_generators[size])

def validate_output(data_dir: str | None, input_config: dict, output: dict, language: str, storage = None) -> str | None:

    result = output.get('result', {})
    if result.get('workload') != input_config.get('workload'):
        return f"Expected workload {input_config.get('workload')}, got {result.get('workload')}"
    if result.get('posix', {}).get('status') != 'ok':
        return f"POSIX counters unavailable: {result.get('posix', {}).get('error')}"
    return None
