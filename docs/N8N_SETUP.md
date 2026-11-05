# Setting up the n8n side

MailRoom keeps its shared state in n8n: settings, tracked senders, the list of handled emails,
the BOOK / ADD / KEEP / SWAP decisions and the recaps all live in n8n **data tables**, and the
**n8n** button runs the same job as the other engines through six workflows. You need this even if
you only use the Claude, ChatGPT, Gemini or Ollama buttons.

Files: `n8n/tables.json` (table definitions) and `n8n/workflows/*.json` (workflow templates).

## Requirements

- An n8n instance you control (self-hosted or n8n Cloud) that has **Data tables** and the
  **public API** enabled, reachable over HTTPS from your Mac.
- An n8n **API key** (n8n → Settings → n8n API) with access to workflows, executions and data tables.

## 1. Create the data tables

Create the five tables listed in `n8n/tables.json`, with exactly those names and columns.
MailRoom finds them by name. Leave them empty.

## 2. Prepare the workflow files

The templates contain placeholders. Replace them before importing (any text editor, or `sed`):

| Placeholder | Replace with |
|---|---|
| `https://YOUR-N8N-HOST` | your n8n address, e.g. `https://n8n.example.com` |
| `mailroom-run-CHANGE-ME`, `mailroom-decide-CHANGE-ME`, `mailroom-confirm-CHANGE-ME` | three long random strings (they become webhook paths; the decision links are only protected by single-use tokens, so do not use guessable paths) |
| `REPLACE_WITH_TABLE_ID__<name>` | the ID of that data table (shown in the table's URL in n8n) |
| `REPLACE_WITH_DRIVE_FOLDER_ID` | the Drive folder for recap Docs (the last part of its URL) |
| `Alex Doe` / `Alex` | your name, as it should appear in drafted emails |

`REPLACE_WITH_WORKFLOW_ID__<name>` placeholders are fixed after import (step 4).

## 3. Credentials

Create these credentials in n8n (the templates refer to them by name):

| Credential | Used for |
|---|---|
| Gmail account (Gmail OAuth2) | reading mail, creating drafts |
| Google Calendar account (Google Calendar OAuth2) | free/busy, creating events after you confirm |
| Google Docs account (Google Docs OAuth2) | the recap Doc |
| Anthropic account | the models (Claude) |
| Inbox Pilot webhook key (Header Auth, header name `X-Inbox-Pilot-Key`) | protects the run webhook |

The OpenAI and Ollama recap nodes ship disabled; you can delete them or link your own credentials.

## 4. Import and connect

1. Import the workflows in this order: `06-busy-calendars`, `05-decisions`, `04-recap`, `03-events`,
   `02-drafting`, `01-gmail-assistant` (n8n → Workflows → Import from file).
2. In every **Execute Workflow** node, pick the right sub-workflow from the list (this replaces
   the `REPLACE_WITH_WORKFLOW_ID__…` placeholders).
3. Open each workflow and select the credential in every node that shows a warning.
4. Publish (activate) all six workflows.

## 5. Point MailRoom at it

In MailRoom → Settings → Advanced:

- **Secrets** → paste the n8n API key.
- **Connections** → n8n address; n8n run webhook = the Production URL of the Webhook node in
  "Gmail Assistant"; decision page webhook = the Production URL of the "Decision Link" node in
  "Gmail Assistant — Decisions".
- **Webhook key** → Create key (stored in the macOS Keychain and in the n8n credential).
- **n8n workflows** → Discover workflows, to check that MailRoom sees them.

Then set your addresses, reply drafting, scheduling and tracked senders in the main Settings
sections; they are written to the data tables you created.

## Notes

- The MailRoom app works in your Mac's time zone (or `MAILROOM_TIMEZONE`). The n8n templates
  are written for **Europe/Rome**: if you live elsewhere, replace `Europe/Rome` in the workflow
  files with your zone (an IANA name such as `America/New_York`) before importing.
- The Daily Trigger in "Gmail Assistant" ships disabled. Enable it if you want a scheduled run.
