"""Email Assistant engine: the n8n Gmail Assistant, run by the app itself.

The app does every Gmail / Calendar / Docs operation; the chosen model (Claude,
ChatGPT, Gemini through their official CLIs, or local Ollama) only classifies
emails and writes text. Settings, tracked senders, the "already handled" list
and decision links are shared with n8n through its data tables, so both engines
behave the same and never handle the same email twice.
"""
