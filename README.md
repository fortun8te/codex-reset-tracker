# codex reset tracker

Pings your Telegram the moment OpenAI announces a **usage-limit reset** for Codex
or ChatGPT — "we have reset usage limits for Codex across all paid plans", "enjoy a
nice reset everyone", "limits are back".

Two independent sources:

- **[@codexreset](https://t.me/codexreset)** — a public Telegram channel that detects
  resets off OpenAI's own API and posts bilingual CN/EN alerts. Usually first.
- **X/Twitter** — [@thsottiaux](https://x.com/thsottiaux) (Tibo; resets are announced
  on his feed and almost nowhere else), plus [@sama](https://x.com/sama),
  [@btibor91](https://x.com/btibor91) and [@OpenAI](https://x.com/OpenAI) as backup.

Resets have historically landed roughly every 8 days.

The hard part isn't the plumbing, it's that the announcements are oblique — a
milestone celebration or an apology for an incident, often without the words
"usage limit" anywhere in the post. That's why a model reads every post instead
of a keyword filter.

Runs on GitHub Actions cron. No X API. No server. No subscription.
Model spend is roughly **$0.05/month**.

```
GitHub Actions cron (*/5 min)
        │
        ├─ 1. fetch  → Nitter RSS mirrors + t.me/s/codexreset (no auth, no API key)
        ├─ 2. diff   → state.json ring buffer of post ids already seen
        ├─ 3. judge  → OpenRouter, one batched call, strict yes/no per post
        └─ 4. alert  → Telegram sendMessage
```

## Why it's built this way

| Problem | Answer |
| --- | --- |
| X's free API tier can't read timelines ($200/mo for Basic) | Nitter RSS mirrors — public, keyless. Pool of 4, tried in random order, dead ones skipped. |
| A scraper that silently returns nothing looks identical to "no news" | If **every** mirror fails, the run aborts *without* touching state, so nothing is falsely marked seen. After ~1h of consecutive failures the bot tells you it's blind, and tells you again when it recovers. |
| Keyword matching misses paraphrases ("everyone gets a fresh start today") | Every new post goes to the model, not just keyword hits. `openai/gpt-5-nano` at ~$0.0002 per batched call — around 10 calls/day, so pennies per month. A keyword prefilter is still in `config.json` if you ever want to cut that to zero. |
| Free/cheap models get rate-limited upstream | Four models in a fallback chain (`gpt-5-nano` → three `:free` models). If all four fail it falls back to strict regex and still alerts, labelled as such. |
| Cron job dies quietly and you never notice | Dead-man's switch: one `💤 tracker alive` line every 24h. No heartbeat = it's broken. |
| Private repos only get 2000 Actions minutes/month | Repo is public, so Actions minutes are unlimited. Every secret is a GitHub Secret; nothing sensitive is committed. |

## Setup

```bash
gh secret set TELEGRAM_BOT_TOKEN --body "<token from @BotFather>"
gh secret set TELEGRAM_CHAT_ID   --body "<your numeric chat id>"
gh secret set OPENROUTER_API_KEY --body "<sk-or-v1-...>"
gh workflow run track.yml
```

Send `/start` to the bot once from your own Telegram account first — until you do,
the API answers `chat not found` and nothing can be delivered.

## Local use

```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... OPENROUTER_API_KEY=...

python3 tracker.py --selftest          # classifier check against fixture posts
python3 tracker.py --test-alert        # prove the Telegram wiring works
python3 tracker.py --dry-run           # full run, prints alerts instead of sending
python3 tracker.py --recheck 20        # re-judge the 20 newest posts per handle
```

`TRACKER_INSECURE_SSL=1` skips cert verification — only for local machines behind a
TLS-intercepting proxy, never in CI.

## Tuning

Everything lives in [`config.json`](config.json):

- `handles` — who to watch. A bare name is an X account; `"tg/<name>"` is a public
  Telegram channel. Both go through the same judge.
- `topic` / `topic_description` — what counts as a hit. This *is* the prompt the
  model sees; rewrite it and the tracker tracks something else entirely.
- `models` — OpenRouter fallback chain, tried in order.
- `prefilter_enabled` — `false` by default (judge everything). Flip to `true` to
  only spend tokens on posts containing a `prefilter` keyword.
- `heartbeat_hours` — `0` silences the alive-pings.
- `max_alerts_per_run` — flood guard.

## Files

- `tracker.py` — everything, stdlib only, no `pip install`
- `config.json` — what to watch and what counts
- `state.json` — post ids already seen, committed back by the workflow
- `.github/workflows/track.yml` — the cron
