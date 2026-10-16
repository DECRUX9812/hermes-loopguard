# Catalog artwork

- `../banner.png`: the 1200×600 (2:1) catalog card and plugin-page hero (`image:` in the catalog entry).
- `../icon.png` / `../icon.svg`: the 512×512 LoopGuard mark.

Both are rendered from these sources with headless Chrome, using only the real dashboard screenshots
in `examples/demo/` (neutral demo data):

```bash
cd docs/media/src
google-chrome --headless=new --hide-scrollbars --window-size=1200,600 --screenshot=../banner.png "file://$PWD/banner.html"
google-chrome --headless=new --hide-scrollbars --default-background-color=00000000 --window-size=512,512 --screenshot=../icon.png "file://$PWD/icon.html"
```
