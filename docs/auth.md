# Authentication & accounts

Phare is **multi-user**: every account is one human with their own credentials and their own,
fully isolated taste world. This replaces the earlier single shared-password (`AUTH_PASSWORD`)
gate, which proved no real identity and so couldn't isolate anyone — see the history note at the
end.

> Principle alignment: **"one account = one user"** (`CLAUDE.md`) is now literal — an account *is*
> a user, 1:1 with a `Profile`. **Per-profile isolation** (`docs/design.md`) is now enforced at
> the query layer against a real authenticated identity, not aspirational.

## The model

```
User      — the identity:  id, email (nullable), display_name, is_admin, created_at
              owns exactly one Profile (1:1; the Profile is created with the account)
Identity  — how a user proves who they are (a User may have several):
              (user_id, provider, subject, secret)
              · local → subject = email,            secret = argon2id(password)
              · plex  → subject = plex account id,   secret = null   (+ SourceToken "plex")
              (trakt, oidc, … slot in later as new provider values — additive only)
Profile   — unchanged: the taste/history container, 1:1 with its User
```

Identity is keyed on **`(provider, subject)`**, not email. Email is preferred metadata, filled in
when a provider supplies one (Plex does; Trakt may not) — never the join key. This is why a new
provider never touches `User`, the token, or the isolation logic: it only adds `Identity` rows.

`Identity.secret` holds the argon2id password hash for `local`; it is `null` for source providers
(the proof is the OAuth grant, not a stored secret). The source provider's access token lives in
the existing `SourceToken` table, encrypted — so signing in with a source also connects it for
ingestion in the same act.

## Tokens

Stateless, signed, **identity-bearing** bearer tokens — the one structural fix that makes
isolation possible. Format: `<user_id>.<expiry>.<hmac_sha256>` signed with `SECRET_KEY`. The
backend resolves the token to a `current_user`; there is no session store. Default TTL 30 days
(`AUTH_TOKEN_TTL_SECONDS`). Held in memory only on the client (never `localStorage`).

However a user authenticates — password today, Plex now, Trakt tomorrow — the flow ends by minting
*our own* token. The SPA never sees a provider token.

`SECRET_KEY` is **required** when any account exists; it signs tokens and derives the
`SourceToken` encryption key. No more fallback to `AUTH_PASSWORD` (which no longer exists).

## Local accounts (email + password)

- `POST /auth/register` — create a local account (gated by provisioning rules, below). Creates the
  `User`, its `Profile`, and a `local` `Identity` with an argon2id hash.
- `POST /auth/login` — email + password → token.
- Passwords hashed with **argon2id** (`argon2-cffi`). Never stored or logged in plaintext.
- Password reset is admin-driven for now (no SMTP). Email-based reset is a later extension.

## Sign in with Plex

The source *is* the identity provider — and Plex carries its own authorization (server
membership), so it's secure-by-default without an allowlist. This is the Overseerr/Jellyseerr
pattern.

Flow (PIN auth):

1. `POST /auth/plex/start` → backend requests a PIN from plex.tv, returns `{ id, code, authUrl }`.
2. Frontend opens `app.plex.tv/auth#?clientID=…&code=…` in a popup.
3. `POST /auth/plex/poll { id }` → backend polls plex.tv until the PIN yields an `authToken`,
   then reads the Plex account (id, username, email).
4. **Membership gate** (see below): is this Plex account allowed?
5. If yes → upsert `User` + `plex` `Identity`, store the `authToken` as `SourceToken("plex")`,
   mint our token. If no → `403`.

### The membership gate — "first sign-in = owner, auto-binds"

There is no allowlist to maintain. The **first** Plex sign-in becomes the **owner/admin**, and the
set of Plex servers that account can access becomes the reference. Every later Plex sign-in must
share access to one of those servers (the owner's server, i.e. the owner plus the friends they've
shared their library with). Strangers with an unrelated Plex account are refused.

Server membership is read from plex.tv's resource list for the signing-in account and intersected
with the owner's. The binding (the owner's server machine identifiers) is stored once at first
sign-in.

## Provisioning — who may get an account

| Method | Gate |
| --- | --- |
| Plex   | Must share a Plex server with the owner (auto-bound at first sign-in). First sign-in becomes owner/admin. |
| Local  | Admin-created for now. `REGISTRATION_OPEN=true` opens public local self-registration. |

Secure by default: with no users yet, the app shows a **first-run setup** (create the first local
admin, or sign in with Plex to become owner). There is no open/anonymous API mode — every data
endpoint requires a valid token, always.

## Isolation (enforced, not promised)

Strict 1:1 makes this simple: a `current_user` maps to exactly one `profile_id`.

- `GET /profiles` returns only the caller's own profile.
- Every `/profiles/{profile_id}/…` route asserts `profile_id == current_user.profile_id`,
  else `404` (not `403` — don't confirm another profile exists).
- The same check guards the source/sync routes that carry a `profile_id`.
- `is_admin` gates admin-only operations (e.g. creating local accounts, future user management).

A regression test proves user A cannot reach user B's profile, history, taste, or memory.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | _(required once an account exists)_ | Signs identity-bearing tokens; derives the `SourceToken` encryption key. |
| `AUTH_TOKEN_TTL_SECONDS` | `2592000` (30 days) | Bearer token lifetime. |
| `REGISTRATION_OPEN` | `false` | When true, anyone may self-register a local account. Default closed. |
| `PLEX_CLIENT_IDENTIFIER` | _(generated/pinned)_ | Stable client id Phare presents to plex.tv. |
| `PLEX_PRODUCT_NAME` | `Phare` | Product name shown on the Plex auth screen. |

`AUTH_PASSWORD` is **removed**. Deployments that used it must create accounts instead.

## Tests stay hermetic

No test hits real Plex (same rule as the no-real-LLM rule in `CLAUDE.md`): a `FakePlexProvider`
behind the `AuthProvider` interface supplies canned identities and membership. `conftest.py` keeps
blanking provider credentials so a developer's `.env` can't leak in.

## Extending later (the seam)

`AuthProvider` interface: `start()`, `resolve(grant) -> Identity(subject, email, display_name)`,
`is_authorized(identity) -> bool`. Plex is the first implementation. Adding Trakt (or generic
OIDC) means a new implementation + a new `provider` value + a callback route — no change to `User`,
the token, the `current_user` dependency, or the isolation checks.

## History (why the change)

The first cut implemented "one account = one user" as **one shared password for the whole
instance**. The token carried no identity, so the backend couldn't tell users apart — the
"per-profile isolation" promised in `docs/design.md` had nothing to isolate against, and the API
was fully open when `AUTH_PASSWORD` was unset. This redesign gives every token a subject, makes
isolation real, and is closed by default.
