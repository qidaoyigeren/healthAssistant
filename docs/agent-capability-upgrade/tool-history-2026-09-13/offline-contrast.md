# Offline A/B contrast (client seam, synthetic double, no provider)

| task | preset failure | arm | requests | final msgs | final chars | terminal | report | originals in context |
|---|---|---|---:|---:|---:|---|---|---|
| invalid-filter | True | A | 6 | 2 | 24069 | checks_completed | bounded_report | none |
| invalid-filter | True | B | 6 | 12 | 23709 | checks_completed | bounded_report | none |
| no-match-rewrite | True | A | 8 | 2 | 23599 | waiting_review | bounded_report | c1 |
| no-match-rewrite | True | B | 8 | 16 | 25710 | waiting_review | bounded_report | c1 |
| conflict-natural | False | A | 8 | 2 | 23266 | checks_completed | bounded_report | c1,c2 |
| conflict-natural | False | B | 8 | 16 | 25378 | checks_completed | bounded_report | c1,c2 |

per task, A and B agree on terminal state AND originals in context: True