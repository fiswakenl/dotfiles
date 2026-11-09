# GV: голос через Gemini Live для Claude Code

Говоришь с Gemini, она передаёт мысль в окно Claude и читает ответ вслух. Поправка к уже переданному стирает старый ход из истории Claude и отправляет исправленный текст.

## Запуск

```zsh
ccg            # Claude с GV и мостом отката (функция в ~/.config/zsh/20-projects.zsh)
/gv            # включить или выключить голос; состояние — ~/.claude/gv.state
```

`ccg` = `--plugin-dir ~/.claude/gv` + `BUN_OPTIONS=--preload=…/preload.mjs`. Preload нужен мосту: штатный API плагина не даёт откатить историю.

## Как устроено

- `core.py` — вся логика: запросы, поправки, очередь речи. Правила — в его шапке.
- `gemini.py` — голосовой канал: сокет Gemini, микрофон, колонки; `Text` — замена без звука для тестов (`GV_TEXT_ONLY=1`).
- `voice.py` — сервис: запуск и HTTP-ручки (список — в его шапке).
- `hooks/register.ts` — плагин: забирает запросы `/next`, шлёт их в окно Claude через мост, quick — веткой разговора, ответ — `/answer`.
- `preload.mjs` + `native/bridge.mjs` — мост в окно Claude: `status`, `list`, `send`, `rewind`. Сокет `$TMPDIR/gv-UID/PID.sock`, свой у каждого процесса.
- `prompt.md` — инструкция Gemini: когда `ask`, когда `ask` с `replaces`, когда `quick`.

Схемы — `~/dotfiles/.excalidraw/` (путь запроса, выбор функции, очередь речи, два сценария).

## Снаружи

Один вход — `POST /call {"name", "args"}`, имена те же, что у Gemini и моста:

```sh
curl -s localhost:8790/call -d '{"name":"ask","args":{"text":"Что в todo?"}}'
curl -s localhost:8790/call -d '{"name":"rewind","args":{"target":1}}'
python3 ~/.claude/gv/native/client.py list      # мост напрямую, без сервиса
```

## Проверки

```sh
uv run --script ~/.claude/gv/tests/sim_test.py   # все сценарии на ядре, пара секунд
uv run --script ~/.claude/gv/tests/run.py        # те же сценарии на живом ccg в tmux, без голоса, ~3 мин
```

Сценарии — данными в `tests/scenarios.py`. Голос, микрофон и колонки тесты не трогают.

Мост — внутренний механизм, проверен на Claude Code 2.1.319, macOS arm64: после обновления Claude preload может потребовать правки.
