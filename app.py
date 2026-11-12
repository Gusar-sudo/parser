#!/usr/bin/env python3
"""TUI-приложение для сбора данных с WordPress-сайтов.

Запуск:  python app.py

Меню:
  • Новый сбор          — указать сайт, типы, куда сохранить и запустить
  • Догрузить новые     — инкрементальный сбор (только что появившиеся записи)
  • Типы на сайте      — посмотреть, какие типы записей доступны
  • Профили сайтов      — сохранить/загрузить/удалить настройки сайтов
  • Настройки           — задержка, таймаут, повторы, User-Agent
  • Мои датасеты        — что скачано, размер, предпросмотр
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)

from wp_scraper import (
    DEFAULT_UA,
    ScraperError,
    WordPressSite,
    load_state,
    safe_filename,
    save_state,
)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / ".parser_config.json"
SITES_DIR = BASE_DIR / "sites"
STATE_DIR = BASE_DIR / ".state"

DEFAULT_CONFIG = {
    "delay": 0.5,
    "timeout": 30,
    "retries": 3,
    "per_page": 100,
    "user_agent": DEFAULT_UA,
    "format": "jsonl",
    "types": "posts,pages",
    "with_taxonomy": True,
    "keep_html": False,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def list_profiles() -> list[dict[str, Any]]:
    if not SITES_DIR.exists():
        return []
    out = []
    for path in sorted(SITES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        data["_file"] = path
        out.append(data)
    return out


def list_datasets() -> list[dict[str, Any]]:
    out = []
    for path in sorted(BASE_DIR.glob("*.jsonl")):
        try:
            size = path.stat().st_size
            with path.open(encoding="utf-8") as f:
                count = sum(1 for _ in f)
        except OSError:
            continue
        out.append({"name": path.name, "size": size, "count": count, "path": path})
    return out


def human_size(size: float) -> str:
    num = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ТБ"


# ────────────────────────────────  Экраны  ────────────────────────────────


class MainMenu(Screen[Any]):
    """Главное меню."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    MENU = [
        ("🚀", "Новый сбор", "Указать сайт, типы записей и запустить"),
        ("🔄", "Догрузить новые", "Инкрементально: только новые записи"),
        ("🏷", "Типы на сайте", "Посмотреть доступные типы записей"),
        ("📁", "Профили сайтов", "Сохранить, загрузить или удалить настройки"),
        ("⚙", "Настройки", "Задержка, таймаут, повторы, User-Agent"),
        ("📊", "Мои датасеты", "Что скачано: размер и предпросмотр"),
        ("❓", "Помощь", "Как этим пользоваться"),
        ("✕", "Выход", "Закрыть приложение"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="menu-wrap"):
            yield Static("  СБОР ДАННЫХ С WORDPRESS-САЙТОВ", id="menu-title")
            yield Static("  ━" * 38, id="menu-line")
            for index, (icon, title, hint) in enumerate(self.MENU):
                with Horizontal(classes="menu-row"):
                    yield Button(f"{icon}  {title}", id=f"menu-{index}", classes="menu-btn")
                    yield Static(hint, classes="menu-hint")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("menu-"):
            return
        try:
            title = self.MENU[int(button_id.split("-", 1)[1])][1]
        except (ValueError, IndexError):
            return
        app = self.app
        assert isinstance(app, ParserApp)
        if title == "Новый сбор":
            app.push_screen(ScrapeScreen(mode="full"))
        elif title == "Догрузить новые":
            app.push_screen(ScrapeScreen(mode="incremental"))
        elif title == "Типы на сайте":
            app.push_screen(TypesScreen())
        elif title == "Профили сайтов":
            app.push_screen(ProfilesScreen())
        elif title == "Настройки":
            app.push_screen(SettingsScreen())
        elif title == "Мои датасеты":
            app.push_screen(DatasetsScreen())
        elif title == "Помощь":
            app.push_screen(HelpScreen())
        elif title == "Выход":
            app.exit()


class ScrapeScreen(Screen[Any]):
    """Форма сбора данных."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(self, mode: str = "full") -> None:
        super().__init__()
        self.mode = mode
        self.cfg = load_config()
        self._profiles: list[dict[str, Any]] = list_profiles()
        self._profile_choices: list[tuple[str, str]] = [("— выбрать профиль —", "")]
        self._profile_choices += [
            (p.get("url", "?"), p["_file"].stem) for p in self._profiles
        ]

    def compose(self) -> ComposeResult:
        title = "Новый сбор" if self.mode == "full" else "Догрузить новые записи"
        yield Header()
        with VerticalScroll(id="scrape-body"):
            yield Static(f"  {title}", id="scrape-title")
            if self.mode == "incremental":
                yield Static(
                    "  Загрузит только то, что появилось с прошлого раза",
                    id="scrape-hint",
                )

            with Vertical(classes="card"):
                with Horizontal(classes="full-row"):
                    yield Label("Сайт", classes="flabel")
                    with Horizontal(classes="value-row"):
                        yield Input(placeholder="https://example.ru", id="in-url")
                        yield Button("Проверить", id="btn-check", classes="tiny")
                with Horizontal(classes="full-row"):
                    yield Label("Профиль сайта", classes="flabel")
                    with Horizontal(classes="value-row"):
                        yield Select(self._profile_choices, id="in-profile",
                                     allow_blank=True, value="")
                with Horizontal(classes="full-row"):
                    yield Label("Типы записей", classes="flabel")
                    with Horizontal(classes="value-row"):
                        yield Input(value=self.cfg["types"], id="in-types")
                        yield Button("Определить", id="btn-types", classes="tiny")
                with Horizontal(classes="full-row"):
                    yield Label("Куда сохранить", classes="flabel")
                    with Horizontal(classes="value-row"):
                        yield Input(placeholder="example.ru_dataset.jsonl", id="in-out")

                with Grid(classes="form"):
                    yield Label("Формат")
                    yield Select(
                        [("jsonl", "jsonl"), ("json", "json"), ("csv", "csv"), ("md", "md")],
                        value=self.cfg["format"],
                        id="in-format",
                        allow_blank=False,
                    )
                    yield Label("Лимит записей")
                    yield Input(placeholder="пусто = все", id="in-max")

                    yield Label("Пауза, сек")
                    yield Input(value=str(self.cfg["delay"]), id="in-delay")
                    yield Label("Таймаут, сек")
                    yield Input(value=str(self.cfg["timeout"]), id="in-timeout")

            with Horizontal(classes="row"):
                yield Checkbox("Разворачивать категории/теги", value=self.cfg["with_taxonomy"], id="in-tax")
                yield Checkbox("Сохранять исходный HTML", value=self.cfg["keep_html"], id="in-html")

            with Horizontal(classes="row actions"):
                yield Button("▶  Собрать", id="btn-run", variant="success")
                yield Button("Проверить всё", id="btn-dry", variant="warning")
                yield Button("Назад", id="btn-back")

            yield Label("", id="scrape-status")
            yield RichLog(id="log", wrap=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#scrape-status", Label).update("")
        if self._profiles:
            self.query_one("#in-profile", Select).value = ""

    # ── вспомогательное ──

    def _values(self) -> dict[str, Any]:
        def num(sel: str, default: float) -> float:
            raw = self.query_one(sel, Input).value.strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        max_raw = self.query_one("#in-max", Input).value.strip()
        try:
            max_items = int(max_raw) if max_raw else None
        except ValueError:
            max_items = None

        return {
            "url": self.query_one("#in-url", Input).value.strip(),
            "types": [t.strip() for t in self.query_one("#in-types", Input).value.split(",") if t.strip()],
            "out": self.query_one("#in-out", Input).value.strip(),
            "format": self.query_one("#in-format", Select).value,
            "max_items": max_items,
            "delay": num("#in-delay", 0.5),
            "timeout": int(num("#in-timeout", 30)),
            "with_taxonomy": self.query_one("#in-tax", Checkbox).value,
            # для markdown нужен исходный HTML — иначе останется plain-текст
            "keep_html": self.query_one("#in-html", Checkbox).value
            or self.query_one("#in-format", Select).value == "md",
        }

    def _status(self, text: str, error: bool = False) -> None:
        label = self.query_one("#scrape-status", Label)
        label.update(text)
        label.set_classes(["error"] if error else ["ok"])

    def _log_direct(self, message: str) -> None:
        """Записать в лог. Вызывать только из потока приложения."""
        self.query_one("#log", RichLog).write(message)

    def _log(self, message: str) -> None:
        """Записать в лог из рабочего потока."""
        self.app.call_from_thread(self._log_direct, message)

    def _suggest_output(self, url: str) -> None:
        domain = safe_filename(url.split("//", 1)[1]) if "//" in url else "dataset"
        fmt = self.query_one("#in-format", Select).value or "jsonl"
        self.query_one("#in-out", Input).value = f"{domain}_dataset.{fmt}"

    # ── кнопки ──

    @on(Button.Pressed, "#btn-check")
    def _check(self) -> None:
        url = self.query_one("#in-url", Input).value.strip()
        if not url:
            self._status("Сначала укажите адрес сайта", error=True)
            return
        self._status("Проверяю сайт...")
        self._check_site(url)

    @work(thread=True)
    def _check_site(self, url: str) -> None:
        try:
            site = WordPressSite(url, verbose=False, logger=self._log)
            site.discover()
            types = site.list_types()
            self.app.call_from_thread(self._show_types, types)
        except ScraperError as e:
            self.app.call_from_thread(self._status, f"❌ {e}", True)

    def _show_types(self, types: dict[str, str]) -> None:
        self.query_one("#in-types", Input).value = ", ".join(types)
        self._status(f"✅ Найдено типов: {len(types)} → {', '.join(types)}")

    @on(Button.Pressed, "#btn-types")
    def _detect_types(self) -> None:
        url = self.query_one("#in-url", Input).value.strip()
        if not url:
            self._status("Сначала укажите адрес сайта", error=True)
            return
        self._status("Определяю типы записей...")
        self._check_site(url)

    @on(Input.Changed, "#in-url")
    def _url_changed(self) -> None:
        if not self.query_one("#in-out", Input).value:
            self._suggest_output(self.query_one("#in-url", Input).value.strip())

    @on(Select.Changed, "#in-format")
    def _format_changed(self) -> None:
        url = self.query_one("#in-url", Input).value.strip()
        if url:
            self._suggest_output(url)

    @on(Select.Changed, "#in-profile")
    def _profile_changed(self, event: Select.Changed) -> None:
        if not event.value:
            return
        for profile in self._profiles:
            if profile["_file"].stem == event.value:
                self.query_one("#in-url", Input).value = profile.get("url", "")
                self.query_one("#in-types", Input).value = ", ".join(profile.get("types", []))
                self.query_one("#in-delay", Input).value = str(profile.get("delay", 0.5))
                self.query_one("#in-timeout", Input).value = str(profile.get("timeout", 30))
                if profile.get("out"):
                    self.query_one("#in-out", Input).value = profile["out"]
                self._status(f"✅ Профиль «{event.value}» загружен")

    @on(Button.Pressed, "#btn-back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#btn-run")
    def _run(self) -> None:
        self._start(dry=False)

    @on(Button.Pressed, "#btn-dry")
    def _dry(self) -> None:
        self._start(dry=True)

    def _start(self, dry: bool) -> None:
        v = self._values()
        if not v["url"]:
            self._status("Укажите адрес сайта", error=True)
            return
        if not v["types"]:
            self._status("Укажите хотя бы один тип записей", error=True)
            return
        self.query_one("#log", RichLog).clear()
        self._status("Запускаю..." if not dry else "Проверяю (ничего не сохраняю)...")
        self._scrape(v, dry)

    @work(thread=True)
    def _scrape(self, v: dict[str, Any], dry: bool) -> None:
        cfg = load_config()
        try:
            site = WordPressSite(
                v["url"],
                delay=v["delay"],
                timeout=v["timeout"],
                retries=cfg["retries"],
                user_agent=cfg["user_agent"],
                verbose=False,
                logger=self._log,
            )
            site.discover()

            last_ids = {}
            state_path = None
            if self.mode == "incremental":
                state_path = STATE_DIR / f"{safe_filename(site.base_url)}.json"
                state = load_state(state_path)
                last_ids = state.get(site.base_url, {}).get("types", {})

            docs = site.scrape(
                v["types"],
                per_page=cfg["per_page"],
                max_items=v["max_items"],
                last_ids=last_ids or None,
                with_taxonomy=v["with_taxonomy"],
                newest_first=self.mode == "incremental",
                keep_html=v["keep_html"],
            )

            if not docs:
                if self.mode == "incremental":
                    self.app.call_from_thread(self._status, "✅ Новых записей нет", False)
                else:
                    self.app.call_from_thread(self._status, "❌ Не удалось получить записи", True)
                return

            out_name = v["out"] or f"{safe_filename(site.base_url.split('//', 1)[1])}_dataset.{v['format']}"
            out_path = Path(out_name)
            if not out_path.is_absolute():
                out_path = BASE_DIR / out_path

            if dry:
                self.app.call_from_thread(
                    self._status,
                    f"👀 Будет сохранено {len(docs)} документов → {out_path.name} "
                    f"(данные НЕ сохранены)",
                    False,
                )
                self.app.call_from_thread(self._log_direct, f"Пример: {json.dumps(docs[0], ensure_ascii=False)[:300]}")
                return

            from wp_scraper import save_documents

            save_documents(docs, out_path, v["format"], backup=True)
            chars = sum(len(d.get("content", "")) for d in docs)
            self.app.call_from_thread(
                self._status,
                f"✅ Сохранено {len(docs)} документов → {out_path.name} "
                f"(~{chars:,} символов)",
                False,
            )

            if self.mode == "incremental" and state_path:
                state = load_state(state_path)
                bucket = state.setdefault(site.base_url, {"types": {}})
                for t in v["types"]:
                    new_id = site.last_ids.get(t)
                    if new_id:
                        bucket["types"][t] = max(new_id, bucket["types"].get(t, 0))
                save_state(state_path, state)
                self.app.call_from_thread(self._log_direct, f"🗂  Состояние: {state_path.name}")

        except ScraperError as e:
            self.app.call_from_thread(self._status, f"❌ {e}", True)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._status, f"❌ Непредвиденная ошибка: {e}", True)


class TypesScreen(Screen[Any]):
    """Показать типы записей на указанном сайте."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static("  Типы записей на сайте", id="scrape-title")
            with Vertical(classes="card"):
                yield Input(placeholder="https://example.ru", id="in-url2")
                yield Button("Показать типы", id="btn-show", variant="primary")
            yield RichLog(id="log2", wrap=True)
        yield Footer()

    @on(Button.Pressed, "#btn-show")
    def _show(self) -> None:
        url = self.query_one("#in-url2", Input).value.strip()
        if not url:
            self.notify("Введите адрес сайта", severity="warning")
            return
        self._fetch(url)

    @work(thread=True)
    def _fetch(self, url: str) -> None:
        log = self.query_one("#log2", RichLog)
        log.clear()
        try:
            site = WordPressSite(url, verbose=False)
            site.discover()
            types = site.list_types()
            self.app.call_from_thread(log.write, f"REST API: {site.api_root}")
            for slug, rest in types.items():
                self.app.call_from_thread(log.write, f"  • {slug:<20} → /wp-json/wp/v2/{rest}")
        except ScraperError as e:
            self.app.call_from_thread(log.write, f"❌ {e}")


class ProfilesScreen(Screen[Any]):
    """Профили сайтов."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("  Профили сайтов", id="scrape-title")
            yield Static(
                "  Профиль — сохранённые настройки сайта: типы, пауза, файл вывода.",
                id="scrape-hint",
            )
            yield DataTable(id="profiles-table")
            with Horizontal(classes="row actions"):
                yield Button("Открыть в форме", id="btn-use", variant="primary")
                yield Button("Удалить", id="btn-del", variant="error")
                yield Button("Назад", id="btn-back")
        yield Footer()

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Имя", "Сайт", "Типы", "Файл")
        for profile in list_profiles():
            table.add_row(
                profile["_file"].stem,
                profile.get("url", ""),
                ", ".join(profile.get("types", [])),
                profile.get("out", "—"),
            )

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#profiles-table", DataTable)
        if not table.row_count:
            self.notify("Профилей пока нет", severity="warning")
            return None
        row = table.cursor_row
        profiles = list_profiles()
        return profiles[row] if 0 <= row < len(profiles) else None

    @on(Button.Pressed, "#btn-use")
    def _use(self) -> None:
        profile = self._selected()
        if not profile:
            return
        app = self.app
        assert isinstance(app, ParserApp)
        screen = ScrapeScreen(mode="full")
        app.push_screen(screen)
        screen.call_after_refresh(self._fill, profile)

    def _fill(self, profile: dict[str, Any]) -> None:
        screen = self.app.screen
        if not isinstance(screen, ScrapeScreen):
            return
        screen.query_one("#in-url", Input).value = profile.get("url", "")
        screen.query_one("#in-types", Input).value = ", ".join(profile.get("types", []))
        screen.query_one("#in-delay", Input).value = str(profile.get("delay", 0.5))
        screen.query_one("#in-timeout", Input).value = str(profile.get("timeout", 30))
        if profile.get("out"):
            screen.query_one("#in-out", Input).value = profile["out"]

    @on(Button.Pressed, "#btn-del")
    def _del(self) -> None:
        profile = self._selected()
        if not profile:
            return
        try:
            profile["_file"].unlink()
        except OSError as e:
            self.notify(f"Не удалось удалить: {e}", severity="error")
            return
        self._reload()
        self.notify(f"Профиль «{profile['_file'].stem}» удалён", severity="information")

    @on(Button.Pressed, "#btn-back")
    def _back(self) -> None:
        self.app.pop_screen()


class SettingsScreen(Screen[Any]):
    """Глобальные настройки."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        cfg = load_config()
        yield Header()
        with VerticalScroll():
            yield Static("  Настройки", id="scrape-title")
            with Vertical(classes="card"):
                yield Label("Пауза между запросами, сек")
                yield Input(value=str(cfg["delay"]), id="in-delay2")
                yield Label("Таймаут ответа, сек")
                yield Input(value=str(cfg["timeout"]), id="in-timeout2")
                yield Label("Сколько раз повторять при обрыве сети")
                yield Input(value=str(cfg["retries"]), id="in-retries")
                yield Label("Размер страницы API")
                yield Input(value=str(cfg["per_page"]), id="in-perpage")
                yield Label("User-Agent")
                yield Input(value=cfg["user_agent"], id="in-ua")
                yield Label("Типы записей по умолчанию")
                yield Input(value=cfg["types"], id="in-types2")
                yield Label("Формат по умолчанию")
                yield Select(
                    [("jsonl", "jsonl"), ("json", "json"), ("csv", "csv"), ("md", "md")],
                    value=cfg["format"], id="in-format2", allow_blank=False,
                )
            with Horizontal(classes="row actions"):
                yield Button("💾  Сохранить", id="btn-save", variant="success")
                yield Button("Назад", id="btn-back")
            yield Label("", id="set-status")
        yield Footer()

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        cfg = load_config()

        def num(sel: str, default: float) -> float:
            raw = self.query_one(sel, Input).value.strip()
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        cfg.update({
            "delay": num("#in-delay2", 0.5),
            "timeout": int(num("#in-timeout2", 30)),
            "retries": int(num("#in-retries", 3)),
            "per_page": int(num("#in-perpage", 100)),
            "user_agent": self.query_one("#in-ua", Input).value.strip() or DEFAULT_UA,
            "types": self.query_one("#in-types2", Input).value.strip() or "posts,pages",
            "format": self.query_one("#in-format2", Select).value or "jsonl",
        })
        save_config(cfg)
        self.query_one("#set-status", Label).update("✅ Сохранено")
        self.query_one("#set-status", Label).set_classes(["ok"])

    @on(Button.Pressed, "#btn-back")
    def _back(self) -> None:
        self.app.pop_screen()


class DatasetsScreen(Screen[Any]):
    """Что уже скачано."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static("  Мои датасеты", id="scrape-title")
            yield DataTable(id="ds-table")
            with Horizontal(classes="row actions"):
                yield Button("Показать начало файла", id="btn-peek", variant="primary")
                yield Button("Открыть папку", id="btn-open")
                yield Button("Назад", id="btn-back")
            yield RichLog(id="log3", wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        table = self.query_one("#ds-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Файл", "Документов", "Размер")
        for ds in list_datasets():
            table.add_row(ds["name"], str(ds["count"]), human_size(ds["size"]))

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#ds-table", DataTable)
        if not table.row_count:
            self.notify("Датасетов пока нет", severity="warning")
            return None
        row = table.cursor_row
        datasets = list_datasets()
        return datasets[row] if 0 <= row < len(datasets) else None

    @on(Button.Pressed, "#btn-peek")
    def _peek(self) -> None:
        ds = self._selected()
        if not ds:
            return
        log = self.query_one("#log3", RichLog)
        log.clear()
        try:
            with ds["path"].open(encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i >= 3:
                        break
                    data = json.loads(line)
                    title = str(data.get("title", ""))[:70]
                    content = str(data.get("content", ""))[:200].replace("\n", " ")
                    log.write(f"[b]{data.get('id')}[/b] {title}")
                    log.write(f"    {content}")
        except (OSError, ValueError) as e:
            log.write(f"❌ {e}")

    @on(Button.Pressed, "#btn-open")
    def _open(self) -> None:
        import subprocess

        try:
            subprocess.Popen(["xdg-open", str(BASE_DIR)])
        except OSError:
            self.notify("Не удалось открыть папку", severity="warning")

    @on(Button.Pressed, "#btn-back")
    def _back(self) -> None:
        self.app.pop_screen()


class HelpScreen(Screen[Any]):
    """Справка."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static("  Помощь", id="scrape-title")
            yield Static(
                """
[b]Как пользоваться[/b]

1. [b]Новый сбор[/b] — вставьте адрес сайта, нажмите «Проверить»,
   затем «Определить с сайта» (подтянет типы записей) и «Собрать».
2. [b]Догрузить новые[/b] — запускает инкрементальный сбор:
   скачает только записи, которых не было в прошлый раз.
3. [b]Типы на сайте[/b] — покажет, какие типы записей доступны
   (posts, pages, products, news и любые кастомные).
4. [b]Профили сайтов[/b] — сохраните настройки часто используемого сайта,
   чтобы не вводить их каждый раз.
5. [b]Настройки[/b] — пауза, таймаут, повторы, User-Agent по умолчанию.
6. [b]Мои датасеты[/b] — список скачанных файлов и предпросмотр.

[b]Подсказки[/b]

• «Проверить всё» — показать, что будет скачано, ничего не сохраняя.
• Esc — вернуться назад.
• Ctrl+C — выйти из приложения.

[b]Горячие клавиши[/b]

• F2 — новый сбор
• F3 — догрузить новые
• F8 — мои датасеты
""",
                id="help-text",
            )
        yield Footer()


# ────────────────────────────────  Приложение  ────────────────────────────


class ParserApp(App[Any]):
    """TUI-приложение парсера."""

    TITLE = "WP-парсер"
    SUB_TITLE = "сбор данных с WordPress-сайтов"

    BINDINGS = [
        Binding("f2", "scrape", "Новый сбор"),
        Binding("f3", "incremental", "Догрузить"),
        Binding("f8", "datasets", "Датасеты"),
        Binding("ctrl+c", "quit", "Выход"),
    ]

    CSS = """
    Screen { background: $surface; }
    #menu-wrap { align: center middle; width: 72; }
    #menu-title { color: $accent; text-style: bold; margin-bottom: 1; }
    #menu-line { color: $panel; margin-bottom: 1; }
    .menu-row { height: 3; }
    .menu-btn { width: 28; min-width: 28; }
    .menu-hint { color: $text-muted; padding: 1 0 0 2; }
    #scrape-title { color: $accent; text-style: bold; margin: 1 0; }
    #scrape-hint { color: $text-muted; margin-bottom: 1; }
    .card {
        border: round $panel; padding: 1 2; margin-bottom: 1;
        background: $panel; height: auto;
    }
    .card Label { color: $text-muted; margin-top: 1; }
    .form {
        grid-size: 4; grid-columns: 17 1fr 17 1fr; grid-rows: 3 3; grid-gutter: 0 2;
        height: auto; margin-top: 1;
    }
    .form Label { color: $text-muted; content-align: left middle; }
    .form Input, .form Select { margin-bottom: 0; height: 3; }
    .full-row { height: 3; }
    .flabel { width: 17; min-width: 17; color: $text-muted; content-align: left middle; }
    .value-row { height: 3; width: 1fr; }
    .value-row Input, .value-row Select { width: 1fr; }
    .value-row Button { width: 14; min-width: 14; height: 3; margin-left: 1; }
    .tiny { height: 3; min-height: 3; border: none; background: $boost; }
    .row { height: auto; }
    .col { width: 1fr; margin-right: 2; }
    Input, Select { margin-bottom: 1; height: 3; }
    .actions { height: auto; margin-top: 1; }
    .actions Button { margin-right: 2; }
    #log, #log2, #log3 {
        height: 9; border: round $panel; background: $panel; margin-top: 1;
    }
    #scrape-status, #set-status { height: auto; margin: 1 0; }
    #scrape-status.ok, #set-status.ok { color: $success; }
    #scrape-status.error { color: $error; }
    DataTable { height: 1fr; margin-bottom: 1; }
    #help-text { margin: 1 2; }
    """

    def on_mount(self) -> None:
        self.push_screen(MainMenu())

    def action_scrape(self) -> None:
        self.push_screen(ScrapeScreen(mode="full"))

    def action_incremental(self) -> None:
        self.push_screen(ScrapeScreen(mode="incremental"))

    def action_datasets(self) -> None:
        self.push_screen(DatasetsScreen())


def main() -> int:
    ParserApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
