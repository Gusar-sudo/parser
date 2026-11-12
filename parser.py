import json
import re
import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://bmtriz.ru"
API_POSTS = f"{BASE_URL}/wp-json/wp/v2/posts"
API_PAGES = f"{BASE_URL}/wp-json/wp/v2/pages"

# Заголовки, чтобы выглядеть как браузер (иногда помогает)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LLM-Data-Collector/1.0)"}


def clean_html(html_content):
    """Извлекает чистый текст из HTML-фрагмента."""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    # Удаляем скрипты и стили, если вдруг попались
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Убираем лишние пустые строки и пробелы
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def fetch_all_pages(api_url, per_page=100):
    """Забирает все объекты с пагинацией."""
    all_items = []
    page = 1
    while True:
        params = {
            "per_page": per_page,
            "page": page,
            "_fields": "id,date,link,title,content,categories,tags,type,slug",
        }
        try:
            resp = requests.get(api_url, headers=HEADERS, params=params, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 400 and "rest_post_invalid_page_number" in resp.text:
                break  # страницы закончились
            print(f"Ошибка запроса: {e}")
            break
        data = resp.json()
        if not data:
            break
        all_items.extend(data)
        print(f"  Загружено {len(all_items)} записей (страница {page})")
        page += 1
        time.sleep(0.5)  # вежливая пауза
    return all_items


def extract_document(item):
    """Преобразует объект WordPress в словарь для LLM."""
    title = clean_html(item.get("title", {}).get("rendered", ""))
    content_html = item.get("content", {}).get("rendered", "")
    content_text = clean_html(content_html)
    # Пропускаем пустые документы
    if not title and not content_text:
        return None
    return {
        "id": item.get("id"),
        "url": item.get("link"),
        "title": title,
        "content": content_text,
        "date": item.get("date"),
        "type": item.get("type"),
        "categories": item.get("categories", []),
        "tags": item.get("tags", []),
        "source": "bmtriz.ru",
    }


def main():
    documents = []

    print("📥 Загружаем записи (posts)...")
    posts = fetch_all_pages(API_POSTS)
    print(f"✅ Всего записей: {len(posts)}")

    print("📥 Загружаем страницы (pages)...")
    pages = fetch_all_pages(API_PAGES)
    print(f"✅ Всего страниц: {len(pages)}")

    for item in posts + pages:
        doc = extract_document(item)
        if doc:
            documents.append(doc)

    # Сохраняем в JSONL
    output_file = "bmtriz_dataset.jsonl"
    with open(output_file, "w", encoding="utf-8") as f:
        for doc in documents:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    print(f"\n💾 Сохранено {len(documents)} документов в {output_file}")
    total_chars = sum(len(d["content"]) for d in documents)
    print(f"📊 Общий объём текста: ~{total_chars:,} символов")


if __name__ == "__main__":
    main()
