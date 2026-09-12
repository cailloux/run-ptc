# User Scripts templates

Scheduled jobs run from the Unraid **User Scripts** plugin, not inside the app.
Each folder here matches the plugin's layout: `script` plus `description`.

## run-ptc-nightly

Syncs new runs from Intervals.icu, then refreshes city data. The refresh
checks each city layer's signature (feature count, highest OID, latest edit
date) and imports only a layer the city has changed, followed by a routing
graph rebuild and a change report.

**Install**

1. User Scripts → **Add new script** → name it `run-ptc-nightly`.
2. Edit the script and paste in `run-ptc-nightly/script`. Adjust the settings
   block at the top if your container name or notify path differ.
3. Set the schedule to **Custom** with `15 3 * * *` (3:15 AM server time).

**Alerts** use Unraid's notify command, so they reach you through whatever
notification agents Unraid has (email, etc.):

| Outcome | Severity |
|---|---|
| A job fails (including the container not running) | alert |
| A job is skipped because another job was running | warning |
| The city published changes (the change report's headline) | normal |
| Nothing changed | none |

**Test the alert path**: set `TEST_ALERT=1` in the settings block and use
**Run Script** once, then set it back to `0`. To test a real failure, point
`CONTAINER` at a stopped container and run it.

The same script runs against the dev pair for testing from a shell on kirk:

```sh
CONTAINER=run-ptc-dev bash script
```
