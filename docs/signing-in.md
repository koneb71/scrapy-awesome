# Signing in

The app runs a small HTTP server on loopback. Two kinds of caller reach it, and they prove
themselves differently.

## People: a username and password

The first time you open the UI it asks you to create a login. After that, `/login` is the way in
and the session lasts 30 days in an HttpOnly cookie.

```bash
uv run scrapy-awesome passwd                # set or change it from the terminal
uv run scrapy-awesome passwd --username me  # …and rename the account
uv run scrapy-awesome passwd --reset        # forget it; the UI offers first-run setup again
```

The password is stored in `credentials.json` in the data dir, mode 0600, as a salted **scrypt**
hash (`n=2^16, r=8, p=1` — about 64 MB of memory per attempt, so a stolen file is not worth
grinding). The plaintext is never written anywhere, never logged, and never leaves the machine.

Sign-in attempts are throttled: 8 failures inside 5 minutes locks the door for a minute, and the
lock applies to the right password too — it is on the door, not on the guess.

Changing the password ends every other browser session, whether you change it in
**Settings → Login** or with `scrapy-awesome passwd` (the CLI asks the running server to drop
them).

Forgot it? Anyone who can run `scrapy-awesome passwd` on this machine can set a new one. That is
not a hole: the same person can already read the data dir. The login keeps *other things running
on the machine* — a stray browser tab, a page you are scraping, a local process — out of your
recipes, runs and logged-in sessions.

## Machines: the per-process token

The MCP server, the CLI and each crawl worker talk to the same API with
`Authorization: Bearer <token>`, where the token is generated per process and written to
`server.json` (also 0600) in the data dir. There is no human in those paths to type a password,
and the file is readable only by the account running the app.

`GET /auth?token=…` trades that token for a session cookie. It exists for the desktop shell, which
opens its window before anyone has set a login. **Once a username and password exist it stops
signing browsers in** and redirects to `/login` — otherwise a URL from a shell history or a log
file would be a way around the password.

So `scrapy-awesome open` prints a plain URL when a login is configured, and the token link only
when there is none yet.

## Over the network

This is a loopback app: passwords go over plain HTTP, which is fine to `127.0.0.1` and is **not**
fine across a network. If you expose the port (a tunnel, a reverse proxy, `--host 0.0.0.0` in a
fork), terminate TLS in front of it — the session cookie sets `Secure` automatically when the
request arrives over HTTPS.
