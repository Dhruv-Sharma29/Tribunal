import copy

TEMPLATE = {
    "headers": {"accept": "application/json"},
    "retries": 3,
    "tags": [],
}


def build_jobs(specs):
    """Return one job dict per spec, each with its own copy of the template."""
    jobs = []
    for spec in specs:
        job = copy.deepcopy(TEMPLATE)
        job["name"] = spec["name"]
        jobs.append(job)
    return jobs
