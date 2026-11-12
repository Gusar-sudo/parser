#!/usr/bin/env python3
"""Сборщик данных с WordPress-сайтов.

Примеры:
    python parser.py                          # как раньше: bmtriz.ru → bmtriz_dataset.jsonl
    python parser.py https://example.ru       # любой WordPress-сайт
    python parser.py https://example.ru --list-types
    python parser.py https://example.ru --types posts,products --max 100
    python parser.py https://example.ru --since 2026-01-01 --out new.jsonl
    python parser.py https://example.ru --incremental --state .state/example.json
    python parser.py https://example.ru --format md --out site.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from wp_scraper import (
    DEFAULT_UA,
    ScraperError,
    SiteConfig,
    WordPressSite,
    load_state,
    safe_filename,
    save_documents,
    save_state,
)

DEFAULT_SITE = None  # сайта по умолчанию нет — адрес всегда указывается явно
SITES_DIR = Path(__file__).parent / "sites"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parser.py",
        description="Скачивает записи с WordPress-сайта через REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  parser.py https://example.ru --list-types\n"
            "  parser.py https://example.ru --types posts,products --max 50\n"
            "  parser.py https://example.ru --incremental --state .state/example.json\n"
        ),
    )
    p.add_argument("url", nargs="?", default=None,
                   help="адрес сайта (обязательно — сайта по умолчанию нет)")
    p.add_argument("--types", default="posts,pages",
                   help="типы записей через запятую (по умолчанию posts,pages)")
    p.add_argument("--list-types", action="store_true",
                   help="показать доступные типы записей и выйти")
    p.add_argument("--out", "-o", default=None,
                   help="файл результата (по умолчанию <домен>_dataset.jsonl)")
    p.add_argument("--append", action="store_true",
                   help="дописать в существующий файл, не перезаписывать")
    p.add_argument("--no-backup", action="store_true",
                   help="не сохранять предыдущий датасет в .bak")
    p.add_argument("--format", "-f", default="jsonl",
                   choices=["jsonl", "json", "csv", "md"], help="формат вывода")
    p.add_argument("--max", type=int, default=None, dest="max_items",
                   help="ограничить количество записей")
    p.add_argument("--since", default=None,
                   help="только записи после даты (ГГГГ-ММ-ДД)")
    p.add_argument("--incremental", action="store_true",
                   help="догружать только новые записи (нужен --state)")
    p.add_argument("--state", default=None,
                   help="файл состояния для инкрементального сбора")
    p.add_argument("--delay", type=float, default=0.5,
                   help="пауза между запросами, сек (по умолчанию 0.5)")
    p.add_argument("--timeout", type=int, default=30, help="таймаут ответа, сек")
    p.add_argument("--retries", type=int, default=3, help="сколько раз повторять при ошибке сети")
    p.add_argument("--per-page", type=int, default=100, help="размер страницы API")
    p.add_argument("--user-agent", default=DEFAULT_UA, help="User-Agent")
    p.add_argument("--keep-html", action="store_true", help="сохранять исходный HTML")
    p.add_argument("--no-taxonomy", action="store_true",
                   help="не разворачивать id категорий/тегов в названия")
    p.add_argument("--profile", default=None,
                   help="имя профиля сайта в ./sites/<имя>.json (создаётся, если не было)")
    p.add_argument("--save-config", default=None,
                   help="сохранить настройки запуска в этот JSON-файл")
    p.add_argument("--quiet", "-q", action="store_true", help="без прогресса")
    return p


def load_profile(name: str) -> SiteConfig | None:
    path = SITES_DIR / f"{name}.json"
    if not path.exists():
        return None
    import json
    try:
        return SiteConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def save_profile(name: str, cfg: SiteConfig) -> Path:
    import json
    SITES_DIR.mkdir(parents=True, exist_ok=True)
    path = SITES_DIR / f"{name}.json"
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbose = not args.quiet

    if not args.url and not args.profile:
        print(
            "❌ Укажите адрес сайта.\n"
            "   python parser.py https://example.ru\n"
            "   или запустите интерактивно:  python start.py",
            file=sys.stderr,
        )
        return 2

    # Профиль сайта — настройки можно переиспользовать
    cfg = load_profile(args.profile) if args.profile else None
    if cfg is not None:
        url = cfg.url
        types = cfg.types
        delay, timeout, retries = cfg.delay, cfg.timeout, cfg.retries
        per_page, user_agent = cfg.per_page, cfg.user_agent
        with_taxonomy = cfg.with_taxonomy
    else:
        url = args.url
        types = [t.strip() for t in args.types.split(",") if t.strip()]
        delay, timeout, retries = args.delay, args.timeout, args.retries
        per_page, user_agent = args.per_page, args.user_agent
        with_taxonomy = not args.no_taxonomy

    site = WordPressSite(
        url,
        delay=delay,
        timeout=timeout,
        retries=retries,
        user_agent=user_agent,
        verbose=verbose,
    )

    try:
        site.discover()

        if args.list_types:
            print(f"\nДоступные типы записей на {site.base_url}:\n")
            available = site.list_types()
            for slug, rest_base in available.items():
                print(f"  {slug:<20} → /wp-json/wp/v2/{rest_base}")
            print("\nСобрать:  parser.py "
                  f"{site.base_url} --types {','.join(available)}")
            return 0

        # Инкрементальный режим: какие id уже скачаны
        last_ids: dict[str, int] = {}
        state_path = args.state
        if args.incremental:
            if not state_path:
                print("Для --incremental нужен файл --state (например .state/site.json)",
                      file=sys.stderr)
                return 2
            state = load_state(state_path)
            key = site.base_url
            last_ids = state.get(key, {}).get("types", {})

        docs = site.scrape(
            types,
            per_page=per_page,
            max_items=args.max_items,
            since=args.since,
            last_ids=last_ids if args.incremental else None,
            with_taxonomy=with_taxonomy,
        )

        if not docs:
            if args.incremental:
                print("\n✅ Новых записей нет — файл остался без изменений.")
                return 0
            print("\n❌ Ни одной записи получить не удалось.", file=sys.stderr)
            print(f"   Сайт: {site.base_url}", file=sys.stderr)
            print("   Проверь в браузере: "
                  f"{site.base_url}/wp-json/", file=sys.stderr)
            return 1

        if args.out:
            out_path = Path(args.out)
        elif cfg is not None and cfg.extra.get("out"):
            out_path = Path(cfg.extra["out"])
        else:
            domain = safe_filename(site.base_url.split("//", 1)[1])
            out_path = Path(f"{domain}_dataset.{args.format}")

        save_documents(docs, out_path, args.format,
                       append=args.append, backup=not args.no_backup)

        total_chars = sum(len(d.get("content", "")) for d in docs)
        print(f"\n💾 Сохранено {len(docs)} документов → {out_path}")
        print(f"📊 Общий объём текста: ~{total_chars:,} символов")

        # Состояние для следующего инкрементального запуска
        if args.incremental and state_path:
            state = load_state(state_path)
            key = site.base_url
            bucket = state.setdefault(key, {"types": {}})
            for post_type in types:
                new_id = site.last_ids.get(post_type)
                if new_id:
                    bucket["types"][post_type] = max(
                        new_id, bucket["types"].get(post_type, 0)
                    )
            bucket["updated"] = docs[0].get("date") if docs else None
            save_state(state_path, state)
            print(f"🗂  Состояние сохранено → {state_path}")

        if args.save_config:
            cfg_out = SiteConfig(
                url=site.base_url, types=types, delay=delay, timeout=timeout,
                retries=retries, per_page=per_page, user_agent=user_agent,
                with_taxonomy=with_taxonomy,
            )
            cfg_out.extra["out"] = str(out_path)
            path = Path(args.save_config)
            path.parent.mkdir(parents=True, exist_ok=True)
            import json
            path.write_text(json.dumps(cfg_out.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
            print(f"⚙  Настройки сохранены → {path}")

        if args.profile and cfg is None:
            cfg_new = SiteConfig(
                url=site.base_url, types=types, delay=delay, timeout=timeout,
                retries=retries, per_page=per_page, user_agent=user_agent,
                with_taxonomy=with_taxonomy,
            )
            cfg_new.extra["out"] = str(out_path)
            path = save_profile(args.profile, cfg_new)
            print(f"⚙  Профиль сохранён → {path}")

        return 0

    except ScraperError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
