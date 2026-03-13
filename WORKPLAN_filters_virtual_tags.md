# Work Plan: Generic Filters & Virtual Tags Refactor

## Goal
Replace per-source ad-hoc filter parameters (mediaFilter, excludedTypes, disabledMedia) with a
generic `filters` array contract. Add virtual tag CRUD on the add-on side and wire virtual tag
selection through the HA integration's shuffle engine.

## Status Key: [ ] todo, [x] done, [~] in progress, [-] skipped

---

## Phase 1: Source modules (ha-frame-art-manager)

- [x] **google_arts.js** — `fetchRandomArtwork(filters, options)`, `getFilterTypes()`, export `DEFAULT_EXCLUDED_TYPES`
- [x] **met_museum.js** — `fetchRandomArtwork(filters, options)`, `getFilterTypes()`
- [x] **google_art_wallpaper.js** — `fetchRandomArtwork(_filters, options)`, `getFilterTypes()` → [], `getExtraOptions(settings)`

## Phase 2: web_sources.js route (ha-frame-art-manager)

- [x] Add `SOURCE_FILTER_TYPES` map at top
- [x] Config migration: `disabledMedia` / `excludedTypes` → filters array in `readWebSourcesConfig`
- [x] Default google_arts objectType exclusions for new configs
- [x] `virtualTags` field in config defaults
- [x] `GET /config` — include `filterTypes`
- [x] `GET /sources/:sourceId/filter-types`
- [x] `PUT /sources/:sourceId/filters`
- [x] Virtual tag CRUD routes (GET, POST, PUT, DELETE)
- [x] Remove `buildFetcherOptions` function
- [x] **fetch-and-display** — use `sourceFilters` + `extraOpts`, accept `virtualTagId`, merge virtual tag filters
- [x] **test-fetch** — same pattern with `virtualTagId` support

## Phase 3: HA integration (frame-art-shuffler)

- [x] **shuffle.py** — `WS_TAG_PREFIX`, `is_virtual_web_tag()`, `get_virtual_tag_id()`; sentinel carries `_virtual_tag_id`; multiple virtual web tags supported in image-weighted and tag-weighted modes
- [x] **shuffle.py** — `_async_fetch_and_display_web_source` accepts `virtual_tag_id`, passes as `virtualTagId` in API payload
- [-] **__init__.py** — No changes needed (fetch handler is in shuffle.py)

## Phase 4: Documentation

- [x] Update `docs/ADDING_WEB_SOURCES.md` — new source contract (filters, getFilterTypes, getExtraOptions)
- [x] Update memory: google_arts fetchRandomArtwork signature change
- [ ] Consider dedicated virtual tags feature doc (deferred — virtual tags not yet user-visible in UI)

## Design Notes

- Tags starting with `ws:` are specific virtual web source tags (e.g. `ws:paintings`).
  The part after `ws:` is the add-on's virtual tag ID.
- The existing `web_sources` umbrella tag continues to work (random enabled source, no virtual tag filtering).
- Both types return a sentinel from `_select_random_image`: `{"_web_sources": True, "_virtual_tag_id": "paintings"}`.
  `_virtual_tag_id` is `None` for the umbrella tag.
- On the add-on side, `fetch-and-display` looks up the virtual tag, uses its `sourceId`,
  and merges its filters on top of the source's stored filters.
- Virtual tags are `{ id, label, sourceId, queryMode, queryParams, filters }` in add-on config.