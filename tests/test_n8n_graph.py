"""Graph reasoning for the webhook injection. These functions decide where a new
trigger gets wired into a live workflow, so they are worth pinning down."""
from __future__ import annotations

from mailroom.connectors import n8n

WF = {
    "id": "abc", "name": "Gmail Assistant", "active": True,
    "nodes": [
        {"name": "Every morning", "type": "n8n-nodes-base.scheduleTrigger",
         "position": [0, 0], "parameters": {}},
        {"name": "Fetch mail", "type": "n8n-nodes-base.gmail",
         "position": [220, 0], "parameters": {}},
        {"name": "Classify", "type": "n8n-nodes-base.openAi",
         "position": [440, 0], "parameters": {}},
    ],
    "connections": {
        "Every morning": {"main": [[{"node": "Fetch mail", "type": "main", "index": 0}]]},
        "Fetch mail": {"main": [[{"node": "Classify", "type": "main", "index": 0}]]},
    },
}


def test_webhook_targets_the_step_the_existing_trigger_feeds():
    assert n8n._downstream_of_triggers(WF) == ["Fetch mail"]


def test_non_trigger_nodes_are_not_treated_as_entry_points():
    # "Fetch mail" also has outgoing connections, but it is not a trigger.
    assert "Classify" not in n8n._downstream_of_triggers(WF)


def test_multiple_triggers_are_deduplicated():
    wf = {**WF,
          "nodes": WF["nodes"] + [{"name": "On email", "type": "n8n-nodes-base.gmailTrigger",
                                   "position": [0, 200], "parameters": {}}],
          "connections": {**WF["connections"],
                          "On email": {"main": [[{"node": "Fetch mail", "type": "main",
                                                  "index": 0}]]}}}
    assert n8n._downstream_of_triggers(wf) == ["Fetch mail"]


def test_trigger_connected_to_nothing_yields_no_targets():
    wf = {**WF, "connections": {}}
    assert n8n._downstream_of_triggers(wf) == []


def test_existing_webhook_is_detected_by_path_not_by_name():
    assert n8n._existing_webhook(WF, "gmail-assistant") is None
    wf = {**WF, "nodes": WF["nodes"] + [
        {"name": "Anything", "type": "n8n-nodes-base.webhook",
         "parameters": {"path": "gmail-assistant"}}]}
    assert n8n._existing_webhook(wf, "gmail-assistant")["name"] == "Anything"
    assert n8n._existing_webhook(wf, "other-path") is None


def test_short_type_strips_the_package_prefix():
    assert n8n.short_type("n8n-nodes-base.gmailTrigger") == "gmailTrigger"
    assert n8n.short_type("@n8n/n8n-nodes-langchain.chatTrigger") == "chatTrigger"


def test_localhost_urls_also_probe_the_ipv4_literal():
    assert n8n.candidates("http://localhost:5678") == [
        "http://localhost:5678", "http://127.0.0.1:5678"]
    assert n8n.candidates("https://n8n.example.com") == ["https://n8n.example.com"]


# --------------------------------------------------- adopting an existing hook
ORPHAN = {
    "id": "5sun", "name": "Gmail Assistant", "active": True,
    "nodes": [
        {"name": "Manual Trigger", "type": "n8n-nodes-base.manualTrigger",
         "position": [0, 0], "parameters": {}},
        {"name": "Morning Trigger", "type": "n8n-nodes-base.scheduleTrigger",
         "position": [0, 160], "parameters": {}},
        {"name": "Run Drafting", "type": "n8n-nodes-base.executeWorkflow",
         "position": [300, 0], "parameters": {}},
        # n8n drops this in with a UUID path and no connections.
        {"name": "Webhook", "type": "n8n-nodes-base.webhook", "typeVersion": 2.1,
         "position": [-160, 688], "parameters": {"options": {}, "path": "run-webhook-uuid"}},
    ],
    "connections": {
        "Manual Trigger": {"main": [[{"node": "Run Drafting", "type": "main", "index": 0}]]},
        "Morning Trigger": {"main": [[{"node": "Run Drafting", "type": "main", "index": 0}]]},
    },
}


def test_an_unconnected_webhook_is_not_counted_as_wired():
    assert n8n._is_wired(ORPHAN, "Webhook") is False
    assert n8n._is_wired(ORPHAN, "Manual Trigger") is True


def test_webhook_nodes_are_found_regardless_of_path():
    assert [n["name"] for n in n8n._webhook_nodes(ORPHAN)] == ["Webhook"]
    assert n8n._existing_webhook(ORPHAN, "gmail-assistant") is None


def test_several_triggers_all_pointing_at_one_node_collapse():
    assert n8n._downstream_of_triggers(ORPHAN) == ["Run Drafting"]
