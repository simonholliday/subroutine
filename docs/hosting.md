# Running Subroutine as a service

**Every `subroutine` command on this page has been run, and every quoted output is what it
actually printed** — with only paths and hostnames moved to the deployment described here.
That includes the refusals, which are worth meeting on a page rather than at two in the
morning, and a test fails the build if the two quoted bind refusals stop matching what the
program says. It includes the credentials: a token is quoted whole, because a reader who has
just run this needs to recognise what came back, and every one on this page was issued on a
throwaway instance that no longer exists. A test fails the build on any *other* whole
credential in the repository. The `useradd`, `systemctl`, nginx and Caddy fragments are ordinary system
administration, are not exercised by anything here, and are a starting point for however your
machines are already set up.

The shape is deliberately ordinary: a Python process listening on loopback, your own TLS proxy
in front of it, systemd keeping it alive, PostgreSQL underneath once more than one person is
using it. There is nothing to cluster and no message broker. If you have hosted a Django or a
Rails application, you have already done this.

**If you are not the one standing the server up, you want
[docs/connecting.md](connecting.md) instead.** This page is the operator's end; that one is
organised by which of five situations a person reaching an instance is in, and says what to ask
you for. Sending it to whoever you issue a token to saves the conversation.

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
- [Adding the people](#adding-the-people)
- [Giving an agent a token](#giving-an-agent-a-token)
- [Reaching it from your own machine](#reaching-it-from-your-own-machine)
- [Reaching it from an agent, with nothing installed](#reaching-it-from-an-agent-with-nothing-installed)
- [Backups](#backups)
- [Credentials](#credentials)
- [Upgrading](#upgrading)
- [What the licence asks of you, which is almost nothing](#what-the-licence-asks-of-you-which-is-almost-nothing)

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

**Make the state directory first.** The unit below carries `StateDirectory=subroutine`, which
creates `/var/lib/subroutine` and hands it to the service account — but only when the service
first starts, and the service cannot start until `init` has run. So on this one occasion you
make it yourself:

```console
# install -d -o subroutine -g subroutine -m 0755 /var/lib/subroutine
```

`StateDirectory=` is content to find it already there, and that owner and mode are what it
would have set. Skip this and `init` stops with `Cannot create the configuration directory`,
because `/var/lib` is root-owned and a `--system` account cannot write in it.

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

### Every setting, and what it does

`config show` lists these; this says what they are for. A test fails the build if the two
disagree, so a setting that exists and is not here cannot ship.

| Setting | Default | What it does |
| --- | --- | --- |
| `database_url` | SQLite under `$XDG_DATA_HOME` | Which database to use. The one setting most installations change |
| `host` | `127.0.0.1` | What `serve` binds to. Never `0.0.0.0` by default — see TLS below |
| `port` | `8471` | What `serve` binds to |
| `public_url` | unset | The address a proxy serves this instance on. Published in `/v1/meta`, and what makes a non-loopback bind legal |
| `secret_key` | written by `init` | Signs pagination cursors, and **only** that. Not mixed into token hashes, so rotating it costs an in-flight page rather than every credential |
| `source_url` | this project | Where this instance's source can be had. A promise the product makes, not a licence obligation |
| `backup_directory` | beside the database | Where `db backup` writes. A network volume is the intended destination |
| `protected` | `false` | Marks an instance whose data is real, so `db restore`, `upgrade` and `profile destroy` refuse without `--yes` |
| `default_connection` | `local` | Which instance a write goes to when the command did not say |
| `local_user` | unset | Which account to act as when the database holds more than one and nobody logged in |
| `default_timezone` | the machine's | The last word in the timezone chain, when no user, workspace or instance says |
| `rate_limit` | on when reachable | Whether to limit requests. Unset means "on unless this is loopback-only and has no `public_url`" |
| `rate_limit_per_minute` | `120` | Requests per credential per minute, once limiting is on |
| `rate_limit_failures_per_minute` | `20` | Failed authentications per **address** per minute. Keyed on the address on purpose: a token prefix is the caller's to choose, so keying failures on it would hand an attacker a fresh allowance per guess |
| `trusted_proxies` | `[]` | Addresses whose `X-Forwarded-For` is believed. Empty ignores the header entirely, which is the safe default behind no proxy |
| `cors_origins` | `[]` | Other origins a browser may call this API from — **and act as a signed-in reader from**. Empty is right for almost everyone, including you: the web UI is served by this instance, so it needs no entry here. See [below](#cors_origins-decides-more-than-it-used-to) before adding one |
| `log_level` | `INFO` | How much `serve` logs |
| `dev_mode` | `false` | Development only. Substitutes a fixed, well-known signing key when `secret_key` is unset, so a throwaway instance starts without one. Never set it on anything real |
| `default_page_size` | `50` | Rows a listing returns when the caller does not say |
| `max_page_size` | `200` | The most a caller may ask for |
| `max_hierarchy_depth` | `10` | How deep a project or subtask tree may nest. Bounds path length and the cost of a move |
| `claim_lease_minutes` | `30` | How long a task claim lasts before it expires. A lease rather than a lock, so a worker that dies does not strand the work |
| `search_backend` | `like` | Which implementation answers a search. `native` uses a full-text index and is **PostgreSQL only** — see below |

**`search_backend` changes what a search finds, not only how fast it finds it**, which is why
it is off by default and why it is worth a paragraph rather than a row.

`like` is what every instance has had until now: it matches your words anywhere inside the
text, so `ursor` finds *cursor*. It cannot be served by an index, so it reads every row's
prose — fine for a personal backlog and increasingly not fine as one grows.

`native` builds a full-text index. Measured at 20,000 tasks, a search matching nothing goes
from **119 ms to 1 ms**. In exchange it matches whole words rather than fragments: `seed` now
finds *seeded* and *seeding*, `curs` still finds *cursor* because a word can be completed from
the start, and `ursor` finds nothing at all.

**And a very common word stops narrowing.** PostgreSQL's text search drops `the`, `of`, `and`
and their kind before the query is built, so `cursor the` finds whatever `cursor` finds, where
`like` requires both and would find nothing. That is inherent to full-text search rather than
a choice made here — it is written down because the rest of this product promises that every
word you type must appear, and under this backend that is one word short of true.

It exists on PostgreSQL only. Asking for it on SQLite is not an error — you get `like`, and
`GET /v1/meta` reports which one is actually answering. Turning it on needs no migration
beyond the ordinary `subroutine db upgrade`; turning it off again is a configuration change
and nothing else.

**There are deliberately no retention settings.** §5.11 and §6.9 both describe a retention
period, and nothing purges anything yet — so `events_retention_days` and
`trash_retention_days` were removed rather than documented. A setting that silently does
nothing is worse than an absent one: you can set it, get no error, and believe it. They come
back with what enforces them.

## PostgreSQL, and when to switch

SQLite is the default and it is not a toy — it is the right answer for one person, and for a
small team that is not writing concurrently. Switch when any of these is true:

- More than a handful of people or agents write at the same time. SQLite serialises writers.
- The database has to live on a different machine from the service. It cannot: a network
  filesystem cannot give SQLite the locking it needs, and `serve` **refuses to start** on one
  rather than corrupting the file quietly.
- You want your existing backup, replication and point-in-time recovery to cover it.

**The `[postgres]` extra is not PostgreSQL.** It installs psycopg, the client driver, and it
installs perfectly happily with no server anywhere. The server is yours to provide.

On Debian or Ubuntu:

```console
# apt install -y postgresql
```

The package creates a cluster, starts it and enables it at boot, so there is no `initdb` step
by hand. `pg_lsclusters` should show one cluster, `online`.

Then a role and a database, and **both take the name of the service account**:

```console
# sudo -u postgres createuser subroutine
# sudo -u postgres createdb --owner=subroutine subroutine
```

`postgresql+psycopg:///subroutine` names no host and no user, so it connects over a Unix socket
as the *operating system* user — which under the unit is `subroutine`. The default
`pg_hba.conf` maps that straight through with peer authentication, so there is no password to
keep anywhere and nothing listening on the network.

**`--owner` is load-bearing, and it looks decorative.** Since PostgreSQL 15 the `public` schema
no longer lets every user create tables in it; the database owner does. Create the database
without it and the first migration stops on `permission denied for schema public` — a message
about schemas, arriving a long way from the decision that caused it.

Worth confirming before Subroutine touches it at all, because both are much cheaper to fix now
than with data in them:

```console
# sudo -u subroutine psql -d subroutine -c '\conninfo'
# sudo -u postgres psql -l
```

The first should report connecting as `subroutine` over a socket. In the second, check that the
`subroutine` row says `UTF8`: a minimal server image with the locale left at `C` can give you a
`SQL_ASCII` cluster.

A database on **another machine**, or one that wants a password, takes the full URL form
instead — and then `After=postgresql.service` in the unit is meaningless and can go.

### Starting on PostgreSQL

If you are setting a server up now, this is the order — and step 3 is the one that costs an
afternoon when it is skipped.

**1. Run `init` with the database named in the environment.** It cannot come from
`config.toml`, because `init` is the thing that creates that file:

```console
# sudo -u subroutine env \
    XDG_CONFIG_HOME=/var/lib/subroutine/config \
    XDG_DATA_HOME=/var/lib/subroutine/data \
    XDG_STATE_HOME=/var/lib/subroutine/state \
    SUBROUTINE_DATABASE_URL=postgresql+psycopg:///subroutine \
    /opt/subroutine/bin/subroutine init
```

**2. Note that it says the value is recorded nowhere**, because it is not:

```console
  Ready. Try: subroutine add "something to do"
  This database came from the environment, and nothing has recorded it. Put 'database_url' in
  /var/lib/subroutine/config/subroutine/config.toml, or anything started without that
  variable — a service, another shell — will look somewhere else.
```

**`init` will not write it for you, and that is deliberate.** A PostgreSQL URL routinely
carries a password, and a password belongs with the tokens rather than beside the connection
settings — that is why `credentials.toml` exists. (`config.toml` is `0600` and holds
`secret_key`; what it does not hold is anything that authenticates you to something else.)

**3. So write it yourself**, into
`/var/lib/subroutine/config/subroutine/config.toml`:

```toml
database_url = "postgresql+psycopg:///subroutine"
```

Skip this and everything looks fine until the service starts: the environment variable went
with the shell, the unit sets only the XDG paths, and `serve` looks at the SQLite default,
finds nothing, and restarts every five seconds.

**And do not answer that by running `init` again**, which is what the message used to suggest.
Without `database_url` set it will build a *second*, empty instance in SQLite, and everything
after that looks healthy: `db current` reports a schema, `list` reports an empty backlog, and
the service starts and serves nothing. `init` warns before doing it — but the remedy is step 3,
not another `init`.

**4. Check before starting anything**, as the service account:

```console
# sudo -u subroutine env \
    XDG_CONFIG_HOME=/var/lib/subroutine/config \
    XDG_DATA_HOME=/var/lib/subroutine/data \
    XDG_STATE_HOME=/var/lib/subroutine/state \
    /opt/subroutine/bin/subroutine db current
```

A schema revision means the configuration and the data agree. If it names a path ending
`.db`, step 3 has not taken effect.

**All three variables, even though this only reads.** Left off, the SQLite default resolves
against *your* data directory rather than the service's — so a check written to catch "it is
using the SQLite default" can report a perfectly healthy schema from the wrong file, which is
the confusion the paragraph above is warning about.

### Switching an instance you already have

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
  Schema is at a3f9c21d7e40.
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

# Subroutine gives requests already in flight 15 seconds and then exits. This is the outer
# bound on top of that: long enough that a clean stop always wins, short enough that a
# wedged process does not hold a restart up for systemd's 90-second default.
TimeoutStopSec=30s

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

# Only if backup_directory points outside /var/lib/subroutine. Keep the leading '-': it
# means "ignore this path if it is not there", so an unmounted volume costs you a backup
# rather than a service that will not start.
# ReadWritePaths=-/srv/backups/subroutine

[Install]
WantedBy=multi-user.target
```

`ExecStart` takes no `--host` or `--port` because both are settings; the defaults are
`127.0.0.1` and `8471`.

**`TimeoutStopSec` is the outer half of a pair, and the order matters.** Subroutine stops
accepting, gives requests already in flight **15 seconds** to finish, and then exits. systemd's
timeout has to be the longer of the two, or it would kill a shutdown that was about to
complete. Left at the default, stopping a server with a request stuck on something takes 90
seconds — and you find that out during an incident, because that is the only time anything is
stuck for long enough to notice.

**When the proxy is on another machine** — a router, a NAS, a box running Nginx Proxy Manager
or Caddy — loopback is no longer enough, because the proxy has to reach this host over the
network. Bind wider and say where the proxy serves it, both in `config.toml`:

```toml
host = "0.0.0.0"
public_url = "https://tasks.example.com"
```

That is the second of the three ways past the TLS refusal above, and it needs **no flag**: the
`https://` scheme in `public_url` is what satisfies the check. `public_url` is also published
at `GET /v1/meta`, so an agent handed a token finds out the address to come back to — which
`--insecure` would not tell it. Restart, and `journalctl -u subroutine` should show it
listening on `0.0.0.0`.

Naming the machine's own address rather than `0.0.0.0` is better where you can: it will not
follow you onto a café's wifi if this is a laptop, and it makes the intent legible to whoever
reads the file next.

**`--insecure` is for the case where there is no proxy at all** and the network is genuinely
trusted — a home LAN with nothing exposed. It goes on `ExecStart`, because it is a decision
about this invocation rather than a property of the installation:

```ini
ExecStart=/opt/subroutine/bin/subroutine serve --insecure
```

Either way, be clear about what a bind beyond loopback without TLS means: **bearer tokens
cross that network in clear**, and anything that can see the traffic can replay them. On a home
LAN that is a reasonable trade. It should be one you have made rather than one you have
inherited from a flag you copied. `ProtectSystem=strict` makes the whole filesystem read-only apart from
what `StateDirectory` grants, which is why a backup directory elsewhere needs naming — a
`ReadWritePaths` you forgot shows up as a backup that cannot be written, on the day you need
one. If the directory is on a network mount, add `Wants=` and `After=` on that mount's unit
in `[Unit]` (`mnt-backups.mount` for `/mnt/backups`) so it is there before the service starts.
`Wants=` rather than `Requires=`, deliberately: a volume that is down should cost you backups,
not the API.

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
  {"status":"ready","api_version":"1.0","schema_revision":"a3f9c21d7e40"}
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

**Setting it also turns rate limiting on**, which matters here more than it looks. The default
is "limit unless nothing outside this machine can reach us", and a proxy in front of an
application listening on `127.0.0.1` is exactly the case where the socket looks private and
the service is not. `public_url` is how this instance knows the difference.

### Telling it which address a request came from

Failed authentications are counted per address, so that guessing a token gets slower. Through
a proxy every request arrives from the *proxy*, so without help they all share one allowance —
one client hammering with a stale credential makes other people's mistakes answer `429`
instead of `401`.

Name the proxy and the real caller is counted instead:

```toml
trusted_proxies = ["127.0.0.1"]
```

That is the address **this instance sees the proxy connecting from**, which is not always the
one you think of as the proxy's. Co-located behind nginx or Caddy it is `127.0.0.1`; if the
proxy runs on another machine it is that machine's address on your network.

**Name only proxies you control.** `X-Forwarded-For` is written by whoever sends the request,
so this setting is you vouching for a specific peer. Point it at something you do not control
and any caller can choose which bucket it is counted in, which is worse than leaving it empty.

Left empty the header is ignored entirely, which is the right behaviour when nothing is in
front.

### `cors_origins` decides more than it used to

**Almost everybody should leave this empty, and that includes anybody who has just noticed the
web UI.** The browser app is served by this instance, from this instance's own address, so it
is not a cross-origin caller and needs no entry here. Adding one because a web interface now
exists is the one mistake this setting invites.

**What an entry does, in full.** It lets a page on that origin call this API from a browser
*and read the replies* — which is what CORS has always been — **and it lets that page act as
somebody who is signed in here.** Since browser sessions arrived, a write authenticated by a
session cookie is refused unless the page making it is one this instance serves, and this list
is how you say another origin counts as one. That is deliberate: naming an origin is already a
statement that a browser there may act on your behalf. It is worth knowing you are making it.

**`*` gives that up to every site on the internet.** Not in the toothless way a wildcard usually
is — this application echoes the requesting origin back with credentials allowed, so a page
anywhere can read your data and change it, using the session of any of your people who happens
to visit it while signed in. There is no case where a self-hosted instance needs this.

So:

```toml
# Only if you run a *separate* front end on another address.
cors_origins = ["https://boards.example.com"]
```

**And a way to check you meant it**, which is what an operator can actually run:

```console
$ curl -si https://subroutine.example.com/v1/meta -H 'Origin: https://somewhere-else.example' \
    | grep -i access-control-allow-origin
```

Nothing back is what you want. A line naming `somewhere-else.example` means this instance is
answering an origin you did not intend to name.

## Adding the people

An instance starts with one account — whoever ran `init`, who is its administrator. Everybody
else is two commands, and they are two on purpose: creating an account says somebody exists,
and giving them a role says where they may work. Those are different decisions and often
different people.

```console
$ subroutine user create thomas --name "Thomas Anderson" --email thomas@example.com
  Created thomas
  Local commands will go on acting as si.

$ subroutine user add thomas --role member --workspace acme
  thomas is now member in acme

$ subroutine user list --workspace acme
  si      owner
  thomas  member  Thomas Anderson
```

A new account belongs to no workspace and can see nothing until it is given a role, which is
why `user create` tells you the next command rather than stopping at "Created".

**There is no password**, so what Thomas needs next depends on what they are going to use.

**If they are going to open the web interface, hand them a sign-in link.** It signs in as
whoever it names, once, and stops working after half an hour — so it is handed over the way
anything private is, and a second one costs nothing if the first goes stale.

```console
$ subroutine login link --username thomas
```

**If they are going to use the command line, or point an agent at this instance, issue a
token.** It is readable exactly once:

```console
$ subroutine token create --username thomas --title "Thomas's laptop"
```

Neither is a lesser version of the other and somebody may want both — the link opens a browser
session, the token is what a terminal and an agent present. What they must not do is try to use
the token to sign in to the browser: a bearer token is not a session, and a **narrowed** token
cannot mint a link for itself either, because a session carries no scopes and issuing one would
hand back more authority than the token holds.

**Run `login link` from the server**, or from anywhere holding an unrestricted credential. It is
also the way back in for you: if the browser is the only way you administer this instance and
something has gone wrong with it, a link minted at the console is a door that does not depend on
anything else working.

`--username` is for somebody who already has an account; `--service-account` is for a machine
identity and creates one if there is none. They are separate flags because they are separate
decisions — naming a person under `--service-account` is refused rather than quietly handing
out their credential. Everything else — scopes, a workspace pin, an expiry — is the same for
either.

A credential is never issued for an account that could not use it: a deactivated account is
refused here rather than given a token that fails the first time it is presented.

**These commands run from wherever you are.** `token create`, `token list` and `token revoke`
go through whichever connection your next write would go to, so `subroutine -c work token list`
administers the server's credentials from a laptop that holds no database of its own. That
matters because setting an agent up is something you do on the machine the agent runs on, and
until 0.3 these were the three commands that could only be run while sitting on the server.

They still open a database *directly* when the connection is local — which, on the server, it
is. That is not a leftover: §12.4 requires the commands that administer credentials to work
when the service is the thing that has gone wrong, and reaching a local database never involved
the service. The route follows the connection precisely so that both remain true.

**Roles belong to a workspace.** `member` in one is not `member` in another; each workspace is
seeded with its own, and `subroutine user list --workspace <slug>` says who holds which.
Deciding membership needs `workspace:admin`, which is a different thing from being able to work
there.

Somebody added by mistake can be removed with `subroutine user remove`. That takes away the
membership and not the account: what they wrote stays, and stays attributed to them. The last
account able to administer a workspace **cannot** be removed from it — a workspace nobody can
administer has thrown away the remedy for every later mistake, including that one, and it
cannot be repaired from inside.

On a single-person instance, adding the second account would leave the CLI unable to tell whose
to-do list to show. It does not: `user create` pins `local_user` to the account that was already
there and says so. Setting somebody up should not take something away from you.

## Giving an agent a token

**One command does all of it**, and it is the one to reach for:

```console
$ subroutine agent create claude --project web --scope task:read --scope task:write
  Created service account claude, with the contributor role.

  Set this in the environment the agent's session starts from:

    SUBROUTINE_TOKEN_WORK=sr_7e6abdce_S2MRP1ehbK3imO9G5hPlGw3ABblhxSi6KUh0Xi4Zv24

  That is the only time the credential is shown. Nothing recovers it afterwards.

  Checked, by presenting it: claude (agent), in projects (task:read, task:write), and only within web

  Until then its shell acts as si, and nothing above bounds what it does there.
```

Three things it does that doing it by hand does not.

**The account, its membership and its credential are one act**, in one transaction. An account
with no membership authenticates and can do nothing, which reads as a broken token rather than
as a missing role — and over a network the alternative is three requests with a half-finished
agent if the second fails.

**The credential is checked by being presented.** What it can do is read back from the instance
before the command claims anything, so a scope naming a permission the role does not carry, or
a pin on a workspace the account cannot reach, is visible here rather than on the agent's first
call.

**The last line is not a warning, it is the other half of the job.** Until that variable is set,
the agent's shell resolves whatever the command line resolves — normally your own credential —
so the restriction above bounds the tools and nothing else.

### Saying what the credential is for

`--profile` names a scenario instead of assembling one out of flags. It works on both
`agent create` and `token create`, and it expands into exactly the flags below — there is
nothing a profile can express that you could not have typed.

| Profile | Reaches | Writes in | For |
| --- | --- | --- | --- |
| `worker` | one project and everything under it | the same | an agent that owns a project |
| `collaborator` | the projects named | the ones named with `--write` | reads related work for context, writes only its own |
| `observer` | the projects named, or the whole workspace | nothing | a reporting or reviewing agent |
| `colleague` | one workspace | the same | a second person, working as they would in their own |

```console
$ subroutine agent create sam --profile collaborator --project sr --project web --write web
```

**The refusals are the point.** A combination that means two things at once is turned down
rather than resolved, because a credential that quietly does something other than what you
just described is one nobody checks again:

```console
$ subroutine agent create nosy --profile observer --write web
  '--write' does not go with the 'observer' profile.
    write: 'observer' changes nothing at all.
      Either drop '--write', or use '--profile collaborator' for an agent that reads widely and writes in one place.
```

Naming one that does not exist prints all four, because four is short enough to read here
rather than go and look up.

The rest of this section is the same work done piece by piece, which is worth reading once
because it says what each piece is for.

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

`--project` is the other axis, and the one to reach for when an agent works on one thing.
`--scope` decides which *verbs* a credential carries; `--project` decides which *items* it can
reach at all:

```console
$ subroutine token create --service-account web --workspace projects --project web
  Created service account web, with the contributor role.
  Restricted to web and anything filed underneath.
```

**It brings the sub-projects with it**, which is why the command says so rather than echoing
what you typed: a restriction that stopped at one level would be useless on any tree deeper
than one. Everything outside it is not merely absent from a listing — the project does not
resolve at all, so the agent is told there is no such project rather than that it may not look.

Name the project by its key. Keys are unique per workspace rather than per instance, so if two
workspaces both hold a `web` the command refuses and asks which, rather than picking one — an
agent pointed at the wrong tree works perfectly, against the wrong tree.

**Give the token to the client as `SUBROUTINE_TOKEN`.** It is never accepted in a query string
and never read from `config.toml`. `--store <connection>` writes it to `credentials.toml`
instead, and is deliberately opt-in: storing a narrow agent token under your own connection
name would quietly narrow your own CLI.

### An agent that can also run a shell

This is the part that catches people out, and it is worth understanding before you conclude an
agent is bounded.

**A credential is resolved per process, not per agent.** An AI agent typically reaches an
instance two ways at once: through tools its editor wired up, and by running `subroutine` in a
shell. Those are separate processes and they resolve credentials separately — so configuring
the agent's tools with its own token does *nothing* about the shell, which finds whatever the
command line finds, normally yours.

The result is an agent that is itself half the time and you the other half. That is worse than
plainly acting as you, because it is partial: check the event log and the agent's own name is
there, on the half that went through its tools.

**One variable settles it.** Credentials are looked for in this order:

1. `SUBROUTINE_TOKEN_<CONNECTION>` in the environment — the connection name upper-cased, with
   anything that is not a letter or a digit as an underscore
2. `SUBROUTINE_TOKEN`, for the default connection only
3. whatever the connection's own `token_env` or `token_command` names
4. `credentials.toml`

The first wins, and **both the shell and the editor's tools inherit it** from the environment
the session was started in. So set it where you launch the agent, and leave the token field in
your editor's plugin settings empty:

```console
$ SUBROUTINE_TOKEN_WORK=sr_… claude
```

`credentials.toml` then holds *you*, which is the right default: the person is who is there
when no session has claimed the terminal.

**Check it rather than assuming it**, from inside the agent's own shell:

```console
$ subroutine whoami
  claude (agent), via token 'web agent' (24148201…).
  Narrowed to workspace 'projects'; projects web; scopes task:read, task:write.

    projects  Contributor  may: task:read, task:write
```

A person's name on that line is the split, and it is the only place it is visible.

**The cheaper answer, where it fits:** your own token does not have to be on a machine an agent
uses. If you work from your laptop and only agents work on the build box, then
`credentials.toml` there should hold the *agent's* credential and nothing else — and the split
stops mattering, because both halves are the agent.

## Reaching it from your own machine

Everything above set up a server. This is the other end: your own account, on your own laptop
or on the same machine, listing the server's work beside your own.

**It is a *connection*, and your own database is one too.** That is the whole design (§13.7):
`subroutine today` asks every connection and merges the answers, so the dentist and the
stand-up appear in one list rather than in two tools. Your own database is called `local` and
exists whether or not you declare it.

**A server account and your own account are separate installations on one machine, and that is
correct rather than a mistake.** Subroutine keeps its files under the XDG directories, so the
service account's instance lives under `/var/lib/subroutine` and yours under `~/.config` and
`~/.local/share`. Running `subroutine list` as yourself shows *your* items and always will;
`subroutine config show` will say `database_url … [default]` pointing at your own SQLite file.
Reaching the server is not a matter of changing that — it is a matter of adding a connection
beside it.

**One command, and it asks for the token** — which you issue on the server first, as the
service account. That is further down this section, and it is worth reading before you start:
a token is shown once and stored nowhere, so there is nothing to go back and look up.

```console
$ subroutine connections add work --url http://127.0.0.1:8471
Token for work:
Reached hpz2g4 as si, in acme.
Added work to …/config.toml
Its token is in …/credentials.toml, readable only by you.
```

The name — `work` here — is *yours*. It is the first segment of every address the server's
items print as, so `work/acme/#42`, and two people connected to the same server may call it
different things. A name must start with a letter, because one made only of digits would read
as a ref.

**It reaches the instance before it writes anything**, with the credential you just gave it —
the same call every listing begins with. A mistyped address, a revoked token, a proxy
answering instead of the server: each is refused there and then, with nothing recorded, rather
than becoming one line of failure among tomorrow's results. That is also why it can tell you
the name the server knows you by, which is the only thing that confirms you pasted the token
you meant to.

If the machine has no instance of its own — a second laptop, a workstation whose work all
lives on the server — it also makes that connection where new work goes, and says so. On a
machine that already has its own list it leaves that alone, because moving somebody's writes
off their own to-do list is their decision. `--default` asks for it either way.

Other things it takes: `--read-only` to reach an instance and refuse to write to it, and
`--token-env` or `--token-command` to fetch the credential from the environment or from
`pass`, `gpg`, `secret-tool` or a password manager instead of storing one.

**There is no `--token`, deliberately.** A credential passed as an argument lands in shell
history and in the process list (§12.3a). Piping one in works, for a script or an agent:

```console
$ pass show work/subroutine | subroutine connections add work --url http://127.0.0.1:8471
```

**Tokens live in their own file and never in `config.toml`** (§12.3a), so that a configuration
file can be copied, committed or pasted into a bug report without taking a credential with it.
`credentials.toml` is written `chmod 600`. If your shell already has `SUBROUTINE_TOKEN` set,
that is another way in and needs no file.

**It is not `secret_key`,** which is the only thing in `config.toml` that looks like a
credential and is the wrong one. Every instance writes its own at `init` — the server has one
already — and it signs pagination cursors and nothing else. Copying it across achieves nothing.

**And there is nothing to look up.** Only a hash of a token is stored, so no command can show
you one that was issued earlier; `token list` prints prefixes, which is what `revoke` takes. If
you have lost a token, issue another and revoke the old one. Issue it on the *server*, as the
service account:

```console
# sudo -u subroutine env \
    XDG_CONFIG_HOME=/var/lib/subroutine/config \
    XDG_DATA_HOME=/var/lib/subroutine/data \
    XDG_STATE_HOME=/var/lib/subroutine/state \
    /opt/subroutine/bin/subroutine token create --title "my laptop"
```

That is the only time the secret is shown. See
[Giving an agent a token](#giving-an-agent-a-token) for narrowing one.

Then it just appears, with each row saying where it lives:

```console
$ subroutine list
  Local
              #1  Pay the gas bill

  work
    work/acme/#1  Fix the deploy script

    Tip: subroutine show work/acme/1 — read one of them in full
```

**`subroutine connections` is how you check it**, and it is worth knowing about because it
stays out of `subroutine --help` until a second connection exists — which is to say, until the
thing you are checking has already worked. `connections add` is hidden with it, for the same
reason and with the opposite effect, which is why this page names it: nothing on a machine can
tell "not set up yet" from "never will be", so the command that fixes the second cannot
announce itself to the first. It lists what this machine reaches and, for each, *which of the
four places its token came from*, which is the question that actually bites:

```console
$ subroutine connections
  local  sqlite:////home/si/.local/share/subroutine/subroutine.db  …/credentials.toml  default
```

No token is printed and none can be recovered from what is. If your new connection is missing
from that list, `config.toml` is not being read the way you think — check the table name and
the spelling of `[connections.<name>]`.

Each row prints **the shortest address that resolves** — a bare number for your own, and the
connection and workspace for anything that needs them. Whatever it prints is what you can type
back, which is the point: a bare number beside an item on somebody else's server would be an
invitation to act on the wrong one.

`subroutine use work` changes which connection a *write* goes to — `subroutine add` and the
rest. It never changes what you can see: reads always span everything reachable, which is what
makes switching safe (§13.7).

If a connection cannot be reached, the rest of the list still prints and one line says which
one failed. That is deliberate: being told nothing about your own to-do list because a work
server is down is the outcome this design exists to avoid.

`connections add` writes a `[connections.<name>]` table, and the file is still yours to edit
for anything it does not ask about. The other keys that table takes: `display_name` for what
it is called in output, `read_only = true` to refuse writes to it from this machine,
`token_env` or `token_command` to fetch the credential from the environment or from `pass`,
`gpg`, `secret-tool` or a password manager rather than from a file, `timeout_seconds`, and
`enabled = false` to keep a connection configured and switched off. Anything else in that table
is refused by name rather than ignored.

**Two connections may not name one instance**, and `connections add` refuses a second name for
a server this machine already reaches. A merged listing would count everything on it twice, so
the refusal is at the moment you can pick a different word rather than on the first listing —
where it withholds every result and can only tell you to go and edit a file.

## Reaching it from an agent, with nothing installed

Everything above assumes the person has Subroutine on their machine. **An agent does not need
it.** The server speaks MCP itself, at `POST /mcp`, so a coding agent reaches this instance with
a URL and a token and nothing else — no Python, no package, no `config.toml`.

This is the case worth designing for: somebody works with you for a month, you send them a URL
and a token, and their agent files work against your instance the same afternoon.

Issue them a credential exactly as above — `subroutine agent create`, or `token create
--service-account` — and give them two things:

```
URL:   https://subroutine.example.com/mcp
Token: sr_…
```

**The easiest thing to tell them is to install the plugin**, which asks for exactly those two
things and carries the working practice as well:

```console
$ claude plugin marketplace add simonholliday/subroutine
$ claude plugin install subroutine-remote@subroutine
```

It has no fields for a command or a connection, because it needs neither. Until they paste an
address in, it sits there configured-but-idle rather than reporting a fault.

**Tell them they will need Git**, though. The marketplace is a repository and the first command
clones it, so on a machine with no development tools that step refuses before anything of ours
is reached. It is the only prerequisite on their side, it is Claude Code's rather than ours, and
it is invisible to everybody whose machine already has it.

Or, without the plugin, one command:

```bash
claude mcp add --transport http subroutine https://subroutine.example.com/mcp \
  --header "Authorization: Bearer sr_…"
```

**The credential is the boundary, and it is the same one everywhere.** The scopes, the project
scope and the workspace pin apply here exactly as they do to `/v1` and to the command line, so
an agent given a read-only token over MCP is read-only over MCP. Rate limiting applies too, per
token.

**Put `?workspace=` in the address you hand over if this instance has more than one:**

```
https://subroutine.example.com/mcp?workspace=projects
```

**This is yours to get right rather than theirs.** Without it, an agent on a multi-workspace
instance has every read refused as ambiguous — the refusal names the workspaces it could have
meant, but the person receiving it has no way to know which one you intended, and on the plugin
path the remedy is a settings field they would have to be told about. You know the answer; put
it in the address.

It is a default rather than a limit — a call may still name another workspace, and a token
pinned to one is what actually narrows access.

**The endpoint needs the instance to be reachable from wherever the agent runs.** Claude Code
connects from the user's own machine, so a LAN address or a VPN-only host is fine. The Claude
desktop and web clients connect from Anthropic's servers instead, which means a publicly
reachable address — see [A reverse proxy](#a-reverse-proxy).

`GET` on the endpoint answers `405`, which is correct rather than a fault: this server has
nothing to send that a client did not ask for, so there is no event stream to hold open. A
client that tries carries on without one.

## Backups

Unset, backups go into the instance's own data directory, and for a single machine that is a
real backup: it protects you from a bad migration, a restore you did not mean, a delete nobody
meant, and a database that corrupts itself. It fails for exactly one thing, which is losing the
device.

So if the device matters, set a directory on another volume:

```toml
backup_directory = "/srv/backups/subroutine"
```

That is a judgement about what you are protecting against, not a requirement. Nothing here
refuses a path, warns about one, or nags about how old the newest copy is — how old is too old
depends on whether this is a laptop or a server, and only you know which.

A network mount is the intended destination and works. The file is built locally and then
copied, because SQLite's `VACUUM INTO` cannot write to an SMB share any more than the live
database can live on one; delivery is then verified where the file landed, by size and by
reading the schema version back out of it. A copy that fails verification is deleted rather
than left looking like a backup.

**Permissions may not survive the trip, and that is worth knowing rather than working around.**
A backup is written `0600`, because it holds every task, comment and token hash. Many network
mounts fix their permissions at mount time — CIFS with `file_mode=`, for instance — so the
`chmod` succeeds and changes nothing, and the file is as readable as everything else on the
share. If that matters, the answer is on the share rather than here.

```console
$ subroutine db backup
  Backed up instance 'default' to /srv/backups/subroutine/subroutine-default-20260731T141853Z-d5d0458f5ad5.sql
  60,069 bytes, schema d5d0458f5ad5.

$ subroutine db backups
  Backups of instance 'default', in /srv/backups/subroutine:
    subroutine-default-20260731T141853Z-d5d0458f5ad5.sql  2026-07-31 14:18 UTC  60,069 bytes  schema d5d0458f5ad5
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
  {"name":"subroutine-…-d5d0458f5ad5.sql","schema_head":"d5d0458f5ad5","size_bytes":61311,…}
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

### Keeping credentials out of your logs

**A sign-in link travels in a URL, so it reaches every access log that sees the request.** It
has to: a link is opened by clicking one, and a click is a `GET`. `subroutine serve` redacts it
from its own access log — you will see `GET /signin?link=REDACTED` rather than the secret — and
it does the same for an API token somebody has wrongly put in `?token=`, `?api_key=` or
`?access_token=`, which is refused but is a real credential by the time it is refused.

**Your proxy logs the same request line, and we cannot reach that.** If you run one, tell it to
drop the query string from the paths it records. In Nginx that is a `log_format` using `$uri`
rather than `$request`:

```nginx
log_format subroutine '$remote_addr - "$request_method $uri" $status';
access_log /var/log/nginx/subroutine.log subroutine;
```

Two things worth knowing rather than guessing:

- **A logged link is usually already spent**, because the log line is written when the response
  goes out and the link is consumed before that. The exception is the confirmation page — if
  the browser was already signed in as somebody else, the link is deliberately left usable so
  that saying *no* costs nothing, and it stays usable for the rest of its half hour.
- **A link is good for thirty minutes and works once.** That is the reason a lapse here is
  worth fixing rather than panicking about, and `subroutine login revoke <username>` cancels
  every unspent link and live session that person has.

**If you run the app under your own uvicorn or gunicorn rather than `subroutine serve`**, call
`subroutine.api.logs.redact_access_logs()` before starting it; the filter belongs to a logger in
your process, and nothing else installs it for you.

## Upgrading

**Before and after, `subroutine doctor` says whether this machine is coherent.** One command:
what is running and where it came from, which configuration it is reading, what each connection
answers, and when a backup was last taken.

```console
$ subroutine doctor
  program  0.2.1, at /opt/subroutine/bin/subroutine
  config   /var/lib/subroutine/config
  data     /var/lib/subroutine/data
  state    /var/lib/subroutine/state
  local    0.2.1, schema ce11c7d2df2f, as si (person)
  backups  19 in /srv/backups/subroutine, newest subroutine-default-20260803T053711Z-d5d0458f5ad5.sql (4,046,848 bytes, today)

  Nothing here needs attention.
```

Run it **as the service account, with the same three variables** as everything else in this
section — that is the whole point of the `config`, `data` and `state` lines. If they are not
the ones the unit sets, you are looking at a different installation from the one that serves
requests, and everything below them is true about the wrong machine.

It exits non-zero when something needs attention, so it can be the last line of an update
script. It talks only to the instances you have configured.

**Subroutine never checks for updates on its own.** There is no setting that makes it, and an
instance can run for years without making an outbound request. Asking is something you do:

```console
$ subroutine db upgrade --check
```

It answers in two or three lines — what is running, what has been released, and **whether
taking it changes the database schema**. That last line is the reason the command exists: it
is the difference between planning a short outage and meeting one halfway through an install.

It reports what is *running*, which is not always what a package manager thinks is installed —
an editable install carries the version it was made at. And it changes nothing at all, so it is
safe on a machine you have not decided about yet.

The package manager moves the code. Subroutine moves the database. In that order, and it will
not try to do the first for you — a tool that installs software over itself fights whatever
installed it, cannot do it safely while running, and is worse at it than your package manager.

```console
# systemctl stop subroutine
# /opt/subroutine/bin/pip install --upgrade subroutine
# sudo -u subroutine env \
    XDG_CONFIG_HOME=/var/lib/subroutine/config \
    XDG_DATA_HOME=/var/lib/subroutine/data \
    XDG_STATE_HOME=/var/lib/subroutine/state \
    /opt/subroutine/bin/subroutine db upgrade
# systemctl start subroutine
```

**Those three variables are not decoration, and this step is the one place leaving them off
fails quietly.** `upgrade` acts on a *database*, and it finds that database through
configuration — so without them it reads *your* `config.toml` rather than the service's, finds
whatever database that names, and reports on the wrong one. It will look like it worked. They
are the same three the unit sets and the same three [`init`](#first-run-and-what-it-writes) was
run with; a test fails the build if the two lists stop matching.

**Stop the service before upgrading, not after.** The order above is deliberate: install first
and start last, so there is never a moment where new code is serving an old database. If there
is, `/readyz` returns 503 saying exactly that — and ordinary requests fail too, because the
code is querying columns the database has not got yet.

`subroutine db upgrade` is the whole of the second step, and its value is the ordering rather than
any one part of it: report what is installed and what the database is at, back up and verify the
copy where it landed, migrate, then read the schema back rather than assuming.

```console
$ subroutine db upgrade
  Subroutine 0.5.0 expects schema a3f9c21d7e40.
  The database is at 233f898a2bee.
  About to upgrade the database of the default instance, at postgresql+psycopg:///subroutine.
  Backed up to /srv/backups/subroutine/subroutine-20260731T144206Z-233f898a2bee.sql (60,069 bytes).
  Upgraded from 547fe53b263c to a3f9c21d7e40.
```

It is safe to run when there is nothing to do — it prints the three numbers and stops, which is
also the cheapest way to ask the question:

```console
$ subroutine db upgrade
  Subroutine 0.5.0 expects schema a3f9c21d7e40.
  The database is at a3f9c21d7e40.
  Nothing to do.
```

**Read the version on that first line, because it is the only part of this that can tell you
step one worked.** A release that carries no migration and an upgrade that never happened print
the same two schema numbers and the same `Nothing to do.` — so if the version is not the one you
just installed, the database is fine and the *software* did not move. That happens more easily
than it sounds: a copy installed from a checkout carries a development version, which compares as
newer than anything published, so `pip install --upgrade` declines it without failing. The
command says so when it sees one.

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
  Local: This database is at schema 233f898a2bee, and this build expects a3f9c21d7e40.
    Run 'subroutine db upgrade' — it backs up first, then migrates.
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

## What the licence asks of you, which is almost nothing

Subroutine is [FSL-1.1-ALv2](../LICENSE). **Running it, modifying it and serving it to your own
people are all free and unconditional** — internally, commercially, at any size, for ever. There
is no obligation to publish anything, and nothing here is triggered by having users.

The one thing the licence withholds is **selling other people access to it as a service**. If
that is what you are setting up, write to simon.holliday@protonmail.com first — a commercial
licence is available by agreement, and it is cheaper than finding out afterwards.

Each release becomes Apache-2.0 two years after it ships, automatically.

**`source_url` is a promise rather than an obligation, and that is worth knowing before you
change it.** Under the AGPL this page carried a legal requirement; under the FSL it does not,
and the field stays anyway because somebody using an instance ought to be able to find the
source of the thing they are using. It is a setting so that a fork can point at *its* source
rather than at this repository, which it would otherwise do while being wrong:

```toml
source_url = "https://git.example.com/us/subroutine"
```

Leave it at the default if you have not modified anything, and change it if you have.
