# Copyright 2020-2025 ETH Zurich and the SeBS authors. All rights reserved.
"""What does the system know about one invocation?

Runs a small workload and reports what the OS and the CPU counted, in two tiers:
  posix    - getrusage(RUSAGE_THREAD), always available on Linux (you implement it: TODO 5)
  papi     - CPU hardware counters through the PAPI library (SeBS's sebs_papi binding)
Each tier reports a status (ok / partial / unavailable) and, if needed, why.

Input: {"workload": "compute" | "memory" | "sleep" | "phases", "size": int, "papi": {...}}
  compute - Python loop with `size` iterations
  memory  - allocate `size` MB and write one byte per 4 KiB page
  sleep   - sleep for `size` ms
  phases  - compute; allocate a 256 MB buffer; `size` random reads of it; compute again
  papi    - optional: other events, and sampling on overflow, e.g.
            {"events": ["PAPI_TOT_INS", "PAPI_TOT_CYC"], "overflow_event": "PAPI_TOT_INS",
             "threshold": 2000000}
            Without it, PAPI counts Papi.EVENTS.
"""

import os
import platform
import resource
import time

# True only for the first invocation in this process: a module is imported once.
_cold = True

PAGE = 4096


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


class Papi:
    """PAPI tier: counting and overflow sampling with sebs_papi, part of SeBS's local run images."""

    EVENTS = ['PAPI_TOT_INS', 'PAPI_TOT_CYC', 'PAPI_L1_DCM', 'PAPI_L2_DCM']

    def __init__(self, config):
        self.config = config or {}
        self.counters = None
        self.error = None
        try:
            import sebs_papi
        except ImportError:
            self.error = 'sebs_papi is missing: the local run image predates SeBS\'s PAPI binding'
            return
        config = self.config
        try:
            self.counters = sebs_papi.PAPI(config.get('events', self.EVENTS), skip_unavailable=True)
            if not self.counters.events:
                reasons = sorted(set(self.counters.unavailable.values()))
                raise sebs_papi.PAPIError(f'no event can be counted: {"; ".join(reasons)}')
            if config.get('overflow_event'):
                self.counters.overflow(
                    config['overflow_event'],
                    int(config.get('threshold', 2_000_000)),
                    int(config.get('max_samples', 100_000)),
                )
        except Exception as e:
            self.error = f'{type(e).__name__}: {e}'
            if self.counters is not None:
                self.counters.finish()
            self.counters = None

    def start(self):
        if self.counters is not None:
            self.counters.start()

    def stop(self):
        events, samples, dropped = {}, [], 0
        if self.counters is not None:
            events = self.counters.stop()
            dropped = self.counters.dropped
            samples = [[t] + values for t, values in self.counters.samples()]
            self.counters.finish()
        errors = dict(self.counters.unavailable) if self.counters is not None else {}
        if self.error:
            errors['papi'] = self.error
        status = 'unavailable' if not events else 'partial' if errors else 'ok'
        result = {'status': status, 'events': events, 'errors': errors}
        if self.config.get('overflow_event'):
            result['overflow'] = {
                'event': self.config['overflow_event'],
                'threshold': int(self.config.get('threshold', 2_000_000)),
                'columns': ['time_us'] + list(events),
                'samples': samples,
                'dropped': dropped,
            }
        return result


def compute(size):
    acc = 0
    for i in range(size):
        acc = (acc + i * i) % 1_000_003
    return acc


def memory(size_mb):
    buf = bytearray(size_mb * 1024 * 1024)  # mapped, but no page touched yet
    for i in range(0, len(buf), PAGE):
        buf[i] = 1
    return len(buf) // PAGE


def sleep(ms):
    time.sleep(ms / 1000)
    return ms


def phases(size):
    """Compute; allocate a buffer far larger than the caches; random reads of it; compute."""
    acc = compute(size)
    buf = bytearray(256 * 1024 * 1024)  # zeroed: every page is written once
    n = len(buf)
    for i in range(size):
        acc += buf[(i * 2654435761) % n]  # a multiplicative hash: scattered addresses
    return acc + compute(size)


WORKLOADS = {'compute': compute, 'memory': memory, 'sleep': sleep, 'phases': phases}

class Rusage:
    """POSIX tier: what getrusage(RUSAGE_THREAD) counted for the calling thread.

    resource.getrusage(resource.RUSAGE_THREAD) returns the thread's usage *so far*,
    as an object with one attribute per field, e.g. usage.ru_minflt.
    """

    FIELDS = ['ru_utime', 'ru_stime', 'ru_minflt', 'ru_majflt', 'ru_nvcsw', 'ru_nivcsw']

    def __init__(self):
        self.events = {}

    def start(self):
        # TODO 5: take a snapshot of the thread's resource usage
        pass

    def stop(self):
        # TODO 5: take another snapshot; store in self.events, for every field in
        #         FIELDS, how much it grew since start()
        pass

    def report(self):
        if not self.events:
            return {'status': 'unavailable', 'events': {}, 'errors': {'posix': 'not implemented yet (TODO 5)'}}
        events = dict(self.events)
        for field in ['ru_utime', 'ru_stime']:  # seconds -> ms
            events[field] = round(events[field] * 1000, 3)
        return {'status': 'ok', 'events': events, 'errors': {}}


def machine():
    cpus = sorted(os.sched_getaffinity(0))
    model = None
    for line in (_read_file('/proc/cpuinfo') or '').splitlines():
        if line.startswith(('model name', 'Model', 'CPU part')):
            model = line.split(':', 1)[1].strip()
            break
    return {
        'arch': platform.machine(),
        'kernel': platform.release(),
        'cpu_model': model,
        'cpus_allowed': ','.join(map(str, cpus)),
        'cpuset_effective': _read_file('/sys/fs/cgroup/cpuset.cpus.effective'),
        'perf_event_paranoid': _read_file('/proc/sys/kernel/perf_event_paranoid'),
        'pmu_exposed': os.path.isdir('/sys/bus/event_source/devices/cpu')
        or os.path.isdir('/sys/bus/event_source/devices/armv8_pmuv3_0'),
    }


def handler(event):
    global _cold
    cold, _cold = _cold, False

    workload = event.get('workload', 'compute')
    size = int(event.get('size', 1_000_000))
    if workload not in WORKLOADS:
        raise ValueError(f'unknown workload {workload}, expected one of {list(WORKLOADS)}')

    papi = Papi(event.get('papi'))
    posix = Rusage()

    posix.start()
    papi.start()
    wall_begin = time.perf_counter_ns()
    cpu_begin = time.thread_time_ns()

    WORKLOADS[workload](size)

    cpu_end = time.thread_time_ns()
    wall_end = time.perf_counter_ns()
    papi_result = papi.stop()
    posix.stop()

    wall_ms = (wall_end - wall_begin) / 1e6
    cpu_ms = (cpu_end - cpu_begin) / 1e6
    return {
        'result': {
            'workload': workload,
            'size': size,
            'cold': cold,
            'pid': os.getpid(),
            'posix': posix.report(),
            'papi': papi_result,
            'machine': machine(),
        },
        'measurement': {
            'wall_time': wall_ms * 1000,  # us, like other SeBS benchmarks
            'thread_cpu_time': cpu_ms * 1000,
        },
    }
