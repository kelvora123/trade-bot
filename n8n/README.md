# n8n setup

Two workflows, both of which end by pushing results into this repository.
Pick the one that matches where your n8n runs.

## Credentials (both workflows)

1. **GitHub token** — a fine-grained personal access token with
   `Contents: Read and write` on `kelvora123/trade-bot`, and `Actions: Read and
   write` if you use the dispatch workflow.
   In n8n: **Credentials → New → GitHub API**, paste the token, name it
   anything. Both workflows reference it via `nodeCredentialType: githubApi`,
   so n8n will prompt you to pick it on import.

2. **Workflow variables** — **Settings → Variables**:

   | Variable | Example | Used by |
   |---|---|---|
   | `GH_OWNER` | `kelvora123` | both |
   | `GH_REPO` | `trade-bot` | both |
   | `GH_BRANCH` | `main` | both |
   | `REPO_DIR` | `/data/trade-bot` | `daily-run-and-push` only |

   On n8n Community Edition, Variables are unavailable — open the two Code
   nodes and replace `$vars.GH_OWNER` etc. with string literals.

3. **`ANTHROPIC_API_KEY`** — only if you enable the news layer. Set it in the
   environment of the machine running the bot (self-hosted), or as a GitHub
   Actions repository secret (dispatch workflow). Never put it in a variable
   that gets committed.

---

## A. `daily-run-and-push.json` — self-hosted n8n

Requires the repo checked out on the same host as n8n, with Python available
to the n8n process.

```
Schedule (22:00 weekdays)
  → Pull latest code        git pull --ff-only
  → Run the bot             python3 -m tradebot.cli run --json
  → Run succeeded?          branches on exitCode
      ├─ true  → Collect artefacts    reports/ + snapshots/ → base64
      │        → Get existing SHA     404 = new file, not an error
      │        → Merge SHA            pairs the lookup back to its payload
      │        → Commit to GitHub     PUT /contents/{path}
      └─ false → Log failure          surfaces stderr in the execution log
```

Setup:

```bash
sudo git clone https://github.com/kelvora123/trade-bot.git /data/trade-bot
cd /data/trade-bot && sudo python3 -m pip install -e .
sudo -u node python3 -m tradebot.cli run --no-news   # prove it runs as n8n's user
```

Then **Workflows → Import from File**, select the JSON, pick your GitHub
credential where prompted, and **Execute Workflow** once manually before
activating.

Notes:
- The SHA lookup node is set to `onError: continueRegularOutput` — a 404 means
  the file is new, which is a normal path, not a failure.
- The commit node batches one request per 600 ms to stay clear of GitHub's
  secondary rate limits on bursts of writes.
- `git pull --ff-only` fails loudly rather than creating a merge commit if the
  local checkout has diverged.

## B. `dispatch-github-actions.json` — n8n Cloud

No local checkout. n8n triggers GitHub Actions, which does the work and the
committing.

```
Schedule (22:00 weekdays)
  → Dispatch workflow    POST /actions/workflows/daily-run.yml/dispatches
  → Wait 4 minutes
  → Fetch latest report  GET /contents/reports/latest.json (raw)
  → Summarise            equity, P&L, theta, delta, flags, expiries, macro
```

Setup:
1. Add `ANTHROPIC_API_KEY` as a repository secret (only if using the news
   layer).
2. Import the workflow, attach the GitHub credential, set the variables.
3. Run once manually and check the Actions tab.

Append a Slack / email / Telegram node after **Summarise** to get the digest
delivered; its output is already flat and ready to template.

The wait is fixed rather than polling to keep the node count down. If your run
takes longer than four minutes, either raise it or replace the Wait node with a
polling loop on `GET /actions/runs?status=in_progress`.

---

## Verifying

```bash
python scripts/validate_n8n.py
```

Checks both files import cleanly, have a trigger, have no duplicate node names,
and — the failure that actually bites — no connection pointing at a node that
does not exist. That one imports fine and then does nothing at run time.
