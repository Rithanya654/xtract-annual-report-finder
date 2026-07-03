import redis
import json
import os

def get_redis():
    return redis.from_url(os.environ["REDIS_URL"])

def push_jobs(jobs: list[dict]):
    """Push list of job dicts to the queue using pipeline."""
    r = get_redis()
    pipe = r.pipeline()
    for job in jobs:
        pipe.rpush("xtract:jobs", json.dumps(job))
    pipe.execute()

def claim_job() -> dict | None:
    """Blocking pop with 5s timeout. Returns job dict or None."""
    r = get_redis()
    try:
        result = r.blpop("xtract:jobs", timeout=5)
        if result:
            _, job_data = result
            return json.loads(job_data)
    except Exception:
        # Redis timeout or connection errors are expected
        pass
    return None

def flush_run_jobs(run_id: str) -> int:
    """Remove all queued jobs belonging to run_id. Returns number removed."""
    r = get_redis()
    all_items = r.lrange("xtract:jobs", 0, -1)
    keep = []
    removed = 0
    for item in all_items:
        try:
            job = json.loads(item)
            if job.get("run_id") == run_id:
                removed += 1
            else:
                keep.append(item)
        except Exception:
            keep.append(item)

    pipe = r.pipeline()
    pipe.delete("xtract:jobs")
    for item in keep:
        pipe.rpush("xtract:jobs", item)
    pipe.execute()
    return removed

def queue_length() -> int:
    """Return current queue depth."""
    r = get_redis()
    return r.llen("xtract:jobs")
