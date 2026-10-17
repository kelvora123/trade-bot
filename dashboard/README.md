# Published dashboard snapshot

Put a `dashboard/index.html` here to publish your real dashboard through GitHub Pages:

```bash
python -m simplebot dashboard --static dashboard/index.html   # where the bot runs
git add dashboard/index.html && git commit -m "dashboard snapshot" && git push
```

The `pages` workflow deploys it on every push to `main`. Without this file it publishes a demo
built from synthetic data. The snapshot shows the journal as it was when you ran the command;
the live, auto-refreshing page is `python -m simplebot dashboard`.
