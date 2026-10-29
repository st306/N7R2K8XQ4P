# Property Brain

STP's property triage dashboard. One file: `index.html`. Live copy is served by GitHub Pages from this repo.

- Data lives in the STP Deals [LIVE] Google Sheet, read and written through an Apps Script web app. The link and key are kept in this browser only, never in this code. Airtable is retired (legacy, read-only).
- The map library is in `vendor/leaflet/`, so it loads from this site, not a third party.

v3.13.0 (11 Oct 2026): moved here from Drive v3.12.1; map library bundled; postcode lookup failures now shown on the map screen.

v3.13.2 (18 Oct 2026): phone copy brought up to the Drive build (red-flag fixes, Yes board flow, flag button writes to the Property Sprint Register), phone fit kept.

v3.15.0 (26 Oct 2026): reads and writes the STP Deals [LIVE] Google Sheet through an Apps Script web app once this browser is connected (one-tap #connect link from the Property chat). The link and key live in this browser only, never in this code. A browser that is not connected keeps the v3.14.0 Airtable behaviour. Rollback: commit 7d22d2d.
