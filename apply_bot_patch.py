"""Run ONCE in the root of mobi_price_finder to integrate the scheduler."""
from pathlib import Path

path = Path('bot.py')
source = path.read_text(encoding='utf-8')
if 'from mts_daily_scheduler import post_init as mts_post_init' not in source:
    needle = 'from database import ('
    if needle not in source:
        raise SystemExit('Unexpected bot.py imports. No changes made.')
    source = source.replace(needle, 'from mts_daily_scheduler import post_init as mts_post_init\n' + needle, 1)
old = 'app = ApplicationBuilder().token(token).build()'
new = 'app = ApplicationBuilder().token(token).post_init(mts_post_init).build()'
if old in source:
    source = source.replace(old, new, 1)
elif new not in source:
    raise SystemExit('Unexpected application builder. No changes made.')
path.write_text(source, encoding='utf-8')
print('OK: bot.py now attaches the optional MTS scheduler')
