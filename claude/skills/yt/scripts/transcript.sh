#!/usr/bin/env bash
# Достаёт субтитры видео и печатает их как чистый текст.
# Всегда завершается кодом 0: ненулевой код прерывает весь вызов скилла,
# и Claude не увидит содержимое вообще.

url=$1
# Лимит в БАЙТАХ, не символах: кириллица в UTF-8 занимает по два байта,
# а потолок Bash-тула считает байты.
max=${YT_MAX_BYTES:-20000}
# Полный транскрипт переживает вызов: в контекст идёт голова, хвост
# дочитывается чтением файла. Папку чистит ОС, не мы.
cache=${YT_CACHE:-${TMPDIR:-/tmp}/yt-transcripts}
mkdir -p "$cache"
d=$(mktemp -d)
err="$d/err.log"
trap 'rm -rf "$d"' EXIT

YTDLP=(uvx yt-dlp@latest --no-update --skip-download
       --ignore-no-formats-error
       --extractor-args "youtube:skip=dash,hls,translated_subs")

# JS-рантайм: без него извлечение из YouTube объявлено устаревшим.
for r in deno node bun; do
  command -v "$r" >/dev/null 2>&1 && { YTDLP+=(--js-runtimes "$r"); break; }
done

# Куки нужны, только если YouTube потребует авторизацию. Берём их из браузера,
# если профиль реально существует, иначе идём без них — на большинстве видео
# субтитры отдаются анонимно. Пути разные на macOS и Linux.
mac="$HOME/Library/Application Support"
for b in chrome chromium brave firefox; do
  case $b in
    firefox)  [ -d "$HOME/.mozilla/firefox" ] || [ -d "$mac/Firefox" ] || continue ;;
    chrome)   [ -d "$HOME/.config/google-chrome" ] || [ -d "$mac/Google/Chrome" ] || continue ;;
    chromium) [ -d "$HOME/.config/chromium" ] || [ -d "$mac/Chromium" ] || continue ;;
    brave)    [ -d "$HOME/.config/BraveSoftware/Brave-Browser" ] || [ -d "$mac/BraveSoftware/Brave-Browser" ] || continue ;;
  esac
  YTDLP+=(--cookies-from-browser "$b"); break
done

# Проход 1: узнать язык оригинала и id (~3 с).
meta=$("${YTDLP[@]}" --print "%(language)s %(id)s" "$url" 2>>"$err" | head -1)
L=$(printf '%s' "$meta" | cut -d' ' -f1 | cut -d- -f1)
id=$(printf '%s' "$meta" | cut -d' ' -f2)
[ "$L" = "NA" ] && L=""
[ -z "$id" ] && id=$(date +%s)

# Язык известен — просим только его. Просить сверх этого en.* значит словить
# HTTP 429 на видео, где английских субтитров нет.
langs="${L:+$L.*,}.*-orig"
[ -z "$L" ] && langs=".*-orig,en.*"

# Проход 2: скачать .vtt во временную папку. Заголовок ловим в переменную,
# чтобы он не перемешался с текстом.
title=$("${YTDLP[@]}" --no-simulate \
  --write-subs --write-auto-subs \
  --sub-langs "$langs" --sub-format vtt \
  --print "%(title)s — %(channel)s (%(duration_string)s) [${L:-orig}]" \
  -o "$d/s" "$url" 2>>"$err" | head -1)

# Качаются два .vtt (-orig и обычный) — берём один, иначе текст задвоится.
f=$(find "$d" -name '*-orig.vtt' | head -1)
[ -z "$f" ] && f=$(find "$d" -name '*.vtt' | head -1)

if [ -n "$f" ]; then
  full="$cache/$id.txt"
  sed -e 's/<[^>]*>//g' \
      -e 's/&nbsp;/ /g' \
      -e 's/&amp;/\&/g' \
      -e 's/&quot;/"/g' \
      -e "s/&#39;/'/g" \
      -e '/-->/d' \
      -e '/^WEBVTT/d' \
      -e '/^Kind:/d' \
      -e '/^Language:/d' \
      -e '/^[[:space:]]*$/d' \
      "$f" | uniq > "$full"

  [ -n "$title" ] && printf '%s\n\n' "$title"
  # Вывод сверх потолка Bash-тула приходит путём к файлу вместо текста,
  # поэтому режем сами — а хвост остаётся на диске и дочитывается.
  n=$(wc -c < "$full" | tr -d ' ')
  if [ "$n" -gt "$max" ]; then
    head -c "$max" "$full"
    printf '\n\n[ПОКАЗАНО %d из %d байт. Остаток НЕ выдумывать: полный транскрипт лежит в %s — дочитай его инструментом Read с offset, файл никуда не денется.]\n' \
      "$max" "$n" "$full"
  else
    cat "$full"
  fi
elif grep -qE '^ERROR|Unable to|Sign in to confirm' "$err"; then
  # Реальный сбой. Показать причину, а не выдавать её за отсутствие субтитров.
  echo "yt-dlp НЕ СМОГ получить субтитры. Скажи это и покажи причину, не выдумывай содержание видео:"
  grep -E '^ERROR|Unable to|Sign in to confirm' "$err" | sort -u | head -5
else
  echo "СУБТИТРОВ НЕТ — скажи это одной строкой, не обходи."
fi

exit 0
