# Static asset rights review

The following files were present in the publication candidate and were used as
browser/PWA icons, except for the last file, which was unreferenced:

| File | Intended use | Provenance evidence | Redistribution permission | Decision |
| --- | --- | --- | --- | --- |
| `static/favicon.ico` | Browser favicon | Not found | Not confirmed | Excluded |
| `static/icon-192.png` | PWA and touch icon | Not found | Not confirmed | Excluded |
| `static/icon-512.png` | PWA install icon | Not found | Not confirmed | Excluded |
| `static/ponkun_icon.png.png` | No active reference found | Not found | Not confirmed | Excluded |

The corresponding HTML, web-manifest, and service-worker references were
removed. The manifests remain valid without an `icons` member, and the service
worker no longer attempts to pre-cache missing assets. Browsers may display a
generic icon until the publisher supplies replacement artwork with documented
ownership or redistribution permission.

No conclusion about authorship or licensing was inferred from file names,
timestamps, directory locations, or visual appearance.
