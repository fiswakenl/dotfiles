---
name: chezmoi
description: Схема работы с dotfiles на chezmoi (~/dotfiles, github.com/fiswakenl/dotfiles). Звать при любой правке конфига, который лежит в dotfiles — ~/.zshrc, ~/.config/**, ~/.ssh/config, ~/.config/*.env, ~/.claude (CLAUDE.md, settings, скиллы, хуки, команды), ~/.local/bin, launchd-службы, Brewfile, — и на «добавь в dotfiles», «закоммить конфиг», «перенеси на сервер», «синхронизируй настройки», «chezmoi». Глобальный, работает из любой папки.
---

# chezmoi: как править и сохранять настройки

Репо `~/dotfiles` открытый, поэтому всё личное в нём шифруется `age`. Источник — `~/dotfiles/home`
(`dot_zshrc` → `~/.zshrc`), система — домашняя папка. Действуй по схеме сам, без уточнений.

Что сейчас расходится между репо и системой:

!`chezmoi status 2>&1 | head -20 || echo "chezmoi недоступен"`

## 1. Определи тип файла

```
chezmoi source-path <файл>      ошибка → файл новый
ls -l <файл>                    стрелка → ссылка
```

- **ссылка** — обычные конфиги, всё в `~/.claude` кроме CLAUDE.md, `keymap.toml` и т. п. Файл в системе и в репо один и тот же.
- **копия** — имя в источнике с `encrypted_`, `private_` или `executable_`: `.env`, `~/.ssh/*`, `~/.claude/CLAUDE.md`, `~/.local/bin/*`, plist-ы.
- **новый** — chezmoi его ещё не знает.

## 2. Сохрани по типу

**Ссылка** — правь на месте, затем закоммить и запушь репо:

```sh
git -C ~/dotfiles add -A && git -C ~/dotfiles commit -m "<что поменял>" && git -C ~/dotfiles push
```

**Копия** — правь на месте, затем одна команда; она сама зашифрует, закоммитит и запушит:

```sh
chezmoi re-add <файл>
```

**Новый** — добавь; секреты, ключи и личные тексты — только с `--encrypt`:

```sh
chezmoi add <файл>              # обычный конфиг
chezmoi add --encrypt <файл>    # секрет, ключ, личное
```

**Удалить из dotfiles** — `chezmoi forget --force <файл>`, затем `git -C ~/dotfiles status` и закоммить остаток, если есть.

**Новая программа на маке** — `brew bundle dump --force --file ~/dotfiles/Brewfile`, затем коммит.

## 3. Проверь

- `chezmoi status` пустой — репо и система совпадают.
- `git -C ~/dotfiles status -sb` показывает `main...origin/main` без расхождений — всё на GitHub.

## 4. Другие машины

На машине, где стоит chezmoi, подтянуть изменения: `chezmoi update` (git pull + раскладка). Сервер — через ssh:
`ssh <хост> chezmoi update`. Новая машина — команда из README `~/dotfiles/README.md`.

## Правила

- Смотри расхождения через `chezmoi status`; `chezmoi diff` запускай только с `--exclude encrypted`, иначе он
  печатает расшифрованные секреты.
- В открытые файлы пиши только то, что можно показать всем; ключи, токены и личные тексты — через `--encrypt`.
- Мак-специфичное (launchd, Homebrew, `defaults`) клади в `home/.chezmoiscripts/darwin/` или под условие
  `{{ if eq .chezmoi.os "darwin" }}`, пути — через `$HOME` или `{{ .chezmoi.homeDir }}`.
- Память и сессии Claude (`~/.claude/projects`) в dotfiles не кладутся — они в `.chezmoiignore`.
