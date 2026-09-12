# User Scripts templates

Scheduled jobs run from the Unraid **User Scripts** plugin, not inside the app.
Each folder here matches the plugin's layout: `script` plus `description`.

## run-ptc-nightly

Syncs new runs from Intervals.icu, then refreshes city data, then sends any
data-health notices the app has for you. The refresh checks each city layer's
signature (feature count, highest OID, latest edit date) and imports only a
layer the city has changed, followed by a routing graph rebuild and a change
report.

**Install**

1. User Scripts → **Add new script** → name it `run-ptc-nightly`.
2. Edit the script and paste in `run-ptc-nightly/script`. In the settings
   block at the top, set `APP_URL` (for example `http://kirk:8010`) so
   notices link to the status page, and adjust the container name or notify
   path if yours differ.
3. Set the schedule to **Custom** with `15 3 * * *` (3:15 AM server time).

**Updating**: the script lives in this repo; after a change to it, paste the
new version over the old one in User Scripts (keep your settings block).

**Notices** use Unraid's notify command, so they reach you through whatever
notification agents Unraid has (email, etc.). The app tracks each problem as
an episode, so a problem that lasts a week sends one alert, not seven:

| Outcome | Severity | How often |
|---|---|---|
| Intervals sync or city data starts failing | alert | once, until it recovers |
| A source has had no success in `stale_after_hours` (36 h) | alert | once, until it recovers |
| A source works again after an alert | normal | once |
| The city published changes (the change report's headline) | normal | each time |
| The container isn't running, or a job fails before the app can record it | alert | every night it happens |
| Nothing wrong, nothing new | none | |

A notice is marked sent only after notify succeeds, so one that fails to
send comes back the next night. The status page (`/status.html`) shows
current health, past episodes, and when the last alert went out.

**Test the alert path**: set `TEST_ALERT=1` in the settings block and use
**Run Script** once, then set it back to `0`. To test a real failure, point
`CONTAINER` at a stopped container and run it.

The same script runs against the dev pair for testing from a shell on kirk:

```sh
CONTAINER=run-ptc-dev APP_URL=http://kirk:8011 bash script
```
