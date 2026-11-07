# Studio pipeline map

The admin Studio now offers **Map** beside Board and List. It is a read-only
projection of the same rendered board rows: no new endpoint, schema, cache,
provider call, stage write or payment calculation.

- **Client quiet** uses the existing `days_quiet` projection. Outbound sends do
  not refresh it. When no client action exists, the existing projection falls
  back to stage/creation time; point descriptions disclose that fallback.
- **Time in stage** uses the existing `days_in_stage` value.
- **Value** is the existing Board value, not revenue received. The common
  linear dollar domain spans the complete current board snapshot, so equal
  values align across stages and changing age windows does not change X.
- Nonpositive/missing displayed values use a separate gutter; missing/future
  ages and projects older than the selected window appear in an expandable
  outside-view list. Window boundaries are inclusive.
- Nearby points are grouped into keyboard-operable project lists. All points
  have names containing their exact displayed age/value and link to existing
  project details. The map does not support drag-to-stage.
- Archived records are absent from the source rows and therefore not plotted.
  Closed projects remain visible as context. Counts describe plotted records,
  not the board's historical Archived count.

The map re-reads the board after HTMX stage swaps and preserves the selected
view. It uses the current Screening Room tokens with the existing legacy
theme fallback. Map controls require JavaScript; Board remains the default.

Verification: `node --test tests/js/studio-map.test.cjs` exercises scale,
membership, missing data and deterministic clusters. `tests/test_studio_map.py`
also verifies that the view is admin-only and reading it changes no project
status. Normal repository gates still apply before release.
