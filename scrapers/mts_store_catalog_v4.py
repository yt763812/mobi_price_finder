
Загрузка более ранних сообщений…
[2026-11-23T12:45:58.187397434Z] [info] SECTION {"section": "televisions", "error": "RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/tv/?page=9"}
[2026-11-23T12:45:58.187400904Z] [info] START MTS full/accessories_all
[2026-11-23T12:46:28.196786975Z] [info] MTS catalog-only accessories_all: pages=1, cards=10, priced=10, issues=0
[2026-11-23T12:46:48.190978077Z] [info] MTS catalog-only accessories_all: pages=2, cards=21, priced=21, issues=0
[2026-11-23T12:47:18.191234599Z] [info] MTS catalog-only accessories_all: pages=3, cards=33, priced=33, issues=0
[2026-11-23T12:47:38.189834032Z] [info] MTS catalog-only accessories_all: pages=4, cards=44, priced=44, issues=0
[2026-11-23T12:47:58.185362495Z] [info] MTS catalog-only accessories_all: pages=5, cards=54, priced=54, issues=0
[2026-11-23T12:47:58.266508533Z] [info] SECTION {"section": "accessories_all", "error": "RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/accessories/?page=6"}
[2026-11-23T12:47:58.266516223Z] [info] START MTS full/terminals
[2026-11-23T12:48:00.165633666Z] [info] SECTION {"section": "terminals", "error": "RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/terminals/"}
[2026-11-23T12:48:00.165640856Z] [info] START MTS full/gaming_devices
[2026-11-23T12:48:20.169515812Z] [info] MTS catalog-only gaming_devices: pages=1, cards=5, priced=2, issues=3
[2026-11-23T12:48:20.169524402Z] [info] SECTION {"section": "gaming_devices", "pages": 1, "cards": 5, "priced": 2, "issues": ["('https://shop.mts.by/news-actions/news/vremya-yarkih-skidok/', 'detail title mismatch: ВРЕМЯ ЯРКИХ СКИДОК')", "('https://shop.mts.by/news-actions/news/black-sale/', 'detail title mismatch: БЕЗУМНАЯ ПЯТНИЦА В МТС')", "('https://shop.mts.by/computer-equipment/consoles-gamepades/game-consoles/', 'detail title mismatch: Игровые приставки')"], "incomplete": false, "conflicts": 0, "pagination_anomalies": [], "error": "incomplete, invalid or empty section"}
[2026-11-23T12:48:20.169530032Z] [info] FULL_STORE_SUMMARY {"failed": ["smartphones: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/phones/?page=37", "tablets: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/tablet/?page=8", "laptops: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/computer-equipment/laptops/?page=3", "watches: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/smart-watch/?page=12", "headphones: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/audio/headphones/?page=10", "tabletpc_all: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/computer-equipment/?page=6", "gadgets_all: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/audio/?page=13", "home_all: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/for-home/?page=25", "tv_all: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/tv/?page=9", "televisions: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/tv/?page=9", "accessories_all: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/accessories/?page=6", "terminals: RuntimeError: No MTS product cards (legacy or modern) on https://shop.mts.by/terminals/", "gaming_devices: incomplete, invalid or empty section"], "total_unique_products": 0, "duplicates_removed": 0, "conflicts_excluded": [], "ready_to_commit": false, "coverage_verified": false, "note": "All configured sections checked; unlisted leaf categories may exist."}
[2026-11-23T12:48:20.170501756Z] [info] NOT SAVED: full-store validation failed; previous supplier untouched
[2026-11-23T12:48:20.170506846Z] [error] 2026-11-23 12:48:12,166 - INFO - MTS scheduled crawl finished: exit=2
[2026-11-23T14:00:56.842503312Z] [error] /usr/local/lib/python3.11/site-packages/openpyxl/styles/stylesheet.py:237: UserWarning: Workbook contains no default style, apply openpyxl's default
[2026-11-23T14:00:56.842510892Z] [error]   warn("Workbook contains no default style, apply openpyxl's default")
[2026-11-23T14:18:18.417881349Z] [error] /usr/local/lib/python3.11/site-packages/openpyxl/worksheet/_reader.py:329: UserWarning: Conditional Formatting extension is not supported and will be removed
[2026-11-23T14:18:18.417885789Z] [error]   warn(msg)
Итак. Парсинг мтс завершился ошибкой
Я скинул последние сообщения логов. Причем парсинг длился оооочень долго/ 
Для сравнения парсинг а1 длился 8 минут. А по наполняемости магазины почти идентичны
…
Теперь у нас есть полная картина. Парсер МТС научился получать цены, но итоговая проверка по-прежнему отклоняет всю выгрузку. И ты прав: такая продолжительность работы неоправданна, особенно если A1 обрабатывает сопоставимый ассортимент за 8 минут.

По твоим логам вижу три проблемы.

1. Неправильное завершение пагинации
Например:

smartphones: No MTS product cards on .../phones/?page=37
accessories_all: No MTS product cards on .../accessories/?page=6
Парсер успешно собирает товары, доходит до конца раздела, но следующую пустую страницу считает ошибкой.

Получается, что мы тратим время на сбор каталога, а потом отклоняем результат из-за пустой последней страницы.

2. В каталог попадают посторонние ссылки
В категории игровых устройств обнаружены:

«ВРЕМЯ ЯРКИХ СКИДОК» — новость.
«БЕЗУМНАЯ ПЯТНИЦА В МТС» — акция.
«Игровые приставки» — ссылка на категорию.
Это не товары. Их нужно исключать ещё на этапе обнаружения карточек, а не пытаться получить для них цены.

3. Парсер работает слишком медленно
По предыдущим результатам он обрабатывает приблизительно 10–12 товаров за 20–30 секунд. Главная вероятная причина — последовательное открытие отдельных товарных страниц.

При таком подходе большой каталог закономерно занимает десятки минут или часы.

Моя цель для следующего исправления: параллельная обработка 4–6 товарных карточек, корректное определение окончания каталога и строгая фильтрация ссылок, без ослабления проверки цен.

Почему процесс занял столько времени
Я дополнительно посмотрел исходный код последнего патча. Нашёл конкретное ограничение: HTTP-клиент МТС выдерживает минимум 2 секунды между началом запросов, даже если запросы выполняются из разных потоков.

Поэтому простое добавление параллельности не даст ожидаемого ускорения. Нужно оптимизировать количество запросов, повторное использование данных и безопасно подобрать частоту обращений.

Также в mts_store_catalog_v4.py уже есть обработка пустой последней страницы, но она срабатывает не во всех случаях — в частности, при несовпадении фактической пагинации с ожидаемым количеством страниц.

Какой результат нам нужен
Сейчас	После исправления
Товарные карточки загружаются последовательно	Ограниченная параллельная загрузка
Лишние страницы могут вызывать ошибку	Проверка реального окончания каталога
Новости попадают в список товаров	Отбор только товарных ссылок
Любая ошибка категории отменяет импорт	Та же защита, но без ложных ошибок
Длительный обход завершается без результата	Полный проверенный каталог либо понятная диагностика
Прайс МТС сейчас не был сохранён: ready_to_commit: false. Судя по сообщению previous supplier untouched, старые данные остались без изменений.

Отдельные предупреждения openpyxl в конце логов относятся к обработке Excel и не являются причиной ошибки МТС.

Следующее исправление должно быть сосредоточено на скорости, правильной пагинации и фильтрации ссылок. При этом я бы не обещал заранее те же 8 минут, что у A1: сначала нужно проверить, сколько запросов действительно требуется для точного получения цен.

И пока оставляем MTS_FULL_STORE_COMMIT=0. Включать запись в PostgreSQL можно только после первого полностью успешного обхода и выборочной проверки цен.

Принимайся за следующее обновление
кстати Игровые приставки - это нужная категрия
Готово! Подготовил MTS Parser v3 — обновление, направленное на ускорение парсинга, исправление пагинации и правильное распознавание товаров.

Категорию «Игровые приставки» сохранил. Исключаются только ссылки на новости, акции и страницы навигации, которые ошибочно принимались за товары.

MTS Parser Optimized v3

ZIP · 3 обновлённых Python-файла, тесты и инструкция

Скачать MTS Parser v3

Что изменилось
Проблема	Исправление
Медленная загрузка товаров	До 4 параллельных запросов к карточкам
Повторная загрузка одних товаров	Кэширование подтверждённых результатов в рамках одного запуска
Ошибка на пустой последней странице	Корректное завершение при сохранении проверки полноты
Новости попадают в каталог	Фильтрация посторонних ссылок
Игровые приставки	Категория сохранена
Риск неверных цен	Используется подтверждённая цена «Единого платежа»
Важный момент: параллельность не отменяет общий лимит частоты запросов к МТС. Я сохранил это ограничение, чтобы не перегружать сайт.

Какие файлы заменить на GitHub
В ветке feature/onliner-review замени три файла:

scrapers/mts_store_catalog_v4.py
scrapers/mts_modern.py
mts_full_store.py
Дополнительно обнови тест:

tests/test_mts_2026.py
Остальные файлы не трогай.

Настройки Railway
В основном сервисе бота рекомендую для следующего теста:

Параметры проверки

MTS_DETAIL_WORKERS	4
SCRAPER_DELAY_SECONDS	1
MTS_FULL_STORE_COMMIT	0
[Скопировать настройки]

При ошибках ограничения частоты запросов 429 или 503 верни задержку 2 секунды и уменьши число потоков до 2.

Если не хочешь, чтобы параллельно с диагностикой запускался МТС по расписанию, временно установи MTS_DAILY_ENABLED=0.

Проверка
11 локальных тестов пройдены

Проверены получение цен, параллельная обработка карточек, исключение новостей, сохранение товаров игровых приставок и обработка конца каталога.

Но полноценный обход сайта на Railway ещё не выполнен. Поэтому сначала проведём тест без сохранения в PostgreSQL.

И помни: оба Railway-сервиса используют одну ветку GitHub. Загружай обновление после завершения текущих задач, чтобы не прервать Worker Onlíner.

После теста пришли мне первые 5–10 строк парсинга смартфонов и итоговый FULL_STORE_SUMMARY. Посмотрим, насколько увеличилась скорость и удалось ли полностью устранить ошибки.

Не хочет загружаться архиив
Проверил архив — он существует, не повреждён и все 5 файлов внутри на месте. Возможно, проблема возникла при скачивании через ChatGPT.

Я создал дополнительную копию с новым названием.

Попробуй скачать архив ещё раз
Скачать MTS Parser v3 — ZIP

Если архив снова не скачивается
Можешь скачать файлы по отдельности:

Три файла для замены на GitHub:

mts_store_catalog_v4.py
mts_modern.py
mts_full_store.py
Дополнительно:

Тесты — test_mts_2026.py
Инструкция — README_MTS_V3.md
При загрузке на GitHub первые два файла должны находиться в папке scrapers/, а третий — в корне репозитория.

Попробуй сначала новую ссылку на ZIP. Если опять не получится, напиши, что именно происходит: появляется ошибка, файл не открывается или скачивание вообще не начинается.


mts_store_catalog_v4.py
Код

mts_modern.py
Код

mts_full_store.py
Код
Показать еще 2




        # not a real extra catalogue page. Never count its cards.
        page_urls = {urljoin(page_url, c.url or '') for c in parser.cards if c.url}
        page_urls.update(c.url for c in modern)
        if page_urls and page_urls.issubset(seen_urls):
            if expected_page_count is not None and n < expected_page_count:
                raise RuntimeError(f'Repeated page {n} before advertised last page {expected_page_count}')
            print(f'MTS catalog-only {category}: terminal repeated page {n}; '
                  f'counting {len(visited)} genuine pages', flush=True)
            break
        visited.add(n)
        seen_urls.update(page_urls)
        cards_total += len(parser.cards) + len(modern)
        if cards_total > max_products:
            incomplete = True
            break
        for card in parser.cards:
            url = urljoin(page_url, card.url or '')
            dest = urlparse(url)
            if not (card.sku and card.name and card.url and dest.scheme == 'https'
                    and valid_store_product_url(url, catalogue_url)):
                issues.append((card.sku or '?', 'invalid SKU/name/link'))
                continue
            price = read_price(card.displayed_price)
            if price is None:
                issues.append((card.sku, f'no full advertised price: {card.displayed_price!r}'))
                continue
            offer = CatalogOffer(card.sku, card.name, url, price, 'BYN',
                                 card.approximate, card.availability, card.ga_price)
            # The site's GA SKU is not necessarily unique: several different
            # links may share it. Disambiguate with stable URL-derived keys,
            # while rejecting contradictory prices for the SAME URL.
            # All keys are stable across page order and daily runs.
            key = hashlib.sha256(url.encode('utf-8')).hexdigest()
            if key in conflicted:
                conflict_prices[key].add(offer.price)
                continue
            old = products.get(key)
            if old is not None and old.price != offer.price:
                # Withhold this URL until the crawl completes. Other errors
                # still block the atomic snapshot as before.
                conflict_prices[key] = {old.price, offer.price}
                conflicted.add(key)
                del products[key]
                conflicts.append((card.sku, url, old))
                continue
            products[key] = offer
        # Fetch verified product details concurrently, but PublicFetcher keeps
        # its global minimum spacing between request starts for this process.
        # Futures are consumed in card order for reproducible diagnostics.
        def get_verified(card):
            url = card.url
            key = (url, card.name)
            with cache_lock:
                cached = detail_cache.get(key)
            if cached is not None:
                return cached
            try:
                detail_html = fetcher.get(url)
                answer = detail_offer(detail_html, url, card.name)
            except Exception as exc:
                # Do not cache failures; a transient error may be recoverable.
                return (None, None, f'detail HTTP/parse failure: {type(exc).__name__}: {exc}')
            with cache_lock:
                detail_cache[key] = answer
            return answer

        pending = []
        for card in modern:
            if not valid_store_product_url(card.url, catalogue_url):
                issues.append(('?', f'invalid modern product URL: {card.url}'))
                continue
            key = hashlib.sha256(card.url.encode('utf-8')).hexdigest()
            if key not in products and key not in conflicted:
                pending.append((key, card))
        if pending:
            with ThreadPoolExecutor(max_workers=detail_workers) as executor:
                verified = executor.map(get_verified, (card for _, card in pending))
                for (key, card), (sku, exact, error) in zip(pending, verified):
                    if error is not None:
                        issues.append((sku or card.url, error))
                        continue
                    products[key] = CatalogOffer(sku, card.name, card.url, exact,
                                                 'BYN', False, '', '')
        print(f'MTS catalog-only {category}: pages={len(visited)}, cards={cards_total}, '
              f'priced={len(products)}, issues={len(issues)}', flush=True)
        links = PaginationLinks()
        links.feed(html)
        last_page_links = {p for href in links.hrefs
                           if (p := page_number(urljoin(page_url, href), catalogue_url))}
        last_page_card_count = len(parser.cards) + len(modern)
        for href in links.hrefs:
            next_url = urljoin(page_url, href)
            next_n = page_number(next_url, catalogue_url)
            if next_n and next_n not in visited and next_n not in (v for v, _ in todo):
                todo.append((next_n, next_url))
        todo.sort(key=lambda x: x[0])
        # Some responses omit pagination links despite showing a full page.
        # Probe the next sequential page rather than declaring completion.
        # A repeated response is handled at the top of the loop, while an
        # empty/invalid probe fails closed (no supplier write based on it).
        if not todo and (len(parser.cards) >= 18 or len(modern) >= 12):
            next_n = n + 1
            parts = urlsplit(catalogue_url)
            params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                      if k != 'page']
            params.append(('page', str(next_n)))
            candidate = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urlencode(params), ''))
            if next_n not in visited:
                todo.append((next_n, candidate))
                pagination_probe_count += 1
                pagination_anomalies.append(f'no pagination after full page {n}; probed page {next_n}')
                print(f'MTS catalog-only {category}: PAGINATION_PROBE page={next_n} '
                      f'(page {n} had {len(parser.cards) + len(modern)} cards)', flush=True)
    if (expected_page_count and len(visited) < expected_page_count
            and advertised_total is not None and cards_total + first_page_size < advertised_total):
        incomplete = True
        pagination_anomalies.append(
            f'only {len(visited)} of {expected_page_count} advertised catalogue pages reached')
    # One detail request per conflicting URL, not one per product.
    for sku, url, original in conflicts:
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        choices = conflict_prices[key]
        resolved, reason = _verify_conflict(fetcher, original, choices)
        if resolved is None:
            print(f'MTS catalog-only {category}: CONFLICT EXCLUDED sku={sku} url={url} '
                  f'prices={sorted(choices)}; {reason}', flush=True)
        else:
            from dataclasses import replace
            products[key] = replace(original, price=resolved)
            print(f'MTS catalog-only {category}: CONFLICT RESOLVED sku={sku} url={url} '
                  f'price={resolved}; {reason}', flush=True)
    return CatalogResult(list(products.values()), len(visited), cards_total, issues, incomplete, conflicts, pagination_probe_count, pagination_anomalies)

