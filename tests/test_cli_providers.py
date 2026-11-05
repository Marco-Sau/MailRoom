"""CLI providers, exercised against fake `claude` / `codex` / `gemini` executables
that record how they were called."""
import json
import os
import stat
import sys
import textwrap

import pytest

from mailroom.providers import cli_providers as cp
from mailroom.providers.base import ProviderError

FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, sys
    rec = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(),
            "files": sorted(os.listdir(".")) + (sorted(os.listdir(".gemini")) if os.path.isdir(".gemini") else []),
            "env_keys": [k for k in os.environ if k.endswith("API_KEY")]}}
    if os.path.isdir(".gemini"):
        rec["gemini_settings"] = json.load(open(".gemini/settings.json"))
    json.dump(rec, open({log!r}, "w"))
    mode = os.environ.get("FAKE_MODE", "ok")
    {body}
''')


def make(tmp_path, name, body):
    log = str(tmp_path / f"{name}.json")
    p = tmp_path / name
    p.write_text(FAKE.format(py=sys.executable, log=log, body=textwrap.indent(textwrap.dedent(body), "").replace("\n", "\n")))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), log


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")


def test_claude_flags_stdin_and_result(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "claude", '''
if "--tools" in sys.argv and mode == "old":
    sys.stderr.write("error: unknown option '--tools'"); sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"a": 1}',
                  "usage": {"input_tokens": 10, "output_tokens": 3}}))
''')
    monkeypatch.setenv("MAILROOM_CLAUDE_BIN", exe)
    p = cp.ClaudeCLIProvider("sonnet")
    assert p.complete_json("SYS", "<email>hi</email>") == {"a": 1}
    rec = json.load(open(log))
    a = rec["argv"]
    assert a[:3] == ["-p", "--output-format", "json"] and a[a.index("--model") + 1] == "sonnet"
    assert a[a.index("--tools") + 1] == "" and "--strict-mcp-config" in a and "mcp__*" in a
    assert a[a.index("--system-prompt") + 1].startswith("SYS")
    assert rec["stdin"] == "<email>hi</email>"                 # email never on the command line
    assert rec["files"] == [] and rec["env_keys"] == []        # empty folder, no API keys
    assert not os.path.exists(rec["cwd"])                      # temp folder removed
    assert (p.usage.input_tokens, p.usage.output_tokens, p.calls) == (10, 3, 1)


def test_claude_old_cli_falls_back_to_minimal_flags(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "claude", '''
if "--tools" in sys.argv:
    sys.stderr.write("error: unknown option '--tools'"); sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "result": "ok"}))
''')
    monkeypatch.setenv("MAILROOM_CLAUDE_BIN", exe)
    p = cp.ClaudeCLIProvider("default")
    assert p.complete("s", "u") == "ok"
    a = json.load(open(log))["argv"]
    assert "--tools" not in a and "--model" not in a and "--disallowedTools" in a
    assert p.notes and "minimal" in p.notes[0]


def test_claude_not_signed_in(tmp_path, monkeypatch):
    exe, _ = make(tmp_path, "claude", '''
print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Invalid API key · Please run /login"}))
''')
    monkeypatch.setenv("MAILROOM_CLAUDE_BIN", exe)
    with pytest.raises(ProviderError, match="not signed in"):
        cp.ClaudeCLIProvider("sonnet").complete("s", "u")


def test_codex_reads_last_message_file(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "codex", '''
out = sys.argv[sys.argv.index("--output-last-message") + 1]
open(out, "w").write("hello from codex")
print("progress noise")
''')
    monkeypatch.setenv("MAILROOM_CODEX_BIN", exe)
    p = cp.CodexCLIProvider("default")
    assert p.complete("SYS", "U") == "hello from codex"
    rec = json.load(open(log))
    a = rec["argv"]
    assert a[0] == "exec" and a[-1] == "-" and a[a.index("--sandbox") + 1] == "read-only"
    assert "shell_tool" in a and "--model" not in a
    assert rec["stdin"].startswith("SYS") and rec["stdin"].endswith("U") and "untrusted" in rec["stdin"]


def test_gemini_settings_exclude_tools_and_json_parse(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "gemini", '''
print("Loaded cached credentials.")
print(json.dumps({"response": "fine", "stats": {"models": {"m": {"tokens": {"prompt": 7, "candidates": 2}}}}}))
''')
    monkeypatch.setenv("MAILROOM_GEMINI_BIN", exe)
    p = cp.GeminiCLIProvider("gemini-x")
    assert p.complete("SYS", "U") == "fine"
    rec = json.load(open(log))
    assert "run_shell_command" in rec["gemini_settings"]["tools"]["exclude"]
    assert rec["argv"][rec["argv"].index("--model") + 1] == "gemini-x"
    assert (p.usage.input_tokens, p.usage.output_tokens) == (7, 2)


def test_missing_binary_is_explained(monkeypatch):
    monkeypatch.setattr(cp, "find_binary", lambda b: None)
    with pytest.raises(ProviderError, match="not found"):
        cp.GeminiCLIProvider("default").complete("s", "u")


def test_stop_kills_the_cli(tmp_path, monkeypatch):
    exe, _ = make(tmp_path, "claude", '''
import time; time.sleep(30)
''')
    monkeypatch.setenv("MAILROOM_CLAUDE_BIN", exe)
    from mailroom.runners import control
    control.begin(1, "assistant")
    control.request_stop()
    try:
        with pytest.raises(control.Stopped):
            cp.ClaudeCLIProvider("sonnet").complete("s", "u")
    finally:
        control.end()


def test_registry_knows_cli_providers():
    from mailroom.providers import PROVIDERS, get_provider
    for name in ("claude_cli", "codex_cli", "gemini_cli"):
        assert name in PROVIDERS and get_provider(name).name == name


# ------------------------------------------------------------- effort and the exact model served
def test_claude_effort_flag_and_reported_model(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "claude", '''
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                  "modelUsage": {"claude-haiku-4-5-20251001": {"outputTokens": 1},
                                 "claude-sonnet-5-5": {"outputTokens": 40}}}))
''')
    monkeypatch.setenv("MAILROOM_CLAUDE_BIN", exe)
    p = cp.ClaudeCLIProvider("claude-sonnet-5-5")
    p.effort = "high"
    assert p.complete("S", "U") == "ok"
    a = json.load(open(log))["argv"]
    assert a[a.index("--effort") + 1] == "high" and a[a.index("--model") + 1] == "claude-sonnet-5-5"
    assert p.served == {"model": "claude-sonnet-5-5", "asked": "claude-sonnet-5-5", "effort": "high"}
    from mailroom import model_prefs
    assert model_prefs.confirmed("claude_cli")["model"] == "claude-sonnet-5-5"


def test_codex_effort_override_and_header(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "codex", '''
out = sys.argv[sys.argv.index("--output-last-message") + 1]
open(out, "w").write("ok")
sys.stderr.write("workdir: /tmp/x\\nmodel: gpt-6.1-sol\\nprovider: openai\\nreasoning effort: high\\n")
''')
    monkeypatch.setenv("MAILROOM_CODEX_BIN", exe)
    p = cp.CodexCLIProvider("default")
    p.effort = "high"
    assert p.complete("S", "U") == "ok"
    a = json.load(open(log))["argv"]
    assert a[a.index("-c") + 1] == 'model_reasoning_effort="high"' and "--model" not in a
    assert p.served["model"] == "gpt-6.1-sol" and p.served["effort"] == "high"


def test_gemini_effort_becomes_an_alias_with_thinking_level(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "gemini", '''
print(json.dumps({"response": "ok", "stats": {"models": {"gemini-3-pro-preview": {"tokens": {"candidates": 3}}}}}))
''')
    monkeypatch.setenv("MAILROOM_GEMINI_BIN", exe)
    p = cp.GeminiCLIProvider("gemini-3-pro-preview")
    p.effort = "low"
    assert p.complete("S", "U") == "ok"
    rec = json.load(open(log))
    assert rec["argv"][rec["argv"].index("--model") + 1] == p.ALIAS
    alias = rec["gemini_settings"]["modelConfigs"]["customAliases"][p.ALIAS]
    assert alias["extends"] == "gemini-3-pro-preview"
    assert alias["modelConfig"]["model"] == "gemini-3-pro-preview"
    assert alias["modelConfig"]["generateContentConfig"]["thinkingConfig"]["thinkingLevel"] == "LOW"
    assert "run_shell_command" in rec["gemini_settings"]["tools"]["exclude"]      # hardening kept
    assert p.served["model"] == "gemini-3-pro-preview"


def test_gemini_auto_has_no_alias(tmp_path, monkeypatch):
    p = cp.GeminiCLIProvider("default")
    p.effort = "high"
    assert p._alias() is None


def test_registry_applies_saved_effort_per_model():
    from mailroom import model_prefs
    from mailroom.providers import get_provider
    model_prefs.set_effort("claude_cli", "claude-opus-5-5", "xhigh")
    assert get_provider("claude_cli", "claude-opus-5-5").effort == "xhigh"
    assert get_provider("claude_cli", "claude-sonnet-5-5").effort is None       # other model: its default
    assert get_provider("claude_cli", "claude-opus-5-5", "low").effort == "low"  # explicit wins


# ------------------------------------------------------------- Gemini via Antigravity CLI (agy)
def test_agy_prompt_in_p_flag_model_effort_and_json(tmp_path, monkeypatch):
    exe, log = make(tmp_path, "agy", '''
if mode == "ok":
    print("note on stdout before the result")
    print(json.dumps({"status": "SUCCESS", "response": "ranked", "usage": {"input_tokens": 12, "output_tokens": 3}}))
elif mode == "auth":
    print(json.dumps({"status": "ERROR", "error": {"message": "authentication required"}}))
    sys.exit(1)
elif mode == "wait":
    print(json.dumps({"status": "WAITING", "response": ""}))
''')
    monkeypatch.setenv("MAILROOM_AGY_BIN", exe)
    p = cp.AntigravityCLIProvider("gemini-3.8-flash-high")
    p.effort = "high"
    assert p.complete("SYSTEM RULES", "USER EMAIL") == "ranked"
    rec = json.load(open(log))
    argv = rec["argv"]
    assert argv[-2] == "-p" and "SYSTEM RULES" in argv[-1] and "USER EMAIL" in argv[-1] and "untrusted" in argv[-1]
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high" and argv[argv.index("--effort") + 1] == "high"
    assert "--output-format" in argv and "--dangerously-skip-permissions" not in argv
    assert rec["stdin"] == "" and rec["files"] == [] and rec["env_keys"] == []
    assert p.usage.input_tokens == 12 and p.served["model"] == "gemini-3.8-flash-high"

    monkeypatch.setenv("FAKE_MODE", "auth")
    with pytest.raises(ProviderError, match="not signed in to Antigravity"):
        p.complete("s", "u")
    monkeypatch.setenv("FAKE_MODE", "wait")
    with pytest.raises(ProviderError, match="tool approval"):
        p.complete("s", "u")


def test_agy_failure_log_hides_the_prompt(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("MAILROOM_DATA_DIR", str(tmp_path))
    from mailroom import settings as st
    p = cp.AntigravityCLIProvider("x")
    p._dump("/x/agy", subprocess.CompletedProcess(["/x/agy", "--model", "x", "-p", "SECRET EMAIL TEXT"], 1, "", "boom"))
    dumped = (st.settings.data_dir / "logs" / "gemini_cli-last-error.txt").read_text()
    assert "SECRET EMAIL TEXT" not in dumped and "boom" in dumped


def test_agy_models_parsed(tmp_path, monkeypatch):
    exe, _ = make(tmp_path, "agy", '''
print("Available models:")
print("  gemini-3.8-flash-high    Gemini 3.8 Flash (high)")
print("  claude-sonnet-4-6        Claude Sonnet 4.6")
print("  gpt-oss-120b             GPT OSS 120B")
''')
    monkeypatch.setenv("MAILROOM_AGY_BIN", exe)
    assert cp.AntigravityCLIProvider("x").list_models() == ["gemini-3.8-flash-high", "claude-sonnet-4-6", "gpt-oss-120b"]
