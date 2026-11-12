#!/usr/bin/env python3
"""Стартовый файл парсера.

Спрашивает адрес сайта (сайта по умолчанию нет — всегда указывай явно),
при необходимости уточняет детали и запускает сбор.

    python start.py                      → спросит всё в интерактиве
    python start.py https://example.ru   → сразу к делу (с дефолтными настройками)
    python start.py https://example.ru --types posts --max 100
"""

from __future__ import annotations

import sys
from urllib.parse import urlparse

from parser import main as run_parser
from wp_scraper import ScraperError, WordPressSite, safe_filename

C = {
    "cyan": "\033[1;36m",
    "green": "\033[1;32m",
    "yellow": "\033[1;33m",
    "red": "\033[1;31m",
    "dim": "\033[2m",
    "off": "\033[0m",
}


eof_reached = False


def ask(prompt: str, default: str | None = None) -> str:
    """Вопрос с необязательным значением по умолчанию.

    На Ctrl+D (EOF) возвращает значение по умолчанию — чтобы можно было
    подставить ответы пайпом. Ctrl+C — отмена.
    """
    global eof_reached
    suffix = f" {C['dim']}[{default}]{C['off']}" if default else ""
    try:
        answer = input(f"{C['cyan']}?{C['off']} {prompt}{suffix}: ").strip()
    except EOFError:
        eof_reached = True
        print()
        return default or ""
    except KeyboardInterrupt:
        print()
        raise SystemExit(1)
    return answer or (default or "")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer.startswith(("y", "д", "1"))


def normalize(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("пустой адрес")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        raise ValueError("не похоже на адрес сайта")
    return url


def choose_types(url: str) -> str:
    """Показывает доступные типы записей и спрашивает, что собирать."""
    print(f"{C['dim']}  Смотрю доступные типы записей...{C['off']}")
    try:
        site = WordPressSite(url, verbose=False)
        site.discover()
        types = site.list_types()
    except ScraperError as e:
        print(f"{C['red']}  Не удалось: {e}{C['off']}")
        return ask("Какие типы собирать", "posts,pages")

    print(f"{C['green']}  Доступно:{C['off']}")
    for slug in types:
        print(f"    • {slug}")
    print()
    return ask("Какие типы собирать (через запятую)", "posts,pages")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough = [a for a in argv if a.startswith("-")]

    # ── 1. Адрес сайта ──
    url = ""
    if argv and not argv[0].startswith("-"):
        url = argv[0]
        passthrough = argv[1:]
        print(f"{C['cyan']}🌐  Сайт: {url}{C['off']}")
    else:
        print()
        print(f"{C['cyan']}╔══════════════════════════════════════╗{C['off']}")
        print(f"{C['cyan']}║   Сбор данных с WordPress-сайта       ║{C['off']}")
        print(f"{C['cyan']}╚══════════════════════════════════════╝{C['off']}")
        print()
        while True:
            raw = ask("Адрес сайта (например https://example.ru)")
            if eof_reached:
                print("Выход.")
                return 0
            if raw.lower() in ("q", "quit", "выход", "exit"):
                print("Выход.")
                return 0
            try:
                url = normalize(raw)
                break
            except ValueError:
                print(f"{C['red']}  Нужно что-то вроде https://example.ru{C['off']}")
        print(f"{C['green']}✓ {url}{C['off']}")
        print()

    # ── 2. Что собирать ──
    types = "posts,pages"
    if not passthrough or "--types" not in passthrough:
        if ask_yes_no("Посмотреть доступные типы записей?", default=False):
            types = choose_types(url)
        else:
            types = ask("Какие типы собирать (через запятую)", "posts,pages")
    print()

    # ── 3. Куда сохранить ──
    out = ""
    if "--out" not in passthrough and "-o" not in passthrough:
        domain = safe_filename(url.split("//", 1)[1])
        out = ask("Куда сохранить (Enter = авто)", f"{domain}_dataset.jsonl")
    print()

    # ── 4. Ограничение ──
    limit = ""
    if "--max" not in passthrough:
        raw_max = ask("Сколько записей максимум (Enter = без ограничения)")
        if raw_max.isdigit():
            limit = raw_max
    print()

    # ── Сборка аргументов ──
    args = [url, "--types", types]
    if out:
        args += ["--out", out]
    if limit:
        args += ["--max", limit]
    args += passthrough

    print(f"{C['cyan']}▶  Запускаю: parser.py {' '.join(args)}{C['off']}")
    print()
    try:
        return run_parser(args)
    except ScraperError as e:
        print(f"{C['red']}❌ {e}{C['off']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
