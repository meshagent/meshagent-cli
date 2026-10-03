# [Meshagent](https://www.meshagent.com)

## MeshAgent CLI

The ``meshagent.cli`` package installs everything you need to streamline room and agent management from your terminal. The CLI assembles submodules for authentication, projects, API keys, participant tokens, messaging, storage, agents, and more.
Check out the [CLI Quickstart](https://docs.meshagent.com/cli/getting_started) for more details.

## User management

The Python and Rust CLIs support the same user commands:

```bash
meshagent user list --project-id "$PROJECT_ID" --filter alice --count 100 -o json
meshagent user get "$USER_ID" -o json
meshagent user update me --metadata '{"timezone":"America/Los_Angeles"}'
meshagent user update "$USER_ID" --project-id "$PROJECT_ID" --annotations '{"team":"support"}'
```

`list` follows pages up to `--count` and returns a continuation token in JSON output; pass it to `--continuation-token` to resume with the same project and filter. `get` and `update` default to your own profile (`me`). Updates preserve omitted fields. Supplied metadata and annotation objects replace their maps, and `{}` clears a map. Annotation values must be strings.

Editing another user or supplying annotations requires a project containing that user and the `user_profile_editor` role (inherited by project admins and owners), along with the required token scopes. See [IAM roles and permissions](https://docs.meshagent.com/project_admin/iam_roles_and_permissions#user-profile-permissions) for details.

---
### Learn more about MeshAgent on our website or check out the docs for additional examples!

**Website**: [www.meshagent.com](https://www.meshagent.com/)

**Documentation**: [docs.meshagent.com](https://docs.meshagent.com/)

---
