"""Measure the disposable dual-slot envelope while runtime QA is running."""

from contextlib import contextmanager
import json
from threading import Event, Thread


SAMPLE_SCRIPT = """
set -eu
cat /sys/fs/cgroup/memory.current
cat /sys/fs/cgroup/memory.peak
cat /sys/fs/cgroup/memory.max
awk '$1 == "usage_usec" {print $2}' /sys/fs/cgroup/cpu.stat
cat /sys/fs/cgroup/cpuset.cpus.effective
ls -1 /proc/1/fd | wc -l
awk '/^Max open files/ {print $4}' /proc/1/limits
"""


def _sample(topology, db):
    docker = topology.docker
    targets = {f"{role}:{slot}": container for (role, slot), container in topology.containers.items()}
    targets.update(edge=topology.edge, postgres=db.pg)
    sample = {}
    for name, container in targets.items():
        current, peak, limit, cpu, cpuset, fds, fd_limit = docker.read(container, "sh", "-ec", SAMPLE_SCRIPT).splitlines()
        sample[name] = {
            "memory_current_bytes": int(current), "memory_peak_bytes": int(peak), "memory_limit_bytes": int(limit),
            "cpu_usage_usec": int(cpu), "cpuset": cpuset, "pid1_open_fds": int(fds), "pid1_fd_limit": int(fd_limit),
        }
    raw = db.query("""
        BEGIN READ ONLY;
        SELECT json_build_object(
            'max_connections', current_setting('max_connections')::int,
            'superuser_reserved', current_setting('superuser_reserved_connections')::int,
            'reserved', current_setting('reserved_connections')::int,
            'connections', count(*),
            'active', count(*) FILTER (WHERE state = 'active'),
            'idle_in_transaction', count(*) FILTER (WHERE state LIKE 'idle in transaction%')
        ) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid();
        COMMIT;
    """)
    # psql's command status lines are kept out of the sampled JSON.
    database = json.loads(next(line for line in raw.splitlines() if line.startswith("{")))
    return {"containers": sample, "postgres": database}


@contextmanager
def observe_capacity(topology, db, report):
    """Capture concurrent activity and bracket it with healthy before/after samples."""
    stop = Event()
    samples = [_sample(topology, db)]
    errors = []

    def sample_loop():
        while not stop.is_set():
            try:
                samples.append(_sample(topology, db))
            except (ValueError, RuntimeError, StopIteration) as exc:
                errors.append(type(exc).__name__)
                return
            stop.wait(0.25)

    observer = Thread(target=sample_loop, daemon=True)
    observer.start()
    try:
        yield
    finally:
        stop.set()
        observer.join(45)
    if errors or observer.is_alive():
        raise RuntimeError("Disposable capacity sample failed")
    samples.append(_sample(topology, db))
    first, final = samples[0], samples[-1]
    for name, values in final["containers"].items():
        values["cpu_delta_usec"] = values["cpu_usage_usec"] - first["containers"][name]["cpu_usage_usec"]
        values["pid1_peak_sampled_fds"] = max(sample["containers"][name]["pid1_open_fds"] for sample in samples)
    db_stats = final["postgres"]
    db_stats["sampled_peak_connections"] = max(sample["postgres"]["connections"] for sample in samples)
    db_stats["sampled_peak_active"] = max(sample["postgres"]["active"] for sample in samples)
    db_stats["sampled_peak_idle_in_transaction"] = max(sample["postgres"]["idle_in_transaction"] for sample in samples)
    metrics = final["containers"].values()
    total_limits = sum(item["memory_limit_bytes"] for item in metrics)
    db_budget = db_stats["max_connections"] - db_stats["superuser_reserved"] - db_stats["reserved"]
    # Read the pool settings from each sealed image with its exact slot env.
    # This is a separate process and does not measure the live pool counters.
    code = "from app.core.database import engine; print(type(engine.pool).__name__, engine.pool.size(), engine.pool._max_overflow)"
    pools = {}
    for slot in ("BLUE", "GREEN"):
        kind, size, overflow = topology.docker.read(
            topology.containers["api", slot], "python", "-B", "-c", code).split()
        assert kind == "QueuePool" and int(size) > 0 and int(overflow) >= 0
        pools[slot] = {"pool_size": int(size), "max_overflow": int(overflow)}
    queue_pool_budget = sum(item["pool_size"] + item["max_overflow"] for item in pools.values())
    passed = (total_limits <= 8_000_000_000 and db_stats["sampled_peak_connections"] < db_budget
              and queue_pool_budget < db_budget
              and all(item["cpuset"] == "0-3" and item["memory_peak_bytes"] < item["memory_limit_bytes"]
                      and item["pid1_peak_sampled_fds"] < item["pid1_fd_limit"]
                      and item["cpu_delta_usec"] >= 0 for item in metrics))
    report.update(result="PASS" if passed else "FAIL", sample_count=len(samples),
                  total_memory_limits_bytes=total_limits, queue_pool_configured_subtotal=queue_pool_budget,
                  pool_configuration=pools,
                  scope="DISPOSABLE_4_CPU_SYNTHETIC_WORKLOAD", **final)
    if not passed:
        raise AssertionError("Disposable dual-slot capacity gate failed")
