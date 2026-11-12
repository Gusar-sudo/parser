"""Универсальный сборщик данных с WordPress-сайтов через REST API.

Модуль умеет:
  * сам найти REST API на сайте (wp-json, rest_route, с www и без);
  * определить доступные типы записей (posts, pages, products, ...);
  * обойти всю пагинацию с ретраями и вежливой паузой;
  * догружать только новые записи (инкрементально, по id);
  * разворачивать id категорий/тегов в названия;
  * сохранять результат в jsonl / json / csv / markdown.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Поля, которые запрашиваем у WordPress (меньше = быстрее)
ITEM_FIELDS = "id,date,modified,link,slug,status,type,title,content,excerpt,author,categories,tags"


class ScraperError(Exception):
    """Ошибка, понятная пользователю (а не traceback в коде)."""


@dataclass
class SiteConfig:
    """Настройки сайта — их можно сохранить в JSON и переиспользовать."""

    url: str
    types: list[str] = field(default_factory=lambda: ["posts", "pages"])
    delay: float = 0.5
    timeout: int = 30
    retries: int = 3
    per_page: int = 100
    user_agent: str = DEFAULT_UA
    keep_html: bool = False
    with_taxonomy: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "types": self.types,
            "delay": self.delay,
            "timeout": self.timeout,
            "retries": self.retries,
            "per_page": self.per_page,
            "user_agent": self.user_agent,
            "keep_html": self.keep_html,
            "with_taxonomy": self.with_taxonomy,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteConfig":
        known = {
            "url", "types", "delay", "timeout", "retries", "per_page",
            "user_agent", "keep_html", "with_taxonomy",
        }
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        cfg = cls(**kwargs)
        cfg.extra = extra
        return cfg


# ────────────────────────────────  HTML  ────────────────────────────────


# Теги, после которых имеет смысл перенос строки
_BLOCK_TAGS = (
    "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "td", "th",
    "blockquote", "section", "article", "ul", "ol", "table", "hr", "pre",
    "figure", "figcaption", "header", "footer", "nav", "aside", "main",
)
_DROP_TAGS = ("script", "style", "noscript", "iframe", "svg", "form", "button")


def _prepare(soup: BeautifulSoup) -> None:
    """Выкидывает мусор и расставляет переносы строк по блочным тегам."""
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")


def clean_html(html_content: str | None) -> str:
    """HTML → чистый текст. Inline-теги не рвут предложения переносами."""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    _prepare(soup)
    text = soup.get_text(separator="")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown(html_content: str | None) -> str:
    """HTML → markdown. Заголовки, списки, ссылки сохраняются."""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    _prepare(soup)

    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.replace_with(f"\n\n{'#' * level} {h.get_text(' ', strip=True)}\n\n")

    for li in soup.find_all("li"):
        li.replace_with(f"\n- {li.get_text(' ', strip=True)}")

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin("https://example.invalid", str(a["href"]))
        a.replace_with(f"[{text}]({href})" if text else href)

    text = soup.get_text(separator="")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_document(
    item: dict[str, Any], source: str, *, keep_html: bool = False
) -> dict[str, Any] | None:
    """Объект WordPress → словарь для LLM."""
    title = clean_html((item.get("title") or {}).get("rendered", ""))
    content_html = (item.get("content") or {}).get("rendered", "")
    content_text = clean_html(content_html)
    if not title and not content_text:
        return None

    doc = {
        "id": item.get("id"),
        "url": item.get("link"),
        "title": title,
        "content": content_text,
        "date": item.get("date"),
        "modified": item.get("modified"),
        "type": item.get("type"),
        "categories": item.get("categories", []),
        "tags": item.get("tags", []),
        "source": source,
    }
    excerpt = clean_html((item.get("excerpt") or {}).get("rendered", ""))
    if excerpt:
        doc["excerpt"] = excerpt
    if keep_html:
        doc["content_html"] = content_html
    return doc


# ────────────────────────────────  Клиент  ────────────────────────────────


class WordPressSite:
    """Клиент WP REST API одного сайта."""

    def __init__(
        self,
        base_url: str,
        *,
        delay: float = 0.5,
        timeout: int = 30,
        retries: int = 3,
        user_agent: str = DEFAULT_UA,
        verbose: bool = True,
    ) -> None:
        self.base_url = self._normalize_url(base_url)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
        })

        self.api_root: str | None = None
        self.routes: dict[str, Any] = {}
        self._types_cache: dict[str, str] | None = None
        # максимальный id по каждому типу записей (для инкрементального сбора)
        self.last_ids: dict[str, int] = {}

    # ── служебное ──

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if not url:
            raise ScraperError("Пустой адрес сайта")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        if not parsed.netloc:
            raise ScraperError(f"Не понимаю адрес: {url}")
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _request(
        self, url: str, params: dict[str, Any] | None = None
    ) -> requests.Response:
        """GET с ретраями на сетевых ошибках и 5xx."""
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            resp = None
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    timeout=(10, self.timeout),
                )
                if resp.status_code >= 500:
                    raise requests.exceptions.HTTPError(
                        f"HTTP {resp.status_code}", response=resp
                    )
                return resp
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError) as e:
                last_error = e
                if attempt >= self.retries:
                    break
                wait = min(2 ** attempt * 2, 15)
                self._log(f"  ⚠ {type(e).__name__} — повтор через {wait} сек")
                time.sleep(wait)
        raise ScraperError(f"Не удалось скачать {url}: {last_error}")

    @staticmethod
    def _is_json_list(resp: requests.Response) -> bool:
        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype.lower():
            return False
        try:
            return isinstance(resp.json(), list)
        except ValueError:
            return False

    # ── обнаружение ──

    def discover(self) -> None:
        """Ищет корень REST API. Бросает ScraperError, если не нашёл."""
        if self.api_root:
            return

        candidates = [
            f"{self.base_url}/wp-json/",
            f"{self.base_url}/?rest_route=/",
            f"{self.base_url}/index.php?rest_route=/",
        ]
        last: str = ""
        for url in candidates:
            try:
                resp = self._request(url)
            except ScraperError as e:
                last = str(e)
                continue
            if resp.status_code != 200:
                last = f"HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
            except ValueError:
                last = "ответ не JSON"
                continue
            if isinstance(data, dict) and data.get("routes"):
                self.api_root = url
                self.routes = data["routes"]
                self._log(f"🔎 REST API найден: {self.api_root}")
                return

        # Запасной вариант: пробуем конкретные эндпоинты
        for endpoint in ("posts", "pages"):
            url = f"{self.base_url}/wp-json/wp/v2/{endpoint}"
            try:
                resp = self._request(url, params={"per_page": 1})
            except ScraperError as e:
                last = str(e)
                continue
            if resp.status_code == 200 and self._is_json_list(resp):
                self.api_root = f"{self.base_url}/wp-json/"
                self._log(f"🔎 REST API найден (по эндпоинту): {self.api_root}")
                return

        raise ScraperError(
            f"На {self.base_url} не нашёл WordPress REST API.\n"
            f"   Последняя ошибка: {last}\n"
            "   Проверь в браузере: "
            f"{self.base_url}/wp-json/ — должен открыться JSON со списком routes."
        )

    def list_types(self) -> dict[str, str]:
        """Доступные типы записей: {'posts': 'posts', 'products': 'products'}."""
        if self._types_cache is not None:
            return self._types_cache

        self.discover()
        result: dict[str, str] = {}
        try:
            resp = self._request(f"{self.api_root}wp/v2/types")
            if resp.status_code == 200:
                data = resp.json()
                for slug, meta in data.items():
                    rest_base = meta.get("rest_base") or slug
                    result[slug] = rest_base
        except ScraperError:
            pass

        if not result:
            # Не все сайты отдают /types — проверяем эндпоинты вручную
            for candidate in ("posts", "pages", "media"):
                if f"/wp/v2/{candidate}" in self.routes:
                    result[candidate] = candidate
        if not result:
            result = {"posts": "posts", "pages": "pages"}

        self._types_cache = result
        return result

    # ── таксономия ──

    def _taxonomy_names(self) -> tuple[dict[int, str], dict[int, str]]:
        """id → название для категорий и тегов."""
        cats: dict[int, str] = {}
        tags: dict[int, str] = {}
        for kind, target in (("categories", cats), ("tags", tags)):
            try:
                resp = self._request(
                    f"{self.api_root}wp/v2/{kind}", params={"per_page": 100, "hide_empty": "false"}
                )
                if resp.status_code != 200:
                    continue
                for item in resp.json():
                    target[int(item["id"])] = item.get("name", "")
            except (ScraperError, ValueError, KeyError, TypeError):
                continue
        return cats, tags

    # ── основное ──

    def fetch(
        self,
        post_type: str = "posts",
        *,
        per_page: int = 100,
        max_items: int | None = None,
        since: str | None = None,
        last_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Скачивает все записи типа post_type (с пагинацией)."""
        self.discover()
        types = self.list_types()
        rest_base = types.get(post_type, post_type)
        url = f"{self.api_root}wp/v2/{rest_base}"

        params: dict[str, Any] = {"per_page": min(per_page, 100), "_fields": ITEM_FIELDS}
        if since:
            params["after"] = f"{since}T00:00:00"
        if last_id is not None:
            # идём от свежих к старым и останавливаемся, дошли до известного
            params["orderby"] = "id"
            params["order"] = "desc"

        items: list[dict[str, Any]] = []
        page = 1
        total_pages: int | None = None

        while True:
            params["page"] = page
            resp = self._request(url, params=params)

            if resp.status_code == 400:
                body = resp.text
                if "rest_post_invalid_page_number" in body or "rest_post_invalid_page_number" in body:
                    break
                raise ScraperError(f"Сайт вернул 400: {body[:200]}")

            if resp.status_code == 404:
                raise ScraperError(f"Тип записей «{post_type}» не найден на сайте")
            if resp.status_code != 200:
                raise ScraperError(f"Сайт вернул HTTP {resp.status_code} для {url}")

            try:
                chunk = resp.json()
            except ValueError as e:
                raise ScraperError(f"Ответ не JSON (сайт мог вернуть заглушку): {e}") from e
            if not isinstance(chunk, list):
                raise ScraperError("Неожиданный формат ответа: ожидался список")
            if not chunk:
                break

            if total_pages is None:
                header = resp.headers.get("X-WP-TotalPages")
                total_pages = int(header) if header and header.isdigit() else None
                total = resp.headers.get("X-WP-Total")
                if total:
                    self._log(f"   на сайте всего: {total}")

            stop = False
            for item in chunk:
                if last_id is not None and int(item.get("id", 0)) <= last_id:
                    stop = True  # дошли до уже скачанного — дальше не идём
                    break
                items.append(item)
                try:
                    item_id = int(item.get("id", 0))
                except (TypeError, ValueError):
                    continue
                if item_id > self.last_ids.get(post_type, 0):
                    self.last_ids[post_type] = item_id

            if stop:
                self._log(f"  остановились на id={last_id} (новых записей нет)")
                break

            page += 1
            self._log(
                f"  Загружено {len(items)} записей"
                + (f" (стр. {page}/{total_pages})" if total_pages else "")
            )
            if self.delay:
                time.sleep(self.delay)

            if max_items and len(items) >= max_items:
                break
            if total_pages and page > total_pages:
                break

        if max_items:
            items = items[:max_items]
        return items

    # ── всё сразу ──

    def scrape(
        self,
        post_types: Iterable[str] = ("posts", "pages"),
        *,
        per_page: int = 100,
        max_items: int | None = None,
        since: str | None = None,
        last_ids: dict[str, int] | None = None,
        with_taxonomy: bool = True,
    ) -> list[dict[str, Any]]:
        """Собирает документы со всех указанных типов записей."""
        last_ids = last_ids or {}
        docs: list[dict[str, Any]] = []

        cats: dict[int, str] = {}
        tags: dict[int, str] = {}
        if with_taxonomy:
            cats, tags = self._taxonomy_names()

        source = urlparse(self.base_url).netloc
        for post_type in post_types:
            self._log(f"📥 {post_type}...")
            items = self.fetch(
                post_type,
                per_page=per_page,
                max_items=max_items,
                since=since,
                last_id=last_ids.get(post_type),
            )
            self._log(f"   получено: {len(items)}")
            for item in items:
                doc = extract_document(item, source)
                if not doc:
                    continue
                if with_taxonomy:
                    if doc.get("categories"):
                        doc["category_names"] = [
                            cats.get(c, f"#{c}") for c in doc["categories"]
                        ]
                    if doc.get("tags"):
                        doc["tag_names"] = [tags.get(t, f"#{t}") for t in doc["tags"]]
                docs.append(doc)
        return docs


# ─────────────────────────────  Сохранение  ─────────────────────────────


def save_documents(
    docs: list[dict[str, Any]],
    path: str | Path,
    fmt: str = "jsonl",
    *,
    append: bool = False,
    backup: bool = True,
) -> Path:
    """Сохраняет документы в jsonl / json / csv / md.

    Запись атомарная: сначала во временный файл, потом rename.
    Если скрипт упадёт или будет прерван — старый датасет останется целым.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(fh) -> None:
        if fmt == "jsonl":
            for doc in docs:
                fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
        elif fmt == "json":
            json.dump(docs, fh, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            fields = ["id", "url", "title", "date", "modified", "type", "content", "source"]
            extra = [k for k in (docs[0] if docs else {}) if k not in fields]
            writer = csv.DictWriter(fh, fieldnames=fields + extra, extrasaction="ignore")
            writer.writeheader()
            for doc in docs:
                row = dict(doc)
                for key in ("categories", "tags", "category_names", "tag_names"):
                    if isinstance(row.get(key), list):
                        row[key] = ", ".join(str(x) for x in row[key])
                writer.writerow(row)
        elif fmt == "md":
            for doc in docs:
                fh.write("---\n")
                for key in ("id", "url", "date", "type", "source"):
                    if doc.get(key) is not None:
                        fh.write(f"{key}: {doc[key]}\n")
                fh.write("---\n\n")
                fh.write(f"# {doc.get('title', '')}\n\n")
                fh.write(doc.get("content", "") + "\n\n")
        else:
            raise ScraperError(f"Неизвестный формат: {fmt}")

    if append:
        with path.open("a", encoding="utf-8") as fh:
            write(fh)
        return path

    # атомарная запись: временный файл рядом → переименование
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            write(fh)
        if backup and path.exists():
            # предыдущий датасет не выбрасываем, а сохраняем рядом
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return path


def safe_filename(name: str) -> str:
    """Имя файла без опасных символов (двоеточия, слеши и т.п.)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    return cleaned.strip("_") or "dataset"


def save_state(path: str | Path, data: dict[str, Any]) -> Path:
    """Сохраняет состояние (последние id) для инкрементального сбора."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
