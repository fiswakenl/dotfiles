---
name: dotfiles
description: Канон для настроек и служб — декларативно, через источник ~/dotfiles/home (github.com/fiswakenl/dotfiles) и chezmoi apply; файлы в системе руками не правим. Звать ДО первой записи, ещё на этапе плана, когда задача трогает ~/.zshrc, ~/.config/**, ~/.config/*.env, ~/.ssh/config, ~/.claude (CLAUDE.md, settings, скиллы, хуки, команды), ~/.local/bin, Brewfile или launchd-службы — туннели, фоновые процессы, всё, что должно «подниматься и держаться само». Фразы: «сделай декларативно», «чтобы поднималось само», «добавь в dotfiles», «закоммить конфиг», «перенеси на сервер», «синхронизируй настройки», «chezmoi». Глобальный, работает из любой папки.
---

# chezmoi: как править и сохранять настройки

Репо `~/dotfiles` открытый, всё личное в нём шифруется `age`. Источник — `~/dotfiles/home`
(`.chezmoiroot`), режим `symlink`: обычные конфиги в системе — ссылки в репо. Действуй по схеме сам, без уточнений.

Что сейчас расходится между репо и системой:

!`chezmoi --no-pager status 2>&1 | head -20 || echo "chezmoi недоступен"`

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
Новая машина — команда из `~/dotfiles/README.md`.

## Правила

- Любую команду chezmoi, которая печатает, запускай с `--no-pager`, иначе она повиснет в пейджере.
- Расхождения смотри через `status`; `diff` — только `chezmoi --no-pager diff --exclude encrypted`, иначе он печатает
  расшифрованные секреты.
- В открытые файлы пиши только то, что можно показать всем; ключи, токены и личные тексты — через `--encrypt`.
- Мак-специфичное (launchd, Homebrew, `defaults`) клади в `home/.chezmoiscripts/darwin/` или под условие
  `{{ if eq .chezmoi.os "darwin" }}`; пути — через `$HOME` или `{{ .chezmoi.homeDir }}`.
- Пустые файлы chezmoi не раскладывает — оставь в нём хотя бы комментарий.
- Память и сессии Claude (`~/.claude/projects`) в dotfiles не кладутся — они в `.chezmoiignore`.
