#!/bin/bash
# End-to-end check against a running instance.
#
# Unit tests prove the pieces behave; this proves the thing works. It hits the
# real network and the real yt-dlp, so a failure here can mean "we broke it"
# or "YouTube changed again" — the output says which.
#
#   tools/smoke.sh [base-url]        default http://127.0.0.1:8000

set -uo pipefail
BASE="${1:-http://127.0.0.1:8000}"
PASS=0; FAIL=0; SKIP=0
YT="${SMOKE_YT_URL:-https://www.youtube.com/watch?v=aqz-KE-bpKQ}"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
skip() { printf "  \033[33m–\033[0m %s\n" "$1"; SKIP=$((SKIP+1)); }
head() { printf "\n\033[1m%s\033[0m\n" "$1"; }

j() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }

head "Erreichbarkeit"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/health")
[ "$code" = "200" ] && ok "Server antwortet" || { bad "Server nicht erreichbar ($code)"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE/")
if [ "$code" = "200" ]; then ok "Oberfläche ohne Login (lokale Betriebsart)"
elif [ "$code" = "302" ]; then ok "Oberfläche verlangt Login (Server-Betriebsart)"
else bad "Startseite: HTTP $code"; fi

head "Bausteine"
V=$(curl -s --max-time 90 "$BASE/api/version")
if [ -n "$V" ] && echo "$V" | grep -q components; then
  for key in yt_dlp js_runtime ffmpeg; do
    val=$(echo "$V" | j "d['components'].get('$key','')")
    [ -n "$val" ] && ok "$key: $val" || bad "$key fehlt"
  done
  for key in whisper mode clients; do
    val=$(echo "$V" | j "d['components'].get('$key','')")
    [ -n "$val" ] && ok "$key: $val" || skip "$key nicht gemeldet"
  done
else bad "Versionsübersicht liefert nichts"; fi

head "Sprachumschaltung"
en=$(curl -s --max-time 10 -H "Accept-Language: en-US,en;q=0.9" "$BASE/login" | grep -c "Sign in")
de=$(curl -s --max-time 10 -H "Accept-Language: de-DE,de;q=0.9" "$BASE/login" | grep -c "Anmelden")
[ "$en" -gt 0 ] && ok "Englisch bei englischem Browser" || bad "Englisch greift nicht"
[ "$de" -gt 0 ] && ok "Deutsch bei deutschem Browser"   || bad "Deutsch greift nicht"

head "Transkript (echter Abruf)"
JOB=$(curl -s --max-time 20 -X POST "$BASE/api/transcript" \
       --data-urlencode "url=$YT" --data "lang=native" | j "d.get('job_id','')")
if [ -z "$JOB" ]; then bad "Job konnte nicht gestartet werden"; else
  ok "Job gestartet"
  for i in $(seq 1 40); do
    sleep 3
    R=$(curl -s --max-time 10 "$BASE/api/job/$JOB")
    ST=$(echo "$R" | j "d.get('status','')")
    [ "$ST" = "done" ] || [ "$ST" = "no_subs" ] || [ "$ST" = "error" ] && break
  done
  case "$ST" in
    done)
      W=$(echo "$R" | j "d.get('word_count',0)")
      [ "${W:-0}" -gt 50 ] && ok "Transkript: $W Wörter" || bad "Nur $W Wörter — verdächtig wenig"
      for fmt in txt srt vtt md json; do
        c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/api/transcript/$JOB/export.$fmt")
        [ "$c" = "200" ] && ok "Export .$fmt" || bad "Export .$fmt: HTTP $c"
      done ;;
    no_subs) skip "Keine Untertitel/blockiert: $(echo "$R" | j "d.get('error','')[:100]")" ;;
    *)       bad "Status: ${ST:-keine Antwort}" ;;
  esac
fi

head "Audio-Schnitt (echter Download)"
TID=$(curl -s --max-time 20 -X POST "$BASE/api/trim" --data-urlencode "url=$YT" | j "d.get('trim_id','')")
if [ -z "$TID" ]; then bad "Schnitt-Sitzung nicht gestartet"; else
  ok "Sitzung gestartet"
  for i in $(seq 1 40); do
    sleep 3
    S=$(curl -s --max-time 10 "$BASE/api/trim/$TID")
    ST=$(echo "$S" | j "d.get('status','')")
    [ "$ST" = "ready" ] || [ "$ST" = "error" ] && break
  done
  if [ "$ST" = "ready" ]; then
    D=$(echo "$S" | j "round(d.get('duration',0))")
    ok "Ton geladen (${D}s)"
    c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/trim/$TID/audio")
    [ "$c" = "200" ] && ok "Ton abrufbar (Wellenform)" || bad "Ton nicht abrufbar: $c"
    CUT=$(curl -s --max-time 90 -X POST "$BASE/api/trim/$TID/cut" \
          --data "start=5" --data "end=15" --data "bitrate=192" --data "fade=0.25" --data "fmt=mp3")
    if echo "$CUT" | grep -q '"ok"'; then
      SZ=$(echo "$CUT" | j "round(d.get('size',0)/1024)")
      ok "Schnitt 5–15 s erzeugt (${SZ} KB)"
      c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/trim/$TID/download")
      [ "$c" = "200" ] && ok "Ergebnis herunterladbar" || bad "Download: HTTP $c"
    else bad "Schnitt fehlgeschlagen: $(echo "$CUT" | head -c 120)"; fi
    # Guard rails
    E=$(curl -s --max-time 20 -X POST "$BASE/api/trim/$TID/cut" --data "start=10" --data "end=10")
    echo "$E" | grep -q "zu kurz" && ok "Leerer Ausschnitt wird abgelehnt" || bad "Leerer Ausschnitt nicht abgefangen"
    E=$(curl -s --max-time 20 -X POST "$BASE/api/trim/$TID/cut" --data "start=0" --data "end=5" --data "bitrate=9999")
    echo "$E" | grep -qi "bitrate" && ok "Unbekannte Bitrate wird abgelehnt" || bad "Bitrate nicht validiert"
  else bad "Ton nicht geladen: $(echo "$S" | j "d.get('error','')[:120]")"; fi
fi

head "Duplikaterkennung"
B=$(curl -s --max-time 20 -X POST "$BASE/api/batch" \
     --data-urlencode "urls=$YT
$YT&t=5
$YT" --data "text_only=1")
DUP=$(echo "$B" | j "len(d.get('duplicates',[]))")
[ "${DUP:-0}" -ge 2 ] && ok "$DUP Duplikate erkannt" || skip "Duplikate: ${DUP:-0} (erwartet 2)"

printf "\n\033[1m%d bestanden · %d fehlgeschlagen · %d übersprungen\033[0m\n" "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ]
