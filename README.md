# 力 Firefox Agent MCP Tools

**Набор MCP-инструментов (Model Context Protocol) для интеллектуального управления антидетект-браузером Camoufox/Firefox с помощью ИИ-агентов.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)]()
[![MCP](https://img.shields.io/badge/Protocol-MCP-green?style=flat-square)]()
[![Camoufox](https://img.shields.io/badge/Browser-Camoufox-orange?style=flat-square)]()
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)]()

---

##  О проекте

`firefox-agent-mcp-tools` — это продвинутая архитектура, состоящая из **MCP-сервера** и **независимого браузерного демона**. Проект решает главную проблему автоматизации браузеров через ИИ: таймауты и закрытие сессий. Демон работает **отдельно от MCP-хоста** (Claude Desktop, Cursor, Qwen и др.), что позволяет сохранять профиль, куки и открытые вкладки даже после завершения сессии ИИ-агента.

### ✨ Ключевые возможности
-  **Управление видео** — воспроизведение, качество, субтитры, обход меню (YouTube, Twitch, Rutube, VK).
- 犯 **Менеджер окон** — сохранение и восстановление геометрии и позиций окон.
-  **Умный поиск и навигация** — нечеткий поиск ссылок, поддержка JS-вкладок и **глубокое сканирование Shadow DOM**.
-  **Работа с формами** — анализ и заполнение с обходом базовых бот-ловушек.
-  **Скриншоты и JS** — выполнение произвольного JavaScript и снятие снапшотов страницы.

---

## ️ Архитектура и структура

```text
firefox-agent-test/
├── server.py                  # MCP-сервер (stdio, связывается с ИИ-хостом)
├── browserd.py                # Браузерный демон (TCP-сервер, управляет Playwright)
├── daemon_control.py          # Скрипт управления демоном (start/stop/status)
├── start_browser.vbs          # Скрытый запуск демона (Windows)
├── stop_browser.vbs           # Скрытая остановка демона (Windows)
├── requirements.txt           # Зависимости Python
├── SKILL.md                   # Системный промпт / Скилл для ИИ-агента
├── config/                    # Конфигурация (алиасы сайтов, окна)
├── modules/                   # Модули (кэш ссылок, видео, окна)
├── adapters/                  # Адаптеры для специфичных сайтов (Rutube, VK)
├── tests/                     # Unit и Live тесты (pytest)
└── playwright_profile/        # Персистентный профиль браузера (создается автоматически)
```

---

## ⚙️ Установка (Пошаговое руководство)

> **Важно:** Проект оптимизирован для ОС **Windows** из-за использования VBS-скриптов и Планировщика заданий, но базовый функционал работает и на Linux/macOS.

### 1. Подготовка окружения
Убедитесь, что у вас установлен Python 3.10+ и Git.
```powershell
git clone https://github.com/openfileshub-code/firefox-agent-test.git
cd firefox-agent-test
```
⚠️ **Критически важно:** Включите в Windows отображение расширений файлов. Убедитесь, что файлы называются `server.py`, `browserd.py`, `daemon_control.py`, а не `.py.md` или `.py.py`.

### 2. Создание виртуального окружения
```powershell
py -3 -m venv .venv
# Если 'py' не найден, используйте: python -m venv .venv
```

### 3. Установка зависимостей
```powershell
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install "camoufox[geoip]" playwright mcp
```
*(Опционально: `pip install orjson` для ускорения сериализации JSON).*

### 4. Загрузка бинарников Camoufox
Этот шаг обязателен для скачивания движка браузера:
```powershell
.venv\Scripts\python.exe -m camoufox fetch
```
>  *Если эта команда завершается ошибкой, не переходите к следующему шагу, пока не исправите проблему в консоли.*

---

##  Настройка и Запуск

Архитектура требует **раздельного запуска** демона и MCP-сервера.

### Шаг 1. Первый ручной запуск демона (для проверки)
Откройте PowerShell в папке проекта:
```powershell
.venv\Scripts\python.exe browserd.py --profile playwright_profile
```
**Ожидаемое поведение:**
1. Откроется окно Camoufox/Firefox.
2. В папке появится `browserd.log`.
3. В `playwright_profile/` создастся `.browserd_state.json`.
*Оставьте это окно открытым. Не запускайте второй экземпляр с тем же профилем!*

### Шаг 2. Настройка MCP-хоста (Claude Desktop, Cursor и т.д.)
Добавьте сервер в конфигурацию вашего MCP-хоста (`mcpServers`). Замените пути на свои:
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
> ⚠️ **Внимание:** `server.py` использует стандартный `stdio` для связи с хостом. **Никогда** не запускайте `server.py` через `pythonw.exe` и не перенаправляйте его `stdout` в файл!

### Шаг 3. Тестирование связи
1. Перезапустите MCP-хост.
2. Вызовите инструмент `browser_start` — он подключится к уже запущенному вручную демону.
3. Используйте `browser_navigate` для открытия сайта.
4. Завершите сессию MCP-хоста. **Окно браузера должно остаться открытым!**

---

##  Автоматизация и Управление (Windows)

Чтобы демон запускался в фоне без черного окна консоли и переживал перезапуски MCP-хоста, используйте `daemon_control.py`.

### 1. Создание ярлыков (VBS)
В репозитории уже есть `start_browser.vbs` и `stop_browser.vbs`.
>  **Важно для русских сообщений:** Откройте `.vbs` файлы в Блокноте и сохраните их в кодировке **UTF-16 LE (Юникод)**. Иначе кириллица в `MsgBox` отобразится кракозябрами.

Создайте ярлыки для этих `.vbs` файлов на Рабочем столе:
-  **Запустить Camoufox** (`start_browser.vbs`)
-  **Остановить Camoufox** (`stop_browser.vbs`)

### 2. Автозапуск через Планировщик заданий
1. Откройте **Планировщик заданий Windows** -> **Создать задачу**.
2. **Общие:** Имя `Camoufox Browser Daemon`, галочка *"Выполнять только для вошедшего пользователя"* (иначе браузер будет невидимым).
3. **Триггеры:** *При входе в систему*.
4. **Действия:** Запуск программы:
   - **Программа:** `E:\...\firefox-agent-test\.venv\Scripts\pythonw.exe`
   - **Аргументы:** `"E:\...\firefox-agent-test\browserd.py" --profile "E:\...\firefox-agent-test\playwright_profile"`
   - **Рабочая папка:** `E:\...\firefox-agent-test`
5. **Условия:** Снять галочку *"Запускать только при питании от электросети"*.
6. **Параметры:** Снять галочку *"Остановить задачу, если она выполняется дольше..."*. Выбрать *"Не запускать новый экземпляр"*.

---

## ️ Использование MCP-инструментов

После подключения к демону через `browser_start`, ИИ-агенту доступны следующие инструменты:

| Инструмент | Описание |
| :--- | :--- |
| `browser_start` | Подключение к работающему демону (TCP). |
| `browser_navigate` | Открытие URL. |
| `browser_tabs` | Управление вкладками (создание, закрытие, переключение). |
| `browser_video` | Управление видеоплеером (play/pause, качество, звук, полноэкранный режим). |
| `browser_expand_menu` | Раскрытие скрытых меню, hover-эффектов и Shadow DOM элементов. |
| `browser_stop` | **Штатная** и безопасная остановка браузера с сохранением сессии. |

---

## 喙 Диагностика и Логи

Если что-то пошло не так, **не убивайте процессы принудительно** через Диспетчер задач! Это может повредить профиль браузера и оставить lock-файлы. Код не удаляет их автоматически в целях безопасности.

**Где искать правду:**
- `browserd.log` — основные ошибки демона, падения Playwright, проблемы с профилем.
- `mcp_debug.log` — лог взаимодействия MCP-сервера и хоста.
- `daemon_control.log` — лог работы VBS/BAT скриптов.
- `playwright_profile/.browserd_state.json` — текущий статус демона (PID, TCP-порт, токен).

**Частые проблемы:**
- **MCP-хост пишет "Демон не найден":** Проверьте, запущен ли `pythonw.exe` / `browserd.py` в Диспетчере задач. Убедитесь, что `server.py` и `browserd.py` смотрят на одну и ту же папку `playwright_profile`.
- **Браузер закрывается при закрытии Claude/Cursor:** Значит, вы запустили `browserd.py` из того же терминала, что и MCP-хост, или Планировщик заданий не настроен. Демон должен быть независимым процессом.
- **VBS выдает кракозябры:** Сохраните `.vbs` в кодировке "Юникод" (UTF-16 LE).
- **Ошибка "Профиль уже используется":** Демон не завершился штатно. Проверьте `browserd.log`, найдите процесс в Диспетчере задач и завершите его, затем удалите `parent.lock` в папке `playwright_profile`.

---
---

#  English Version

## 力 Firefox Agent MCP Tools

**A set of MCP (Model Context Protocol) tools for intelligent Firefox/Camoufox browser automation via AI agents.**

###  About
`firefox-agent-mcp-tools` is an advanced architecture consisting of an **MCP Server** and an **Independent Browser Daemon**. It solves the main issue of AI browser automation: timeouts and session drops. The daemon runs **separately from the MCP host** (Claude Desktop, Cursor, etc.), preserving the browser profile, cookies, and tabs even after the AI session ends.

### ✨ Key Features
-  **Video Control** — playback, quality, subtitles, menu expansion (YouTube, Twitch, Rutube, VK).
- 犯 **Window Manager** — save and restore window geometry.
-  **Smart Navigation** — fuzzy link search, JS-tab support, and **Deep Shadow DOM scanning**.
-  **Form Tools** — analysis and filling with basic bot-trap evasion.
-  **Screenshots & JS** — arbitrary JavaScript execution and page snapshots.

### ⚙️ Installation
1. **Clone & Venv:**
   ```powershell
   git clone https://github.com/openfileshub-code/firefox-agent-test.git
   cd firefox-agent-test
   py -3 -m venv .venv
   ```
2. **Dependencies:**
   ```powershell
   .venv\Scripts\python.exe -m pip install --upgrade pip
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   .venv\Scripts\python.exe -m pip install "camoufox[geoip]" playwright mcp
   ```
3. **Fetch Browser Binaries:**
   ```powershell
   .venv\Scripts\python.exe -m camoufox fetch
   ```

###  Setup & Launch
**Architecture requires separate processes for the Daemon and the MCP Server.**

1. **Start Daemon Manually (First Time):**
   ```powershell
   .venv\Scripts\python.exe browserd.py --profile playwright_profile
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

###  Background Automation (Windows)
Use the included `start_browser.vbs` and `stop_browser.vbs` to run the daemon silently.
>  **Note for Russian users:** Save `.vbs` files in **UTF-16 LE (Unicode)** encoding via Notepad to prevent mojibake in MsgBox alerts.

**Task Scheduler Setup:**
- **Trigger:** At log on.
- **Action:** Start a program.
  - **Program:** `.venv\Scripts\pythonw.exe`
  - **Arguments:** `"browserd.py" --profile "playwright_profile"`
- **Settings:** Uncheck "Stop the task if it runs longer than...".

### 喙 Troubleshooting
- **Do not force kill processes** via Task Manager. Use the `browser_stop` MCP tool or `stop_browser.vbs` to gracefully save the session.
- **Logs:** Check `browserd.log` for Playwright errors, `mcp_debug.log` for host communication issues, and `.browserd_state.json` for TCP port/PID status.
- **Profile Locks:** If the daemon crashes, the OS will release the lock. Do not manually delete lock files unless you are sure the process is dead.
