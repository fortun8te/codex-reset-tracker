# codex reset tracker

Pings your Telegram the moment [@btibor91](https://x.com/btibor91) or
[@sama](https://x.com/sama) posts about OpenAI **resetting / refreshing / raising
usage limits** — "we're going to do a reset", "limits are back", "everyone gets
extra usage", that kind of thing.

Runs on GitHub Actions cron. No X API. No server. No paid model. Costs $0.

```
GitHub Actions cron (*/5 min)
        │
        ├─ 1. fetch  → Nitter RSS mirrors (rotating pool, no auth, no API key)
        ├─ 2. diff   → state.json ring buffer of post ids already seen
        ├─ 3. filter → keyword prefilter (kills ~95% of posts for free)
        ├─ 4. judge  → OpenRouter FREE model, one batched call, strict yes/no
        └─ 5. alert  → Telegram sendMessage
```

## Why it's built this way

| Problem | Answer |
| --- | --- |
| X API free tier can't read timelines ($200/mo for Basic) | Nitter RSS mirrors — public, keyless. Pool of 4, tries them in random order, skips dead ones. If **all** mirrors fail the run aborts *without* touching state, so nothing gets silently marked "seen". |
| Free OpenRouter models are capped at ~50 requests/day | The keyword prefilter runs first, and all survivors go in **one batched call**. Real volume: ~2 candidate posts per 48h across both accounts. Nowhere near the cap. |
| Free models get rate-limited upstream | 4 models in a fallback chain. If every one fails, it falls back to strict regex matching and still alerts (marked as such). |
| Cron job dies quietly and you never notice | Dead-man's switch: one `💤 tracker alive` line every 24h. No heartbeat = it's broken. |
| Private repos only get 2000 Actions minutes/month | Repo is public. Every secret lives in GitHub Secrets, nothing sensitive is committed. Public repos get unlimited Actions minutes. |

## Setup

```bash
gh secret set TELEGRAM_BOT_TOKEN --body "<token from @BotFather>"
gh secret set TELEGRAM_CHAT_ID   --body "<your numeric chat id>"
gh secret set OPENROUTER_API_KEY --body "<sk-or-v1-...>"
gh workflow run track.yml
```

You must send `/start` to the bot once from your own Telegram account, otherwise
the API answers `chat not found` and nothing can be delivered.

## Local use

```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... OPENROUTER_API_KEY=...

python3 tracker.py --selftest          # classifier sanity check on fixture posts
python3 tracker.py --test-alert        # prove the Telegram wiring works
python3 tracker.py --dry-run           # full run, prints alerts instead of sending
python3 tracker.py --recheck 20        # re-judge the 20 newest posts per handle
```

`TRACKER_INSECURE_SSL=1` skips cert verification — only for local machines with a
TLS-intercepting proxy, never in CI.

## Tuning

Everything lives in [`config.json`](config.json):

- `handles` — who to watch. Add `"OpenAI"`, `"embirico"`, anyone.
- `topic` / `topic_description` — what counts as a hit. This is the actual prompt
  the model sees; rewrite it and the tracker tracks something else entirely.
- `prefilter` — cheap keyword gate. Widen it if you think you're missing posts;
  the model is the thing that decides, this just controls cost.
- `models` — OpenRouter fallback chain, tried in order.
- `heartbeat_hours` — set to `0` to silence the alive-pings.
- `max_alerts_per_run` — flood guard.

## Files

- `tracker.py` — everything, stdlib only, no `pip install`
- `config.json` — what to watch and what counts
- `state.json` — post ids already seen, committed back by the workflow
- `.github/workflows/track.yml` — the cron
