# Sending events to music software (OSC)

**Most people will never need this page.** If you do not make music with software, you can stop
here: nothing it describes happens unless a workspace's administrator turns it on.

Subroutine can send a short message to music software whenever something happens in a workspace -
an item filed, finished or commented on, a milestone closed - so that the music can answer with a
sound, or by changing what it plays. It is how Subroutine joins in with the other apps of the
Subsystem family, which already talk to each other this way.

The messages use **OSC**, short for Open Sound Control: a common way for music programs to talk to
each other over a network. Wikipedia has [a plain introduction](https://en.wikipedia.org/wiki/Open_Sound_Control).

## Turning it on

The last section of a workspace's settings page is **Sending events to music software (OSC)**. Only
the workspace's administrator can change it.

- **Where to send them** - the computer the music software runs on, and the port it listens on:
  `studio.local:9000`, or an IP address such as `192.168.0.20:9000`. The form OSC tools write,
  `osc.udp://studio.local:9000`, is accepted too. Empty sends nothing, which is how every workspace
  starts.
- **Include each item's title** - off unless you turn it on. OSC is not encrypted, so anybody on the
  same network could read what the messages carry.

Saving the address is itself something that happens in the workspace, so the music software hears
`/subroutine/workspace/edited` straight away - a quick way to see that the two can reach each other.

Through the API the two settings are `osc.send_to` and `osc.titles`, on the workspace:

```
PATCH /v1/workspaces/studio
{"settings": {"osc.send_to": "studio.local:9000"}}
```

## What is sent

**Fire and forget.** Each event is one small UDP message, sent once the change has been saved.
Nothing waits for it and nothing checks that it arrived: if the music software is not running, the
message is simply lost, and nothing in Subroutine slows down or goes wrong.

**Every event in the workspace is sent, except anything in a private project or beneath one.** A
busy moment - an agent closing ten items, say - sends one message for each, as it happens, and the
music software decides what to play.

### The address

Each message's address says what happened, and to what kind of thing:

```
/subroutine/<type>/<word>
```

`<type>` is the item's type - `task`, `bug`, `feature`, `milestone` and the rest, as the workspace
names them - or a document's type, such as `decision`, or `project` or `workspace`. A comment, a
link or a check is addressed by the item it is on, so a comment on a bug is
`/subroutine/bug/commented`.

`<word>` says what happened:

| To | Words |
| --- | --- |
| An item | `filed`, `done`, `cancelled`, `reopened`, `edited`, `moved`, `deleted`, `restored`, `claimed`, `released`, `commented`, `comment-edited`, `comment-deleted`, `linked`, `unlinked`, `verified` |
| A document | `written`, `revised`, `moved`, `deleted`, `restored`, `commented`, `comment-edited`, `comment-deleted`, `linked`, `unlinked` |
| A project | `created`, `edited`, `moved`, `deleted`, `restored`, `joined`, `left`, `commented`, `comment-edited`, `comment-deleted` |
| The workspace | `created`, `edited`, `deleted`, `restored`, `seeded`, `joined`, `left`, `role-changed` |

`done` means finished, so closing a milestone is `/subroutine/milestone/done`. A change that neither
finishes an item nor reopens it is `edited`.

### The values

After its address, each message carries these, in this order:

1. the item's number, or 0 where the event is about no item;
2. its importance, 1 to 5, or 0 where it has none;
3. its urgency, the same way;
4. its project, written from the top - `subroutine/ui` - or empty;
5. `person` or `agent`, for whoever made the change, or empty where Subroutine did it itself;
6. its title, only where titles are turned on.

Numbers are 32-bit integers and the rest are strings. OSC has no way to say *none*, so 0 and the
empty string say it instead.

## In Subsequence

A composition listens with `composition.osc()` and gives an address a handler with
`composition.osc_map`. The handler is given the address, then the values above:

```python
composition.osc(receive_port=9000)

def milestone (address, number, importance, urgency, project, who, *title):
    composition.data["milestone"] = number

def filed (address, number, importance, urgency, project, who, *title):
    composition.data["importance"] = importance

composition.osc_map("/subroutine/milestone/done", milestone)
composition.osc_map("/subroutine/*/filed", filed)
```

A `*` in an address matches any one part of it, so the last line catches everything filed,
whatever its type. Any other program that receives OSC can listen the same way.
