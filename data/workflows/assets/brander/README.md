# Brander assets

Wallpaper backgrounds for the **Brander (Self-Serve)** template.

- `lockscreen-template.png` — default background when no role matches.
- `null.png` — background when the device has no role set.
- `brandlogo.png` — reserved for a logo overlay (not composited by default).
- `<role>.png` — one background per role, matched case-insensitively against
  the Jamf Setup extension attribute value with spaces removed
  (`Video Conferencing` → `videoconferencing.png`). The six shipped role images
  are examples; replace them with your own at the same size.

Text is rendered with Pillow's built-in font unless the template's
"Font file path" setting points at a `.ttf`/`.otf` you supply. No font ships
here.

## Templates (optional)

Instead of the layout above, Brander can render a **wallpaper template**: a
JSON file designed in EWOK and rendered by the same code (`wallrender`,
vendored and hash-pinned in `data/workflows/lib`), so the preview is what the
device receives.

- Set the template's "Wallpaper template" setting to a JSON path, absolute or
  relative to this directory. `legacy` (the default) keeps the original
  layout. `example-template.json` is a starting point sized for an iPhone.
- Image layers name an asset id; Brander loads `<id>.png`, `.jpg` or `.jpeg`
  from this directory and nowhere else.
- A template can print only these fields of the UDID-verified Jamf record:
  `{{device_name}}`, `{{serial_number}}`, `{{asset_tag}}`, `{{jss_id}}` and
  `{{location.building}}`. Anything else renders empty. Nothing from the
  request that triggered Brander is ever printed.
- Rendering stops after 20 seconds (exit 33). The image is sent as PNG, or as
  JPEG when the PNG is over the "Largest wallpaper" setting (default 1500 KB);
  exit 34 if even JPEG is too large.

To update the renderer, run `bin/vendor_wallrender.py` against an EWOK
checkout. Never edit `data/workflows/lib/wallrender` by hand: Brander refuses
to run an edited copy (exit 31).
