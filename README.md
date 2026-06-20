# LeetCode Daily Discord Report

A simple GitHub Actions workflow that runs once daily and posts a LeetCode solve summary to Discord.

## What it does

- Reads `users.json` for a mapping of display names to LeetCode usernames
- Queries LeetCode's public GraphQL API for each username
- Counts accepted solves for the current day
- Sends a daily report to a Discord webhook

## Setup

1. Create a new GitHub repository and push this project.
2. Edit `users.json` with your Discord display names and corresponding LeetCode usernames.
3. Create a Discord webhook for the channel you want the report in.
4. In your GitHub repo, go to Settings → Secrets → Actions and add a secret:
   - `DISCORD_WEBHOOK_URL`
   - value: your webhook URL
5. The GitHub Actions workflow is configured to run every day at 11:00 PM IST.

## Change the timezone or schedule

If you want a different timezone or schedule, update `.github/workflows/daily.yml` and/or set `REPORT_TIMEZONE` in the workflow env block.

## Test it now

- Open the Actions tab in GitHub
- Select `Daily LeetCode Report`
- Click `Run workflow`

## Notes

- No database is required.
- All usernames are loaded from `users.json`.
- The workflow uses a webhook, so there is no persistent bot required.
