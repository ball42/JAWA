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
