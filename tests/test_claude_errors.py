"""Claude Code exits 1 with a JSON result on stdout: the message must reach the user, not the usage counters."""
import json
import subprocess

from mailroom.providers import cli_providers as cp


def _res(payload, code=1, err=""):
    return subprocess.CompletedProcess(["claude"], code, json.dumps(payload), err)


BASE = {"type": "result", "subtype": "success", "is_error": True, "duration_api_ms": 0, "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0}}


def test_model_refused_names_the_model_and_settings():
    p = cp.ClaudeCLIProvider("claude-sonnet-5-5")
    msg = p._explain(_res({**BASE, "result": "There's an issue with the selected model (claude-sonnet-5-5). "
                                          "It may not exist or you may not have access to it."}))
    assert "claude-sonnet-5-5" in msg and "Settings → Models" in msg and "usage" not in msg


def test_limit_and_login_are_classified():
    p = cp.ClaudeCLIProvider("sonnet")
    assert "subscription limit" in p._explain(_res({**BASE, "result": "Claude AI usage limit reached|1793000000"}))
    assert "not signed in" in p._explain(_res({**BASE, "result": "Invalid API key · Please run /login"}))


def test_errors_list_and_empty_result():
    p = cp.ClaudeCLIProvider("sonnet")
    assert "boom" in p._explain(_res({**BASE, "result": "", "errors": ["boom"]}))
    assert "no message" in p._explain(_res({**BASE, "result": ""}))


def test_non_json_falls_back_to_stderr():
    p = cp.ClaudeCLIProvider("sonnet")
    out = p._explain(subprocess.CompletedProcess(["claude"], 2, "", "segfault"))
    assert "segfault" in out


def test_gemini_uses_cached_google_sign_in_and_explains_exit_41(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "_home", lambda: tmp_path)
    g = cp.GeminiCLIProvider("default")
    assert g.extra_env() == {"GOOGLE_GENAI_USE_GCA": "true"}   # Login with Google, wherever the CLI keeps it
    out = 'Hook registry initialized with 0 hook entries\n{"session_id": "x", "error": {"type": "Error", ' \
          '"message": "Please set an Auth method in your settings.json", "code": 41}}'
    msg = g._explain(subprocess.CompletedProcess(["gemini"], 41, out, ""))
    assert "not signed in" in msg and "Please set an Auth method" in msg and "Hook registry" not in msg


def test_claude_json_on_stderr_and_dump(tmp_path, monkeypatch):
    from mailroom import settings as st
    monkeypatch.setenv("MAILROOM_DATA_DIR", str(tmp_path))
    p = cp.ClaudeCLIProvider("claude-sonnet-5-5")
    res = subprocess.CompletedProcess(["claude", "-p", "--model", "claude-sonnet-5-5"], 1, "",
                                      "warn line\n" + json.dumps({**BASE, "result": "Credit balance is too low"}))
    assert "Credit balance is too low" in p._explain(res)
    p._dump("/x/claude", res)
    dumped = (st.settings.data_dir / "logs" / "claude_cli-last-error.txt").read_text()
    assert "exit code: 1" in dumped and "Credit balance" in dumped and "--model claude-sonnet-5-5" in dumped


def test_empty_result_shows_remaining_fields():
    p = cp.ClaudeCLIProvider("sonnet")
    msg = p._explain(_res({**BASE, "result": "", "subtype": "error_during_execution", "num_turns": 0}))
    assert "error_during_execution" in msg


def test_login_shell_lookup(monkeypatch):
    cp._LOGIN.clear()
    monkeypatch.setattr(cp, "_login_shell", lambda cmd: "/bin/sh" if "command -v" in cmd else "/opt/a:/opt/b")
    assert cp.login_which("sh") == "/bin/sh" and cp.login_path() == ["/opt/a", "/opt/b"]
    cp._LOGIN.clear()


def test_login_env_merged_and_api_keys_still_removed(monkeypatch):
    cp._LOGIN.clear()
    cp._LOGIN["path"] = []
    cp._LOGIN["env"] = {"GOOGLE_CLOUD_PROJECT": "my-proj", "ANTHROPIC_API_KEY": "sk-x"}
    env = cp.child_env("/bin/sh", "/tmp")
    assert env["GOOGLE_CLOUD_PROJECT"] == "my-proj" and "ANTHROPIC_API_KEY" not in env
    cp._LOGIN.clear()


def test_exec_reads_full_output_from_files(tmp_path, monkeypatch):
    cp._LOGIN.clear()
    cp._LOGIN.update({"path": [], "env": {}})
    p = cp.ClaudeCLIProvider("sonnet")
    res = p._exec(["/bin/sh", "-c", "cat >/dev/null; head -c 200000 /dev/zero | tr '\\0' x; echo err >&2; exit 3"],
                  "in", str(tmp_path), "/bin/sh")
    assert res.returncode == 3 and len(res.stdout) == 200000 and res.stderr.strip() == "err"
    assert list(tmp_path.iterdir()) == []                      # the CLI's folder stays empty
    cp._LOGIN.clear()
