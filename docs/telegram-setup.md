# Telegram setup

Use one dedicated family bot; it can deliver private reports to all enrolled profiles. Existing SMS gateway credentials and bot settings are not read or modified.

1. In Telegram, open the official `@BotFather`, run `/newbot`, and choose a name/username. Save its token directly in private `.env` as `TELEGRAM_BOT_TOKEN`; do not paste the token into this chat, GitHub or a shell command.
2. Run `call-secretary worker --offline-summaries` for initial enrollment. This uses Telegram but makes no OpenAI requests. The worker refuses a bot with an existing webhook and never removes it. Only one worker per state directory can poll updates.
3. For each approved profile, run `call-secretary invite --profile <profile-id> --bot-username <bot-username>`. The printed link is a private credential valid for one hour. Give it only to its intended family member.
4. That person opens their link in a private Telegram chat and presses Start. The token is consumed once; only its hash was stored. One private chat cannot occupy two profiles. Names alone cannot claim a profile.
5. Enable intended profiles in private YAML. Before live voice, fill the OpenAI/rate settings described in the [voice prototype guide](voice-prototype.md). Run `call-secretary worker` to generate translated summaries and deliver them; without an OpenAI key, reports explicitly contain the original caller text rather than a claimed translation.

Reports received before enrollment wait for the destination instead of being discarded. A recipient remains fixed in each queued report. `unbind --profile <id>` revokes the binding and blocks unsent old reports; reassignment does not redirect those reports to someone else's chat. Remove any explicit YAML chat ID before unbinding so a restart cannot silently restore it.

Revocation also covers assigned active calls and pending summaries that have not created an outbox entry yet. Durable per-call revocation markers prevent a late summary worker from making an old call deliverable after reassignment. New calls can use the new binding. If a private chat ID is supplied through YAML instead of an invitation, importing it releases matching waiting reports once the profile is enabled; restarting also synchronizes that binding's enablement with YAML.

Transient send errors use persistent backoff and Telegram retry-after. A crash after a successful send but before local acknowledgement can cause a duplicate with the same call ID. Blocked destinations retain their reports for operator review. `backup --destination <new-path>` creates a consistent SQLite copy; treat it as private, like the original state.

Automated tests use a fake Telegram service. Real bot enrollment/delivery has not been performed yet.
