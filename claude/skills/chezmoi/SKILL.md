---
name: chezmoi
description: ОБЯЗАТЕЛЬНО звать ДО любой правки, git commit или push в ~/dotfiles и до первой записи в файлы, которыми управляет chezmoi: ~/.zshrc, ~/.config/**, ~/.config/*.env, ~/.ssh/config, ~/.claude (CLAUDE.md, settings, скиллы, хуки, команды), ~/.local/bin, Brewfile, launchd-службы (туннели, фоновые процессы, всё, что должно подниматься само). Фразы: «chezmoi», «добавь в dotfiles», «закоммить конфиг», «перенеси на сервер», «синхронизируй настройки», «сделай декларативно». Глобальный, работает из любой папки.
---

# chezmoi: как править и сохранять настройки

Репо `~/dotfiles` открытый, всё личное в нём шифруется `age`. Источник — `~/dotfiles/home`
(`.chezmoiroot`), режим `symlink`: обычные конфиги в системе — ссылки в репо. Действуй по схеме сам, без уточнений.

Что сейчас расходится между репо и системой:

!`chezmoi --no-pager status 2>&1 | head -20 || echo "chezmoi недоступен"`

Эта машина — переменные, которые видят шаблоны (ОС решает, что раскладывается — раздел 5):

!`chezmoi execute-template 'os={{ .chezmoi.os }} arch={{ .chezmoi.arch }} host={{ .chezmoi.hostname }} user={{ .chezmoi.username }} home={{ .chezmoi.homeDir }} source={{ .chezmoi.sourceDir }}' 2>&1`

Шаблоны в репо (`.tmpl` — копии, собираются под машину; ссылки шаблонами не делаем — раздел 5):

!`cd "$(chezmoi source-path 2>/dev/null)" 2>/dev/null && find . -name '*.tmpl' | sed 's|^\./||' | sort`

Все команды установленной версии — опирайся на них, а не на память (подробно — `chezmoi help <команда>`):

!`chezmoi help 2>&1 | sed -n '/^Documentation commands:/,/^Flags:/p' | grep -v '^Flags:'`

Quick Start из документации, если нужен с нуля: https://raw.githubusercontent.com/twpayne/chezmoi/master/assets/chezmoi.io/docs/quick-start.md

## 1. Найди источник и тип файла

```sh
chezmoi --no-pager managed | grep <кусок>   # под управлением ли вообще
chezmoi source-path <файл>                  # точный файл в репо; ошибка → файл новый
```

Тип читается по имени источника:

| В имени источника | Что в системе | Тип |
|---|---|---|
| без приставок, `dot_`, `symlink_` | ссылка в репо | **ссылка** |
| `encrypted_`, `private_`, `executable_`, `.tmpl` | отдельный файл | **копия** |

Примеры: `dot_config/yazi/keymap.toml` — ссылка; `dot_config/encrypted_private_openai.env.age`,
`private_dot_ssh/encrypted_private_config.age`, `private_dot_claude/encrypted_CLAUDE.md.age`,
`dot_local/bin/executable_netwatch.sh` — копии.

## 2. Сохрани по типу

**Ссылка** — правь на месте (это и есть файл в репо), затем:

```sh
git -C ~/dotfiles add -A && git -C ~/dotfiles commit -m "<что поменял>" && git -C ~/dotfiles push
```

**Копия** — правь на месте и сразу забери в репо, до любого `apply`: `apply --force` молча вернёт старую версию из репо.
Одна команда сама зашифрует, закоммитит и запушит (`autoCommit`/`autoPush`):

```sh
chezmoi re-add <файл>
```

**Новый файл** — добавь; секреты, ключи и личные тексты — с `--encrypt`. Затем `chezmoi apply --force <файл>`,
чтобы на месте файла появилась ссылка:

```sh
chezmoi add <файл>              # обычный конфиг
chezmoi add --encrypt <файл>    # секрет, ключ, личное
```

**Убрать из dotfiles, файл оставить** — `chezmoi forget --force <файл>`.
**Удалить совсем** — `chezmoi destroy --force <файл>` до удаления источника: так уходят и файл, и источник.
После обоих — `git -C ~/dotfiles status`, остаток закоммитить.

**Новая программа на маке** — `brew bundle dump --force --file ~/dotfiles/Brewfile`, затем коммит.

## 3. Разложи и проверь

```sh
chezmoi --no-pager apply --dry-run --verbose   # сначала посмотреть, что изменится и какие скрипты сработают
chezmoi apply --force                          # без терминала вопрос «файл изменился» падает — --force отвечает «да»
chezmoi --no-pager status                      # пусто — репо и система совпадают
git -C ~/dotfiles status -sb                   # main...origin/main — всё на GitHub
```

Правил `home/.chezmoi.toml.tmpl` — сначала `chezmoi init`, иначе `apply` возьмёт старый конфиг.

## 4. Другие машины

На машине с chezmoi подтянуть изменения: `chezmoi update` (git pull + раскладка), на сервере — `ssh <хост> chezmoi update`.
Второй `update` подряд не должен ничего делать — так проверяется идемпотентность.
Новая машина — команда из `~/dotfiles/README.md`; пароль от ключа age спрашивается один раз, дальше ключ лежит
в `~/.config/age/key.txt`. Проверять холодный старт — на чистом тестовом пользователе сервера, не на живом.

## 5. Мак и сервер: что общее, что своё

Различие — по ОС (`.chezmoi.os`: `darwin` — мак, `linux` — сервер), четыре приёма:

| Нужно | Как | Пример |
|---|---|---|
| файл только на одной ОС | `.chezmoiignore`, блок `{{ if ne .chezmoi.os "darwin" }}` — исключить НЕ на маке, блок `{{ if ne .chezmoi.os "linux" }}` — НЕ на Linux | `40-mac.zsh`, `30-iterm.zsh`, `Library` — только мак; `.config/gh/hosts.yml` — только Linux |
| файл-копия с разными частями | шаблон `.tmpl` с `{{ if eq .chezmoi.os "darwin" }}` | plist'ы launchd |
| файл-ссылка с разными частями | ветвление внутри самого файла (`[[ $OSTYPE == darwin* ]]` в zsh) — шаблон превратил бы ссылку в копию | `20-projects.zsh` |
| программы | `.chezmoiscripts/darwin/` (Homebrew по `Brewfile`), `.chezmoiscripts/linux/` (`uv`, `yazi`, `gh` в `~/.local/bin`, без sudo) | |

Блок в `.chezmoiignore` читается наоборот: в `{{ if ne .chezmoi.os "darwin" }}` лежит то, что НЕ ставится на маке.
После правки исключений — `chezmoi --no-pager managed`/`ignored` на маке и на сервере: файл должен быть ровно там, где задуман.

Не делай шаблоном то, что правят программы: `~/.claude/settings.json` (его пишет Claude), `.gitconfig`
(его пишет `git config --global`) — они ссылки, шаблон сделал бы их копией, и правки перестали бы попадать в репо.
Отличие по ОС в таких файлах — без путей: `!gh auth git-credential`, а не `/opt/homebrew/bin/gh`.

GitHub: git ходит по https через токен `gh`. На маке токен в связке ключей, на Linux — `~/.config/gh/hosts.yml`
из dotfiles (шифрованный, только Linux). Поэтому на новом сервере хватает пароля от ключа age.

## 6. Команды chezmoi (проверены)

| Задача | Команда |
|---|---|
| открыть исходник в редакторе (секрет расшифрует и зашифрует обратно) | `chezmoi edit <файл>`; `--apply` — сразу разложить, `--watch` — на каждое сохранение |
| забрать в репо копию, поправленную на месте | `chezmoi re-add [файл]` |
| закоммитить и запушить всё изменённое в репо (правки ссылок) | `chezmoi push` |
| взять новый файл | `chezmoi add [--encrypt] <файл>` |
| сменить тип файла | `chezmoi chattr +template` / `+encrypted` / `+private <файл>` |
| разложить / подтянуть с GitHub и разложить | `chezmoi apply` / `chezmoi update` |
| что расходится | `chezmoi --no-pager status`, `chezmoi --no-pager diff --exclude encrypted` |
| слить, если поменялось и тут, и в репо | `chezmoi merge <файл>` |
| перестать управлять / удалить с исходником | `chezmoi forget --force` / `chezmoi destroy --force` |
| что под управлением / вне / исключено на этой ОС | `chezmoi --no-pager managed` / `unmanaged` / `ignored` |
| переменные шаблонов (`os`, `arch`, `hostname`…) | `chezmoi --no-pager data` |
| зайти в репо / git оттуда | `chezmoi cd` / `chezmoi git -- <команда>` |
| проверка установки | `chezmoi --no-pager doctor` |
| зашифровать / расшифровать вручную | `chezmoi encrypt` / `chezmoi decrypt <файл.age>` |

## Правила

- Любую команду chezmoi, которая печатает, запускай с `--no-pager`, иначе она повиснет в пейджере.
- Расхождения смотри через `status`; `diff` — только `chezmoi --no-pager diff --exclude encrypted`, иначе он печатает
  расшифрованные секреты.
- В открытые файлы пиши только то, что можно показать всем; ключи, токены и личные тексты — через `--encrypt`.
- Мак-специфичное (launchd, Homebrew, `defaults`) — по разделу 5: `home/.chezmoiscripts/darwin/`, исключение или условие
  `{{ if eq .chezmoi.os "darwin" }}`; пути — через `$HOME` или `{{ .chezmoi.homeDir }}`.
- Пустые файлы chezmoi не раскладывает — оставь в нём хотя бы комментарий.
- Память и сессии Claude (`~/.claude/projects`) в dotfiles не кладутся — они в `.chezmoiignore`.
