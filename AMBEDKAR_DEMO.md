# Ambedkar Demo — Content Addendum

The platform is a generic museum tour system. This file describes the **content
instance** currently loaded: the life and work of **Dr. B.R. Ambedkar** (1891–1956).

Everything here lives in `data/exhibits/seed_exhibits.json`. Swapping in a
different subject means replacing that file and re-running the ingest — no code
changes.

---

## The museum

| | |
|---|---|
| Display name | Dr. B.R. Ambedkar Memorial Museum |
| `museum_id` | `demo-library` |
| Qdrant collection | `demo-library_knowledge` |

**The `museum_id` is deliberately still `demo-library`.** It is embedded in every
MQTT topic (`museum/demo-library/device/+/…`) and in `device_config.yaml` on each
Pi. Renaming it would mean re-provisioning every device for no functional gain,
so only the display name describes the content.

---

## Galleries

The floor plan (`dashboard/app.py`, `FLOOR_PLAN`) has four galleries, named by
exhibit type:

| Gallery | Holds |
|---|---|
| A — Writings | Books and journalism |
| B — Documents & Drafts | Constitutional and legal papers |
| C — Monuments | Physical sites |
| D — Memorials & Legacy | Memorial sites and recognition |

---

## The five beacon exhibits

These are the only exhibits with a physical beacon. **The UUIDs are unchanged
from the original books demo**, so no beacon needs reprogramming.

| Beacon UUID | Exhibit | Year | Gallery |
|---|---|---|---|
| `550e8400-e29b-41d4-a716-446655440001` | Annihilation of Caste | 1936 | A, Bay 1 |
| `550e8400-e29b-41d4-a716-446655440002` | The Buddha and His Dhamma | 1957 | A, Bay 2 |
| `550e8400-e29b-41d4-a716-446655440003` | The Constitution of India | 1949 | B, Bay 2 |
| `550e8400-e29b-41d4-a716-446655440004` | Deekshabhoomi, Nagpur | 1956 | C, Bay 1 |
| `550e8400-e29b-41d4-a716-446655440005` | Chaitya Bhoomi, Mumbai | 1956 | D, Bay 2 |

## The eight archive entries

These have `beacon_uuid: null` and `location: null`. They are stored in Postgres
and embedded into Qdrant, so a visitor can **ask about them by voice from
anywhere**, but they never trigger a narration and never appear on the map.

`amb-a01` The Problem of the Rupee (1923) · `amb-a02` Mooknayak and Bahishkrit
Bharat (1920/1927) · `amb-a03` The Mahad Satyagraha (1927) · `amb-a04` The Poona
Pact (1932) · `amb-a05` Columbia, the LSE and Gray's Inn (1923) · `amb-a06`
Rajgruha and the fifty thousand book library · `amb-a07` The Hindu Code Bill and
his resignation (1951) · `amb-a08` The Bharat Ratna and the Panchteerth (1990)

---

## Exhibit schema

```json
{
  "id": "amb-001",
  "beacon_uuid": "550e8400-e29b-41d4-a716-446655440001",
  "title": "Annihilation of Caste",
  "author": "Dr. B.R. Ambedkar",
  "year": 1936,
  "genre": "Writings — Social and Political Thought",
  "location": "Gallery A, Bay 1",
  "tts_script": "Spoken aloud the moment the beacon fires. 2-4 sentences.",
  "full_content": "Chunked and embedded for RAG. The only thing Q&A can answer from."
}
```

Field notes:

- **`beacon_uuid`** — `null` for an archive entry. No beacon row is written.
- **`author`** — read as *attribution*. An author for a book, an architect for a
  monument, a body for a document. Rendered as `[Title — Attribution]`.
- **`year`** — must be a plain integer or `null`; the column is `INTEGER`.
  A range like `"1891–1956"` will fail the insert.
- **`location`** — `"Gallery X, Bay N"`, X in A–D, N in 1–3. This is the only
  field with a format contract: `dashboard/app.py` parses it to place the marker.
  The older `"Section X, Shelf N"` wording still parses. `null` keeps the exhibit
  off the map. Anything unrecognised lands at a scatter position on the open floor.
- **`tts_script`** — aim for 320–390 characters. It is read aloud; longer scripts
  make the visitor wait.
- **`full_content`** — 1300–1700 characters for beacon exhibits, 900–1200 for
  archive entries, written as labelled prose (`Overview: … Historical context: …
  Key facts: … Significance: … Notable details: …`). This is the **only** text the
  Q&A can draw on; anything not written here gets the "I don't have that detail"
  reply.

---

## Loading the content

```bash
python scripts/ingest_exhibits.py                    # all 13, with prune
python scripts/ingest_exhibits.py --exhibit-id amb-001   # one, no prune
```

**The prune step matters.** A full run deletes any exhibit in this museum that is
no longer in the seed file, from both Postgres and Qdrant. Without it, renaming an
exhibit id orphans the old row and its vectors — and because `qa_service` falls
back to a whole-collection search when an exhibit-scoped search returns nothing,
those orphans keep surfacing in answers. That is exactly how the old book content
would leak into an Ambedkar tour.

Pruning is skipped when `--exhibit-id` names a single exhibit, since it would
otherwise delete everything else.

If your Postgres volume predates this content, the museum display name will still
say "Demo Book Library" — migrations only auto-run on a first container start:

```bash
docker exec -it museum-postgres psql -U museum -d museum_tour \
  -c "UPDATE museums SET name = 'Dr. B.R. Ambedkar Memorial Museum' WHERE id = 'demo-library';"
```

---

## Adding an exhibit

1. Add an entry to `data/exhibits/seed_exhibits.json`.
2. For a beacon-backed exhibit, assign a new UUID and set a `location`; for a
   searchable-only entry, leave both `null`.
3. Run `python scripts/ingest_exhibits.py`.
4. Program a physical beacon with the UUID and add it to `KNOWN_UUIDS` in
   `device/beacon_bridge.py` — that set is an allowlist, and unlisted beacons are
   silently dropped.

No service restart is needed. `scripts/simulate_device.py` and
`scripts/benchmark.py` read the seed file directly, so their menus update on their own.

---

## Sources

The exhibit text was written from: Wikipedia (B. R. Ambedkar, Annihilation of
Caste, Deekshabhoomi, Chaitya Bhoomi, Mahad Satyagraha), the Dr. Ambedkar
International Centre's Panchteerth pages (daic.gov.in), the Supreme Court of
India's centenary page on his enrolment as an advocate, and the LSE History blog.
Dates, figures and quotations should not be edited without re-checking them —
several widely circulated figures (the number of converts at Nagpur, the size of
his personal library) vary between sources, and the values used here are the ones
those pages state.
