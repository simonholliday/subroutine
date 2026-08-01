# Running Subroutine as a service

**Every `subroutine` command on this page has been run, and every quoted output is what it
actually printed** — with only paths and hostnames moved to the deployment described here.
That includes the refusals, which are worth meeting on a page rather than at two in the
morning, and a test fails the build if the two quoted bind refusals stop matching what the
program says. The `useradd`, `systemctl`, nginx and Caddy fragments are ordinary system
administration, are not exercised by anything here, and are a starting point for however your
machines are already set up.

The shape is deliberately ordinary: a Python process listening on loopback, your own TLS proxy
in front of it, systemd keeping it alive, PostgreSQL underneath once more than one person is
using it. There is nothing to cluster and no message broker. If you have hosted a Django or a
Rails application, you have already done this.

> **One thing that is not optional.** Subroutine authenticates with bearer tokens, and a bearer
> token sent over plain HTTP is a compromised token — anything on the path has it, and it does
> not expire on being read. `serve` refuses to listen beyond this machine unless you have said
> out loud that TLS is handled. [Below](#tls-and-why-serve-refuses-without-it) is what that
> refusal looks like and how to satisfy it honestly.

## Contents

- [An account and an install](#an-account-and-an-install)
- [First run, and what it writes](#first-run-and-what-it-writes)
- [PostgreSQL, and when to switch](#postgresql-and-when-to-switch)
- [TLS, and why `serve` refuses without it](#tls-and-why-serve-refuses-without-it)
- [The systemd unit](#the-systemd-unit)
- [A reverse proxy](#a-reverse-proxy)
- [Giving an agent a token](#giving-an-agent-a-token)
- [Backups](#backups)
- [Upgrading](#upgrading)
- [The AGPL obligation, which is a product requirement here](#the-agpl-obligation-which-is-a-product-requirement-here)

## An account and an install

A service account with no login and no home to speak of, and a virtualenv it does not own:

```console
# useradd --system --no-create-home --shell /usr/sbin/nologin subroutine
# python3 -m venv /opt/subroutine
# /opt/subroutine/bin/pip install "subroutine[postgres]"
```

Drop the `[postgres]` extra if you are staying on SQLite. Python 3.11 or newer.

Subroutine keeps its files under the XDG directories — configuration in
`$XDG_CONFIG_HOME/subroutine`, the database in `$XDG_DATA_HOME/subroutine`, the current
context in `$XDG_STATE_HOME/subroutine`. The unit below points all three inside
`/var/lib/subroutine`, so the service does not depend on the account having a home directory
at all. That was checked with `HOME=/nonexistent`, which is the situation a `--system` account
is usually in.

## First run, and what it writes

Run `init` **as the service account, with the same environment the unit will use**, so that the
database and the signing key land where the service will look for them:

```console
# sudo -u subroutine env \
    XDG_CONFIG_HOME=/var/lib/subroutine/config \
    XDG_DATA_HOME=/var/lib/subroutine/data \
    XDG_STATE_HOME=/var/lib/subroutine/state \
    /opt/subroutine/bin/subroutine init
  Ready. Try: subroutine add "something to do"
```

One line, because `init` is written for somebody setting up a to-do list. It has made the
database, the first workspace, an Inbox and the first user — who is this instance's
administrator — and it has written exactly one setting:

```toml
# Subroutine configuration. See 'subroutine config show'.
secret_key = "9ZetNDEWdo6Nu35ujhcOYa7baweWIi66A38HUPSLjaU"
```

That file is created `0600`, and so is `credentials.toml`. The key signs pagination cursors
and nothing else — it is deliberately *not* mixed into stored token hashes, so rotating it
costs an in-flight page of results rather than every credential in the installation.

**Everything else in `config.toml` you add yourself.** When a value surprises you, ask where it
came from rather than guessing:

```console
$ subroutine config show
  …
  database_url                      postgresql+psycopg:///subroutine  [/var/lib/subroutine/config/subroutine/config.toml]
  default_page_size                 50  [default]
  public_url                        https://tasks.example.com  [/var/lib/subroutine/config/subroutine/config.toml]
  secret_key                        (set)  [/var/lib/subroutine/config/subroutine/config.toml]
  …
```

It lists every setting, not only the ones you have changed, which is how you find out what
there is to change. Flags beat the environment, the environment beats the file, the file beats the defaults, and
that column tells you which one won. Every setting can also be given as
`SUBROUTINE_<NAME>` in the environment — useful for `database_url` when the credential comes
from a secrets manager rather than from a file on disk.

**The process reads its configuration once, at start.** Change `config.toml` and restart the
service; `serve` does not reload. A setting that appears not to have taken effect is nearly
always this.

## PostgreSQL, and when to switch

SQLite is the default and it is not a toy — it is the right answer for one person, and for a
small team that is not writing concurrently. Switch when any of these is true:

- More than a handful of people or agents write at the same time. SQLite serialises writers.
- The database has to live on a different machine from the service. It cannot: a network
  filesystem cannot give SQLite the locking it needs, and `serve` **refuses to start** on one
  rather than corrupting the file quietly.
- You want your existing backup, replication and point-in-time recovery to cover it.

Create the database and role however you normally would. If this is a **new** installation,
name it in `config.toml` and you are done:

```toml
database_url = "postgresql+psycopg:///subroutine"
```

**If you already have data in SQLite, copy it across first.** Do not just change the URL — that
gives you an empty database and leaves everything you have in a file nothing is reading. A
backup will not do it either: backups are per-engine, so a SQLite one cannot be restored into
PostgreSQL.

```console
$ subroutine db copy --to postgresql+psycopg:///subroutine
  Copying sqlite:////var/lib/subroutine/subroutine.db
       to postgresql+psycopg:///subroutine

  event: 776
  task: 137
  link: 102
  ...

  Copied 1,619 rows, and read them back to check.

  Nothing has changed here yet. To start using the copy, set in config.toml:
    database_url = "postgresql+psycopg:///subroutine"
```

**It is a copy and the original is untouched**, so nothing is at risk while you check. The
target must be empty; it is migrated to the right schema for you, and every table is read back
and counted before the command reports success. Stop the service first, so nothing writes to
the old database after the copy is taken.

When the new one looks right, set `database_url`, restart, and confirm with `subroutine db
current`. Keep the SQLite file until you are sure — deleting it is the only irreversible step
in the whole move, and nothing here does it for you.

It works in the other direction too, which is what you want for a laptop copy of a served
instance, or for going back.

The driver is `psycopg` (version 3), which is what the `[postgres]` extra installs. Confirm
before starting the service:

```console
$ subroutine db current
  Schema is at 0c8f7a7027e6.
```

An empty database says so and tells you to run `init`. It is never silently created underneath
you.

## TLS, and why `serve` refuses without it

Ask for a public bind with nothing in front of it and you get this:

```console
$ subroutine serve --host 0.0.0.0
  Refusing to listen on 0.0.0.0 without TLS: bearer tokens sent over plain HTTP are compromised tokens.
  Either put a TLS-terminating proxy in front and set public_url to its https:// address, or pass --insecure if this network is genuinely trusted.
```

There are three honest ways past it, and one of them is the right one.

**Put a proxy in front and keep Subroutine on loopback.** This is the recommended arrangement
and the refusal never fires, because the bind is `127.0.0.1` — the default. Set `public_url` to
the address the proxy serves, which is what clients and agents are told to come back to:

```toml
public_url = "https://tasks.example.com"
```

**Bind publicly with `public_url` set to an `https://` address.** For the case where TLS is
terminated somewhere the check cannot see — a load balancer, a service mesh. The check is
satisfied by the `https://` scheme, so this is you taking responsibility rather than the
program verifying anything. A wrong scheme is caught:

```console
$ subroutine serve --host 0.0.0.0     # with public_url = "http://tasks.example.com"
  Refusing to listen on 0.0.0.0 without TLS: public_url is set to 'http://tasks.example.com', which is not an https:// address.
  Point public_url at the https:// address your proxy serves this on, or pass --insecure if this network is genuinely trusted.
```

**`--insecure`.** The honest way to say "this is my home network and I know". It is a flag you
type rather than a setting you forget, which is the point.

A *warning* was considered and rejected: a warning on a long-running server scrolls out of view
in the first minute, and the risk lasts as long as the process does.

## The systemd unit

`/etc/systemd/system/subroutine.service`:

```ini
[Unit]
Description=Subroutine
Documentation=https://github.com/simonholliday/subroutine
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=subroutine
Group=subroutine

# systemd creates /var/lib/subroutine, owned by the service account. Subroutine keeps its
# configuration, database and current context under the XDG directories, so point those
# inside it — a service account should not depend on having a home directory.
StateDirectory=subroutine
Environment=XDG_CONFIG_HOME=/var/lib/subroutine/config
Environment=XDG_DATA_HOME=/var/lib/subroutine/data
Environment=XDG_STATE_HOME=/var/lib/subroutine/state

ExecStart=/opt/subroutine/bin/subroutine serve
Restart=on-failure
RestartSec=5s

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# Only if backup_directory points outside /var/lib/subroutine, which it should.
# ReadWritePaths=/srv/backups/subroutine

[Install]
WantedBy=multi-user.target
```

`ExecStart` takes no `--host` or `--port` because both are settings; the defaults are
`127.0.0.1` and `8471`. `ProtectSystem=strict` makes the whole filesystem read-only apart from
what `StateDirectory` grants, which is why a backup directory elsewhere needs naming — a
`ReadWritePaths` you forgot shows up as a backup that cannot be written, on the day you need
one.

```console
# systemctl daemon-reload
# systemctl enable --now subroutine
# systemctl status subroutine
# journalctl -u subroutine -f
```

Two endpoints exist for whatever is watching, and neither needs a credential:

```console
$ curl -s localhost:8471/healthz
  {"status":"ok","api_version":"1.0"}

$ curl -s localhost:8471/readyz
  {"status":"ready","api_version":"1.0","schema_revision":"0c8f7a7027e6"}
```

`/healthz` says the process is up. `/readyz` says it can reach its database *and* that the
database is at the schema this build expects — which is the one that goes red after an upgrade
you have not finished.

## A reverse proxy

nginx:

```nginx
server {
    listen 443 ssl;
    server_name tasks.example.com;

    ssl_certificate     /etc/letsencrypt/live/tasks.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tasks.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8471;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Caddy, which gets you a certificate on its own:

```caddy
tasks.example.com {
    reverse_proxy 127.0.0.1:8471
}
```

Whichever you use, set `public_url` to match. It is published at `GET /v1/meta`, so an agent
handed a token can find out where it is talking to without being told separately.

## Giving an agent a token

A token may be narrower than the person who issued it, and this is where that earns its keep.
Give an agent a machine identity of its own and only the permissions it needs:

```console
$ subroutine token create --service-account reporter --scope task:read --title "weekly digest"
  Created service account reporter, with the contributor role.

  sr_d9fb02fa_UxzFqMe7i_NGb_eXRbOAsVhcm5_O-4pphVO6JhPe494

  That is the only time it is shown. Store it now.
  Give it to a client as SUBROUTINE_TOKEN, or add it to
  /var/lib/subroutine/config/subroutine/credentials.toml.
```

What that buys, checked against a running instance:

```console
$ curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
    https://tasks.example.com/v1/tasks
  200

$ curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d '{"title":"nope"}' https://tasks.example.com/v1/tasks
  403  This needs the 'task:write' permission. Your role allows it, but the token
       you used is scoped to a narrower set.
```

Note what the refusal distinguishes: the *role* would have allowed it and the *token* would
not. An agent reading that knows it has been deliberately bounded rather than misconfigured.

`--workspace` pins a token to one workspace. A token can never be wider than the credential
that issued it — `token create` presented with a narrow token will not mint a broad one, which
is what stops an agent quietly promoting itself.

**Give the token to the client as `SUBROUTINE_TOKEN`.** It is never accepted in a query string
and never read from `config.toml`. `--store <connection>` writes it to `credentials.toml`
instead, and is deliberately opt-in: storing a narrow agent token under your own connection
name would quietly narrow your own CLI.

## Backups

Set a directory, and **put it on a different volume from the database** — a backup on the disk
you are worried about is not one:

```toml
backup_directory = "/srv/backups/subroutine"
```

A network mount is the intended destination and works. The file is built locally and then
moved, because SQLite's `VACUUM INTO` cannot write to an SMB share any more than the live
database can live on one; delivery is then verified where the file landed, by size and by
reading the schema version back out of it. A copy that fails verification is deleted rather
than left looking like a backup.

```console
$ subroutine db backup
  Backed up instance 'default' to /srv/backups/subroutine/subroutine-default-20260731T141853Z-0c8f7a7027e6.sql
  60,069 bytes, schema 0c8f7a7027e6.

$ subroutine db backups
  Backups of instance 'default', in /srv/backups/subroutine:
    subroutine-default-20260731T141853Z-0c8f7a7027e6.sql  2026-07-31 14:18 UTC  60,069 bytes  schema 0c8f7a7027e6
```

**The name is `subroutine-<instance>-<when>-<schema><suffix>`, and the suffix says which
engine took it** — `.sql` for a PostgreSQL dump, which is a script `psql` replays, and `.db`
for a SQLite copy, which is a database. They are not interchangeable in either direction, and
a restore refuses the wrong one rather than discovering it partway through. A retention script
written against `*.sql` therefore matches nothing on SQLite; match both, or match on
`subroutine-*`.

`--keep N` prunes to the newest N afterwards, which is the whole of the retention policy. Run
it from a timer — it names every file it deletes, so the timer's log is the record of what
went.

Backups are written owner-only, like the database and `config.toml`. A backup is the whole
database, so it is exactly as sensitive as the thing it copies.

**Every backup carries the schema version it was taken on, inside the file** — the filename
echoes it, but the value inside is the authority, because anybody can rename a file. Restoring
an older one works and offers you the upgrade; restoring a *newer* one is refused outright,
because there is no downgrade and a partial read is worse than a clear failure.

An agent can take one before attempting something bulk:

```console
$ curl -s -X POST -H "Authorization: Bearer $TOKEN" https://tasks.example.com/v1/admin/backups
  {"name":"subroutine-…-0c8f7a7027e6.sql","schema_head":"0c8f7a7027e6","size_bytes":61311,…}
```

That endpoint needs `instance:admin`, which **no role carries** — only an administrator of the
instance holds it, so an ordinary agent token gets a 403 naming the permission.

**There is deliberately no restore endpoint.** Putting a backup back replaces the database the
serving process has open, and recovery has to work when the service will not start — which is
exactly when you need it. `subroutine db restore` is the only way, and it will not run without
you saying which kind of restore this is:

```console
$ subroutine db restore <file> --recover     # this instance, lost data, same identity
$ subroutine db restore <file> --as-clone    # a copy to poke at, new identity
```

Neither is a safe default. A recovery keeps the instance's identity, which is what agents key
their caches on; a clone mints a new one, so that two live instances never claim to be the
same. Getting it wrong is invisible in both directions, so you are asked.

**Stop the service before you restore.** A running one keeps its file handles on the database
that was just replaced: it goes on writing to something with no name any more, its reads are
stale, and its next checkpoint can land on top of the restored file and corrupt it — while the
API answers normally throughout, `/readyz` included. Subroutine refuses when it can see another
connection, and `--force` overrides that for the case where it cannot:

```console
$ sudo systemctl stop subroutine
$ subroutine db restore <file> --recover
$ sudo systemctl start subroutine
```

Two more things this will not do to you. **A backup from the other engine is refused before
anything is dropped** — a `.db` is a SQLite database and a `.sql` is a PostgreSQL script, they
cannot be read by each other's tools, and `subroutine db backups` names the engine when a
directory holds both. To move an instance between engines, use `subroutine db copy`, not a
backup. And **the safety copy taken before a restore is never allowed to block the restore**:
if the database being replaced is too damaged to copy — which is the usual reason to be
restoring at all — you are told so plainly and asked whether to go on, rather than refused.

Mark a production instance as one worth protecting, and destructive commands will require
agreement before touching it:

```toml
protected = true
```

A setting Subroutine does not recognise is named on every command rather than ignored, with
the nearest real one suggested. `protectd = true` is not a protected instance and never was;
before, nothing said so.

## Credentials

`subroutine token list` shows every credential this instance has issued — its prefix, who owns
it, what it can reach, when it expires and when it was last used. No secret is stored, so
there is nothing in that listing to leak, and the prefix is what revoking takes:

```console
$ subroutine token list
  a1b2c3d4  si      My laptop        no expiry
            everything its owner can do · last used 2026-07-31
  e5f6a7b8  claude  claude's token   until 2026-08-30
            task:read, task:write · in acme only · never used

$ subroutine token revoke a1b2c3d4
```

Revoking is immediate: a revoked credential is checked on every request rather than cached, so
there is no session to wait out. That is the answer to "a key leaked", and it is why the
listing shows what each one can reach — the question at that moment is which of them could
write.

## Upgrading

The package manager moves the code. Subroutine moves the database. In that order, and it will
not try to do the first for you — a tool that installs software over itself fights whatever
installed it, cannot do it safely while running, and is worse at it than your package manager.

```console
# systemctl stop subroutine
# /opt/subroutine/bin/pip install --upgrade subroutine
# sudo -u subroutine … /opt/subroutine/bin/subroutine upgrade
# systemctl start subroutine
```

`subroutine upgrade` is the whole of the second step, and its value is the ordering rather than
any one part of it: report both versions, back up and verify the copy where it landed, migrate,
then read the schema back rather than assuming.

```console
$ subroutine upgrade
  This version expects schema 0c8f7a7027e6.
  The database is at 233f898a2bee.
  About to upgrade the database of the default instance, at postgresql+psycopg:///subroutine.
  Backed up to /srv/backups/subroutine/subroutine-20260731T144206Z-233f898a2bee.sql (60,069 bytes).
  Upgraded from 233f898a2bee to 0c8f7a7027e6.
```

It is safe to run when there is nothing to do — it prints both numbers and stops, which is also
the cheapest way to ask the question:

```console
$ subroutine upgrade
  This version expects schema 0c8f7a7027e6.
  The database is at 0c8f7a7027e6.
  Nothing to do.
```

Add `--yes` when the instance is marked `protected` and there is no terminal to answer the
prompt — a timer or a deploy script. On a **protected** instance without it, the command says
what it was about to touch and stops.

If the migration fails, the message says where it stopped and where the backup is, with the
`db restore … --recover` command spelled out. It does not claim the database is unchanged:
Alembic runs each migration in its own transaction, so an upgrade spanning three releases can
leave the first two applied, and that is exactly the case where somebody needs the truth.

**You should not usually meet the check, but here is what it looks like.** Run anything against
a database this build does not match and it is refused, with the direction of the mismatch
deciding the remedy:

```console
$ subroutine today
  Nothing could be read.
  Local: This database is at schema 233f898a2bee, and this build expects 0c8f7a7027e6.
    Run 'subroutine upgrade' — it backs up first, then migrates.
```

A database *newer* than the software is refused the other way — update the software, because
there is no downgrade. **The administrative commands are deliberately outside the check**:
`db current`, `db backup`, `db backups`, `db restore` and `upgrade` itself all keep working
while it is firing, because they are what you reach for once it does.

`curl localhost:8471/readyz` makes the same comparison for the served path and has always done
so. `subroutine --version` prints the release and the schema this build wants.

**Whether a release needs a migration at all is on the release itself.** Each entry in
[CHANGELOG.md](../CHANGELOG.md) that moves the schema carries a notice saying so, with the
revisions it moves between — and CI refuses a release that moves the schema without one, by
comparing the migration history against the previous tag rather than by trusting anybody to
remember. So the question "will this upgrade need downtime?" is answered before you download
anything, which is the whole point.

## The AGPL obligation, which is a product requirement here

Subroutine is [AGPL-3.0-or-later](../LICENSE). The network clause matters the moment you serve
it: **if you modify Subroutine and let other people use it over a network, those people are
entitled to your modified source.** Running an unmodified copy, or using it internally however
you like, does not trigger anything.

This is built in rather than left to a footnote. `GET /v1/meta` publishes `source_url`, and it
is a setting for exactly this reason — somebody running a modified fork must be able to point
at *their* source, and will be wrong to point at this one:

```toml
source_url = "https://git.example.com/us/subroutine"
```

Leave it at the default if you have not modified anything, and change it if you have.

**A commercial licence is available by agreement.** If the copyleft does not suit you — your
organisation's policy rules it out, or you want to build on Subroutine without publishing what
you build — write to simon.holliday@protonmail.com and say what you have in mind.
