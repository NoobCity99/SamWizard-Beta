# SamWizard Academy Customization Guide

This guide explains how the Academy tree works and how to customize the lesson layout, bridge connections, order, icons, and lesson content.

## Main Files

- `app/academy/trees/ubuntu_cli_basics.json`
  - The lesson plan and unlock graph.
  - This is the first file to edit when changing lessons, order, titles, summaries, commands, or unlocks.

- `app/templates/academy.html`
  - The browser-side renderer for the Academy page.
  - Contains the fixed honeycomb slot positions, SVG bridge drawing logic, built-in icon SVGs, and click behavior.

- `app/static/styles.css`
  - The visual styling for the Academy page.
  - Controls hex colors, locked/unlocked/completed states, bridge colors, spacing, banner, logo, and responsive behavior.

- `app/academy/tree.py`
  - Server-side tree validation.
  - Contains the allowed fixed slot names and checks for duplicate IDs, missing unlock targets, invalid lesson data, and invalid layout data.

## How The Academy Tree Works

The Academy page loads its data from the backend with:

```text
GET /academy/api/state
```

The backend loads `app/academy/trees/ubuntu_cli_basics.json`, reads the shared progress file, removes stale progress IDs that no longer exist in the tree, and returns the current skill state to the browser.

Each skill has three separate jobs:

- Content: `title`, `summary`, `lesson`
- Placement: `layout.slot`
- Progress flow: `starts_unlocked`, `unlocks`

The browser renders the tree as fixed-position HTML hex buttons on top of an SVG connector layer. The map uses a static 12-slot honeycomb layout. It is not currently auto-arranged.

## Current Static Slots

The active layout uses these fixed slot names:

| Slot | Current Lesson ID | Current Title |
|---|---|---|
| `top-left` | `ubuntu-server` | What Ubuntu Server Is |
| `top-center` | `terminal-basics` | What the Terminal Is |
| `top-right` | `server-identity` | Server Identity |
| `upper-left` | `linux-filesystem` | Files And Folders |
| `upper-right` | `networking-basics` | Networking Basics |
| `middle-left` | `navigation-basics` | Navigation Basics |
| `middle-right` | `reading-output` | Reading Output |
| `far-left` | `drives-and-mounts` | Drives And Mounts |
| `lower-left` | `permissions-basics` | Permissions |
| `lower-center` | `services-basics` | Services |
| `bottom-center` | `samba-basics` | How Samba Works |
| `far-right` | `tailscale-basics` | How Tailscale Works |

The slot coordinates are defined in `app/templates/academy.html` in the `slotPositions` constant.

## How Lesson Order Is Determined

Lesson order is controlled by the unlock graph, not by the order of objects in the JSON file.

The first available lessons are the skills with:

```json
"starts_unlocked": true
```

Currently the top three starter lessons are:

- `ubuntu-server`
- `terminal-basics`
- `server-users`

The `unlocks` list creates prerequisite relationships. A child lesson becomes available when all parent lessons that point to it are complete.

Example:

```json
{
  "id": "terminal-basics",
  "unlocks": ["navigation-basics", "reading-output"]
}
```

If `navigation-basics` and `reading-output` have no other parent lessons, completing `terminal-basics` unlocks both lessons.

If another lesson also points to `reading-output`, then `reading-output` waits until both parent lessons are complete:

```json
{
  "id": "terminal-basics",
  "unlocks": ["reading-output"]
}
```

```json
{
  "id": "server-users",
  "unlocks": ["reading-output"]
}
```

In that example, `reading-output` unlocks only after both `terminal-basics` and `server-users` are complete.

To change the lesson plan order:

1. Decide which lesson should come first, second, and third.
2. Set only those starter lessons to `"starts_unlocked": true`.
3. Update each skill's `unlocks` list to match the desired flow.
4. Put each skill in a sensible `layout.slot`.
5. Run the tests or at least validate the JSON before shipping.

For the current static layout, keep the tree at 12 lessons unless you also update the template, CSS, validation, and tests.

## Current Unlock Graph

The current flow is:

```text
ubuntu-server -> linux-filesystem
terminal-basics -> firewall-basics
server-users -> samba-basics
linux-filesystem -> drives-and-mounts
firewall-basics -> updates-maint
samba-basics -> tailscale-basics
drives-and-mounts -> samba-mounting
```

`reading-output`, `samba-mounting`, `updates-maint`, `script-basics`, and `tailscale-basics` currently unlock nothing.

## How SVG Bridge Connections Work

There is no separate bridge list.

The visual bridge lines are generated from the same `unlocks` lists that control progression. If a skill unlocks another skill, the page draws a bridge between those two hexes.

In `app/templates/academy.html`, `renderConnectors()` loops through every skill and every child in `skill.unlocks`. For each pair, it finds the two slot positions and draws an SVG `<line>`.

The bridge line is computed by `bridgeLine(fromPosition, toPosition)`:

1. Find the center of the source hex.
2. Find the center of the target hex.
3. Calculate the direction from source to target.
4. Trim both ends inward using `academy.bridgeInset`.
5. Draw a short SVG line between the trimmed points.

This keeps bridges from running through the center of each hex.

## Editing Bridge Connections

To add a bridge and unlock relationship, edit the source lesson's `unlocks` list:

```json
"unlocks": ["next-skill-id"]
```

To remove a bridge and unlock relationship, remove that child ID from the source lesson's `unlocks` list:

```json
"unlocks": []
```

To make a skill unlock multiple lessons:

```json
"unlocks": ["first-child-id", "second-child-id"]
```

Every ID in `unlocks` must match an existing skill ID. The server validation will reject missing targets.

## Editing Bridge Appearance

Bridge styling lives in `app/static/styles.css`.

Look for:

```css
.academy-connector
.academy-connector-open
```

Use these rules to change:

- Line color
- Line thickness with `stroke-width`
- Rounded ends with `stroke-linecap`
- Glow or shadow with `filter`

The default connector is used for locked or incomplete paths. The open connector is used when the parent skill is completed and the child is unlocked.

To change bridge length, edit this value in `app/templates/academy.html`:

```js
bridgeInset: 62
```

Higher values make bridge segments shorter. Lower values make bridge segments longer.

## Editing Hex Node Colors

Hex node colors are controlled by a global Academy node palette in `app/static/styles.css`.

Look near the top of the file inside the `:root` block for variables that start with:

```css
--academy-node-
```

The hex rules later in the file use those variables. You should usually edit the palette variables, not the individual `.academy-node-*` rules.

Important base variables:

```css
--academy-node-bg
--academy-node-border
--academy-node-text
--academy-node-inner-ring
--academy-node-glow
--academy-node-hover-bg
--academy-node-hover-border
--academy-node-hover-text
--academy-node-hover-glow
```

These control the default node state and the generic hover state.

## Hex State Color Variables

Unlocked nodes:

```css
--academy-node-unlocked-bg
--academy-node-unlocked-border
--academy-node-unlocked-text
--academy-node-unlocked-inner-ring
--academy-node-unlocked-glow
```

Unlocked hover:

```css
--academy-node-unlocked-hover-bg
--academy-node-unlocked-hover-border
--academy-node-unlocked-hover-text
--academy-node-unlocked-hover-glow
```

Completed nodes:

```css
--academy-node-completed-bg
--academy-node-completed-border
--academy-node-completed-text
--academy-node-completed-inner-ring
--academy-node-completed-glow
```

Completed hover:

```css
--academy-node-completed-hover-bg
--academy-node-completed-hover-border
--academy-node-completed-hover-text
--academy-node-completed-hover-glow
```

Locked nodes:

```css
--academy-node-locked-bg
--academy-node-locked-border
--academy-node-locked-text
--academy-node-locked-filter
```

Locked hover:

```css
--academy-node-locked-hover-bg
--academy-node-locked-hover-border
--academy-node-locked-hover-text
--academy-node-locked-hover-glow
--academy-node-locked-hover-filter
```

Selected nodes:

```css
--academy-node-selected-border
--academy-node-selected-text
--academy-node-selected-inner-ring
--academy-node-selected-glow
--academy-node-selected-hover-border
--academy-node-selected-hover-text
--academy-node-selected-hover-glow
```

## Hex Color Editing Notes

Node backgrounds can contain multiple background layers. This is valid:

```css
--academy-node-unlocked-hover-bg: linear-gradient(180deg, rgba(55, 168, 255, 0.18), transparent), rgba(8, 31, 56, 0.96);
```

If you want a simple flat color, use one color value instead:

```css
--academy-node-unlocked-hover-bg: #123a5c;
```

Glow variables are used in `box-shadow`, so they should usually be transparent `rgba(...)` values:

```css
--academy-node-unlocked-hover-glow: rgba(55, 168, 255, 0.48);
```

The locked state also uses a CSS filter. To make locked tiles less dim, raise the saturation:

```css
--academy-node-locked-filter: saturate(0.8);
```

To remove locked dimming completely:

```css
--academy-node-locked-filter: none;
```

If changing colors does not appear to work, check that you edited the `:root` variables and not an old hardcoded value. The active rules are:

```css
.academy-node
.academy-node:hover
.academy-node-unlocked
.academy-node-unlocked:hover
.academy-node-completed
.academy-node-completed:hover
.academy-node-locked
.academy-node-locked:hover
.academy-node-selected
.academy-node-selected:hover
```

## Editing Hex Positions

Hex positions are controlled in `app/templates/academy.html`:

```js
const slotPositions = {
  'top-left': { x: 250, y: 40 },
  'top-center': { x: 407, y: 130 },
  ...
};
```

The `x` and `y` values are the upper-left position of each hex button inside the map canvas.

To move a lesson:

1. Change the lesson's `layout.slot` in `ubuntu_cli_basics.json`.
2. Or change the coordinates for that slot in `slotPositions`.

If you add a new slot name, also update:

- `slotPositions` in `app/templates/academy.html`
- `ALLOWED_LAYOUT_SLOTS` in `app/academy/tree.py`
- Any tests that assert the allowed slots or skill count

Avoid assigning two skills to the same slot.

## Editing Lesson Content

Each lesson is defined as one skill object in `app/academy/trees/ubuntu_cli_basics.json`.

Basic shape:

```json
{
  "id": "terminal-basics",
  "title": "What the Terminal Is",
  "icon": { "type": "builtin", "name": "terminal" },
  "summary": "A short locked or unlocked tile description.",
  "layout": { "slot": "top-center", "group": "basics" },
  "starts_unlocked": true,
  "unlocks": ["navigation-basics", "reading-output"],
  "lesson": {
    "intro": [
      "Short beginner-friendly paragraph.",
      "Another short paragraph if needed."
    ],
    "commands": [
      {
        "command": "pwd",
        "origin": "print working directory",
        "purpose": "Shows where you are.",
        "outcome": "Prints the current folder path."
      }
    ],
    "exercise": "Run the command on your server.",
    "paste_prompt": "Paste what you saw here if you want to compare it."
  }
}
```

## Lesson Field Notes

- `id`
  - Stable machine name for the lesson.
  - Use lowercase letters, numbers, and hyphens.
  - Avoid changing this after users have progress unless you are okay with that progress being reset for this lesson.

- `title`
  - Human-readable tile and lesson title.

- `summary`
  - Short description shown on the tile and locked preview.

- `icon`
  - Controls the icon shown on the hex tile.

- `layout.slot`
  - The fixed honeycomb slot for this lesson.

- `layout.group`
  - Optional label for future grouping. It does not currently drive behavior.

- `starts_unlocked`
  - Whether the lesson is available when progress is new or reset.

- `unlocks`
  - List of direct child lesson IDs.
  - Controls both progression and bridge drawing.
  - If a child has multiple parent lessons pointing to it, all parent lessons must be complete before the child unlocks.

- `lesson.intro`
  - Array of paragraphs.
  - Keep each paragraph short and beginner-friendly.
  - Use real line breaks inside a paragraph if you need line-by-line formatting.

- `lesson.commands`
  - Optional list of command explanations.
  - These are instructional only. The Academy does not execute commands.
  - Use real line breaks in `outcome` when showing CLI-style output.

- `lesson.exercise`
  - The hands-on task for the learner.

- `lesson.paste_prompt`
  - Text shown above the optional paste box.
  - The paste box is UI-only in this version.

## Multiline Command Output

Use JSON newline escapes for command output that should appear on multiple lines. In the standalone editor, press Enter inside the `Outcome` box. When exported, those line breaks appear in JSON as `\n`.

Example:

```json
"outcome": "Documents\nDownloads\nSamwizard_Data"
```

The Academy page renders command outcomes with preserved whitespace, so the learner sees:

```text
Documents
Downloads
Samwizard_Data
```

Do not use `<br>` in lesson JSON. Lesson text is rendered safely as text, not HTML, so `<br>` will appear literally instead of creating a line break. This is intentional so lesson content cannot inject markup into the page.

For aligned CLI tables, keep the spacing in the raw string:

```json
"outcome": "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda2        50G   14G   34G  29% /"
```

## Multiline Intro Paragraphs

Intro paragraphs also preserve JSON newline escapes. In the standalone editor, press Enter inside an intro paragraph. In raw JSON, newline characters are written as `\n`.

Do not type literal `\n` into the standalone editor unless you want the learner to see the two characters `\` and `n`. The editor creates real line breaks when you press Enter, then exports those breaks as JSON `\n`.

Example:

```json
"intro": [
  "Step 1: Open your terminal.\nStep 2: Run the command.\nStep 3: Read the output."
]
```

This renders as multiple lines inside one lesson paragraph. Separate array entries still render as separate paragraphs.

Do not use `<br>` in intro text. It will display literally because lesson content is rendered as safe text, not HTML.

## Writing Good Intro Lessons

The Academy is meant to be an intro to Linux and CLI concepts, not a full masterclass.

Recommended lesson style:

- Teach one concept per lesson.
- Keep intros to 1 to 3 short paragraphs.
- Prefer safe read-only commands early.
- Explain what the command name means when useful.
- Explain what success looks like.
- Do not require users to paste secrets, tokens, private keys, or passwords.
- Avoid destructive commands unless the lesson is specifically about safety and cleanup.

Good early commands:

```text
pwd
ls
ls -la
cd
whoami
hostname
ip addr
df -h
mount
systemctl status
```

Be careful with commands that modify files, permissions, services, network configuration, disks, or Samba/Tailscale settings.

## Icon Options

Built-in icon names are defined in `app/templates/academy.html` in the `builtinIcons` object.

Current built-in names include:

```text
server
terminal
identity
folder
compass
document
network
drive
lock
gear
share
link
graduate
spark
```

Use a built-in icon like this:

```json
"icon": { "type": "builtin", "name": "terminal" }
```

To use a custom image asset, put the file under:

```text
app/static/assets/academy/icons/
```

Then reference it with a browser path:

```json
"icon": {
  "type": "asset",
  "src": "/static/assets/academy/icons/my-icon.png"
}
```

Use square transparent PNG or SVG icons for best results.

## Banner And Logo

The top banner area is part of `app/templates/academy.html` and styled in `app/static/styles.css`.

The intended custom banner path is:

```text
app/static/assets/academy/academy_banner.png
```

The page also uses the SamWizard logo in two places:

- The Academy page header
- The center of the honeycomb map

Both currently reference:

```text
app/static/assets/samwizard_logo.png
```

## Map Panel Size And Lesson Image Slot

The map card has a fixed height so it does not stretch downward to match a long Lesson Plan card.

The size value lives near the top of `app/static/styles.css` inside the `:root` block:

```css
--academy-map-panel-height: 760px;
```

Increase this value if you want a taller map card. Decrease it if you want more room below the map for future content.

The actual map canvas is still controlled in `app/templates/academy.html` by the JavaScript values:

```js
mapWidth: 900,
mapHeight: 600
```

The CSS variable controls the full visible map card height. The JavaScript values control the internal map canvas size used for hex placement and SVG bridge coordinates. If the internal map canvas is taller than the available card space, the map area scrolls internally.

Below the map card is a separate 16:9 lesson image slot. It changes with the selected lesson.

Lesson image files live in:

```text
app/static/assets/academy/
```

Use this exact naming pattern:

```text
Lesson_Image1.png
Lesson_Image2.png
Lesson_Image3.png
...
Lesson_Image12.png
```

The image number follows the lesson order in `app/academy/trees/ubuntu_cli_basics.json`. The first skill in the JSON uses `Lesson_Image1.png`, the second skill uses `Lesson_Image2.png`, and so on. If the selected lesson image file is missing, the Academy page shows a 16:9 placeholder with the expected filename.

The image aspect ratio value also lives in `app/static/styles.css` inside `:root`:

```css
--academy-lesson-image-aspect: 16 / 9;
```

To change the image slot shape later, edit that variable. To replace lesson artwork, add or replace the matching `Lesson_ImageN.png` file in `app/static/assets/academy/`.

## Progress And Reset Behavior

Academy progress is stored outside the lesson JSON.

The progress file is:

```text
${SAMWIZARD_STATE_DIR}/academy-progress.json
```

The default production state directory is expected to be:

```text
/var/lib/samwizard
```

When you change lesson IDs or remove lessons, the backend cleans old progress IDs that no longer exist in the current tree. If you are testing lesson order changes, use the page's reset button or delete the progress file in your local state directory.

Resetting through the API requires:

```json
{ "confirm": "RESET" }
```

## Server-Saved Note Pad

The Academy page includes a global `NOTE PAD` below the lesson image. It is saved on the SamWizard server, not in the browser, and it is shared by anyone using that local SamWizard install.

The notepad file is:

```text
${SAMWIZARD_STATE_DIR}/academy-notepad.json
```

The notepad is not per lesson. Selecting a different lesson changes the lesson image and lesson card, but the `NOTE PAD` text stays the same. Resetting Academy progress only resets `academy-progress.json`; it does not clear `academy-notepad.json`.

The note area is square by default. To change its shape, edit this value in `app/static/styles.css`:

```css
--academy-notepad-aspect: 1 / 1;
```

## Validation Checklist

Before calling a lesson plan finished, check:

- The JSON is valid.
- Every skill has a unique `id`.
- Every `unlocks` target exists.
- The current static map has exactly 12 skills.
- No two skills use the same `layout.slot`.
- The top three starter lessons have `"starts_unlocked": true`.
- Non-starter lessons usually have `"starts_unlocked": false`.
- Lesson commands are instructional and safe.
- The tree still makes sense after a full progress reset.

## Useful Commands

Validate the JSON file:

```bash
python -m json.tool app/academy/trees/ubuntu_cli_basics.json
```

Run the full test suite:

```bash
python -m unittest discover -s tests
```

If your shell needs the virtual environment first:

```bash
source .venv/bin/activate
python -m unittest discover -s tests
```

## Common Customization Recipes

Rename a lesson:

1. Edit `title`.
2. Edit `summary`.
3. Leave `id` unchanged unless you intentionally want progress for that lesson to reset.

Move a lesson to another hex:

1. Change `layout.slot`.
2. Confirm no other lesson uses that slot.
3. Refresh `/academy`.

Change what a lesson unlocks:

1. Edit its `unlocks` list.
2. Confirm every child ID exists.
3. Remember that a child with multiple parents requires all parents to be complete.
4. Reset progress if you want to test the flow from the beginning.

Changing the graph may relock uncompleted lessons after refresh because unlocked lessons are recalculated from completed prerequisites. Completed lessons stay completed.

Add or replace a command:

1. Edit `lesson.commands`.
2. Include `command`, `origin`, `purpose`, and `outcome`.
3. Keep commands safe and explain what the learner should expect to see.

Replace the banner:

1. Add a 1920x631 image at `app/static/assets/academy/academy_banner.png`.
2. Refresh the Academy page.

Replace lesson images:

1. Add 16:9 images named `Lesson_Image1.png` through `Lesson_Image12.png` under `app/static/assets/academy/`.
2. Keep the numbering aligned with the lesson order in `app/academy/trees/ubuntu_cli_basics.json`.
3. Refresh the Academy page.

Resize the map card:

1. Edit `--academy-map-panel-height` in `app/static/styles.css`.
2. Refresh `/academy`.

Change connector color or thickness:

1. Edit `.academy-connector` and `.academy-connector-open` in `app/static/styles.css`.
2. Refresh `/academy`.

Shorten or lengthen bridge segments:

1. Edit `bridgeInset` in `app/templates/academy.html`.
2. Higher means shorter bridges.
3. Lower means longer bridges.
