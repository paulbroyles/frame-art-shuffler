# Mood System — Dynamic Shuffle Composition

## Overview

Moods let Home Assistant conditions (time of day, weather, season, holidays) influence
which images are shuffled **without** creating a tagset for every possible combination.
Multiple moods can be active simultaneously and their effects compose automatically.

**Key properties:**
- **Backward compatible** — existing tagset behavior is fully preserved; moods are opt-in
- **Composable** — night + snow + holiday all active at once; their boosts combine
- **HA-native** — moods are driven by HA template sensors and automations
- **Gradual** — moods tilt probability toward matching images, not hard-switch
- **Web-source aware** — active moods compose search keywords and filter post-fetch results

---

## Mood Definition Fields

Moods are created and edited in the Frame Art Manager add-on UI (Advanced → Moods tab).
They are stored in `moods.json` on the add-on side and cached by the integration.

| Field | Type | Description |
|-------|------|-------------|
| `id` | string (slug) | Unique identifier, e.g. `night`, `christmas` |
| `label` | string | Human-readable display name |
| `boost_tags` | string[] | Library tags to weight more heavily |
| `suppress_tags` | string[] | Library tags to penalize or hard-exclude |
| `suppress_mode` | `"penalize"` \| `"exclude"` | How to handle suppress_tags |
| `search_terms` | string[] | Keywords for web source searches |
| `search_compose` | bool (default true) | If true, merge with other moods' terms; if false, independent search |
| `reject_terms` | string[] | Post-fetch metadata filter — retry if any term matches title/description/creator |
| `filters` | filter[] | Filter cascade objects injected into web source requests (same format as virtual tag filters) |
| `strength` | float (0.1–10, default 1.0) | Weight multiplier for this mood's influence |
| `exclusive` | bool (default false) | If true, replaces base pool entirely when active |

### Example Mood Definitions

```json
{
  "night": {
    "id": "night",
    "label": "Nighttime",
    "boost_tags": ["night", "nocturne", "stars", "moon"],
    "suppress_tags": ["sunny", "bright", "daytime"],
    "suppress_mode": "penalize",
    "search_terms": ["night", "nocturne", "moonlight"],
    "search_compose": true,
    "reject_terms": ["sunny", "daylight"],
    "filters": [],
    "strength": 1.0
  },
  "winter": {
    "id": "winter",
    "label": "Winter",
    "boost_tags": ["winter", "snow", "frost"],
    "suppress_tags": [],
    "suppress_mode": "penalize",
    "search_terms": ["winter", "snow"],
    "search_compose": true,
    "reject_terms": [],
    "filters": [],
    "strength": 1.0
  },
  "christmas": {
    "id": "christmas",
    "label": "Christmas",
    "boost_tags": ["christmas", "nativity", "holiday", "winter"],
    "suppress_tags": ["halloween", "easter"],
    "suppress_mode": "exclude",
    "search_terms": ["christmas", "nativity"],
    "search_compose": false,
    "reject_terms": ["halloween", "easter"],
    "filters": [],
    "strength": 3.0,
    "exclusive": true
  }
}
```

---

## Per-TV Mood Configuration

Two mechanisms activate moods for a TV:

1. **Mood sensor** (dynamic): An HA entity whose state or attributes provide active mood IDs
2. **Mood overrides** (service-call): Manually activated moods with optional expiry

Both sources are merged (union) at shuffle time.

### Supported Mood Sensor Formats

**Comma-separated state** (simplest):
```
sensor.art_moods → state: "night,winter"
```

**JSON list in attributes** (for complex logic):
```
sensor.art_moods → state: "2"  (count)
                 → attributes.moods: ["night", "winter"]
```

---

## Services

### `frame_art_shuffler.set_mood_sensor`

Bind a TV to a mood sensor entity. The sensor state is read at each shuffle.

```yaml
service: frame_art_shuffler.set_mood_sensor
target:
  entity_id: sensor.living_room_frame_recent_activity
data:
  sensor: sensor.art_moods   # HA entity ID; set to "" to unbind
```

### `frame_art_shuffler.activate_mood`

Manually activate mood(s) on a TV (service-call override).

```yaml
service: frame_art_shuffler.activate_mood
target:
  entity_id: sensor.living_room_frame_recent_activity
data:
  moods: ["christmas"]
  expiry: "2025-12-27T00:00:00"   # optional ISO timestamp
```

### `frame_art_shuffler.deactivate_mood`

Remove service-call mood overrides.

```yaml
service: frame_art_shuffler.deactivate_mood
target:
  entity_id: sensor.living_room_frame_recent_activity
data:
  moods: ["christmas"]   # omit to clear all overrides
```

---

## Shuffle Algorithm — How Moods Affect Selection

### Local Image Pool

**Normal mode (no exclusive mood):**
1. Base pool = tagset's eligible images (include/exclude tags as configured)
2. Expand pool: any boost_tags images not already in the base pool are added
3. Apply suppress_mode="exclude" tags → hard-remove matching images from pool
4. Score each remaining image (see Scoring below)
5. Weighted-random selection by score

**Exclusive mood:**
- Pool = all library images matching the exclusive mood's boost_tags
- Highest-strength exclusive mood wins when multiple are active
- Non-exclusive moods still boost/suppress within this pool
- `mood_baseline_floor` is forced to 0 (no base rotation guaranteed)

### Image Scoring

```
score = 1.0  (base pool)  or  0.5  (mood-expanded pool)

For each active mood:
  matching boosts → score *= (1 + mood.strength × matches × 0.5)
  matching penalized suppresses → score *= 0.2 ^ matches

final_score = log(1 + score)
```

Selection is weighted-random using `final_score`. The log compression provides
diminishing returns so one highly-matching mood doesn't dominate completely.

### Web Source Selection

**Mood search entries compete alongside virtual tags:**
- Active moods with `search_compose: true` → their `search_terms` are joined into one
  combined keyword query with weight = sum of participating moods' strengths
- Active moods with `search_compose: false` → each adds an independent search entry
  at weight = mood.strength
- These entries compete with the tagset's virtual tags in the weighted random draw

**Post-fetch filters:**
- `reject_terms`: after fetching an image, metadata (title, description, creator) is checked;
  if any reject term matches, the fetch is retried (up to 5 attempts, best-effort)
- `filters`: filter cascade objects (e.g., color filters) are injected into the source request;
  same format as virtual tag filters — hooks into existing source infrastructure

**Example (night + winter moods active, both search_compose: true):**
```
composed query = "night nocturne moonlight winter snow"
weight = 1.0 + 1.0 = 2.0
```
This entry competes alongside the tagset's virtual tags.

### Baseline Floor

`mood_baseline_floor` (0.0–1.0, default 0) reserves a fraction of shuffles for the
unmodified base tagset rotation, preventing moods from making base images vanishingly
unlikely. Set on the TV config; exclusive moods override this to 0.

---

## Template Sensor Examples

### Time of Day

```yaml
# configuration.yaml
template:
  - sensor:
      - name: "Art Moods"
        unique_id: art_moods_sensor
        state: >
          {% set hour = now().hour %}
          {% set moods = [] %}
          {% if hour >= 21 or hour < 6 %}
            {% set moods = moods + ['night'] %}
          {% elif hour >= 6 and hour < 9 %}
            {% set moods = moods + ['morning'] %}
          {% endif %}
          {{ moods | join(',') }}
```

### Weather + Season

```yaml
template:
  - sensor:
      - name: "Art Moods"
        unique_id: art_moods_sensor
        state: >
          {% set moods = [] %}
          {% set month = now().month %}

          {# Season #}
          {% if month in [12, 1, 2] %}
            {% set moods = moods + ['winter'] %}
          {% elif month in [3, 4, 5] %}
            {% set moods = moods + ['spring'] %}
          {% elif month in [6, 7, 8] %}
            {% set moods = moods + ['summer'] %}
          {% else %}
            {% set moods = moods + ['autumn'] %}
          {% endif %}

          {# Live weather #}
          {% set condition = states('weather.home') %}
          {% if condition in ['snowy', 'snowy-rainy'] %}
            {% set moods = moods + ['snow'] %}
          {% elif condition in ['rainy', 'pouring'] %}
            {% set moods = moods + ['rain'] %}
          {% endif %}

          {{ moods | join(',') }}
```

### Combined: Time + Weather + Season

```yaml
template:
  - sensor:
      - name: "Art Moods"
        unique_id: art_moods_sensor
        state: >
          {% set moods = [] %}
          {% set hour = now().hour %}
          {% set month = now().month %}

          {# Time of day #}
          {% if hour >= 21 or hour < 6 %}
            {% set moods = moods + ['night'] %}
          {% elif hour >= 6 and hour < 9 %}
            {% set moods = moods + ['morning'] %}
          {% endif %}

          {# Season #}
          {% if month in [12, 1, 2] %}
            {% set moods = moods + ['winter'] %}
          {% elif month in [3, 4, 5] %}
            {% set moods = moods + ['spring'] %}
          {% elif month in [6, 7, 8] %}
            {% set moods = moods + ['summer'] %}
          {% else %}
            {% set moods = moods + ['autumn'] %}
          {% endif %}

          {# Live weather #}
          {% set condition = states('weather.home') %}
          {% if condition in ['snowy', 'snowy-rainy'] %}
            {% set moods = moods + ['snow'] %}
          {% elif condition in ['rainy', 'pouring'] %}
            {% set moods = moods + ['rain'] %}
          {% endif %}

          {{ moods | join(',') }}
```

---

## Automation Examples

### Seasonal Holiday Tagset Switch

Use `set_tagset` (see Tagsets docs) combined with `activate_mood` for holiday events:

```yaml
automation:
  - alias: "Christmas Mode On"
    trigger:
      - platform: template
        value_template: "{{ now().month == 12 and now().day >= 1 }}"
    action:
      - service: frame_art_shuffler.activate_mood
        target:
          entity_id: sensor.living_room_frame_recent_activity
        data:
          moods: ["christmas"]
          expiry: "{{ (now().replace(month=12, day=26, hour=0, minute=0, second=0)).isoformat() }}"

  - alias: "Christmas Mode Off"
    trigger:
      - platform: template
        value_template: "{{ now().month == 12 and now().day >= 26 }}"
    action:
      - service: frame_art_shuffler.deactivate_mood
        target:
          entity_id: sensor.living_room_frame_recent_activity
        data:
          moods: ["christmas"]
```

### Bind Mood Sensor on Startup

```yaml
automation:
  - alias: "Set Art Mood Sensor"
    trigger:
      - platform: homeassistant
        event: start
    action:
      - service: frame_art_shuffler.set_mood_sensor
        target:
          entity_id: sensor.living_room_frame_recent_activity
        data:
          sensor: sensor.art_moods
```

---

## Mood with No Web Sources

A mood can have empty `search_terms`, `reject_terms`, and `filters`. In that case it
only affects local image scoring (boost/suppress probabilities). The tagset's virtual
tags continue at their normal weight, unmodified. This is the simplest way to start:
create a mood with just `boost_tags` and `suppress_tags`, bind the sensor, and see
local images shift toward the boosted content.

---

## Where Things Live

| Component | Location |
|-----------|----------|
| Mood definitions | Manager add-on `moods.json` (CRUD via UI and `GET/POST/PUT/DELETE /api/moods`) |
| Mood cache | HA integration `mood_cache.py` (same pattern as `TagsetCache`) |
| Mood sensor binding | HA config entry per-TV (`mood_sensor` entity ID) |
| Mood overrides | HA config entry per-TV (`mood_overrides` + expiry) |
| Mood services | HA integration `__init__.py` (`activate_mood`, `deactivate_mood`, `set_mood_sensor`) |
| Local scoring | Manager `routes/shuffle.js` (`scoreMoodImage`, `isMoodHardSuppressed`) |
| Web search composition | Manager `routes/shuffle.js` (`buildMoodSearchEntries`, `selectWebEntry`) |
| Post-fetch filtering | Manager `routes/web_sources.js` (in retry loop, `moodRejectTerms`) |
| Filter injection | Manager `routes/web_sources.js` (`moodFilters` → `mergeFilterCascade`) |

---

## Tuning Constants

These are defined in `routes/shuffle.js` and can be adjusted empirically:

| Constant | Default | Effect |
|----------|---------|--------|
| `BOOST_FACTOR` | `0.5` | Per matching boost_tag score multiplier per unit of strength |
| `SUPPRESS_PENALTY` | `0.2` | Per matching suppress_tag score multiplier (penalize mode) |

The log compression `log(1 + score)` provides diminishing returns — a mood with 10
matching tags won't completely dominate a mood with 1 matching tag.

---

## Design Notes

**Why not mood-specific tagsets?** Combinatorial explosion: n conditions × m seasons ×
p times of day requires n×m×p tagsets. Moods compose automatically.

**Why HA sensors for activation?** HA already has template sensors, weather integrations,
time helpers, and a rich automation system. Building a parallel rules engine in the add-on
would duplicate this infrastructure poorly.

**Why probability tilt instead of hard switching?** Hard switching (show ONLY night images)
feels jarring and can result in repetitive content if the pool is small. Tilting
probabilities keeps variety while shifting the aesthetic.

**Color matching (future):** The `filters` field on moods passes filter cascade objects to
web sources. For sources that support color filtering (e.g., Google Arts `color` API param),
this is already wired. Local library color scoring (Delta-E in CIELAB space) requires
dominant color metadata per image — not yet implemented.
