# JLPT Vocabulary Skim

A quick vocabulary pass for N5–N1. Choose Japanese- or English-first cards, tap once to reveal the other language and a furigana-marked example sentence, then tap again to continue. Each level is randomized without repeats. Progress is stored in this browser and can be reset per level.

The word table lists Japanese, English, and Japanese/English examples; its filter shows words not yet seen in the selected level. The app has no server, account, ratings, or spaced-repetition schedule. After its first load, the static app shell and data are available offline.

## Run locally

Serve this folder over HTTP (for example, `python -m http.server 8000`) and open `http://localhost:8000`. The app uses `fetch()` for the per-level JSON files, so opening `index.html` directly as a `file:` URL will not load the data.

## Data

The static lists combine OpenJLPT with the workspace CSVs, N3–N5 topic lists, and extracted entries from the five Tanos JLPT vocabulary PDFs. Each word is assigned once to the easiest level found. The files in `data/levels/` are the browser's runtime data; `data/source/` contains the inputs needed to rebuild them.

The dataset is redistributed under CC BY-SA 4.0. Attribution and upstream source terms are in `data/source/openjlpt/NOTICE.md` and `data/NOTICE.md`. The full vocabulary PDFs are not copied into this app repository.

## Rebuild data

Install the build dependencies listed in `requirements-build.txt`, then run:

```powershell
python scripts/build_dataset.py
python scripts/validate_dataset.py
```

The builder extracts selectable PDF text when the local source PDFs are available, records source pages, merges spellings/readings and meanings, adds ruby markup, and creates a level-appropriate example pair only when the source data has no usable example. It writes five JSON files to `data/levels/`.

## GitHub Pages

The folder is a standalone local Git repository configured as a static Pages site. It has no remote and has not been committed or published.
