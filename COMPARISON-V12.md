# V12 — Frozen comparison table headings
All three comparison scopes share the `.rp8-scroll` component. The table now has a bounded vertical and horizontal scroll area, sticky financial-year headers and a frozen first column. The top-left cell stays above both sticky layers. Existing report data and exports are unchanged. The stylesheet URL and service-worker cache version are bumped.
Scroll inside the table to keep headings visible; scrolling outside the table moves the entire section normally. No database migration is required.
