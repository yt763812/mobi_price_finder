# Mobi Price Finder

Telegram bot for importing supplier price lists (Telegram text or XLSX) and searching offers across suppliers.

## New import behavior

After a successful confirmed import, **all previous offers of the selected supplier are replaced** in one database transaction. Other suppliers' offers are unchanged. If a file cannot be imported or has no valid rows, the old list stays intact. A new import is the complete snapshot for that supplier; if a supplier sends several separate files, each subsequent file replaces the previous one.

Both text and Excel imports retain products not recognized by the model parser, using original names. Exact repeated product names within a price list are collapsed to the lowest price, but different names and variants stay separate.

## Search

Send a product query without memory, e.g. `iphone 17 pro`, to list matching lines from **all currently fresh supplier price lists**. Results include supplier, original name, price, currency, VAT, storage, color, SIM type, region, and condition. Missing details are displayed as «Не указан» (condition: «Не указано»). They are sorted by price within each currency/VAT group, but different currencies and VAT types are never ranked against one another. More specific queries, such as `iphone 17 pro 256`, filter storage when recognized. Unrecognized models and categories use word matching against original names.

## Limitations

Excel importer supports `.xlsx` with recognizable name and price headers on a single unambiguous sheet. Arbitrary supplier Excel formats (merged headers, multiple sheets, price columns without supported names) still require importer enhancements. Text messages must contain parseable item/price pairs. For large catalogs the current Python-side all-offers search should be replaced with database-backed indexing. Testing against Telegram and a live PostgreSQL database is still required.

Install the packages listed in `requirements.txt`; configure `DATABASE_URL`, `BOT_TOKEN`, and `OWNER_TELEGRAM_ID` as before.

## Deployment

Set `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `OWNER_TELEGRAM_ID` in your deployment environment. Install `requirements.txt`, then run `python bot.py`. Choose a supplier, configure currency and VAT, upload XLSX or text, review preview, confirm replacement. Send a product name or `/find iphone 17 pro 256gb` to search all fresh suppliers. Changing the selected supplier does not delete any other supplier data. The bot does not infer a missing SIM type or region.

## Verified sample Excel files

Stock MSK: sheet `CRM_Trade`, 108 rows with prices; BLUE: sheet `13.10.2026`, 3093 rows with prices. This is file parsing, not a live PostgreSQL or Telegram end-to-end test.
