# Archivio del forum di Azzurra

Recupero di `forum.azzurra.org` (vBulletin, 2001-06-28 → 2016-07-29) dalla Wayback
Machine, insieme agli strumenti che l'hanno tirato giù, all'importatore che lo trasforma
in un database interrogabile e al generatore che ne ricava un sito statico. Il forum non
esiste più: qui c'è tutto quello che l'Archive aveva ancora.

Il risultato è online: **<https://sindro.me/t/forum-azzurra/>**

## Cosa c'è nel repository

| Percorso | Cos'è |
|----------|-------|
| `pages/` | 8834 snapshot HTML grezzi, byte per byte, ISO-8859-1. **La parte irripetibile.** |
| `retry/` | 1314 snapshot alternativi dei thread importati vuoti: i candidati perdenti restano, rifarli costa ore. |
| `assets/` | 530 immagini allegate ai post (12 MB), recuperate dall'Archive e indirizzate per hash SHA-1. |
| `smilies/` | 508 faccine della board originale. |
| `forum_import.py` | HTML → SQLite. Ricostruisce `forum.db` da zero a ogni giro (~30 s). |
| `forum_render.py` | SQLite → sito statico: un indice, una pagina per forum, una per thread, più la ricerca. |
| `slow_get*.sh`, `batch_get*.sh`, `get_one.sh`, `retry_snaps.sh`, `retry_zero.sh` | I fetcher, nell'ordine in cui sono stati scritti. Quello che ha funzionato è `slow_get.sh`: pausa lunga, ripartibile, salta i file già pieni. |
| `fetch_cdx*.sh`, `cdx_*.t*` | Interrogazioni all'indice CDX della Wayback e loro output: le liste di target nascono da qui. |
| `fetch_assets.sh`, `assets_list.py`, `assets.tsv` | Il giro sulle immagini dei post: estrazione dei riferimenti, probe CDX, download. |
| `targets*.tsv` | Le liste di lavoro, una riga per URL, snapshot più recente in testa e i vecchi come riserva. |
| `pick_zero.py` | Promuove da `retry/` a `pages/` il candidato che estrae più post: sceglie *parsando*, non guardando la dimensione. |
| `bin/pagefind` | L'indicizzatore della ricerca, versionato per non dipendere da un download. |
| `build_index.py` | Il primo indicizzatore, usa e getta. Superato da `forum_import.py`. |

`forum.db` **non** è nel repository: 183 MB (oltre il limite GitHub di 100 MB per file) e
si rigenera da `pages/` in mezzo minuto. Stesso ragionamento per `site/`, che
`forum_render.py` ricostruisce in una ventina di secondi.

## Come ci si lavora

Quattro passi, in quest'ordine. I passi 1-3 servono finché restano buchi da chiudere; il
4 è quello da rifare dopo ogni modifica al parser.

```sh
# 1. elenca cosa ha la Wayback Machine (già fatto: l'output sta in cdx_*.t*)
./fetch_cdx_full.sh

# 2. scarica, con educazione. Una richiesta alla volta, ripartibile, rallenta se rifiutato.
DELAY=4 COOL=240 nohup ./slow_get.sh > slow_get.log 2>&1 &

# 3. secondo giro per i thread importati vuoti: tutti gli snapshot di entrambi i markup
#    finiscono in retry/, poi il candidato migliore viene promosso in pages/
DELAY=4 COOL=240 nohup ./retry_zero.sh > slow_get_zero.log 2>&1 &
python3 pick_zero.py --dry-run     # prima si guarda
python3 pick_zero.py               # poi si promuove

# 4. ricostruisci il database da quello che c'è su disco (~30 s, sempre da zero)
python3 forum_import.py
```

E per rifare il sito statico:

```sh
python3 forum_render.py                          # ~6700 pagine in una ventina di secondi
./bin/pagefind --site site --output-subdir pagefind
```

**Mai due fetcher insieme**, e prima di lanciarne uno si controlla:

```sh
ps -eo pid,cmd | grep '[s]low_get'      # `pgrep -f` matcha il proprio grep: non usarlo
```

Ricostruire costa mezzo minuto, quindi una correzione al parser si verifica a buon
mercato: cambia, rigira, conta.

```sh
sqlite3 forum.db "SELECT count(*) FROM threads WHERE post_count = 0"
sqlite3 forum.db "SELECT count(*) FROM posts WHERE truncated = 1"
```

Stato attuale: **99 forum, 6565 discussioni, 154198 post** (133825 dal lo-fi, 20373 dallo
showthread), 22 post senza data, arco `2001-06-28T22:29` → `2016-07-29T16:07`.

Schema: `forums` / `threads` / `posts`, più un indice FTS5 `posts_fts` su nome utente e
testo (`unicode61 remove_diacritics 2`). Ogni post conserva sia `body_html` sia
`body_text`; `source` dice da quale markup arriva.

## Tre markup, un forum

Dieci anni di skin vBulletin stanno qui dentro, e l'importatore li parsa tutti:

- **archivio lo-fi** (`t-N.html`) — `div.post` > `div.username` / `div.date` /
  `div.posttext`. Nessun id di post, quindi `seq` è la posizione nella pagina.
- **showthread completo** (`st-N.html`) — `table#postN`, `a.bigusername`,
  `id="postcountN"`. Porta l'id vero del post e quello del membro, quindi vince sul lo-fi
  a parità di posizione.
- **`azzurra2.0`, 2001-2003** (id di thread bassi) — una skin di showthread senza i
  delimitatori `<!-- status icon and date -->`: la data sta nuda dentro `td.thead`.

## Snapshot troncati

Molti snapshot sono stati salvati a metà: la testa c'è, l'ultimo `posttext` non riceve mai
il suo `</div>` di chiusura. L'importatore accetta un corpo che finisce a EOF e gli mette
`posts.truncated = 1` — oggi sono 771. Mezzo post del 2001 vale più di nessun post.

```sql
SELECT count(*) FROM posts WHERE truncated = 1;
```

## Buchi noti

- **764 discussioni hanno una pagina su disco e zero post**: lo snapshot è stato tagliato
  *prima* del corpo, quindi il file contiene solo `<head>` e la barra di navigazione. Non
  c'è niente da parsare: servono snapshot alternativi, cioè lavoro di scraping, non di
  parser.
- **12 discussioni sono perse per sempre**: `t-{3009,3222,6976,7382,8149,8286,8438,8439,10312,11104,11198,12455}`.
  Ogni snapshot che l'Archive elenca per loro restituisce un corpo vuoto.
- **22 post senza data**: lo snapshot non la conteneva.

## Lezioni imparate

Ognuna di queste è costata tempo vero. Sono scritte qui perché il giro successivo non le
ricompri.

**L'Archive punisce il parallelismo, in silenzio.** Il primo tentativo lanciava i batch in
parallelo e sembrava funzionare: HTTP 200 per tutto. Circa 2360 di quei 200 avevano il
**corpo di lunghezza zero** — un rate-limit che non si dichiara come errore. Scaricando in
serie, con pausa e periodo di raffreddamento, la stessa lista ha reso il 100%. L'`exit 7`
di `curl` è il rifiuto onesto; un 200 vuoto è quello disonesto, ed è il secondo a essere
pericoloso, perché `rc=0` si legge come successo.

**Un file non vuoto non è un file completo.** I fetcher saltano un target che ha già byte
su disco: giusto per riprendere, sbagliato per riparare. Le discussioni importate vuote
*avevano* tutte una pagina. Riparare vuole una directory di output separata, non una corsa
ripresa.

**Quelle pagine non le abbiamo troncate noi, le ha troncate l'Archive.** Riscaricarle
restituisce file identici byte per byte (`t-10` 1284, `t-12` 1386, `t-2` 1580). L'istinto
di incolpare il proprio trasferimento era sbagliato, e un solo refetch l'ha misurato in un
minuto. Quello che salva il contenuto è l'*altro* markup: lo `showthread` dello stesso
thread pesa 30 KB e restituisce post dove il lo-fi non ne dà nessuno. **Quando una forma
di una pagina è rovinata, si cerca un'altra forma della stessa pagina** prima di
dichiararla persa.

**`grep -c` conta le righe, e questi file sono una riga sola.** Segnalava 2 `bigusername`
in una pagina da 60 KB che ne conteneva dieci, e per un'ora l'indagine ha inseguito un
"terzo markup" immaginario. Su HTML a riga singola si usa `grep -o … | wc -l`. E queste
pagine sono ISO-8859-1, quindi `grep` le tratta come binarie e le salta *senza dirlo*:
sempre `grep -a`. **Una misura che mente è peggio di nessuna misura.**

**Il parsing severo butta via dati recuperabili.** Pretendere i delimitatori
`<!-- status icon and date -->` azzerava 1939 discussioni le cui pagine erano
perfettamente leggibili in una skin più vecchia; pretendere il `</div>` di chiusura
buttava via ogni snapshot tagliato a metà corpo dall'Archive. Entrambe le correzioni erano
due caratteri di regex e insieme hanno recuperato ~16k post. Si parsa quello che c'è, si
registra quello che manca (`posts.truncated`), e si lascia decidere a chi legge.

**Una regex non-greedy non chiude un tag annidato.** Il corpo del post veniva catturato
con `.*?` fino al primo `</div>`: dentro vBulletin le citazioni e i blocchi di codice
*sono* dei `<div>`, quindi 2744 post finivano tagliati sulla parola "Cita:" e 36 su
"Codice:". La chiusura giusta si trova contando i `<div>` annidati, non con una regex.

**vBulletin duplica ogni post in un blocco JavaScript.** `pd[N] = '...'` è la cache del
"mostra post" senza ricaricare, con i newline sfuggiti a barra rovesciata e i commenti
HTML spezzati sulle concatenazioni di stringa. Il parser ci pescava dentro e salvava 218
post con i `\r\n` letterali dentro il testo.

**Il probe CDX si chiede in testo, non in JSON.** Con `output=json` il timestamp arriva
dopo una virgola: un `sed` scritto per la forma vecchia restituiva campo vuoto per *tutti*
gli URL, e 1122 immagini su 1122 risultavano "mai catturate" mentre l'Archive le aveva.
Si usa `output=text&fl=timestamp`, che una riga per timestamp la dà davvero.

**I candidati si valutano parsandoli, non pesandoli.** Un troncamento che lascia solo la
testa può essere più grande di una discussione corta ma completa. `pick_zero.py` fa girare
i parser veri e ordina per post estratti, a parità per corpi interi: l'unica misura che
corrisponde a quello che si vuole.

**Il database è sacrificabile, le pagine no.** `forum.db` si ricostruisce da zero in mezzo
minuto, quindi una modifica al parser costa un comando e sull'output non c'è niente di
prezioso. `pages/` è l'opposto: dodici discussioni sono già perse per sempre, e non c'è
codice che le riporti indietro.

## Licenza e contenuti

Il codice è di chi l'ha scritto; i post sono di chi li ha scritti, e restano qui come
documento storico di una comunità che è esistita per quindici anni. Se sei l'autore di un
messaggio e vuoi che sparisca, si apre una issue.
