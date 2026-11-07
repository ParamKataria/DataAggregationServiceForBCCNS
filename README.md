# Black history metadata scraper

This project lives outside the hackathon git repo. It reads public Nova Scotia Archives virtual exhibits and writes one Dublin Core row per record. Only the CSV is copied into the web app.

Collections:

- Nova Scotia Archives virtual exhibits: slavery and abolition, the African Nova Scotian diaspora, Africville, and Viola Desmond
- The Canadian Encyclopedia, Black History in Canada
- A Black People's History of Canada
- Black Loyalists: Our Story, Our People, plus the Black Loyalist Heritage Centre history pages
- Library and Archives Canada, Black Canadians research guide

## Run

From this folder:

```bash
uv sync
uv run scrape-metadata
```

A short check of two records per collection:

```bash
uv run scrape-metadata --limit 2 --copy-to -
```

The full run writes `output/black_history_metadata.csv` here and copies it to `RBC HackHub/data/black_history_metadata.csv`.

Repeated values in one cell are separated with ` | `.
