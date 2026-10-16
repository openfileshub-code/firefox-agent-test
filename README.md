# 🦊 Firefox Agent MCP Tools

**Набор MCP-инструментов (Model Context Protocol) для интеллектуального управления антидетект-браузером Camoufox/Firefox с помощью ИИ-агентов.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)]()
[![MCP](https://img.shields.io/badge/Protocol-MCP-green?style=flat-square)]()
[![Camoufox](https://img.shields.io/badge/Browser-Camoufox-orange?style=flat-square)]()
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)]()

---

## 📖 О проекте

**Этот проект находится в стадии РАННЕГО предварительного тестирования и является пока довольно сырым (raw/prototype stage).**

✅ Проект предоставляет функционал для управления браузером Firefox через MCP (Model Context Protocol) интерфейс с использованием Camoufox.

❌ **НЕ ИСПОЛЬЗУЙТЕ ЭТОТ ПРОЕКТ В ПРОИЗВОДСТВЕННЫХ СРЕДАХ.** Код находится в активной разработке, может содержать баги, неполный функционал или нестабильные реализации.

🔬 Проект предназначен для тестирования, исследования и разработки. Любое использование "как есть" осуществляется на ваш собственный риск.

---

`firefox-agent-test` — это продвинутая архитектура, состоящая из **MCP-сервера** и **независимого браузерного демона**. Проект решает главную проблему автоматизации браузеров через ИИ: таймауты и закрытие сессий. Демон работает **отдельно от MCP-хоста** (Claude Desktop, Cursor, Unsloth, Qwen и др.), что позволяет сохранять профиль, куки и открытые вкладки даже после завершения сессии ИИ-агента или закрытия программы-хоста.

Проект включает встроенный **Agent Skill** (`SKILL.md`) — системный промпт, который задаёт ИИ-агенту строгий протокол работы с браузером: правило одного хода, политику одной вкладки, защиту от слепого повтора действий и правила обработки видео, форм и капчи.

### ✨ Ключевые возможности
- 🎥 **Управление видео** — воспроизведение, качество, субтитры, обход меню, переключение театрального режима (YouTube, Twitch, Rutube, VK Video) через систему адаптеров платформ.
- 🪟 **Менеджер окон** — сохранение и восстановление геометрии и позиций окон.
- 🔗 **Умный поиск и навигация** — нечеткий поиск ссылок, поддержка JS-вкладок, **глубокое сканирование Shadow DOM**, раскрытие скрытых меню и дропдаунов.
- 📝 **Работа с формами** — анализ и заполнение с обходом базовых бот-ловушек, детекция капчи.
- 🛡️ **Policy Guard и Context Manager** — система политик, предотвращающая слепые клики по скрытым элементам, контролирующая контекст страницы (watch/playlist/radio) и защищающая от случайных действий.
- ⚙️ **Пользовательские настройки** — конфигурация `user_tool_settings.json` с глобальными политиками, site-specific intents и override-фразами.
- 📸 **Скриншоты и JS** — выполнение произвольного JavaScript и снятие снапшотов страницы.
- ↩️ **Навигация по истории** — шаги назад и вперёд по истории вкладки (`browser_go_back`, `browser_go_forward`).

---

## 🏗️ Архитектура и структура

```text
firefox-agent-test/
├── server.py                          # MCP-сервер (stdio, связывается с ИИ-хостом)
├── browserd.py                        # Браузерный демон (TCP-сервер, управляет Playwright)
├── daemon_control.py                  # Скрипт управления демоном (start/stop/status)
├── start_browser.vbs                  # Скрытый запуск демона (Windows)
├── stop_browser.vbs                   # Скрытая остановка демона (Windows)
├── requirements.txt                   # Зависимости Python
├── pytest.ini                         # Конфигурация тестов
├── SKILL.md                           # Системный промпт / Agent Skill для ИИ-агента
├── config/
│   ├── sites.json                     # Алиасы сайтов
│   ├── window.json                    # Настройки окон
│   ├── player_selectors.json          # CSS-селекторы видеоплееров
│   ├── synonyms.json                  # Словарь синонимов для fuzzy-поиска
│   ├── user_tool_settings.json        # Активные пользовательские настройки политик
│   ├── user_tool_settings.example.json  # Шаблон настроек
│   └── user_tool_settings.schema.json # JSON-схема для валидации настроек
├── modules/
│   ├── context_manager.py             # Отслеживание контекста страницы (watch/playlist/radio, скрытые элементы)
│   ├── policy_guard.py                # Система защитных политик и правил
│   ├── settings_loader.py             # Загрузчик и fallback настроек
│   ├── video_controller.py            # Контроллер HTML5-видео и адаптеров платформ
│   ├── link_cache.py                  # Кэш ссылок и интерактивных элементов
│   ├── page_agent.py                  # Агент взаимодействия со страницей
│   ├── forms.py                       # Работа с формами и полями ввода
│   ├── text_utils.py                  # Утилиты обработки текста (лемматизация через pymorphy3)
│   ├── url_validator.py               # Валидация URL
│   ├── window_manager.py              # Управление окнами браузера
│   └── websocket_monitor.py           # Мониторинг WebSocket-событий
├── adapters/                          # Адаптеры для специфичных сайтов (Rutube, VK и др.)
├── tests/                             # Unit и Live тесты (pytest)
├── docs/                              # Документация
├── logs/                              # Runtime-логи (в git не попадают, см. logs/README.md)
└── playwright_profile/                # Персистентный профиль браузера (создается автоматически)
```

---

## ⚙️ Установка (Пошаговое руководство)

> **Важно:** Проект оптимизирован для ОС **Windows** из-за использования VBS-скриптов и Планировщика заданий, но базовый функционал работает и на Linux/macOS.

### 1. Подготовка окружения
Убедитесь, что у вас установлен Python 3.10+ и Git. Включите в Windows отображение расширений файлов!
```powershell
$Project = "E:\Program\AI Exlinks\firefox-agent-test"
git clone https://github.com/openfileshub-code/firefox-agent-test.git $Project
Test-Path $Project
py --version
```
Первая команда должна вернуть `True`. Если `py --version` не сработает, попробуйте `python --version`.

### 2. Создание виртуального окружения
```powershell
py -3 -m venv "$Project\.venv"
```
Убедитесь, что интерпретатор появился: `Test-Path "$Project\.venv\Scripts\python.exe"` (должно вернуть `True`).

### 3. Установка зависимостей
Активируйте виртуальное окружение и установите все пакеты из `requirements.txt`:
```powershell
& "$Project\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$Project\.venv\Scripts\python.exe" -m pip install -r "$Project\requirements.txt"
```

Текущий список зависимостей (`requirements.txt`):
| Пакет | Версия | Назначение |
| :--- | :--- | :--- |
| `camoufox[geoip]` | `>=0.4.0` | Антидетект-браузер на базе Firefox + GeoIP |
| `mcp[cli]` | `>=1.0.0` | Model Context Protocol SDK (FastMCP) |
| `playwright` | `>=1.40.0` | Движок автоматизации браузера |
| `pymorphy3` | `>=2.0.0` | Морфологический анализатор для русского языка (fuzzy-поиск, леммы) |
| `orjson` | `>=3.9.0` | Опциональное ускорение JSON-сериализации (graceful fallback на stdlib `json`) |

### 4. Загрузка бинарников Camoufox
Этот шаг обязателен для скачивания движка браузера:
```powershell
& "$Project\.venv\Scripts\python.exe" -m camoufox fetch
```
> 🛑 *Если эта команда завершается ошибкой, не переходите к следующему шагу, пока не исправите проблему в консоли.*

---

## 🧠 Активация Agent Skill (SKILL.md)

Файл `SKILL.md` содержит системный промпт (Agent Skill), который обучает ИИ-агента правильному протоколу работы с браузером: правилу одного хода, строгой политике одной вкладки, защите от слепых повторов и правилам работы с видео, формами и капчей.

### Для Unsloth
Если вы используете среду **Unsloth** (как в текущем окружении), скилл активируется автоматически при регистрации в каталоге навыков платформы. Поместите репозиторий в доступную директорию, и Unsloth подхватит `SKILL.md` как enabled skill `firefox-agent-browser`.

### Для других MCP-хостов (Claude Desktop, Cursor и т.д.)
1. Скопируйте путь к файлу `SKILL.md` (например, `E:\Program\AI Exlinks\firefox-agent-test\SKILL.md`).
2. Добавьте содержимое `SKILL.md` в системный промпт вашего агента или подключите его через механизм пользовательских инструкций вашей платформы.
3. Убедитесь, что агент имеет доступ к MCP-инструментам, описанным в скилле.

> 💡 **Рекомендация:** Не редактируйте `SKILL.md` напрямую без понимания архитектуры. Для пользовательских настроек поведения используйте `config/user_tool_settings.json`.

---

## 🚀 Настройка и Запуск

Архитектура требует **раздельного запуска** демона и MCP-сервера. `browserd.py` должен работать отдельно, тогда закрытие MCP-сессии или её таймаут не остановят демон.

### Шаг 1. Первый ручной запуск демона (для проверки)
Откройте PowerShell в папке проекта:
```powershell
& "$Project\.venv\Scripts\python.exe" "$Project\browserd.py" --profile "$Project\playwright_profile"
```
**Ожидаемое поведение:**
1. Откроется окно Camoufox/Firefox.
2. В папке `logs/` появится `browserd.log`.
3. В `playwright_profile/` создастся `.browserd_state.json`.

*Оставьте это окно PowerShell открытым на время проверки. **Не запускайте второй экземпляр демона с тем же профилем**!*

### Шаг 2. Настройка MCP-хоста (Claude Desktop, Cursor, Unsloth и т.д.)
Добавьте сервер в конфигурацию вашего MCP-хоста (`mcpServers`). Укажите интерпретатор из виртуального окружения:

```json
{
  "mcpServers": {
    "firefox-agent": {
      "command": "E:\\Program\\AI Exlinks\\firefox-agent-test\\.venv\\Scripts\\python.exe",
      "args": [
        "E:\\Program\\AI Exlinks\\firefox-agent-test\\server.py"
      ],
      "cwd": "E:\\Program\\AI Exlinks\\firefox-agent-test"
    }
  }
}
```
> ⚠️ **Внимание:** `server.py` использует стандартный ввод-вывод (`stdio`) для связи с хостом. **Никогда** не запускайте `server.py` через `pythonw.exe` и не перенаправляйте его `stdout` в файл! Хост должен общаться с сервером по MCP stdio.

### Шаг 3. Тестирование связи
1. Перезапустите MCP-хост.
2. Вызовите инструмент `browser_start` — он подключится к уже запущенному вручную демону.
3. Используйте `browser_navigate` для открытия сайта.
4. Завершите сессию MCP-хоста. **Окно браузера должно остаться открытым!**

---

## 🔄 Автоматизация и Управление (Windows)

Чтобы демон запускался в фоне без черного окна консоли и переживал перезапуски MCP-хоста, используйте управляющий скрипт `daemon_control.py`.

### 1. Создание ярлыков (VBS)
В репозитории уже есть `start_browser.vbs` и `stop_browser.vbs`.
> 🇷🇺 **Критически важно для русских сообщений:** Откройте `.vbs` файлы в Блокноте и сохраните их в кодировке **UTF-16 LE (Файл → Сохранить как → Кодировка: «Юникод»)**. Иначе классический Windows Script Host неверно прочитает кириллицу и в `MsgBox` отобразятся кракозябры. (Python-файлы при этом оставляйте в обычном UTF-8).

Создайте ярлыки для этих `.vbs` файлов на Рабочем столе:
- 🟢 **Запустить Camoufox** (`start_browser.vbs`)
- 🔴 **Остановить Camoufox** (`stop_browser.vbs`)

*Не помещайте ярлыки в другую папку, если потом собираетесь перемещать сами `.vbs`: скрипты определяют каталог проекта относительно собственного расположения.*

### 2. Автозапуск через Планировщик заданий
1. Откройте **Планировщик заданий Windows** -> **Создать задачу**.
2. **Общие:** Имя `Camoufox Browser Daemon`, галочка *"Выполнять только для вошедшего пользователя"* (иначе браузер окажется в невидимом пользовательском сеансе и будет невидимым).
3. **Триггеры:** *При входе в систему* для вашей учетной записи.
4. **Действия:** Запуск программы:
   - **Программа:** `E:\...\firefox-agent-test\.venv\Scripts\pythonw.exe`
   - **Аргументы:** `"E:\...\firefox-agent-test\browserd.py" --profile "E:\...\firefox-agent-test\playwright_profile"`
   - **Рабочая папка:** `E:\...\firefox-agent-test`
5. **Условия:** Снять галочку *"Запускать только при питании от электросети"*.
6. **Параметры:** Снять галочку *"Остановить задачу, если она выполняется дольше..."*. Выбрать *"Не запускать новый экземпляр"*.

---

## 🛠️ Использование MCP-инструментов

После подключения к демону через `browser_start`, ИИ-агенту доступны следующие инструменты (актуальный список из `server.py`):

### Основные инструменты

| Инструмент | Описание |
| :--- | :--- |
| `browser_start` | Подключение к работающему демону (TCP). |
| `browser_stop` | **Штатная** и безопасная остановка браузера с сохранением сессии. |
| `browser_navigate(url, new_tab=False)` | Открытие URL в текущей или новой вкладке. |
| `browser_go_back(timeout=45)` | Шаг назад по истории вкладки. |
| `browser_history_back(timeout=45)` | Синоним `browser_go_back`. |
| `browser_go_forward(timeout=45)` | Шаг вперёд по истории вкладки. |
| `browser_tabs` | Получить перечень вкладок. |
| `browser_new_tab(url="")` | Создать новую вкладку. |
| `browser_close_tab(index=-1)` | Закрыть вкладку по индексу. |
| `browser_switch_tab(index)` | Переключиться на вкладку по индексу. |
| `browser_click_text(text, index=0, selector="")` | Клик по тексту элемента или CSS-селектору. |
| `browser_click_selector(selector)` | Клик по CSS-селектору (для кнопок/иконок без текста). |
| `browser_press_key(key)` | Нажатие клавиши (Enter, Escape и т.д.). |
| `browser_type_text(selector, text, clear=True)` | Заполнение поля по CSS-селектору. |
| `browser_scroll(direction, amount)` | Прокрутка страницы. |
| `browser_get_content(max_chars=8000, mode="text")` | Получение текста или HTML страницы. |
| `browser_screenshot(full_page=False)` | Снимок страницы. |
| `browser_search_on_page(pattern, regex=False)` | Поиск текста на текущей странице. |
| `browser_find_link(pattern="")` | Нечёткий поиск ссылок и интерактивных элементов. |
| `browser_eval(js)` | Выполнение произвольного JavaScript (заблокирован для управления видео). |

### Видео и медиа

| Инструмент | Описание |
| :--- | :--- |
| `browser_video(action, value=None)` | Управление видеоплеером через систему адаптеров (YouTube, Twitch, VK Video, Rutube, generic HTML5). Поддерживает: play, pause, volume, seek, state, set_quality, toggle_theater_mode, next_video, toggle_chat_visibility, fullscreen_enter, detect_login. |
| `browser_expand_menu(trigger_text, mode="hover", wait_ms=1500, zone_selector="")` | Раскрытие скрытых меню/дропдаунов с последующим сканированием новых элементов (включая Shadow DOM). |

### Расширенные инструменты (доступны при регистрации в MCP-хосте)

| Инструмент | Описание |
| :--- | :--- |
| `browser_inspect_page(include_frames=True)` | Инвентаризация интерактивных элементов и iframe. |
| `browser_search_text(query, include_frames=True, limit=30)` | Поиск текста, в том числе в iframe. |
| `browser_scroll_to_text(text)` | Прокрутка к найденному тексту. |
| `browser_form_inspect()` | Просмотр полей формы и признаков капчи. |
| `browser_form_fill(values)` | Заполнение найденных полей формы. |
| `browser_websocket_events(limit=50)` | Метаданные WebSocket-событий. |
| `browser_network_errors()` | Сетевые ошибки и HTTP 4xx/5xx. |
| `browser_dialog_state()` | Проверка ожидающего браузерного диалога. |
| `browser_dialog_resolve(action, prompt_text="")` | Ответ на диалог. |

---

## ⚙️ Пользовательские настройки (user_tool_settings.json)

Поведение Policy Guard и Context Manager настраивается через файл `config/user_tool_settings.json`. Он поддерживает:
- **Глобальные политики** — правила работы со скрытыми элементами, повторами и контекстом.
- **Site-specific intents** — специфичные настройки для отдельных сайтов (например, YouTube).
- **Override-фразы** — пользовательские фразы для переопределения поведения агента.
- **Debug-настройки** — параметры отладки.

Для начала работы скопируйте шаблон:
```powershell
Copy-Item "$Project\config\user_tool_settings.example.json" "$Project\config\user_tool_settings.json"
```
Валидация настроек осуществляется по схеме `config/user_tool_settings.schema.json`. При отсутствии файла система использует безопасные значения по умолчанию (fallback).

---

## 🩺 Диагностика и Логи

Если что-то пошло не так, **не убивайте процессы принудительно** через Диспетчер задач! Это может повредить профиль браузера и оставить lock-файлы. Код не удаляет их автоматически в целях безопасности (иначе можно нарушить работу живой сессии).

**Где искать правду (все логи пишутся в папку `logs/`):**
- `logs/browserd.log` — основные ошибки демона, падения Playwright, проблемы с профилем.
- `logs/mcp_debug.log` — лог взаимодействия MCP-сервера и хоста (ротация до 5 МБ, 3 копии).
- `logs/daemon_control.log` — лог работы VBS/BAT скриптов.
- `logs/tool_audit.jsonl` — аудит каждого tool call (решения PolicyGuard).
- `playwright_profile/.browserd_state.json` — текущий статус демона (PID, TCP-порт, токен, версия протокола v4).

**Частые проблемы:**
- **MCP-хост пишет "Демон не найден":** Проверьте, запущен ли `pythonw.exe` / `browserd.py` в Диспетчере задач. Убедитесь, что `server.py` и `browserd.py` смотрят на одну и ту же папку `playwright_profile`. Проверьте, что порт в state-файле ещё доступен.
- **Ошибка версии протокола:** Демон и MCP-сервер используют `PROTOCOL_VERSION = 4`. Если версии не совпадают, остановите старый демон и запустите новую версию.
- **Браузер закрывается при закрытии хоста:** Значит, вы запустили `browserd.py` из того же терминала, что и MCP-хост, или Планировщик заданий не настроен. Демон должен быть независимым процессом.
- **VBS выдает кракозябры:** Сохраните `.vbs` в кодировке "Юникод" (UTF-16 LE с BOM).
- **Ошибка "Профиль уже используется":** Демон не завершился штатно. Проверьте `logs/browserd.log`, найдите процесс в Диспетчере задач и завершите его, затем удалите `parent.lock` в папке `playwright_profile`.
- **`start_browser.vbs` не запускает второй демон:** Это защитная мера. Если state-файл указывает на живой, но недоступный процесс, скрипт откажется запускать новый демон, чтобы не повредить профиль.

---

## 🧪 Тестирование

Проект включает набор unit и live тестов. Конфигурация находится в `pytest.ini`.
```powershell
& "$Project\.venv\Scripts\python.exe" -m pytest "$Project\tests"
```

---
---

# 🇬🇧 English Version

## 🦊 Firefox Agent MCP Tools

**A set of MCP (Model Context Protocol) tools for intelligent Firefox/Camoufox browser automation via AI agents.**

### 📖 About
`firefox-agent-test` is an advanced architecture consisting of an **MCP Server** and an **Independent Browser Daemon**. It solves the main issue of AI browser automation: timeouts and session drops. The daemon runs **separately from the MCP host** (Claude Desktop, Cursor, Unsloth, etc.), preserving the browser profile, cookies, and tabs even after the AI session ends.

The project includes a built-in **Agent Skill** (`SKILL.md`) — a system prompt that enforces a strict browser interaction protocol: one-turn rule, single-tab policy, blind-retry protection, and safe video/form handling.

### ✨ Key Features
- 🎥 **Video Control** — playback, quality, subtitles, theater mode toggle, menu expansion (YouTube, Twitch, Rutube, VK Video) via platform adapters.
- 🛡️ **Policy Guard & Context Manager** — prevents blind clicks on hidden elements, tracks page context (watch/playlist/radio), and enforces safety rules.
- 🪟 **Window Manager** — save and restore window geometry.
- 🔗 **Smart Navigation** — fuzzy link search, JS-tab support, history back/forward, and **Deep Shadow DOM scanning**.
- 📝 **Form Tools** — analysis and filling with basic bot-trap evasion and CAPTCHA detection.
- ⚙️ **User Settings** — `user_tool_settings.json` for global policies, site-specific intents, and override phrases.
- 📸 **Screenshots & JS** — arbitrary JavaScript execution and page snapshots.

### ⚙️ Installation
1. **Clone & Venv:**
   ```powershell
   $Project = "E:\Program\AI Exlinks\firefox-agent-test"
   git clone https://github.com/openfileshub-code/firefox-agent-test.git $Project
   py -3 -m venv "$Project\.venv"
   ```
2. **Dependencies:**
   ```powershell
   & "$Project\.venv\Scripts\python.exe" -m pip install --upgrade pip
   & "$Project\.venv\Scripts\python.exe" -m pip install -r "$Project\requirements.txt"
   ```
   Current dependencies: `camoufox[geoip]>=0.4.0`, `mcp[cli]>=1.0.0`, `playwright>=1.40.0`, `pymorphy3>=2.0.0`, `orjson>=3.9.0`.
3. **Fetch Browser Binaries:**
   ```powershell
   & "$Project\.venv\Scripts\python.exe" -m camoufox fetch
   ```

### 🧠 Activating the Agent Skill (SKILL.md)
The `SKILL.md` file contains the system prompt that teaches the AI agent the correct browser interaction protocol.
- **For Unsloth:** The skill is auto-detected as `firefox-agent-browser` when the repository is registered in the platform's skill catalog.
- **For other MCP hosts:** Copy the contents of `SKILL.md` into your agent's system prompt or custom instructions.
- **Customization:** Use `config/user_tool_settings.json` instead of editing `SKILL.md` directly. Copy from `user_tool_settings.example.json` to get started.

### 🚀 Setup & Launch
**Architecture requires separate processes for the Daemon and the MCP Server.**

1. **Start Daemon Manually (First Time):**
   ```powershell
   & "$Project\.venv\Scripts\python.exe" "$Project\browserd.py" --profile "$Project\playwright_profile"
   ```
2. **Configure MCP Host:**
   Add to your MCP config (`mcpServers`):
   ```json
   {
     "mcpServers": {
       "firefox-agent": {
         "command": "C:\\path\\to\\firefox-agent-test\\.venv\\Scripts\\python.exe",
         "args": ["C:\\path\\to\\firefox-agent-test\\server.py"],
         "cwd": "C:\\path\\to\\firefox-agent-test"
       }
     }
   }
   ```
   > ⚠️ `server.py` uses `stdio`. **Never** run it via `pythonw.exe` or redirect its stdout!

### 🔄 Background Automation (Windows)
Use the included `start_browser.vbs` and `stop_browser.vbs` to run the daemon silently.
> 🇷🇺 **Note for Russian users:** Save `.vbs` files in **UTF-16 LE (Unicode)** encoding via Notepad to prevent mojibake in MsgBox alerts.

**Task Scheduler Setup:**
- **Trigger:** At log on.
- **Action:** Start a program.
  - **Program:** `.venv\Scripts\pythonw.exe`
  - **Arguments:** `"browserd.py" --profile "playwright_profile"`
- **Settings:** Uncheck "Stop the task if it runs longer than...".

### 🛠️ Available MCP Tools
Core tools: `browser_start`, `browser_stop`, `browser_navigate`, `browser_go_back`, `browser_go_forward`, `browser_tabs`, `browser_new_tab`, `browser_close_tab`, `browser_switch_tab`, `browser_click_text`, `browser_click_selector`, `browser_press_key`, `browser_type_text`, `browser_scroll`, `browser_get_content`, `browser_screenshot`, `browser_search_on_page`, `browser_find_link`, `browser_eval`, `browser_video`, `browser_expand_menu`.

Extended tools (if registered): `browser_inspect_page`, `browser_search_text`, `browser_scroll_to_text`, `browser_form_inspect`, `browser_form_fill`, `browser_websocket_events`, `browser_network_errors`, `browser_dialog_state`, `browser_dialog_resolve`.

### 🩺 Troubleshooting
- **Do not force kill processes** via Task Manager. Use the `browser_stop` MCP tool or `stop_browser.vbs` to gracefully save the session.
- **Protocol version:** Both daemon and server use `PROTOCOL_VERSION = 4`. Mismatched versions will throw an error.
- **Logs:** All logs live in the `logs/` folder: check `logs/browserd.log` for Playwright errors, `logs/mcp_debug.log` for host communication issues, and `.browserd_state.json` for TCP port/PID status.
- **Profile Locks:** If the daemon crashes, the OS will release the lock. Do not manually delete lock files unless you are sure the process is dead.
