from pathlib import Path

from mailroom.jobs import load_jobs

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "jobs.example.yaml"


def test_example_jobs_parse():
    jobs = load_jobs(EXAMPLE)
    ids = {j.job_id for j in jobs}
    assert {"triage-claude", "gmail-assistant-n8n"} <= ids
    n8n = next(j for j in jobs if j.runner == "n8n")
    assert n8n.webhook_url
    pipe = next(j for j in jobs if j.runner == "pipeline")
    assert "classify" in pipe.steps and pipe.provider


def test_email_assistant_jobs():
    jobs = {j.job_id: j for j in load_jobs(EXAMPLE)}
    names = {j.name for j in jobs.values()}
    assert {"Claude", "ChatGPT", "Gemini", "Ollama", "n8n"} <= names
    providers = {jobs[i].provider for i in jobs if jobs[i].runner == "assistant"}
    assert providers == {"claude_cli", "codex_cli", "gemini_cli", "ollama"}
    from mailroom.providers import PROVIDERS
    assert providers <= set(PROVIDERS)
