# Generating per-addon screenshots with `nexterp-gen-screenshots`

This article explains how to capture the Configuration / How-it-works
screenshots that appear in each NextERP module's Odoo Apps presentation
page. The capture is automated via Playwright, but the tool is **a
manual command, not a pre-commit hook** — runs take several minutes and
require a live Odoo instance, so they happen on demand.

> **TL;DR**
> 1. Start the Odoo dev stack for the target version.
> 2. Create a database with the addon(s) installed (with demo data).
> 3. Author or update `<addon>/readme/screenshots.yaml`.
> 4. `nexterp-gen-screenshots --addons-dir=. --odoo-url=… --db=…`.
> 5. Commit the PNGs in `<addon>/static/description/`.

---

## 1. Prerequisites

### Odoo running

Each Odoo version uses its own dev stack under
`nexterp_dev/docker/<version>/<flavor>/docker-compose.yml`. For the
NextERP odooapps repo on Odoo 19:

```bash
cd nexterp_dev/docker/19.0/nexterp
docker compose up -d
```

Wait until:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:1990/web/login
```

returns `200`. The first cold start can take **2-5 minutes** because
the entrypoint installs Python deps inside the container.

Default credentials baked into this compose file:

| Service | Port (host) | Login | Password |
|---|---|---|---|
| Odoo  | 1990 | `admin` | `admin` |
| pgAdmin | 1991 | `admin@example.com` | `admin` |
| Postgres | 1994 | `odoo` | `odoo` |
| MailHog UI | 1992 | — | — |

### Database with the addon installed

Easiest path is to bootstrap the DB from the command line *inside*
the container (much faster than the UI):

```bash
docker exec -it odoo_nexterp \
    /opt/odoo/odoo/odoo-bin \
    -c /etc/odoo/odoo.conf \
    -d sgr_screenshots \
    -i nexterp_sgr \
    --without-demo=False \
    --stop-after-init
```

What this does:

- `-d sgr_screenshots` — creates a fresh DB named `sgr_screenshots`.
- `-i nexterp_sgr` — installs the addon (`base` plus all dependencies
  come automatically).
- `--without-demo=False` — keeps demo data. **Required** for many
  scenarios (categories, beverage products, warehouses).
- `--stop-after-init` — exit when install finishes, so subsequent
  `docker compose up` runs serve normally.

If the YAML references multiple addons (rare; usually one per scenario
file), pass `-i addon_a,addon_b`.

### Playwright Chromium

The tool runs Chromium headless by default. Install it once on the
host:

```bash
playwright install chromium
```

(`playwright` is shipped via the maintainer-tools deps; first use
asks for the browser binary.)

---

## 2. Authoring `readme/screenshots.yaml`

Every addon that wants captures ships a YAML at
`<addon>/readme/screenshots.yaml`. The tool only acts on files that
declare at least one scenario; addons without a YAML are skipped.

### Schema

```yaml
setup:                              # optional, applies to whole file
  viewport: {width: 1440, height: 900}
  pause_ms_default: 500             # debounce after each step (ms)

scenarios:
  - name: <short_slug>              # logical identifier
    description: <human readable>   # not used at runtime, helpful in PRs
    steps:
      - <step1>
      - <step2>
      - …
```

Outputs land in `<addon>/static/description/<filename>` — the same
folder used by the OCA README pipeline, so screenshots are picked up by
both `README.rst` and the branded `index.html` without extra plumbing.

### Available step actions

Steps accept either a **shorthand** (single-key mapping with a string
value) or a **full mapping** (when extra keys like `selector`,
`timeout_ms`, `full_page` are needed).

| Action | Shorthand example | Full form | Purpose |
|---|---|---|---|
| `goto` | `goto: /odoo/settings` | `{action: goto, value: …, timeout_ms: 30000}` | Navigate. Paths starting with `/` are joined to `--odoo-url`. |
| `wait` | `wait: .o_form_view` | `{action: wait, value: <selector>, timeout_ms: …}` | Wait until a selector exists. |
| `wait_url` | `wait_url: "**/odoo/**"` | same | Wait for a URL pattern (glob). |
| `click` | `click: text=Save` | `{action: click, value: …, timeout_ms: …}` | Click. |
| `fill` | — (always full) | `{action: fill, selector: input[name=login], value: admin}` | Type into a field (replaces existing value). |
| `press` | `press: Enter` | `{action: press, value: <key>}` | Send a keyboard key. |
| `scroll` | `scroll: .o_section_3` | `{action: scroll, value: 500}` | Scroll into view *or* by N pixels (int). |
| `sleep` | `sleep: 800` | `{action: sleep, value: <ms>}` | Hard wait. Use sparingly — prefer `wait`. |
| `screenshot` | `screenshot: foo.png` | `{action: screenshot, filename: foo.png, selector: …, full_page: true}` | Save a PNG. With `selector`, only that element is captured. |

### Selectors

The Playwright runtime accepts:

- CSS: `.o_form_view`, `tr.o_data_row:first-child`
- Text: `text=Save`, `text=SGR Categories`
- `:has-text(...)`: `.o_settings_block:has-text("SGR")`
- Comma-separated alternatives: `.o_list_view, .o_kanban_view`
  (matches whichever appears first)

**Prefer text/`:has-text` over deeply chained CSS** — Odoo's class
names change between minor versions, but menu labels rarely do.

### Worked example

The pilot lives at
[`nexterp_sgr/readme/screenshots.yaml`](../template/module/readme/screenshots.yaml.example)
(see also the actual file in `odooapps/nexterp_sgr/readme/`). A short
extract:

```yaml
setup:
  pause_ms_default: 500
  viewport: {width: 1440, height: 900}

scenarios:
  - name: settings
    description: SGR section in Inventory settings
    steps:
      - goto: /odoo/settings#cashapp=inventory
      - wait: input[type="search"]
      - fill: {selector: input[type="search"], value: SGR}
      - sleep: 800
      - screenshot:
          selector: .o_settings_block:has-text("SGR")
          filename: sgr_settings.png
```

---

## 3. Running the tool

Typical invocation against the local Odoo 19 stack:

```bash
cd <repo-root-with-addons>      # e.g. nexterp_dev/19.0/nexterp/odooapps

nexterp-gen-screenshots \
    --addons-dir=. \
    --odoo-url=http://localhost:1990 \
    --db=sgr_screenshots \
    --user=admin \
    --password=admin
```

Restrict to a single addon while iterating:

```bash
nexterp-gen-screenshots \
    --addon-dir=./nexterp_sgr \
    --odoo-url=http://localhost:1990 --db=sgr_screenshots
```

Watch the browser drive itself (very useful while authoring selectors):

```bash
nexterp-gen-screenshots --headed --addons-dir=. …
```

The CLI prints a one-line summary per scenario at the end:

```
▶ nexterp_sgr
  ✓ settings: ok
  ✗ categories: FAIL: TimeoutError: locator.click: Timeout 15000ms exceeded.
```

A failure does **not** abort the batch — the rest of the scenarios in
that addon, and all subsequent addons, still run.

---

## 4. Iteration workflow

Selectors are the brittle part. A typical authoring loop:

1. Open the page in your browser at the relevant URL.
2. Use DevTools → **Inspect** to find a stable label or class.
3. Add the step to the YAML.
4. Run with `--headed` and a single `--addon-dir` for fast feedback.
5. When all scenarios are green, switch back to headless and commit.

If a scenario keeps timing out:

- Add a `wait:` step right before the failing action. Odoo loads
  many views lazily; without an anchor the next selector races the
  fetch.
- Replace deep CSS with `text=` or `:has-text(...)` — they're more
  resilient to upstream UI tweaks.
- Try `--headed` with a `page.pause()` workaround: add a long
  `sleep: 600000` step temporarily, then open the inspector tab.

---

## 5. Committing

Once the PNGs land in `static/description/`, run the full pre-commit
pass (the branded `index.html` regenerates and the OCA README pulls in
the new images):

```bash
pre-commit run -a
```

Then commit the PNGs together with any updated `README.rst` /
`index.html`. Keep the YAML in the same commit so a future contributor
can regenerate identical captures.

---

## 6. Anti-patterns to avoid

- **No DB bootstrapping inside the tool.** Creating databases, loading
  demo data and installing modules belong in odoo-bin / scripts, not in
  a screenshotter. Keep the screenshotter dumb on purpose.
- **No `pause_ms_default` > 1500ms.** If you need that much idle, you
  probably need a real `wait:` selector — long pauses make the batch
  unbearable.
- **Don't capture full-page screenshots by default.** They tend to
  include the Odoo navbar / right sidebar; an element-scoped capture
  (`selector:` plus `filename:`) reads much better on the Odoo Apps
  page.
- **Don't commit DB-specific fixtures into the YAML.** The scenarios
  should work against the addon's standard demo data; if a scenario
  needs richer records, add them to the addon's `demo/` and point the
  YAML at them.
