# Archivio del forum di Azzurra

Recupero di `forum.azzurra.org` (2001-06-28 → 2016-07-29) dalla Wayback Machine, insieme
agli strumenti che l'hanno tirato giù, all'importatore che lo trasforma in un database
interrogabile e al generatore che ne ricava un sito statico. Il forum non esiste più: qui
c'è tutto quello che l'Archive aveva ancora.

Il forum ha cambiato software due volte — phpBB 1.4.0, poi phpBB 2.0.x, infine vBulletin —
e le tre generazioni stanno in **un corpus solo**: non erano forum diversi, era lo stesso
forum con un motore nuovo. Il mirror del vecchio board (`oldboard/`) viene ricucito dentro
i thread di vBulletin, con il dedup che riconosce i post già presenti.

Il risultato è online: **<https://vjt.github.io/azzurra-forum-archive/>**
(il vecchio indirizzo `sindro.me/t/forum-azzurra/` reindirizza qui, link profondi
compresi). Lo costruisce GitHub Actions a ogni push e lo pubblica su Pages, quindi
il sito nel repository non entra mai; il database e una copia offline sono allegati
alle [release](https://github.com/vjt/azzurra-forum-archive/releases), perché
`forum.db` da solo pesa 169 MB e GitHub rifiuta i file oltre i 100 MB.

## Cosa c'è nel repository

| Percorso | Cos'è |
|----------|-------|
| `pages/` | 8834 snapshot HTML grezzi, byte per byte, ISO-8859-1. **La parte irripetibile.** |
| `retry/` | 1314 snapshot alternativi dei thread importati vuoti: i candidati perdenti restano, rifarli costa ore. |
| `assets/` | 530 immagini allegate ai post (12 MB), recuperate dall'Archive e indirizzate per hash SHA-1. |
| `smilies/` | 508 faccine della board originale. |
| `oldboard/` | 1604 pagine del vecchio board phpBB (1.4.0 e 2.0.x, 2001-2004), stessa regola di `pages/`: write-once. |
| `forum_import.py` | HTML → SQLite. Ricostruisce `forum.db` da zero a ogni giro (~30 s). |
| `oldboard_import.py` | Le pagine phpBB → tabelle di appoggio `old_posts` / `old_topics` / `old_forums` (~3 s). |
| `oldboard_merge.py` | Ricuce i thread del mirror dentro quelli di vBulletin e inserisce i post che mancano (~18 s). |
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

# 4. ricostruisci il database da quello che c'è su disco (sempre da zero)
make db
```

E per rifare tutto — database, sito e indice di ricerca — basta:

```sh
make          # db (~3 min) + 7229 pagine (~20 s) + ricerca (~30 s)
make serve    # e lo si guarda su http://localhost:8000/
```

`make check` ristampa i numeri di questo README interrogando il database. I singoli
passi restano `make db`, `make site`, `make search`.

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

Stato attuale: **114 forum, 7070 discussioni, 159484 post** (133825 dal lo-fi, 20373 dallo
showthread, 3056 dal phpBB 2.0, 2230 dal phpBB 1.4.0), 22 post senza data, arco
`2001-06-28T22:11` → `2016-07-29T16:07`.

Schema: `forums` / `threads` / `posts`, più un indice FTS5 `posts_fts` su nome utente e
testo (`unicode61 remove_diacritics 2`). Ogni post conserva sia `body_html` sia
`body_text`; `source` dice da quale markup arriva. `threads.old_topic_id` e
`posts.old_post_id` tengono la numerazione phpBB, ed è quella che rende risolvibili in
locale i vecchi link `viewtopic.php`.

## Un forum, due numerazioni

vBulletin si portò dietro il contenuto phpBB — nel database ci sono già 4928 post del
2001-2002 — quindi il mirror non è un `append`, è una **fusione con dedup**. Il dedup non
può usare l'orario da solo: fra i due corpus c'è uno scarto di un'ora (il cambio d'ora
attorno alla migrazione) e due post dello stesso utente a due minuti di distanza sono post
diversi, non doppioni. La chiave che regge è il **corpo** (token set, contenimento ≥ 0.8 e
Jaccard ≥ 0.5) dentro 180 s da uno degli scarti 0/+1h/−1h.

Due cose che sembravano dettagli e valevano 296 doppioni rimasti in pagina:

- **l'autore è un indizio, non una chiave.** L'importatore di vBulletin riscrisse i nick
  che non sapeva scrivere — `C|ty_Hunter` diventò `City_Hunter`, `_theone_` diventò
  `theo` — e indicizzare per nome lasciava passare 175 copie. Oggi il nome pesa solo come
  conferma, ed è obbligatorio soltanto sotto le cinque parole, dove «quoto» non identifica
  niente.
- **lo scarto si misura per post, non per thread.** Una discussione che va da marzo a
  novembre attraversa il cambio d'ora e ha doppioni a due scarti diversi: sceglierne uno
  per tutto il thread ne lasciava in piedi altri 104. L'interleaving usa lo scarto del
  doppione più vicino nel tempo.

Risultato misurato: 8686 post nel mirror, **5286 nuovi** e 3400 già presenti; 924 topic
ricuciti su un thread esistente, 505 diventati thread nuovi, 48 lasciati staccati apposta
(stesso titolo, nessun post in comune, oltre un anno di distanza). Otto forum che avevano
raggiunto il crawler senza nome hanno riavuto il loro dal mirror. I 30 doppioni che
restano sono doppi post veri del board, non copie.

## L'ordine dei messaggi

Un thread si legge in ordine di data, e per tre motivi diversi non era così in 144
discussioni. Nessuno dei tre era il parser: erano tre modi di credere che la posizione nella
pagina scaricata fosse la posizione nel forum.

- **La pagina del vecchio board tiene dieci post, non quindici.** Ogni `start=` del mirror è
  un multiplo di 10, e dividere per 15 faceva finire `topic281_s0` e `topic281_s10` sulla
  stessa pagina 1: la fusione, che ordinava per `(pagina, posizione)`, li incrociava uno a
  uno, e il *Sondaggio : chi è il/la più gnocco/a di #punkitalia??* alternava il 1° ottobre
  col 29. Erano 72 topic. Oggi l'ordine del mirror è l'**id del post phpBB**, che è la
  numerazione del board e concorda con l'orologio in 8685 righe su 8686.
- **Quattrocentosessanta date erano su dodici ore.** vBulletin rende l'orario secondo le
  preferenze di chi guarda, e in quelle pagine dice `01:21 PM`: leggere l'ora e buttare via
  il marcatore metteva il post dodici ore prima, cioè una risposta del pomeriggio sopra la
  domanda del mattino.
- **`seq` è la posizione nello *snapshot*, non nel thread.** Due copie della stessa pagina
  prese a mesi di distanza non concordano — un post cancellato in mezzo sposta tutti quelli
  dopo — e la dimensione della pagina non è nemmeno costante fra una scansione e l'altra.
  Dove il post ha il suo id vero l'ordine torna a essere quello (721 post rimessi a posto);
  dove il thread esiste solo in lo-fi, e id non ce ne sono, l'unico ordine disponibile è
  l'orologio.
- **L'ora del mirror va anche mostrata, non solo usata.** Il post del vecchio board viene
  inserito nel thread con lo scarto misurato sui doppioni vicini; salvarlo poi con l'ora
  grezza lo faceva comparire alle 09:53 sotto le 10:53 a cui rispondeva.

Restano **8 discussioni su 7070** con un salto all'indietro: lì gli id dicono che l'ordine è
quello giusto ed è l'orologio a mentire, perché due pagine dello stesso thread furono
salvate dall'Archive ai due lati del cambio d'ora. Fra il rimettere in ordine i post e il
credere all'orologio, vince il board.

## Tre markup, un forum

Dieci anni di skin vBulletin stanno qui dentro, e l'importatore li parsa tutti:

- **archivio lo-fi** (`t-N.html`) — `div.post` > `div.username` / `div.date` /
  `div.posttext`. Nessun id di post, quindi `seq` è la posizione nella pagina.
- **showthread completo** (`st-N.html`) — `table#postN`, `a.bigusername`,
  `id="postcountN"`. Porta l'id vero del post e quello del membro, quindi vince sul lo-fi
  a parità di posizione.
- **`azzurra2.0`, 2001-2003** (id di thread bassi) — una skin di showthread senza i
  delimitatori `<!-- status icon and date -->`: la data sta nuda dentro `td.thead`.

E due in più nel mirror del vecchio board, che `oldboard_import.py` parsa a parte:

- **phpBB 2.0.x** (`class="postbody"`) — quattro patch level (2.0.1, 2.0.2, 2.0.5, 2.0.8)
  che rendono lo stesso subSilver: un parser solo, il patch level non conta. Il template
  però è tradotto in due modi — «Leggi il Topic» e «Visualizza topic» — e un regex che
  conosceva solo il primo lasciava 289 topic senza titolo, quindi non agganciabili.
- **phpBB 1.4.0** — nessuna classe CSS, solo `<FONT FACE="Verdana">` e tabelle. Ci si
  ancora alla *forma* della riga (`<TR ... BGCOLOR="#xxxxxx" ... ALIGN="LEFT">`) e a
  `Registrato:` / `Inviato:` / `_________________`: **non** al colore, che cambiò quando
  il board fu riskinnato e faceva sparire in silenzio le pagine dell'altra skin. Il corpo
  sta fra due `<HR>`, ma fra il **primo e l'ultimo** della riga, non fra il primo e il
  secondo: una citazione è una tabella che si porta dietro due `<HR>` suoi, e chiudere sul
  secondo tagliava 213 post alla loro prima citazione.

Le due generazioni phpBB disegnavano la citazione come tabella, non come BBCode, e la
fetta del corpo ereditava dal markup intorno un tag di chiusura orfano (2408 `</font>` in
1.4.0, 445 `</span>` in 2.0.x): il primo chiudeva il `<font color>` della skin e rendeva
illeggibile mezza pagina, il secondo faceva fallire ogni passata ancorata all'inizio del
corpo. Oggi il tag orfano cade all'import e il renderer riscrive quelle tabelle nello
stesso `blockquote.bbq` di tutte le altre citazioni del sito.

## Snapshot troncati

Molti snapshot sono stati salvati a metà: la testa c'è, l'ultimo `posttext` non riceve mai
il suo `</div>` di chiusura. L'importatore accetta un corpo che finisce a EOF e gli mette
`posts.truncated = 1` — oggi sono 771. Mezzo post del 2001 vale più di nessun post.

```sql
SELECT count(*) FROM posts WHERE truncated = 1;
```

## Buchi noti

- **596 discussioni hanno una pagina su disco e zero post** (erano 764: 168 le ha riempite
  il mirror phpBB): lo snapshot è stato tagliato
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
