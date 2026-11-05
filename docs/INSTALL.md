# Installing MailRoom

About 30 minutes the first time, most of it in the Google Cloud console. Everything runs on your
Mac; you use your own Google OAuth client, your own n8n instance and your own AI plans.

**Contents:** 1. Requirements · 2. Get the app · 3. Google · 4. n8n · 5. Engines ·
6. Your settings · 7. First run · 8. Automatic runs · 9. Updating · 10. Uninstalling ·
11. Troubleshooting

---

## 1. Requirements

| What | Notes |
|---|---|
| macOS 12 or later | secrets are kept in the macOS Keychain |
| Python 3.11 or later | `python3 --version`; if needed: `brew install python@3.12` |
| git | `xcode-select --install` provides it |
| A Google account | Gmail, Google Calendar, Google Docs |
| An n8n instance | self-hosted or n8n Cloud, with Data tables and the public API (section 4) |
| At least one engine | Claude Code, Codex CLI, Antigravity CLI, or Ollama (section 5) |

## 2. Get the app

```bash
git clone https://github.com/Marco-Sau/MailRoom.git
cd MailRoom
./scripts/dev.sh
```

`dev.sh` creates a virtual environment in `.venv`, installs the dependencies, copies
`config/*.example.yaml` to `config/*.yaml` and serves the app at <http://127.0.0.1:8765>.
Open that address in your browser.

For a double-clickable app:

```bash
./scripts/build_app.sh        # builds dist/MailRoom.app
open dist/MailRoom.app        # or drag it to /Applications
```

The `.app` is a thin launcher around this folder: on first launch it sets up `.venv` (with the
native window), and after a `git pull` you do not need to rebuild it. Rebuild only if you move the
folder. Launch logs: `~/Library/Logs/MailRoom/launch.log`.

## 3. Google

MailRoom talks to Gmail, Calendar, Docs and Drive with an OAuth client that **you** create, so
your mail is never shared with a third-party app.

### a. Project and APIs

1. Go to <https://console.cloud.google.com> and create a project (for example `MailRoom`).
2. **APIs & Services → Library**: enable **Gmail API**, **Google Calendar API**,
   **Google Docs API**, **Google Drive API** and **Google Sheets API**.

### b. Consent screen

3. **APIs & Services → OAuth consent screen** (or **Google Auth Platform → Branding / Audience**):
   - User type: **External** (Internal is available only on Workspace domains).
   - App name and support email: anything you like; your own address is fine.
   - Scopes: leave empty. MailRoom requests them when you connect.
   - **Test users**: add the Gmail address you will connect. Without it, consent fails.

### c. OAuth client

4. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
   - Application type: **Desktop app**. A Desktop client accepts the loopback redirect
     `http://127.0.0.1:8766` that MailRoom uses; a Web client would not.
   - Create, then **Download JSON**.

### d. Connect

5. In MailRoom: **Settings → Advanced → Secrets → Google OAuth desktop client JSON →
   Choose file…** and pick the downloaded file.
6. **Settings → Advanced → Google account → Connect**. Your browser opens. Google shows
   *"Google hasn't verified this app"*: choose **Advanced → Go to … (unsafe)**. The warning means
   Google has not reviewed *your* client; the connection itself is direct between your Mac and
   Google. Allow every permission: the diagnostics report any scope you leave out.

### e. The 7-day limit

While the consent screen's publishing status is **Testing**, Google expires refresh tokens after
7 days and MailRoom asks you to reconnect roughly weekly. To avoid that, set the publishing status
to **In production**. The app stays unverified (you keep seeing the warning at consent), but
tokens stop expiring. Verification is only needed to let other people use your OAuth client.

Scopes requested: `gmail.modify` (read mail, create drafts; MailRoom never sends),
`calendar.events` and `calendar.readonly`, `documents`, `drive.file` (only files the app creates)
and `spreadsheets` (the legacy recap sheet).

## 4. n8n

MailRoom keeps its shared settings, tracked senders, handled emails, decisions and recaps in n8n
data tables, for every engine; the **n8n** engine also runs the job as n8n workflows.

Follow **[N8N_SETUP.md](N8N_SETUP.md)**: create the tables, import the six workflows, add the
credentials, then in MailRoom fill **Settings → Advanced → Secrets** (n8n API key),
**Connections** (n8n address and webhooks) and **Webhook key**.

## 5. Engines

Install the ones you want. Each CLI keeps its own sign-in; MailRoom never sees your password.

| Engine | Install | Sign in |
|---|---|---|
| Claude | Claude Code, see <https://code.claude.com> | Settings → Engine sign-in → Claude → **Sign in** (runs `claude auth login` in Terminal) |
| ChatGPT | `brew install codex` (or `npm i -g @openai/codex`) | Engine sign-in → ChatGPT → **Sign in** (`codex login`, choose *Sign in with ChatGPT*) |
| Gemini | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` | Engine sign-in → Gemini → **Sign in** |
| Ollama | <https://ollama.com>, then `ollama pull <model>` (for example `ollama pull qwen3:32b`) | none; set the address in Advanced → Connections if it is not `http://127.0.0.1:11434` |

After signing in, press **Verify**: MailRoom sends one tiny prompt ("Reply with the word ok.")
and shows the model that answered. Choose a model per engine in **Settings → Models**
("Default" lets the CLI pick).

The CLIs are found through your login shell, so anything on your `PATH` in Terminal works.

## 6. Your settings

Fill the main **Settings** sections in order. Each field has an **(i)** button that explains it.

1. **Your addresses**: the addresses you receive mail at (**Scan my mailbox** suggests them).
2. **Reply drafting**: inbox or tracked senders only, signature name, default instructions.
3. **Meeting scheduling**: working-hour windows, busy calendars and booking calendars.
4. **Tracked senders**: people whose mail always gets a reply draft, with per-sender notes.
5. **Event words** and **Priority levels**: what counts as an event, and the rubric the models use to rank the recap.
6. **Engine sign-in** and **Models** (section 5).

Developer settings (look-back windows, timeouts, quick links, diagnostics) are under
**Settings → Advanced**.

Time zone: MailRoom uses your Mac's time zone. To override it, start the app with
`MAILROOM_TIMEZONE=America/New_York` (any IANA zone name) in the environment.

## 7. First run

1. **Settings → Advanced → Diagnostics → Run all checks**: everything should be green.
2. **Run** tab → choose a look-back window → press **Run** on one engine.
3. Watch the progress (Drafting, Events, Recap). Press **Stop** at any time; drafts already created
   stay in Gmail Drafts.
4. **Review** tab: check the drafts, edit them, open them in Gmail to send.

## 8. Automatic runs

**Run** tab → **Automatic runs** → **Automatic**, then choose the engine, the look-back window,
the days and times, and optionally the allowed hours. Press **Save** and confirm.

- Saving installs one per-user LaunchAgent, `~/Library/LaunchAgents/com.mailroom.autorun.plist`.
  It starts the run at those times even when the window is closed. Choosing **Manual** and
  **Save** removes it. Nothing else on the Mac changes.
- Your Mac must be on and you must be logged in. A time missed during sleep runs once on wake;
  times missed while the Mac was off are skipped. A run that would start outside the allowed hours
  is skipped, and so is one that would overlap another run.
- The app shows a notification for each new automatic result, and the panel lists recent runs.
- **Start one now** runs it once in the background, which is a good way to test the setup.
- Background log: `data/logs/autorun.log`.

## 9. Updating

```bash
cd MailRoom && git pull
```

Restart the app. The launcher reinstalls dependencies when `pyproject.toml` changes. If automatic
runs are on and the panel says the background job is out of date, press **Save** again.

## 10. Uninstalling

1. Run tab → Automatic runs → **Manual** → **Save** (removes the LaunchAgent).
2. Optional: revoke access at <https://myaccount.google.com/permissions> and delete the OAuth
   client in the Google Cloud console.
3. Remove the secrets: open **Keychain Access** and delete the items whose name is `inbox-pilot`.
4. Delete `dist/MailRoom.app` (or the copy in /Applications) and the `MailRoom` folder. Your local
   data (`data/`, `config/*.yaml`) is inside that folder.

## 11. Troubleshooting

| Symptom | What to do |
|---|---|
| An engine says it is not signed in | Settings → Engine sign-in → **Sign in**, then **Verify** |
| `... was not found` for a CLI | install it (section 5); check `command -v claude` (or `codex`, `agy`) in Terminal |
| Google asks to reconnect every week | set the consent screen to **In production** (section 3e) |
| `missing scope` in Diagnostics | Advanced → Google account → **Connect** again and allow every permission |
| n8n errors (401/403) | the n8n API key in Advanced → Secrets is wrong or lacks data-table access |
| A run is stuck | press **Stop**; a run whose process ended is marked *interrupted* automatically |
| Anything else | Advanced → **Diagnostics**; the last engine error is saved in `data/logs/` |
