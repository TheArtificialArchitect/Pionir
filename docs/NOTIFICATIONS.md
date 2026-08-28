# Notification ownership

Pionir does not absorb or rewrite specialist notification processes during integration.
Existing Discord webhooks, offsets, digest schedules, watchdogs, and local state remain owned
by their source agent.

| Agent | Existing owner | Pionir policy |
|---|---|---|
| Bryo | `scripts/discord-notify.ps1` on the `build` branch | Preserve unchanged; do not send the same Bryo lifecycle events |
| Other specialists | Their current local supervisors | Preserve until each notifier is audited |

Rules:

1. Webhook URLs are never copied into Pionir or committed to Git.
2. Pionir records the source agent and event identity before adding any future notification.
3. A specialist-owned event has exactly one notification owner by default.
4. Centralization requires an explicit migration with idempotency keys, dry-run comparison,
   and rollback; installing Pionir is not such a migration.
5. Pionir adapters never stop or replace specialist watchdogs.

Bryo's current notifier is especially isolated: it tails `state/bryo.log`, persists its own
byte offset in `state/discord_notify.json`, obtains its webhook from
`BRYO_DISCORD_WEBHOOK` or a gitignored state file, and catches send/parse failures so Discord
cannot take the organism down.
