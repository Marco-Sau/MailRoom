"""'Did you read the doc I sent you?': find the earlier item, then draft a reply that confirms it
arrived (never that it was read) or asks for it again."""
import json
from datetime import timedelta

from mailroom.assistant import logic, prompts
from mailroom.models import RunSpec
from mailroom.runners import assistant_runner as ar

from tests.test_assistant_runner import NOW, FakeModel, mail, world  # noqa: F401 - pytest fixture

MS = int(NOW.timestamp() * 1000)


def msg(i, subject, days_ago, thread="tX", attachments=(), text="", labels=("INBOX",)):
    return {"id": f"r{i}", "threadId": thread, "subject": subject, "text": text, "html": "",
            "internalDate": str(MS - days_ago * 86400000), "labelIds": list(labels),
            "attachments": [{"filename": f, "mimeType": m} for f, m in attachments]}


CURRENT = {"id": "c1", "threadId": "tA", "internalDate": str(MS)}


def test_candidates_rank_keywords_thread_and_skip_noise():
    msgs = [
        msg(1, "Foto", 2, attachments=[("image001.png", "image/png"), ("invite.ics", "text/calendar")]),
        msg(2, "Bozza bilancio 2027", 5, attachments=[("Bilancio_2027_v3.pdf", "application/pdf")],
            labels=("UNREAD", "INBOX")),
        msg(3, "Notes", 1, thread="tA", text="see https://docs.google.com/document/d/abc/edit?usp=sharing&amp;x=1."),
        msg(4, "Later", -1, attachments=[("future.pdf", "application/pdf")]),          # after the current email
        msg(5, "Old", 20, attachments=[("scan.jpg", "image/jpeg")]),                     # a real image, kept
    ]
    c = logic.reference_candidates(msgs, CURRENT, ["bilancio", "2027"])
    names = [x["name"] for x in c]
    assert names[0] == "Bilancio_2027_v3.pdf" and c[0]["score"] == 6 and c[0]["unread"] is True
    assert names[1] == "https://docs.google.com/document/d/abc/edit?usp=sharing&x=1" and c[1]["sameThread"]
    assert "scan.jpg" in names
    assert not {"image001.png", "invite.ics", "future.pdf"} & set(names)
    assert c[0]["at"] == (NOW - timedelta(days=5)).date().isoformat()


def test_reference_prompt_lists_items_and_rules():
    j = {**mail(1, "Re: bilancio", text="Hai letto il documento che ti ho mandato?"),
         "output": {"referenceDescription": "the budget document"}}
    c = logic.reference_candidates([msg(2, "Bozza", 5, attachments=[("Bilancio.pdf", "application/pdf")])],
                                   {"id": "x", "threadId": "t", "internalDate": str(MS)}, ["bilancio"])
    _, user = prompts.reference_reply(j, c, NOW)
    assert '0. Attachment: Bilancio.pdf | received' in user and "the budget document" in user
    assert "Never say or imply that it has been read" in user and "[your comments]" in user
    _, empty = prompts.reference_reply(j, [], NOW)
    assert "(none found)" in empty


class RefModel(FakeModel):
    def __init__(self, match):
        super().__init__()
        self.match = match

    def complete(self, system, user, *, max_tokens=1500, temperature=0.2):
        self.prompts.append(user)
        if "STEP 1 -- CONFIRMATION CHECK" in user:
            return json.dumps({"isAppointmentRequest": False, "isConfirmingProposal": False, "confirmedSlotIndex": -1,
                               "needsReply": True, "draftReply": "generic", "refersToEarlier": True,
                               "referenceDescription": "the budget file", "referenceKeywords": ["bilancio"]})
        if "Items found in the recipient's mailbox" in user:
            return json.dumps({"matchIndex": self.match,
                               "draftReply": "Sì, è arrivato.\n\n[your comments]" if self.match >= 0 else "Puoi reinviarlo?"})
        return super().complete(system, user, max_tokens=max_tokens, temperature=temperature)


def _run(world, monkeypatch, model, found):
    inbox = [mail(1, "Hai letto il bilancio?")]
    seen = []

    def search(q, after, limit, full=True):
        seen.append(q)
        if "has:attachment" in q:
            return found
        return inbox if q.startswith("{from:") else []
    monkeypatch.setattr(ar.google_io, "search", search)
    res = ar.AssistantRunner(lambda n, m: model).run(
        RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli", steps=["drafting"]))
    return res, seen


def test_found_item_reply_confirms_arrival(world, monkeypatch):
    found = [msg(2, "Bozza", 3, attachments=[("Bilancio.pdf", "application/pdf")])]
    res, seen = _run(world, monkeypatch, RefModel(0), found)
    assert res.status == "succeeded", res.log
    assert any(q.startswith('from:"anna@acme.com"') and "has:attachment" in q for q in seen)
    draft = [d for d in world["drafts"] if d.get("to")][0]
    assert "[your comments]" in draft["body"]
    assert any("found Bilancio.pdf" in line for line in res.log) and res.stats["references_found"] == 1


def test_missing_item_reply_asks_to_resend(world, monkeypatch):
    res, _ = _run(world, monkeypatch, RefModel(-1), [])
    draft = [d for d in world["drafts"] if d.get("to")][0]
    assert draft["body"] == "Puoi reinviarlo?" and res.stats["references_missing"] == 1


def test_search_failure_never_claims_not_found(world, monkeypatch):
    inbox = [mail(1, "Hai letto il bilancio?")]

    def search(q, after, limit, full=True):
        if "has:attachment" in q:
            raise RuntimeError("quota")
        return inbox if q.startswith("{from:") else []
    monkeypatch.setattr(ar.google_io, "search", search)
    model = RefModel(-1)
    res = ar.AssistantRunner(lambda n, m: model).run(
        RunSpec(job_id="x", name="x", runner="assistant", provider="claude_cli", steps=["drafting"]))
    draft = [d for d in world["drafts"] if d.get("to")][0]
    assert draft["body"] == "generic" and not any("Items found" in p for p in model.prompts)
