# Permissions

Everything somebody can ask an instance to do, the permission each act asks, and where it
asks it. A role in a workspace grants a set of permissions, a credential can be narrowed to
fewer, and a private project is seen only by the accounts it is shared with and whoever
those answer to, whatever their role. [Section 7 of the
design](design.md#7-identity-authentication-and-authorisation) sets out the whole model.

**How to read a row.** *Asks* is what the permission check is asked, and *On* is where: the
installation, the workspace, the project the act lands in, or one's own account, which every
credential reaches. An act asking no permission is answered without one, and one on the
workspace still needs somebody who can reach it. *Also* is a rule of the act's own that can
refuse what its permissions allow, and whether the agent tools can do it: a session through
them is never handed a credential, a sign-in link or a calendar feed, and never holds a
permission over the installation, `workspace:admin` or `workspace:delete`.

**A listing holds only what its asker can see.** Reading the work of a workspace asks
`task:read` once, and the work of a private project is still left out for anybody who cannot
see it. The accounts listed are those within the asker's reach: whoever shares a workspace
with them, and their own chain.

The page is generated from the acts the permission tests drive through every way in - the
terminal, the HTTP API and the agent tools - so it says what the instance does.

<!-- Generated from subroutine/acts.py. Do not edit by hand. -->

## Reading the installation and oneself

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Say which instance this is | - | - | - |
| Read the agent's guide | - | - | - |
| Read a workspace's vocabulary | - | the workspace | - |
| Say who is asking | - | - | - |
| Read an account by name | - | - | An account out of the asker's reach is answered as a name nobody has. |
| List the accounts | - | - | - |
| List one's credentials | - | - | - |
| List one's calendar feeds | - | - | - |
| List every workspace on the installation | `instance:admin` | the installation | Never through the agent tools. |
| List the projects nobody can reach | `instance:admin` | the installation | Never through the agent tools. |
| List the workspaces nobody can administer | `instance:admin` | the installation | Never through the agent tools. |
| List the workspaces with no owner who can act | `instance:admin` | the installation | Never through the agent tools. |
| List the installation's backups | `instance:admin` | the installation | Never through the agent tools. |
| List the workspaces one reaches | - | - | - |

## Reading a workspace

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Read a workspace's settings | `workspace:read` | the workspace | - |
| Read a workspace | `workspace:read` | the workspace | - |
| List a workspace's members | `workspace:read` | the workspace | - |
| List the statuses | - | the workspace | - |
| List the link types | - | the workspace | - |
| List the tags | - | the workspace | - |
| List the saved views | - | the workspace | - |
| Read a shared saved view | - | the workspace | - |
| List the projects | `project:read` | the workspace | - |
| Read a project | `project:read` | a project | - |
| Read what happened to a project | `project:read` | a project | - |
| Read a project's settings | `project:read` | a project | - |
| List who a private project is shared with | `project:read` | a private project | - |
| Read the agenda | `task:read` | the workspace | - |
| Count the work | `task:read` | the workspace | - |
| List the work | `task:read` | the workspace | - |
| Search the work | `task:read` | the workspace | - |
| List the documents | `task:read` | the workspace | - |
| Count the documents | `task:read` | the workspace | - |
| Read what changed | - | the workspace | - |
| Read what happened | - | the workspace | - |
| Take away every workspace row | - | the workspace | - |
| Take away every task row | - | the workspace | - |
| Take away every comment row | - | the workspace | - |
| Take away every event row | - | the workspace | - |

## Reading one item

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Read a task | `task:read` | a project | - |
| Read a task in a private project | `task:read` | a private project | - |
| Read a document | `task:read` | a project | - |
| Read a task's comments | `comment:read` | a project | - |
| Read a task's history | `task:read` | a project | - |
| Read what happened to a task | `task:read` | a project | - |
| Read a task's links | `task:read` | a project | - |
| Read what mentions a task | `task:read` | a project | - |
| Read what is beneath a task | `task:read` | a project | - |
| Read what governs a task | `task:read` | a project | - |
| Read what was checked against a task | `task:read` | a project | - |
| Read the links a task's words suggest | `task:read` | a project | - |
| Read when a repeating task comes round | `task:read` | a project | - |

## Writing work

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| File a task in a project | `task:write` | a project | - |
| Retitle a task | `task:read`, `task:write` | a project | - |
| Plan a task for a day | `task:read`, `task:write` | a project | - |
| Finish a task | `task:read`, `task:write` | a project | - |
| Finish a task whose parent is in the trash | `task:read`, `task:write` | a project | Nothing in the trash, or beneath it, changes but by restoring or deleting it. |
| Let one occurrence of a repeat go by | `task:read`, `task:write` | a project | - |
| Put a task under another | `task:read`, `task:write` | a project | - |
| Take a task | `task:read`, `task:write` | a project | - |
| Give a task back | `task:read`, `task:write` | a project | - |
| Put a task in the trash | `task:read`, `task:delete` | a project | - |
| Take a task out of the trash | `task:read`, `task:delete` | a project | - |
| Record a check against a task | `task:read`, `task:write` | a project | - |
| Link two tasks | `task:read`, `task:write` | a project | - |
| Link a task to one in a private project | `task:read`, `task:write` | a project | Both ends must be work the asker can see. |
| Withdraw a link | `task:read`, `task:write` | a project | - |
| Comment on a task | `task:read`, `comment:write` | a project | - |
| Delete a comment its author wrote | `task:read`, `comment:read`, `comment:write` | a project | A comment is deleted by its author, whoever they answer to, or an administrator. Through the agent tools, never as an administrator. |
| Rewrite a comment | `task:read`, `comment:read`, `comment:write` | a project | Only the person who wrote a comment rewrites it. |
| Write a document | `task:write` | a project | - |
| Revise a document | `task:read`, `task:write` | a project | - |

## A workspace's vocabulary and views

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Add a status | `status:write` | the workspace | - |
| Rename a status | `status:write` | the workspace | - |
| Remove a status | `status:write` | the workspace | - |
| Add a link type | `link_type:write` | the workspace | - |
| Reword a link type | `link_type:write` | the workspace | - |
| Remove a link type | `link_type:write` | the workspace | - |
| Add a tag | `tag:write` | the workspace | - |
| Rename a tag | `tag:write` | the workspace | - |
| Remove a tag | `tag:write` | the workspace | - |
| Rename a tag used only on private work | `tag:write` | the workspace | A tag the asker cannot see is not there to rename. |
| Save a view | `task:write` | one's own account | - |
| Change somebody else's shared view | `task:write` | the workspace | A saved view is changed by whoever saved it. |
| Forget somebody else's shared view | `task:write` | the workspace | A saved view is forgotten by whoever saved it, whoever they answer to, or an administrator where it is shared. Through the agent tools, never as an administrator. |
| Take somebody else's shared view from in front of the workspace | `task:write` | the workspace | A shared view is managed by whoever saved it, whoever they answer to, or an administrator. Through the agent tools, never as an administrator. |

## Projects

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Make a project | `project:write` | the workspace | A credential narrowed to projects makes none outside them. |
| Retitle a project | `project:write` | a project | - |
| Change a project's key | `project:write` | a project | - |
| Put a project inside another | `project:write` | a project | The new parent is written into, so it is within reach too. |
| Make a private project public | `project:write` | a private project | Whoever stewards a private project decides who sees it. Through the agent tools, never as an administrator. |
| Share a private project with one more person | `project:write` | a private project | Whoever stewards a private project decides who sees it. Through the agent tools, never as an administrator. |
| Take a private project away from somebody | `project:write` | a private project | Whoever stewards a private project decides who sees it. |
| Make a public project private | `project:write` | a project | Whoever stewards a project decides who sees it. Through the agent tools, never as an administrator. |
| Move a task out of a public project into a private one | `task:read`, `task:write` | a private project | Work leaves the sight of who can see it only by its project's steward. Through the agent tools, never as an administrator. |
| Put a project in the trash | `project:delete` | a project | - |
| Take a project out of the trash | `project:delete` | a project | - |

## Workspaces

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Make a workspace | `instance:workspace_create` | the installation | Never through the agent tools. |
| Describe a workspace | `workspace:write` | the workspace | - |
| Change a workspace's short name | `workspace:write` | the workspace | - |
| Choose the colour a workspace's work is marked with | `workspace:write`, `workspace:admin` | the workspace | Never through the agent tools. |
| Delete a workspace | `workspace:delete` | the workspace | Never through the agent tools. |
| Bring a deleted workspace back | `workspace:delete` | - | Only somebody who could delete it brings a workspace back. Never through the agent tools. |
| Add somebody to a workspace | `user:admin` | the workspace | - |
| Change somebody's role | `user:admin` | the workspace | - |
| Take somebody out of a workspace | `user:admin` | the workspace | - |

## Accounts, credentials and the installation

| Act | Asks | On | Also |
| --- | --- | --- | --- |
| Say where one keeps one's diary | - | - | - |
| Mint a credential for oneself | - | - | A credential never mints a wider one. Never through the agent tools. |
| Mint a sign-in link for oneself | - | - | Only a person signs in, and only with a credential that never expires. Never through the agent tools. |
| Mint a calendar feed for oneself | - | the workspace | A credential never mints a wider one. Never through the agent tools. |
| Make an account for a person | `instance:user_create` | the installation | Only a person makes a person. Never through the agent tools. |
| Mint a credential for somebody else | `instance:user_create` | the installation | Only an administrator of accounts does this for another account. Never through the agent tools. |
| Mint a sign-in link for somebody else | `instance:user_create` | the installation | Only an administrator of accounts does this for another account. Never through the agent tools. |
| Sign another person out everywhere | `instance:user_create` | the installation | An administrator of accounts does this, or whoever the account answers to. Through the agent tools, never as an administrator. |
| Revoke another person's credential | `instance:user_create` | the installation | An administrator of accounts does this, or whoever the account answers to. Through the agent tools, never as an administrator. |
| Reset another person's calendar feed | - | - | Only its owner resets a feed. Never through the agent tools. |
| Revoke another person's calendar feed | `instance:user_create` | the installation | An administrator of accounts does this, or whoever the account answers to. Through the agent tools, never as an administrator. |
| Mark somebody as having left | `instance:user_create` | the installation | An administrator of accounts does this, or whoever the account answers to. Through the agent tools, never as an administrator. |
| Hand somebody's agent to another person | `instance:user_create` | the installation | Only an administrator of accounts does this for another account. Never through the agent tools. |
| Rename the installation | `instance:admin` | the installation | Never through the agent tools. |
| Take a backup of the installation | `instance:admin` | the installation | Never through the agent tools. |

## The roles every workspace is given

A member of a workspace holds one of these roles, and it grants the permissions marked. The
permissions over the installation itself - its accounts, its workspaces and its settings -
are held by an administrator of the installation and are never part of a role.

| Permission | Covers | Owner | Admin | Member | Contributor | Viewer |
| --- | --- | --- | --- | --- | --- | --- |
| `comment:read` | - | yes | yes | yes | yes | yes |
| `comment:write` | - | yes | yes | yes | yes | - |
| `link_type:write` | - | yes | yes | - | - | - |
| `project:delete` | - | yes | yes | - | - | - |
| `project:read` | - | yes | yes | yes | yes | yes |
| `project:write` | projects, and which documents bind the whole workspace | yes | yes | yes | - | - |
| `status:write` | - | yes | yes | - | - | - |
| `tag:write` | - | yes | yes | yes | - | - |
| `task:delete` | tasks and documents | yes | yes | - | - | - |
| `task:read` | tasks and documents | yes | yes | yes | yes | yes |
| `task:write` | tasks and documents | yes | yes | yes | yes | - |
| `user:admin` | who belongs to this workspace | yes | yes | - | - | - |
| `workspace:admin` | - | yes | yes | - | - | - |
| `workspace:delete` | - | yes | - | - | - | - |
| `workspace:read` | - | yes | yes | yes | yes | yes |
| `workspace:write` | - | yes | yes | - | - | - |
