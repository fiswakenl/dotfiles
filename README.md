# dotfiles

Мои настройки. Раскладывает их [chezmoi](https://chezmoi.io), файлы лежат в `home/`.

## Новая машина

```sh
chezmoi init --apply fiswakenl
```

Если chezmoi ещё не установлен:

```sh
sh -c "$(curl -fsLS get.chezmoi.io)" -- init --apply fiswakenl
```

## Каждый день

Конфиги правятся прямо в репо, их просто коммитишь.

Секреты, `~/.ssh/config`, `~/.claude/CLAUDE.md` и скрипты в `~/.local/bin` лежат копиями. После правки:

```sh
chezmoi re-add
```

Новый файл:

```sh
chezmoi add <путь>
```

Новая программа:

```sh
brew bundle dump --force --file ~/dotfiles/Brewfile
```
