---
name: Bug report
about: Something behaves incorrectly
title: ''
labels: bug
assignees: ''
---

## What happened

<!-- What you observed. Error text, wrong output, silent wrongness. -->

## What you expected

## How to reproduce

```bash
# the exact command, with any non-default configuration
uv run betfeed validate
uv run betfeed run odds
```

## Environment

**Key type:** <!-- Delay or Live — they behave differently and most confusion is here -->

```
OS:
Python / uv version:
betfeed version: <!-- git rev-parse --short HEAD -->
```

`betfeed validate` output:

```
```

Relevant log lines:

```
```

## Credentials

**Do not include your app key, username, password, session token, or
`DATABASE_URL`.** See [SECURITY.md](../../SECURITY.md).
