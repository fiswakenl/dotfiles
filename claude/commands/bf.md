---
description: Положить текст в буфер обмена iPhone - через сервер bf и пуш Bark с айди
argument-hint: [текст; пусто - то, что я последним предлагал скопировать]
allowed-tools: Bash(python3 *)
---

Текст уходит на сервер `https://bf.devml4dbs.duckdns.org` (dnk, `~/tools/bf`), сервер
отвечает айди, айди уходит пушем Bark с заголовком `bf`. На iPhone автоматизация по
заголовку `bf` забирает `GET /<айди>` и кладёт ответ в буфер. Сам текст в пуш не идёт:
iOS отдаёт автоматизации только начало уведомления.

Текст:
- аргумент ниже - его, как есть, без правок;
- аргумента нет - последний текст, который я в этом разговоре предлагал скопировать
  или положил в буфер (сообщение, команда, ссылка), ровно в том виде, как показывал.
  Неясно, какой - спроси одним вопросом, не гадай.

Аргумент: $ARGUMENTS

Отправка - одной командой; текст подставь вместо ТЕКСТ, строго между маркерами heredoc,
ничего не экранируя:

```bash
python3 -c '
import json, pathlib, sys, urllib.request
def env(name):
    return dict(l.strip().split("=", 1) for l in open(pathlib.Path.home() / ".config" / name) if "=" in l)
text = sys.stdin.read().rstrip("\n").encode()
key = urllib.request.urlopen(urllib.request.Request("https://bf.devml4dbs.duckdns.org/", data=text,
    headers={"Authorization": "Bearer " + env("bf.env")["BF_TOKEN"]}), timeout=10).read().decode()
req = urllib.request.Request("https://api.day.app/" + env("bark.env")["BARK_KEY"].strip("\"\x27"),
    data=json.dumps({"title": "bf", "body": key}).encode(),
    headers={"Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=10)).get("message"), key, len(text.decode()), "знаков")
' <<'BF_TEXT'
ТЕКСТ
BF_TEXT
```

Ответь одной строкой: ушло или нет, айди и сколько знаков. Токены не печатай.
