# Privacy lock, multiple books and shared books: design

Status: being built in phases (see E). Phase 0 (the session check) and phase 1 (encryption at
rest, 1a and 1b) are done. It was drafted from
a read-only review of the code on 2026-11-11. While the user was away they asked for the
recommended answers to be used; the answers taken are at the end.

## What the current code means for the design

- **The local address is open to other Windows sessions.** Another person signed in to the same
  PC can open `127.0.0.1:<port>`, because the app has no session check. It only has the
  host allow-list and the same-origin write check. Fix this first, whatever else is decided.
- **"Unreadable file reads as empty" becomes dangerous with encryption.** Several loaders return
  the empty value on any read error. With encryption, a locked or wrong-key read would look
  empty, and the next save would overwrite real data. Locked or failed decryption must raise.
- **`cryptography` is already installed.** It comes in through `mcp` → `pyjwt[crypto]`. It has
  AES-GCM, scrypt, Argon2id, HKDF, Ed25519 and X25519. Making it a direct dependency needs the
  user's OK, but it needs no new download.
- **File input and output already go through a few functions.** Writes go through
  `config.write_atomic`, reads through about a dozen `read_text` calls, and secrets through
  `secrets._write_in` and `_read_secret_file`.
- **Account ids differ in how stable they are.**
  - Stable on every computer: `hs-<id>`.
  - Random: `m-<hex>`.
  - Based on a local connection number, so they can differ between computers: `sp<n>-` and
    `fd<n>-`.
- **`Note.by == ""` means "you".** That is ambiguous once two people share a journal.

## A. Threat model

| Who | Today | Windows protection (DPAPI) only | Plus app password |
|---|---|---|---|
| Another Windows account browsing files | Blocked by folder permissions, unless they are an admin | Blocked | Blocked |
| Another Windows account opening the local address | **Open** | Blocked by the session cookie (phase 0) | Blocked |
| Someone at your unlocked Windows login | Open | **Open** | Blocked once Wardmind has locked |
| Stolen laptop or removed disk | Open | Protected by your Windows password | Protected |
| OneDrive or backup copies | Readable | Unreadable | Unreadable |
| Malware running as you | Open | Open | Open while unlocked |

- **Windows protection and Lock now:** the lock flag lives in each process. With Windows
  protection only, the MCP server unlocks itself through Windows even while the app shows its
  unlock page. Only a password keeps Claude out while Wardmind is locked.
- **What no app can stop:** malware running as you, or an admin who installs a keylogger. The
  page file, hibernation and crash dumps can also hold decrypted data. Recommend Windows Device
  Encryption or BitLocker for those.
- **Why not hashing:** a hash is one-way, so the book could never be read back. Hashing is used
  only to stretch the password into a key.

### Open source doesn't weaken this

Following Kerckhoffs's principle, the design assumes an attacker has read every line of the code.
The protection comes from the keys, never from how the code works:
- **The algorithms are public standards:** AES-256-GCM, Argon2id, HKDF and Ed25519, from the
  widely audited `cryptography` library. Wardmind never invents its own cryptography.
- **The keys are never in the code or the repository.** Each install makes its own random master
  key, and the only ways to unwrap it are:
  - your Windows login (DPAPI)
  - your password (Argon2id)
  - your printed recovery key
- **Authenticated encryption makes tampering show.** A changed byte, or one file swapped for
  another, fails to decrypt instead of being read.
- **What an attacker can still try:**
  - guessing a weak password: Argon2id makes each guess slow, and the minimum length helps
  - using your Windows login, in DPAPI-only mode
  - stealing the key from memory while Wardmind is unlocked: malware running as you can do that
    with any app
- **Obfuscating the code** would add nothing against these attacks, so it isn't planned.

## B. Encryption at rest

The recommendation: Windows protection (DPAPI) by default, an optional app password, and a
recovery key every time.

### Keys

- **Master key:** 32 random bytes, stored only in wrapped form, in `keys.json` as "slots".
  - **DPAPI slot:** wrapped by Windows for your account. Nobody has to type anything.
  - **Password slot:** Argon2id (64 MiB, t=3, p=4) derives the wrapping key; AES-256-GCM wraps
    the master key.
  - **Recovery slot:** a 125-bit code, shown once as 5×5 groups of characters, with HKDF and a
    wrap.
  - Turning on the password deletes the DPAPI slot.
- **Book key:** one per book, wrapped by the master key.
- **Per-file key:** HKDF of the book key and the file's relative path.

### File format

Each file starts with:
1. `"WMD1"`
2. the version
3. the algorithm
4. the key epoch
5. a 12-byte random nonce

After those come the ciphertext and its tag. The header and the relative path are the
additional authenticated data (AAD), so one file can't be swapped for another.

### The vault layer

A new module, `core/vault.py`, provides `read_text`, `write_text`, `read_bytes`, `write_bytes`,
`unlock`, `lock` and `is_unlocked`, plus two exceptions:
- `Locked`
- `Damaged`, which is never treated as empty

Every data module and `secrets.py` go through it. YAML preferences stay plain. An AST test fails
if code reads or writes a data path directly.

### Moving existing data over

At the first unlock, every file without the header is encrypted in place. This can be run again
safely and survives a crash.

### Encrypted or plain

- **Encrypted:** everything under `data/`, all secrets, logos and their index, and the mailbox
  settings.
- **Plain:** `scoring.yaml` and `settings.yaml`. They hold no customer data, and the theme has to
  be read before unlock.

### Sign-in

1. **Phase 0:** a session-token cookie (HttpOnly, SameSite=Strict). The desktop app and
   `wardmind ui` open a one-time `/enter?t=…` link to get it. Any other request gets 403.
2. **Lock gate:** while locked, only `/unlock` and `/static` are served.
3. **Unlocking:** the key is kept in memory only.
4. **Slowing guesses:** Argon2 takes about 0.4 s per try, with a backoff after 5 failures.
   Passwords are at least 10 characters.
5. **Locking:** "Lock now", plus an automatic lock after 15 idle minutes by default. Locking
   stops the scheduler and drops the in-memory book. Feeds and the mailbox sync only while
   unlocked.
6. **MCP and the command line:**
   - In DPAPI mode they unwrap the key themselves.
   - In password mode, an unlocked app writes a short-lived "session ticket" (wrapped by DPAPI)
     for them to read. It is deleted when the app locks.
   - `wardmind unlock` serves command-line-only use.

### Other places data can leak

- Large uploads are briefly spooled to `%TEMP%` in plain text.
- Exported CSV files are plain text by design.
- "Delete all local data" deletes `keys.json` first, which makes the encrypted files unreadable,
  then deletes the files.

## C. Multiple books

- **On disk, a book is a folder, a key and its own connections:**
  ```
  %LOCALAPPDATA%\wardmind\
    keys.json  books.json (plain: id, label, kind)  config\settings.yaml
    books\<id>\ data\  secrets\  scoring.yaml  book.key
  ```
- **One book is open at a time per process.** Switching books means locking, then unlocking the
  other one, so nothing can mix.
- **Per book:** all secrets (including the OpenRouter key) and the scoring.
- **Global:** appearance settings.
- **Demo data** becomes a built-in, read-only book. That removes the `DataSource.DEMO` branches.
- **Labels are stored in plain text.** Default to "Book 1", and warn if a label looks like a
  customer or employer name.
- **UI:** a book switcher in the header, and Settings > Books for add, rename, delete and back up.
- **MCP:** `wardmind mcp --book <id>`. Every result names its book, and there is no tool for
  switching books.

## D. Sharing between computers

### How the computers connect

Use a shared folder the two people already have: OneDrive or SharePoint, Google Drive, or a
network drive.
- It works without both being online at the same time.
- It opens no ports and needs no server.
- The provider sees only encrypted files.

Peer-to-peer on the local network, a self-hosted relay and a Wardmind cloud service were all
considered and rejected.

### Each computer publishes its own state

- Each device writes only its own files: `state/<device-id>/<shard>.wms`. Each file holds
  encrypted, signed state, with a time stamp per entity and tombstones for deletions.
- Every computer merges all devices' files entity by entity, and the newest stamp wins.
- Because each file has a single writer, OneDrive conflict copies can't happen.
- A merge gives the same result in any order and can be repeated safely.
- The stamps live in a sidecar file, `syncmeta.json`, so the existing models barely change.

### What is shared

| Shared | Stays on this computer |
|---|---|
| Notes and activity, follow-ups, outcomes, team | All secrets and API keys |
| Contacts, client updates | Research cache, AI summaries |
| Field edits, links | Snoozes, pins, hidden accounts, notices |
| Hand-added accounts and readings | History snapshots |
| Connection definitions (not their credentials), scoring config | |
| Snapshots of uploaded sheets | |

Each computer pulls HubSpot and the feeds with its own credentials. HubSpot ids match across
computers.

### When two computers disagree

| Data | Rule |
|---|---|
| Notes | Add-only, and only the author can edit them. Ids widen to 16 hex. |
| Follow-ups | Newest wins, field by field; on a tie, done wins. Adds an assignee and who marked it done. |
| Field edits and contacts | Newest wins, with a notice that someone else also changed it. |
| Outcomes | Add-only. |
| Deletions | Tombstones, kept for 180 days. |
| Limits | They apply only when entering data; a merge never drops data. |

### Clocks, identity and trust

- **Clocks:** a hybrid logical clock. A stamp more than 24 hours in the future is clamped and
  flagged.
- **Identity:** each device has an Ed25519 key for signing and an X25519 key for receiving keys.
  Every file is signed and carries a rising version, so an old copy put back is detected.

### Joining and leaving

- **Inviting:**
  1. The owner picks the shared folder.
  2. Wardmind creates a one-time invite code: 20 characters, valid for 7 days.
  3. The owner sends the code by a different channel from the folder link.
  4. The teammate chooses Add book > Join and pastes it.
- **Removing someone:** a new key is sealed to each remaining member, and the owner removes the
  person's access to the folder. The removed person keeps whatever they had already synced.

### What the user sees

- A Sharing panel: the members and when each last synced.
- A status line in the header, for example "Synced" or "3 new from Sam".
- The author on every note.
- On Today, a choice between your own follow-ups and everyone's.

## E. Phases

| Phase | Size | Contents |
|---|---|---|
| 0 | S | Session-token cookie, closing the open local address |
| 1a | M | Vault, the DPAPI slot, moving existing data over, `Locked`/`Damaged`, secrets through the vault, the AST test |
| 1b | M | Password and recovery slots, `/unlock`, auto-lock, scheduler on unlock, MCP session ticket |
| 2 | M–L | Multiple books, demo data as a book, the switcher, `--book` |
| 3a | L | Identities, stamps and the merge engine, tested locally with simulated devices |
| 3b | L | The shared-folder transport, invites, the Sharing UI |
| 3c | M | Signed membership, removal and key rotation, detecting an old copy put back |

**Packages:** `cryptography`, made a direct dependency. It is already installed. DPAPI is reached
through ctypes.

## Decisions for the user

1. **Forgotten password:** a printed recovery key (recommended), or no recovery at all?
2. **Password scope:** one password for all books (recommended), or one per book?
3. **Claude Desktop:** may it read a password-protected book only while Wardmind is unlocked
   (recommended)?
4. **Shared folder:** which service do the CSM and AM actually share?
5. **Notes:** can teammates see all of each other's notes, or should there be an "only me" option?
6. **HubSpot:** does each person pull with their own access (the default)?
7. **Hidden accounts and pins:** personal (recommended) or shared?
8. **Dependency:** may `cryptography` become a direct dependency?
9. **Auto-lock time:** 15 minutes by default?

### Answers taken (2026-11-11)

The user asked for the recommended answers while they were away. They can change any of them.

1. A printed recovery key.
2. One password for all books.
3. Claude Desktop reads a password-protected book only while Wardmind is unlocked.
4. Any folder the user picks: OneDrive, SharePoint, Google Drive or a network share. OneDrive is
   suggested first, as the user already uses it.
5. Teammates see each other's notes. A note can be marked "Only me", and such a note is never
   shared.
6. Each person pulls HubSpot with their own access.
7. Hidden accounts and pins are personal.
8. `cryptography` becomes a direct dependency (already installed; no new download).
9. Auto-lock after 15 idle minutes.

### Phase 0 as built

- `web/session.py`: `SessionGate` (entry and session tokens, 32 random bytes each), `safe_next`,
  and `session.json` for `wardmind open`.
- `create_app(gate)`: without the cookie every path but `/enter` and `/static/` gets 403. The
  launchers always pass a gate; tests build the app without one.

### Phase 1 plan (reviewed 2026-11-11)

Changes from sections A, B and E above, after a review of the code:

- **Encryption is opt-in.** Settings > Data & privacy > "Encrypt your data on this computer".
  Turning it on shows the recovery key first and asks the user to confirm they saved it, then
  encrypts the files in place. Plain stays the default, and in plain mode the vault passes files
  through unchanged. The "protected" columns of the threat table apply once encryption is on.
- **A random book key now.** `keys.json` holds a master key in slots (DPAPI, recovery; password
  in 1b) and one "default" book key wrapped by the master key. File key = HKDF(book key,
  b"wardmind file v1\x00" + logical path), where the logical path is root-tagged and relative:
  `data/journal.json`, `secrets/hubspot-token`, `secrets/feeds/feed-3`. Phase 2 then moves
  folders without re-encrypting.
- **Primitives:** `cryptography` 50.0.2 (pin `>=44`): AESGCM, HKDF-SHA256, Argon2id (parameters
  stored in the slot; about 0.1 s at 64 MiB, t=3, p=4 here, so 1b may raise memory).
- **Reads sniff the header.** A file starting with `WMD1` is decrypted, anything else is plain.
  Writes re-check `keys.json` (cached by mtime and size), so the MCP process never writes
  plaintext over an encrypted book. `os.replace` retries briefly on `PermissionError`.
- **Errors:** `VaultError` (not an `OSError` or `ValueError`), with `Locked` and `Damaged`.
  Empty-on-error loaders let it through untouched. Places that delete or set aside unreadable
  files (`history.forget`, the import and feed draft purges, `journal._set_aside_if_unreadable`)
  must re-raise it, and every `except Exception` adds `except VaultError: raise` first. The web
  app answers `VaultError` with a plain 503 page until `/unlock` exists.
- **Migration walks an explicit list:** the data folder recursively, `hubspot-token`,
  `openrouter-key` and `feeds/*`, never the whole secrets folder (it holds the venv, config,
  session.json and keys.json). State goes PLAIN -> ENCRYPTING -> ON (and ON -> DECRYPTING ->
  PLAIN), with `keys.json.bak`. Each file is replaced atomically and keeps its mtime. The
  scheduler stops and `WRITE_LOCK` is held throughout.
- **`secrets.rename_secret`** goes through `vault.move` (decrypt, re-encrypt under the new path).
- **Tests:** the DPAPI wrap and unwrap are two module functions that tests replace; one real
  DPAPI test runs on Windows only. Argon2 costs are parameters (tiny in tests). Without DPAPI
  (POSIX), turning encryption on needs a password. `feeds/setup.py` imports `secrets as vault`;
  rename that alias first.
- **Lock gate (1b):** registered inside the session check, so a request without the cookie still
  gets 403. `/unlock` needs the session cookie. While locked, GET pages redirect to
  `/unlock?next=` (`HX-Redirect` for htmx), and other requests get 423. The unlock page renders
  without the store-backed context. The scheduler starts only when unlocked and stops on lock.
  The idle timer skips `/static/`.

### Phase 1a as built

- `core/vault.py`: `read_text`, `read_bytes`, `write_text`, `write_bytes`, `move`, `state`,
  `enable` (returns the recovery code), `disable`, `resume`, `unlock`, `lock`, `is_unlocked`,
  `managed_files`, `logical_path`; `VaultError`, `Locked`, `Damaged`.
- `keys.json` (plus `keys.json.bak`) in the per-user folder: state, epoch, a DPAPI slot and a
  recovery slot (HKDF of the 125-bit code with a random salt), and the default book key wrapped
  by the master key. The code itself is never stored.
- Every data module (store, journal, contacts, client updates, mailbox, history, logos, research,
  summaries, import and feed drafts) and every secret go through the vault. `tests/test_vault_guard.py`
  fails on any direct file access outside its allow-list.
- The feed and mailbox syncs re-raise `VaultError`, so a locked book isn't reported as a failed
  sync. The web app answers `VaultError` with a plain 503 page.
- Not reachable from the UI yet: phase 1b adds the Settings switch, the password, `/unlock` and
  the lock gate.

### Phase 1b as built

- `core/vault.py`: a password slot (Argon2id, 128 MiB, 3 passes, 4 lanes; at least 10
  characters), `set_password` (replaces the Windows slot), `remove_password` (puts it back),
  `replace_recovery_code`, `ensure_unlocked`, `forget_key`. `lock()` marks the process locked
  by the user, so Windows protection doesn't unlock it again by itself.
- The unlock ticket: with a password slot, unlocking writes `unlock-ticket.json`, the master key
  wrapped by DPAPI, valid 12 hours. The MCP server and the CLI open the book from it; a session
  opened that way ends as soon as the ticket is gone. `lock()` deletes it.
- Settings > Data & privacy > Encryption: turn on (Windows or a password, then the recovery key
  page, which needs a tick before anything is encrypted; the pending key lives in memory for 10
  minutes under a random token), change or remove the password, make a new recovery key, turn
  off, and the auto-lock time (5, 15, 30, 60 minutes or never; 15 by default).
- `web/lock.py` and the lock gate: registered inside the session check, so a request without
  the cookie still gets 403 first. A locked book sends GET pages to `/unlock?next=...` (htmx gets
  `HX-Redirect`) and answers other methods with 423. `/unlock`, `/enter` and `/static/` stay
  open. The idle clock is touched at the start and end of each request; a background thread
  checks every 30 seconds. A `Locked` raised mid-request (the idle lock raced it) also goes to
  the unlock page.
- `/unlock`: Windows button, password, or recovery key; typed values never come back in the
  page. Five free wrong tries, then waits of 2, 4, 8 ... up to 60 seconds (429).
- "Lock now" in the header whenever encryption is on. `sync_due_feeds` does nothing while
  locked. The MCP tools answer "Wardmind is locked. Open the app and unlock it." for `Locked`
  and a plain message for `Damaged`.
