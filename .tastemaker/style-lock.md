# Style lock — BCCNS metadata desk

Established: 2026-11-07. Source: generated palette, elegant mood. The desk is a cataloging tool for community history records, closer to a publishing desk than a developer console.

## Palette
- Background: #fdfaf1 (page)
- Surface: #f3f0e7 (intake panels)
- Primary: #836f3c (actions)
- Primary press: #6f5c2f (hover fill, white label 6.47:1)
- On primary: #ffffff (button label, 4.88:1 on primary)
- Secondary: #e5decb (disabled fill, skeleton bars)
- Accent: #60709d (focus ring, links on the page background)
- Border: #e3dfd7 (decorative hairline)
- Text: #1e1a11 (16.61:1 on background, 15.22:1 on surface, 12.92:1 on secondary)
- Danger: #8a3a32 (7.37:1 on background, 6.75:1 on surface, white label 7.69:1)
- Dark mode: not needed. One light mode.

## Color contract

Generated with `generate_palette.py --mood elegant --mode light`, complementary harmony, base hue 89.3.

- Text-safe (>=4.5): text/on-primary, text/bg, text/surface, text/border, text/secondary, accent/on-primary, primary/on-primary, on-primary/primary-press, danger/bg, danger/surface, on-primary/danger, bg/accent, bg/primary
- UI-safe (>=3.0 and <4.5): surface/accent, surface/primary, accent/border, primary/border, text/primary, text/accent
- Decorative (<3.0): border/on-primary, bg/border, surface/border, surface/on-primary, bg/surface, bg/on-primary, primary/accent

Links on a hovered table row switch to text, because accent on surface is only UI-safe.

## Typography
- Display: Gloock
- Body: Inter
- Scale: 16px body, 48px page title, 28px section titles

## Shape language
- Corner radius: 2px
- Shadow: none
- Borders: 1px hairline. The file drop uses a dashed accent border because that edge has to show the target.

## Density & spacing
- Base unit: 4px
- Content card internal padding: 24px
- Compact row padding: 10px 12px
- Overall density: dense enough to work in, with a quiet page margin
- Section separation: hairline divider

## Structure
App shell, one screen. No marketing macrostructure. Two intake panels and a ledger.

## Reference intelligence
- Reference board: `.tastemaker/reference-board.md` (viewed sources)
- Design read: cataloging desk for one operator, mode Operate, editorial paper lane
- Dials: variance 4, motion 3, density 6, art direction 5
- Direction: a page link and a spreadsheet are the whole job. The ledger shows the Dublin Core rows the way Omeka lists Title, Creator, Type, and Date.

## Assets
- Icons: Fluent 24 regular, ink `#1e1a11`, download-on-gold in white. Fetched from the Fluent set. The unDraw library was not used. This screen is a form and a table, not an illustrated page.
- Photography: none. The ledger is the visual.

## Motion
- Button press: transform 120ms ease-out, scale 0.98
- Result rows: opacity and 8px rise, 220ms, stagger 40ms, skipped when reduced motion is on
- Loading: the status sentence changes. The skeleton bars stay still so a long Gemini read does not pulse for minutes.
- Engine: GSAP for the row entrance only. No ScrollTrigger.

## Avoid
- Purple gradients, pill buttons, a sidebar with one destination, writing over the main collection CSV
