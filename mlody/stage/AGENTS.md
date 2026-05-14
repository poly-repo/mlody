# Stage Notes

## Merged Server Asset Rules

- `mlody -- --server` serves the stage shell and static assets from Bazel runfiles, not just from the source tree.
- Generated stage assets such as `bundle.js` must be resolvable through the runfiles manifest.
- Keep regression coverage for manifest-served assets when changing static file serving.

## Dynamic Asset URLs

- Backend-provided asset paths must not be treated as plain root-relative URLs.
- Normalize dynamic asset URLs against `resolveServerBaseUrl()` so they work both:
  - on the merged same-origin server
  - on split stage/API ports during local dev
- Current known external dynamic asset type in stage is user avatars.

## Avatar-Specific Gotchas

- User avatars come from backend payloads like `assets/images/avatars/...` and must be converted to absolute URLs using the active server base URL.
- The selected-user avatar can get stuck after an image failure if the image element is reused across user switches.
- Keep the selected-user avatar keyed by user identity and avatar URL so it remounts cleanly when switching users.

## Current Non-Issues

- Stage shell assets such as `bundle.js`, `styles.css`, and favicons are expected to work through the merged server.
- Table image cells in `StageTableBlock` use `data:` URLs, so they do not depend on static file routing.

## Future Rule Of Thumb

- If stage starts rendering any new backend-provided file or image path, route it through `resolveServerBaseUrl()` unless it is already a full `http(s)` URL or a `data:` URL.
