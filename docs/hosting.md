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
- [Adding the people](#adding-the-people)
- [Giving an agent a token](#giving-an-agent-a-token)
- [Reaching it from your own machine](#reaching-it-from-your-own-machine)
- [Backups](#backups)
- [Credentials](#credentials)
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
carries a password, and this file is the one that holds no secrets — that is why
`credentials.toml` exists.

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
# sudo -u subroutine env XDG_CONFIG_HOME=/var/lib/subroutine/config \
    /opt/subroutine/bin/subroutine db current
```

A schema revision means the configuration and the data agree. If it names a path ending
`.db`, step 3 has not taken effect.

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
  Schema is at 547fe53b263c.
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
`127.0.0.1` and `8471`.

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
  {"status":"ready","api_version":"1.0","schema_revision":"547fe53b263c"}
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

**There is no password.** Subroutine authenticates with bearer tokens, so what Thomas needs next
is one of her own, and it is readable exactly once:

```console
$ subroutine token create --username thomas --title "Thomas's laptop"
```

`--username` is for somebody who already has an account; `--service-account` is for a machine
identity and creates one if there is none. They are separate flags because they are separate
decisions — naming a person under `--service-account` is refused rather than quietly handing
out their credential. Everything else — scopes, a workspace pin, an expiry — is the same for
either.

A credential is never issued for an account that could not use it: a deactivated account is
refused here rather than given a token that fails the first time it is presented.

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

Two files, both under `~/.config/subroutine`. First the connection, in `config.toml`:

```toml
[connections.work]
url = "http://127.0.0.1:8471"
```

The name — `work` here — is *yours*. It is the first segment of every address the server's
items print as, so `work/acme/#42`, and two people connected to the same server may call it
different things. A name must start with a letter, because one made only of digits would read
as a ref.

Then the token, in `credentials.toml` beside it, keyed by that same name:

```toml
[work]
token = "sr_…"
```

**Tokens live in their own file and never in `config.toml`** (§12.3a), so that a configuration
file can be copied, committed or pasted into a bug report without taking a credential with it.
Make it `chmod 600`. If your shell already has `SUBROUTINE_TOKEN` set, that is another way in
and needs no file.

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
thing you are checking has already worked. It lists what this machine reaches and, for each,
*which of the four places its token came from*, which is the question that actually bites:

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

Other keys a `[connections.<name>]` table takes: `display_name` for what it is called in
output, `read_only = true` to refuse writes to it from this machine, `token_env` or
`token_command` to fetch the credential from the environment or from `pass`, `gpg`,
`secret-tool` or a password manager rather than from a file, `timeout_seconds`, and
`enabled = false` to keep a connection configured and switched off. Anything else in that table
is refused by name rather than ignored.

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
  Backed up instance 'default' to /srv/backups/subroutine/subroutine-default-20260731T141853Z-547fe53b263c.sql
  60,069 bytes, schema 547fe53b263c.

$ subroutine db backups
  Backups of instance 'default', in /srv/backups/subroutine:
    subroutine-default-20260731T141853Z-547fe53b263c.sql  2026-07-31 14:18 UTC  60,069 bytes  schema 547fe53b263c
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
  {"name":"subroutine-…-547fe53b263c.sql","schema_head":"547fe53b263c","size_bytes":61311,…}
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
  This version expects schema 547fe53b263c.
  The database is at 233f898a2bee.
  About to upgrade the database of the default instance, at postgresql+psycopg:///subroutine.
  Backed up to /srv/backups/subroutine/subroutine-20260731T144206Z-233f898a2bee.sql (60,069 bytes).
  Upgraded from 233f898a2bee to 547fe53b263c.
```

It is safe to run when there is nothing to do — it prints both numbers and stops, which is also
the cheapest way to ask the question:

```console
$ subroutine upgrade
  This version expects schema 547fe53b263c.
  The database is at 547fe53b263c.
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
  Local: This database is at schema 233f898a2bee, and this build expects 547fe53b263c.
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
