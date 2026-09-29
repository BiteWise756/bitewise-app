# BiteWise AI Customer Support — Setup Guide

This document explains how to configure the AI customer-support feature end to
end. The feature reads email sent to **mybitewise@gmail.com**, identifies the
customer, generates an answer with the **existing Gemini integration** (same
`GEMINI_MODEL`, no second AI provider), replies inside the same Gmail thread,
and escalates sensitive issues to a human through the staff dashboard
(`support_dashboard.html`).

> **Keep OTP separate and untouched.** The existing OTP email flow uses
> `GMAIL_REFRESH_TOKEN` with scope `gmail.send` and is unchanged. The support
> feature uses its **own** refresh token in `GMAIL_SUPPORT_REFRESH_TOKEN` with
> scope `gmail.modify`. Never swap or share these two tokens.

---

## A. Google Cloud requirements

1. Open the Google Cloud Console for the OAuth project currently used by
   BiteWise (the OTP mailer references **nutrilens-ai-fd9fb**; use that same
   project — do **not** create a new OAuth client unless it is missing).
2. Confirm the project has the **Gmail API** enabled:
   `APIs & Services > Library > Gmail API > Enable`.
3. Confirm an **OAuth 2.0 Client ID** of type **Web application** exists with
   the redirect URI `http://127.0.0.1` (loopback redirects are allowed by
   default for Web clients) — this is what the local helper uses.
4. Note down (without committing) the client **ID** and **secret**:
   they are already used for OTP sending, so they also exist in
   `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`.

Package dependencies already present in the backend venv:
`google-api-python-client`, `google-auth`, `google-auth-httplib2`
(see `backend/requirements*.txt`). The local OAuth helper uses only the
Python standard library for authorization.

---

## B. Gmail API

- Outbound side (unchanged): `gmail_mailer.py` sends OTP/verification mail
  with scope `https://www.googleapis.com/auth/gmail.send`.
- Inbound side (support): `gmail_inbox.py` reads mail (`users.messages.list`
  / `users.messages.get`), marks it read (`users.messages.modify`) and sends
  thread replies (`users.messages.send` with `threadId`).

---

## C. gmail.modify scope

The support feature requires a refresh token granted with:

```
https://www.googleapis.com/auth/gmail.modify
```

`gmail.modify` allows reading messages, changing labels (marking as read) and
sending messages — everything the support pipeline needs. **The existing OTP
refresh token only has `gmail.send` and cannot read the inbox.**

---

## D. Get the support refresh token locally

A dedicated local utility is provided:

```
backend/tools/get_support_token.py
```

It is **a developer tool for your own machine**:

- It is never imported or executed by the application.
- It refuses to run inside Railway (detects `RAILWAY_*`) or in a
  non-interactive terminal.
- It does **not** modify `GMAIL_REFRESH_TOKEN` or any OTP credential.
- It reads `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` from the environment or
  `backend/.env`; it never prints the client secret, access token, or id token.
- It writes the refresh token to a file **outside the repository**
  (temp dir, permissions `0600`) and prints the value so you can copy it.

Steps:

```powershell
# from the backend/ directory
$env:GMAIL_CLIENT_ID = "..."            # or put these in backend/.env (never commit)
$env:GMAIL_CLIENT_SECRET = "..."
.\venv\Scripts\python.exe tools\get_support_token.py
```

1. The browser opens the Google consent screen for `mybitewise@gmail.com`
   with `https://www.googleapis.com/auth/gmail.modify`.
2. Sign in as `mybitewise@gmail.com` and allow access (offline access with
   forced consent is used so a refresh token is always issued).
3. The script prints the **support refresh token** and saves it to
   `%TEMP%\bitewise-support-refresh-token.txt`.
4. Copy the token into Railway as `GMAIL_SUPPORT_REFRESH_TOKEN` (only), then
   delete the temp file.

Optional verification:

```powershell
.\venv\Scripts\python.exe tools\get_support_token.py --verify
```

This uses the token to call `gmail.users.getProfile` and confirms the scope
works and the account is `mybitewise@gmail.com`.

> If the browser does not open, paste the printed authorization URL manually.
> If you see `error=access_denied`, you signed in with the wrong account.
> If no refresh token is returned, complete the consent screen fully.

---

## E. Railway variables

| Variable | Value |
| --- | --- |
| `GMAIL_CLIENT_ID` | unchanged (already set) |
| `GMAIL_CLIENT_SECRET` | unchanged (already set) |
| `GMAIL_REFRESH_TOKEN` | **unchanged** — OTP sending (do not touch) |
| `GMAIL_SUPPORT_REFRESH_TOKEN` | the new refresh token from section D, `gmail.modify` scope, account `mybitewise@gmail.com` |
| `SUPPORT_STAFF_EMAILS` | comma-separated emails allowed to open the dashboard (doctor/admin accounts are always allowed) |
| `SUPPORT_POLL_ENABLED` | `true` to run the background poller |
| `SUPPORT_POLL_INTERVAL_SECONDS` | default `60` (minimum `10`) |

---

## F. How OTP remains separate

| Concern | Mechanism |
| --- | --- |
| Tokens | `gmail_mailer.py` (OTP) uses `GMAIL_REFRESH_TOKEN` + `gmail.send`; `gmail_inbox.py` (support) uses `GMAIL_SUPPORT_REFRESH_TOKEN` + `gmail.modify`. |
| Data | Support pipelines read **incoming** mail (`to:mybitewise@gmail.com is:unread`) and skip any message whose sender is `mybitewise@gmail.com`, so OTP emails sent by the app are never treated as conversations. |
| Secrets in AI | `safe_customer_profile()` / `build_support_context()` whitelist safe fields and explicitly drop keys containing `otp`, `password`, `hash`, `secret`, `token`. Tested in `backend/test_support.py` (`test_build_context_never_contains_secrets`, `test_context_handled_to_ai_never_contains_secrets`). |
| Escalation | Messages containing `otp`, `password`, `refund`, `legal`, etc. go to `NEEDS_HUMAN` **before** any Gemini call. |

---

## G. Enable the poller

```powershell
# Railway variables
SUPPORT_POLL_ENABLED=true
SUPPORT_POLL_INTERVAL_SECONDS=60
```

At startup `app.py` starts a daemon thread that polls the inbox on the
interval. The poller only starts when the variable is true **and** Gmail
credentials exist; failures are logged and never crash the server. You can
also trigger a poll manually at any time from the dashboard
(“Check inbox now”), which calls `POST /support/poll` (staff only).

---

## H. Test a real incoming email

1. Deploy with `GMAIL_SUPPORT_REFRESH_TOKEN` and `SUPPORT_POLL_ENABLED=true`.
2. From any mailbox, send an email **to** `mybitewise@gmail.com` with a
   benign question, e.g. “How do I log a meal in the app?” from an address
   registered in the users collection.
3. Within one poll interval the customer receives an auto-reply inside the
   same thread, and the conversation appears in
   `/app/support_dashboard.html` with status `AI_REPLIED`.

---

## I. Test Gmail threading

1. Send a second email **replying to the previous thread** (use Reply in your
   mail client so Gmail keeps the same `threadId`).
2. The AI answer arrives in the same thread on Gmail’s side.
3. In the dashboard, both inbound messages and the AI reply appear under one
   conversation — `threadId` is preserved because replies send with the
   original `threadId` plus `In-Reply-To` / `References` headers.

---

## J. Test duplicate protection

1. With the poller enabled, configure `SUPPORT_POLL_INTERVAL_SECONDS=10` and
   send one email.
2. Observe several poll cycles (or press “Check inbox now” repeatedly).
3. No duplicate replies are sent: each Gmail message id is stored in
   `support_conversations/{threadId}/messages/{messageId}` and messages
   already stored are skipped. Restarting the server also does not re-process
   already-stored messages.

---

## K. Test human escalation

Each of the following creates a conversation with status **`NEEDS_HUMAN`**
and no auto-reply:

- “I want a refund” / “I was charged twice” (refunds, billing)
- “I did not receive my OTP” / “help me reset my password” (OTP/password)
- “someone hacked my account” (security)
- “I am contacting a lawyer” (legal)
- any question where Gemini is unavailable or reports `needs_human`
- any message in a conversation a human already took over

Open the dashboard, open the conversation, and try:

- **Take over** → status `HUMAN_HANDLED`, AI disabled for that thread
  (further customer emails are escalated, never auto-answered).
- **Reply** → the answer is sent to the customer in the same thread and
  recorded as a human message.
- **AI enable/disable** → toggles the autopilot per conversation.
- **Resolve** → closes the conversation (`RESOLVED`).

---

## L. Test OTP isolation

1. Sign up a new account through the app (this sends the OTP email with the
   existing `GMAIL_REFRESH_TOKEN` sender flow — nothing changed).
2. Confirm the OTP email still arrives and the verification still works.
3. Send the account’s OTP **code** (or “I did not receive my OTP”) in an
   email to `mybitewise@gmail.com` — the conversation must escalate to
   `NEEDS_HUMAN` and the code must never appear in a Gemini prompt or reply.
4. Run the suite: `backend/test_support.py` includes the OTP-isolation tests,
   and the OTP suites (`test_verify_email.py`, `test_gmail_mailer.py`,
   `test_auth_routes.py`) must stay green.

---

## Documentation tables summary

| File | Purpose |
| --- | --- |
| `backend/services/gmail_inbox.py` | inbox read + threaded reply (`gmail.modify`) |
| `backend/services/support_service.py` | pipeline, dedupe, statuses, context whitelist |
| `backend/services/gemini_service.py` | `generate_support_reply` (existing Gemini) |
| `backend/routes/support.py` | staff-only API |
| `support_dashboard.html` | staff dashboard |
| `backend/tools/get_support_token.py` | local-only OAuth helper |
| `backend/test_support.py` | support + isolation tests |