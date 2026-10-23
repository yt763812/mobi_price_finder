# Mobi Price Finder — web catalog integration (experimental foundation)

This is an **additive prototype**, not a claim that four complete catalogue scrapers are working.

## Tested scope
- JSON-LD `Product` / `Offer` extractor and unit tests.
- Separate PostgreSQL tables (`web_offers`, `web_price_history`, `web_scrape_runs`).
- No changes to the current `suppliers` / `offers` tables or bot.
- No deletion of prices due to partial crawls.
- Known category entry points for A1 and MTS; DNS/NSV require verification.

## Run
Copy `scrapers/` and `scrape_cli.py` alongside `bot.py`. Your project's existing psycopg dependency is sufficient.

```bash
python -m unittest discover -s tests -v
python scrape_cli.py --shop mts --category smartphones --dry-run
DATABASE_URL=... python scrape_cli.py --shop mts --category smartphones
```

**Important:** The CLI reads only the first catalogue page; if there is no JSON-LD with complete full-purchase BYN price it exits without writing. It does not infer prices from installment amounts. The script deliberately does not paginate, classify promotional conditions, or merge into Telegram search until site-specific extraction can be validated. Never schedule it as a reliable two-daily full catalogue updater in its current state.

## Next integration tasks
1. Validate current `robots.txt`, category URLs, site terms and actual HTML responses on deployment host.
2. Implement site-specific extractors (including full buy-now prices, promotional conditions, availability and product variants).
3. Discover and exhaust safe pagination; check count/completeness before replacing any catalogue snapshot.
4. Extend the bot search layer to join normalized online prices with supplier offers (distinguish public retail / B2B-verified).
5. Add bounded scheduler (twice daily), failed-run logging and monitoring.
6. DNS and NSV category routes need concrete confirmation before enablement.
