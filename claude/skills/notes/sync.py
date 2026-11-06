#!/usr/bin/env python3
"""Заметки Apple Notes как .md в волте dnk/Notes. Источник правды — Apple Notes (правится и с телефона),
dnk/Notes — рабочая копия под git волта, синхронизация через icloud-md.

  sync.py pull   забрать изменения из Notes, коммит «notes: из Notes»
  sync.py push   отправить правки в Notes, стереть корзину и пустые папки, коммит «notes: правка»
  sync.py today  вчера, сегодня и завтра — в «Заметки», остальные дни — в папку своей недели в Планы/
  sync.py sync   то же, что today: pull → раскладка дней → push и уборка (облако подгоняется под локальное)
  sync.py ls     карта: папки и заметки
  sync.py load   загрузка скилла: pull → today → карта, по очереди
  sync.py pre    PreToolUse (Edit|Write заметки): pull перед правкой; конфликт — заблокировать правку
  sync.py post   PostToolUse: в Notes/ есть незакоммиченное — push

icloud-md — не наш код: npm install -g icloud-md, github.com/coddingtonbear/icloud-md.
Сессия Apple ID протухла — icloud-md откроет окно входа, войти должен пользователь.
Коммиты только по Notes/, остальные изменения волта не трогаются; git push волта — не здесь.
"""
import datetime, fcntl, json, os, re, subprocess, sys, tempfile

REPO = os.path.expanduser("~/Documents/store/dnk")
VAULT = os.path.join(REPO, "Notes")
# Корневые папки — постоянная структура: их не удаляем даже пустыми. Вложенные — как получится.
ROOTS = {"Notes", "Планы", "Быт", "Обвес", "Тушка", "HEAD", "Архив"}
LOCK = os.path.join(tempfile.gettempdir(), "notes-sync.lock")
ENV = {**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_FILE = re.compile(r"^(Пн|Вт|Ср|Чт|Пт|Сб|Вс) (\d\d)\.(\d\d)\.md$")

quiet = False  # в хуках вывод icloud-md не нужен


class Conflict(Exception):
    pass


class Refused(Exception):
    """icloud-md не взял правку (например, новая таблица) — локально она есть, в Notes нет."""


def say(text):
    if text and not quiet:
        print(text)


def icloud(*args):
    """icloud-md без прогресс-баров и цветов."""
    r = subprocess.run(["icloud-md", *args], cwd=VAULT, env=ENV, capture_output=True, text=True)
    lines = ANSI.sub("", r.stdout + r.stderr).splitlines()
    return "\n".join(l for l in lines if l.strip() and "■" not in l)


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)


def dirty():
    return bool(git("status", "--porcelain", "--", "Notes").stdout.strip())


def commit(message):
    git("add", "-A", "--", "Notes")
    # Автор «hook» — чтобы в истории отличать автокоммиты от ручных. Нечего коммитить — git откажет.
    git("-c", "user.name=hook", "-c", "user.email=hook@local", "commit", "-qm", message, "--", "Notes")


def conflicts():
    """pull сливает правки из Notes с локальными; где правили одно место — оставляет маркеры."""
    found = []
    for dirpath, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                path = os.path.join(dirpath, f)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    if any(line.startswith("<<<<<<<") for line in fh):
                        found.append(os.path.relpath(path, VAULT))
    return sorted(found)


def conflict_message():
    files = "\n".join(f"  Notes/{f}" for f in conflicts())
    return (
        "КОНФЛИКТ в заметках — одно и то же место поменяли и здесь, и в Apple Notes:\n"
        f"{files}\n"
        "Маркеры: <<<<<<< local — здесь; ||||||| base — как было до обеих правок; "
        "======= … >>>>>>> remote — из Notes.\n"
        "Разрешить по смыслу (неясно — спросить пользователя), убрать маркеры и сохранить."
    )


def refused(status):
    """Файлы, которые icloud-md отказался отправлять: в status под «modified: файл» строка «! причина»."""
    found, current = [], None
    for line in status.splitlines():
        m = re.match(r"\s*(?:modified|added|deleted|moved|renamed|new):\s*(.+)", line)
        if m:
            current = m.group(1).strip()
        elif line.strip().startswith("!") and current:
            reason = line.strip()[1:].split(' Run "')[0].strip()
            found.append(f"  Notes/{current} — {reason}")
    return found


def refused_message(lines):
    return (
        "НЕ ОТПРАВЛЕНО в Apple Notes — icloud-md отказался, на телефоне старая версия:\n"
        + "\n".join(lines)
        + "\nПоправить по причине и сохранить заново; новая таблица — переделать в список."
    )


def local_dirs():
    """Папки локальной копии «A/B» — эталон структуры для облака."""
    found = set()
    for dirpath, dirs, _ in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "attachments"]
        rel = os.path.relpath(dirpath, VAULT)
        if rel != ".":
            found.add(rel.replace(os.sep, "/"))
    return found


def local_note_ids():
    """id заметок из шапок apple-note-id локальных .md."""
    ids = set()
    for dirpath, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                with open(os.path.join(dirpath, f), encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"^apple-note-id:\s*(\S+)", fh.read(300), re.M)
                    if m:
                        ids.add(m.group(1).lower())
    return ids


def cleanup():
    """Локальная копия — эталон: всё в облаке, что ей не соответствует, режем.
    1. Локально удалить пустые каталоги (кроме корневых).
    2. В облаке удалить живые заметки, которых нет локально (нет файла с таким apple-note-id):
       дубли, хвосты переносов. Перед этим всегда pull — заметки с телефона уже лежат локально.
    3. В облаке удалить папки, которых нет локально. Сразу после переноса сервер ещё держит ссылку
       заметки на прежнюю папку и отклоняет удаление (VALIDATING_REFERENCE_ERROR) — следующая
       уборка повторит.
    4. Стереть корзину насовсем. Вернуть можно только из git волта. DRY=1 — только показать план."""
    for dirpath, _, _ in os.walk(VAULT, topdown=False):
        rel = os.path.relpath(dirpath, VAULT)
        if rel == "." or rel.startswith(".") or (os.sep not in rel and rel in ROOTS):
            continue
        if not os.listdir(dirpath):
            os.rmdir(dirpath)

    dry = bool(os.environ.get("DRY"))
    record = re.compile(r"^(\S+)\s+(Note|Folder)\s+(\S+)\s+\S+\s*(.*)$")
    folders, notes, trashed = {}, {}, []
    for line in icloud("object", "list").splitlines():
        m = record.match(line)
        if not m:
            continue
        rid, kind, state, rest = m.groups()
        parents = re.findall(r"([0-9A-Za-z_-]{8})\.\.\.", rest.split("<- ")[0])
        if kind == "Note" and state == "trashed":
            trashed.append(rid)
        elif kind == "Note" and state == "live":
            notes[rid] = rest.split(" -> ")[0].strip()
        elif kind == "Folder" and state == "live" and not rid.endswith("-CloudKit"):
            name = re.search(r'"(.*)"', rest)
            folders[rid] = (name.group(1) if name else rid, parents[0] if parents else None)
    if not folders and not notes:  # список не получили — ничего не трогать
        return

    def delete(rid, label, *flags):
        if dry:
            print(f"DRY: удалить — {label}")
            return False
        if "Permanently deleted" in icloud("object", "delete", rid, *flags):
            say(f"удалено — {label}")
            return True
        return False

    ids = local_note_ids()
    gone_notes = {rid for rid, p in notes.items() if rid.lower() not in ids and delete(rid, f"заметка «{p}» — её нет локально")}

    by_prefix = {rid[:8]: rid for rid in folders}
    # Локально папка лежит под dirName из state.json: «HEAD:» в Notes — каталог HEAD.
    with open(os.path.join(VAULT, ".icloud-md", "state.json"), encoding="utf-8") as fh:
        dir_names = {rid: f.get("dirName") for rid, f in json.load(fh).get("folders", {}).items()}

    def path(rid, seen=()):
        name, parent = folders[rid]
        name = dir_names.get(rid) or name
        parent = by_prefix.get(parent)
        return name if not parent or parent in seen else f"{path(parent, seen + (rid,))}/{name}"

    keep = local_dirs()
    victims = sorted(((path(rid).count("/"), path(rid), rid) for rid in folders if path(rid) not in keep), reverse=True)
    # Сначала глубокие: родитель освобождается после детей.
    gone = {rid for _, p, rid in victims if delete(rid, f"папка «{p}» — её нет локально", "--yes")}
    for rid in trashed:
        delete(rid, f"из корзины заметка {rid}")
    if not dry:
        forget(set(folders) - gone | {"DefaultFolder-CloudKit"}, gone_notes)


def forget(live_folders, gone_notes):
    """icloud-md помнит записи в state.json и не узнаёт, что их удалили мимо него: pull создаёт
    папки пустыми заново, перенос в папку с тем же именем сервер отклоняет, а удалённую заметку
    со старым путём (Head:/ и HEAD:/ на маке один каталог) pull снёс бы локально. Забываем."""
    path = os.path.join(VAULT, ".icloud-md", "state.json")
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    stale = [rid for rid in state.get("folders", {}) if rid not in live_folders]
    for rid in stale:
        del state["folders"][rid]
    for rid in gone_notes:
        state.get("notes", {}).pop(rid, None)
    if stale or gone_notes:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)


last_pull = ""  # что пришло при последнем pull — load показывает это пользователю


def do_pull():
    global last_pull
    last_pull = icloud("pull")
    say(last_pull)
    if conflicts():
        raise Conflict
    commit("notes: из Notes")


def do_push():
    if conflicts():
        raise Conflict
    out = icloud("push")
    # Повторяем, пока icloud-md видит неотправленное: заметка успела поменяться в Notes
    # («re-run push»), или файл переименовали вместе с правкой текста — за один push уходит
    # только перенос, а новый текст остаётся висеть в status как modified.
    # Отказы («N refused») повторный push не лечит — на них не крутимся.
    for _ in range(3):
        status = icloud("status")
        if "Nothing to push" in status or "0 to create, 0 changed, 0 to delete" in status:
            break
        out += "\n" + icloud("push")
        if conflicts():
            raise Conflict
    else:
        status = icloud("status")
    say(out)
    cleanup()
    commit("notes: правка")
    bad = refused(status)
    if bad:
        raise Refused(refused_message(bad))


def date_of(name, today):
    m = DAY_FILE.match(name)
    if not m:
        return None
    d = datetime.date(today.year, int(m.group(3)), int(m.group(2)))
    if (d - today).days > 180:
        d = d.replace(year=d.year - 1)
    elif (today - d).days > 180:
        d = d.replace(year=d.year + 1)
    return d


def week_dir(d):
    mon = d - datetime.timedelta(days=d.weekday())
    sun = mon + datetime.timedelta(days=6)
    left = f"{mon:%d}" if mon.month == sun.month else f"{mon:%d.%m}"
    return os.path.join(VAULT, "Планы", f"Неделя {left}–{sun:%d.%m}")


def today():
    """Вчера, сегодня и завтра — в «Заметки» (папка Notes), остальные дни — в папку своей недели:
    Планы/Неделя 14–20.09/Пн 14.09.md. Завтра нет нигде — создаём пустым. Имя файла = название заметки."""
    global quiet
    # Сначала pull: переносить можно только свежие файлы, иначе push откажет «changed remotely».
    was, quiet = quiet, True
    do_pull()
    quiet = was
    day = datetime.date.today()
    keep = [day + datetime.timedelta(days=n) for n in (-1, 0, 1)]
    root = os.path.join(VAULT, "Notes")
    moved = []
    for f in sorted(os.listdir(root)):
        d = date_of(f, day)
        if d and d not in keep:
            dst = week_dir(d)
            os.makedirs(dst, exist_ok=True)
            os.rename(os.path.join(root, f), os.path.join(dst, f))
            moved.append(f"{f[:-3]} → Планы/{os.path.basename(dst)}")
    for d in keep:
        name = f"{DAYS[d.weekday()]} {d:%d.%m}.md"
        if os.path.exists(os.path.join(root, name)):
            continue
        # Ищем по всем Планам: с телефона день могут переложить не в свою неделю.
        for dirpath, _, files in os.walk(os.path.join(VAULT, "Планы")):
            if name in files:
                os.rename(os.path.join(dirpath, name), os.path.join(root, name))
                moved.append(f"{name[:-3]} → Заметки")
                break
        else:
            if d > day:
                with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                    fh.write(f"# {name[:-3]}\n")
                moved.append(f"{name[:-3]} — создан в Заметках")
    if not moved:
        say("дни на месте")
        return
    say("\n".join(moved))
    quiet = True
    do_push()


def show_map():
    print(VAULT)
    for dirpath, dirs, files in os.walk(VAULT):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "attachments")
        rel = os.path.relpath(dirpath, VAULT)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if rel != ".":
            print("  " * depth + os.path.basename(dirpath) + "/")
        for f in sorted(files):
            if f.endswith(".md"):
                print("  " * (depth + 1) + f[:-3])


def touches_notes(payload):
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    return bool(path) and os.path.realpath(path).startswith(os.path.realpath(VAULT) + os.sep)


def main():
    global quiet
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not os.path.isdir(VAULT):
        sys.exit(f'нет клона {VAULT} — сделать: icloud-md clone "{VAULT}"')
    if cmd == "ls":
        return show_map()
    if cmd == "load":  # загрузка скилла: pull → раскладка дней → карта, строго по очереди
        lock = open(LOCK, "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        head = git("rev-parse", "HEAD").stdout.strip()
        try:
            quiet = True
            today()
            cleanup()  # пустые папки и корзину — и при /notes, не только после правки
        except Conflict:
            print(conflict_message())
        except Refused as e:
            print(e)
        lock.close()
        # Без этого по карте не видно, пришло ли что-то: отчёт писался бы вслепую.
        changes = [l.strip() for l in last_pull.splitlines() if re.match(r"\s*(modified|added|deleted|moved|renamed):", l)]
        print("Из Notes: " + ("; ".join(changes) if changes else "ничего нового"))
        commits = git("log", "--format=%h %s", f"{head}..HEAD", "--", "Notes").stdout.strip()
        print("Коммиты: " + (commits.replace("\n", "; ") if commits else "нет"))
        return show_map()

    hook = cmd in ("pre", "post")
    if hook:
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            payload = {}
        if cmd == "pre" and not touches_notes(payload):
            return
        if cmd != "pre" and not dirty():
            return
        quiet = True
    elif cmd not in ("pull", "push", "today", "sync"):
        sys.exit(__doc__)

    # Хуки и ручной вызов не должны гонять icloud-md одновременно.
    lock = open(LOCK, "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        {"pull": do_pull, "push": do_push, "today": today, "sync": lambda: (today(), do_push()), "pre": do_pull}.get(cmd, do_push)()
    except Conflict:
        if hook:
            # Код 2: PreToolUse блокирует правку, PostToolUse — сообщает; stderr уходит Claude.
            print(conflict_message(), file=sys.stderr)
            sys.exit(2)
        print(conflict_message())
        sys.exit(3)
    except Refused as e:
        if hook:
            print(e, file=sys.stderr)
            sys.exit(2)
        print(e)
        sys.exit(3)


if __name__ == "__main__":
    main()
