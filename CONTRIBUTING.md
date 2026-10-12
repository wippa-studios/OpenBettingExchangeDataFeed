# Contributing

Thanks for considering it. This package is pre-1.0 and small, so the bar is
mostly "does it survive contact with the real Betfair API".

## Getting set up

```bash
uv sync
uv run pytest -q
```

Tests run **offline** — no Betfair credentials, no PostgreSQL. If you find
yourself needing a live key to write a unit test, the test is probably asserting
the wrong thing.

To work against real data, follow the README's quickstart and
`uv run betfeed validate`.

## The rule that matters most

**A test that passes only because of an assumption about real Betfair is worse
than no test.** Several of the bugs fixed here were invisible until they met the
live API:

| Bug | Why no test caught it |
|---|---|
| `selection` keyed on `selection_id` alone | fakes used unique ids per market; Betfair reuses them across markets |
| JSONB written without `Jsonb()` | fakes passed strings, not dicts |
| `publish_time` assumed present | fakes set it; a Delay key withholds it |
| Infinite-size lay offer | no synthetic book had an infinite size |
| Partition bounds as bind parameters | no migration ran against a real server |

So when you add a fixture, make it match what Betfair *actually* returns —
including the awkward parts. A fake that is tidier than reality will hide the
next bug.

## Pull requests

- One concern per PR.
- Say what you observed and how you observed it. "Betfair returned X on a Delay
  key" is worth more than a hypothesis.
- Run `uv run pytest -q` before pushing.
- Update the README if you changed a default, a command, or a limit.

## Commit messages

Explain **why**, and what the symptom was. The existing history is the
reference:

```
fix(odds): make the odds poll work, and report data delay honestly

`run odds` had never completed. It failed three ways against real Betfair data.
```

Not "fix odds".

## Changing a default

Defaults are a public contract. Before changing one:

1. Say what breaks if the old value returns.
2. If it is a Betfair limit, say how you measured it, and pin it in
   `betfeed validate` so the new default cannot be silently broken later.

## Reporting bugs

Open an issue with:

- what you ran, and the output
- `betfeed validate` output
- the app key type (**Delay** or **Live**) — they behave differently and most
  confusion is here
- anything from the postgres log

**Never include credentials.** See [SECURITY.md](SECURITY.md).
