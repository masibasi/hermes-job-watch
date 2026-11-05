# hermes-job-watch

A [Hermes Agent](https://hermes-agent.nousresearch.com) skill that watches the public job boards of the
companies you pick and tells you only about **new** early-career-friendly software engineering roles.

It reads the keyless public APIs of Greenhouse, Lever and Ashby. Code does the deterministic work
(fetching, title and location filters, de-duplication, first-seen dates); the agent does the judgment
(reading a job description and deciding fit). Standard library only, Python 3.9+. Nothing is posted
anywhere and no account is needed.

## Install

```bash
hermes skills tap add masibasi/hermes-job-watch
hermes skills search job-watch
hermes skills install masibasi/hermes-job-watch/job-watch
```

Hermes scans the skill before installing it. Then ask your agent, for example:
"Use the job-watch skill: any new roles this week?"

Run it on a schedule and stay quiet when nothing is new:

```bash
hermes cron create "0 */2 * * *" "Use the job-watch skill: run check, assess any new roles, and report \
only roles worth applying to. If there is nothing new, reply with only [SILENT]." --skill job-watch
```

## What it does

| Command | Purpose |
|---|---|
| `check` | Fetch every watched board and report roles not seen before. The first successful check of a board records a baseline and reports only roles posted in the last 7 days, so you are not flooded with old postings |
| `recent` | Roles first seen in the last N days, without a network call |
| `details URL` | Official job description for a role (marked `untrusted_text`) |
| `boards`, `add-board`, `remove-board` | See and change the watched companies |

Default boards: Anthropic, Stripe, Roblox, Datadog, Cloudflare, MongoDB (Greenhouse), Palantir (Lever),
OpenAI and Snowflake (Ashby). Companies with their own career system (Google, Meta, Amazon, Apple, ...) have
no public API and cannot be watched this way.

Filters keep engineering titles about software, backend, full-stack, product, platform, developer tools,
applied AI, agents, integrations, APIs and forward-deployed work, and drop senior/staff/lead/manager/intern/PhD,
hardware, robotics, SRE and networking titles. U.S. locations (or plain "remote") by default. `--include`,
`--exclude` and `--location any` adjust this.

## Safety

Job-description text is written by the employer. The skill marks it untrusted and tells the agent to treat
it as data, never to follow instructions inside it. The script makes only HTTPS requests to the three
job-board APIs, verifies certificates, and writes only its own state files under `$JOB_WATCH_HOME`,
`$HERMES_HOME/job-watch` or `~/.hermes/job-watch`.

## Development

```bash
python3 -m pip install pytest
pytest -q
```

MIT licensed.
