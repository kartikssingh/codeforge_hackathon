# Protocol library

The offline knowledge base. Every PDF here is embedded into the vector index at
`backend/vector_store/` and cited by name in triage output, so a volunteer can
check the source of any instruction they are given.

## What is here

| Prefix | Meaning | Count |
|---|---|---|
| `MED-` | Medical conditions and injuries | 12 |
| `QR-` | Quick-reference cards (CPR, tourniquet, splint, recovery position…) | 6 |
| `SIT-` | Situational responses (collapse, earthquake, violence, contamination) | 4 |
| `OPS-` | Volunteer operations, welfare, handover, shift change | 4 |

## Adding documents

1. Drop the PDF (or `.txt` / `.md`) in this directory.
2. Rebuild the index:
   ```bash
   curl -X POST 'http://127.0.0.1:8000/admin/reload?rebuild_index=true'
   ```

The index is fingerprinted by file name, size and modification time, so a
normal restart reuses it and only a genuine change triggers a rebuild. Deleting
`backend/vector_store/` also forces one.

## Citing a document from a triage rule

`backend/data/triage_rules.json` references files here by exact name:

```json
"protocols": ["QR-02_CPR_AED.pdf", "MED-04_cardiac_stroke.pdf"]
```

A rule whose cited protocol also turns up in retrieval gets a small confidence
bonus — the two engines agreeing is evidence. Keep the names in the rules in
step with the file names here.

## If this directory is empty

Retrieval reports itself unavailable and every report is flagged vague, but
triage still runs: the deterministic rule engine carries it, and requests are
marked `degraded` with a note saying why.
