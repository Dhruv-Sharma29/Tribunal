from before import TEMPLATE, build_jobs

SPECS = [{"name": "a"}, {"name": "b"}]


def test_builds_one_job_per_spec():
    assert [job["name"] for job in build_jobs(SPECS)] == ["a", "b"]


def test_jobs_do_not_share_nested_state():
    jobs = build_jobs(SPECS)
    jobs[0]["tags"].append("x")
    assert jobs[1]["tags"] == []


def test_the_template_is_not_mutated():
    build_jobs(SPECS)[0]["headers"]["accept"] = "text/plain"
    assert TEMPLATE["headers"]["accept"] == "application/json"
