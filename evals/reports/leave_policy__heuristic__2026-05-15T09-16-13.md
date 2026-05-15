# Eval Report: leave_policy

- **Attacker**: `heuristic`
- **Started**: 2026-05-15T09:16:13
- **Finished**: 2026-05-15T09:16:13

## Summary

| metric | value |
|---|---|
| seed cases | 3 |
| attack cases | 200 |
| total violations | 1 |
| unexpected exceptions | 1 |

## Violations (attack)

### `no_unexpected_exception` — 1 failure(s)

- **input**: `{'leave_type': 'personal', 'start_date': 'datetime.date(2025, 5, 15)', 'end_date': 'datetime.date(2026, 5, 14)', 'leave_hours': 0.5, 'today': 'datetime.date(9999, 12, 31)'}`
  - reason: OverflowError: date value out of range
  - exception: `OverflowError: date value out of range`
