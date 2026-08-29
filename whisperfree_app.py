"""Точка входа для PyInstaller.

Собирать напрямую whisperfree/__main__.py нельзя: PyInstaller запускает скрипт
как __main__ без пакетного контекста, и первый же относительный импорт падает
с «attempted relative import with no known parent package». Здесь импорт
абсолютный, поэтому пакет собирается целиком.

Для обычного запуска этот файл не нужен — есть python -m whisperfree.
"""

from whisperfree.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
