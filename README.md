# champions-data

Usage mensili di Pokémon Champions (Pokémon più usati, mosse, strumenti, abilità, spread, compagni) presi dalle statistiche pubbliche di Smogon / Pokémon Showdown e ridotti a file JSON leggeri per l'app.

Fonte dei dati: [Smogon – Pokémon Showdown usage stats](https://www.smogon.com/stats/). Sono dati del simulatore Showdown, non del ranked in-game: citali sempre nell'app.

## Come funziona

- `scripts/build_usage.py` trova l'ultimo mese su Smogon, sceglie da solo il formato giusto (oggi `...regmc...`, domani `...regmd...`), scarica il file completo e lo riduce.
- Prima di salvare confronta i propri calcoli con i file di testo di Smogon. Se non coincidono si ferma e non pubblica nulla.
- `mapping.json` collega gli ID di Showdown agli ID del database dell'app: ogni Pokémon, mossa, strumento e abilità nei dati ha il suo `game_id`.
- `.github/workflows/update-usage.yml` lo esegue in automatico dal 1° al 5° giorno di ogni mese. Se non c'è niente di nuovo non fa nulla.
- I risultati finiscono in `docs/`, pubblicata con GitHub Pages.

## Attivazione (una volta sola)

1. **Crea un repository pubblico** su GitHub, per esempio `champions-data`, e caricaci tutti i file di questa cartella.
   La cartella `.github` è nascosta: se caricando dal sito non compare, usa *Add file → Create new file*, scrivi come nome `.github/workflows/update-usage.yml` e incolla il contenuto del file.
2. **Attiva GitHub Pages**: *Settings → Pages → Build and deployment → Source: Deploy from a branch*, ramo `main`, cartella `/docs`, *Save*.
3. **Primo avvio a mano**: scheda *Actions → Aggiorna usage → Run workflow*, spunta *Rigenera* e avvia. Dopo un minuto i dati sono su
   `https://<tuo-utente>.github.io/champions-data/latest.json`.
4. **Dominio tuo (consigliato)**: in *Settings → Pages → Custom domain* scrivi per esempio `data.tuodominio.it`. Poi su Cloudflare, in *DNS*, aggiungi un record `CNAME` con nome `data` e destinazione `<tuo-utente>.github.io`, con proxy disattivato (nuvola grigia). Quando GitHub ha emesso il certificato, attiva *Enforce HTTPS*.
   Nell'app usa questo indirizzo: se un giorno sposti i dati altrove, l'app non va aggiornata.

Se il passo 3 fallisce con un errore di permessi sul `git push`, vai in *Settings → Actions → General → Workflow permissions* e scegli *Read and write permissions*.

Quando un avvio automatico fallisce, GitHub ti manda una email. Il log nella scheda *Actions* dice cosa non va.

## Cosa legge l'app

**`latest.json`** (piccolo, da controllare all'avvio):

```json
{
  "month": "2026-09",
  "source": { "name": "Smogon / Pokémon Showdown", "url": "https://www.smogon.com/stats/" },
  "datasets": {
    "doubles_bo3": {
      "format": "gen9championsvgc2026regmcbo3",
      "regulation": "M-C",
      "rating": 1760,
      "battles": 223477,
      "url": "data/doubles_bo3/2026-09.json",
      "history": ["2026-09"]
    }
  }
}
```

Logica consigliata: se `month` è diverso da quello che l'app ha già salvato, scarica `url` (relativo allo stesso indirizzo di `latest.json`) e aggiorna il database locale.

**`data/<dataset>/<mese>.json`**: un elemento per Pokémon, ordinati per utilizzo.

```json
{
  "rank": 5, "name": "Salamence-Mega", "id": "salamencemega",
  "game_id": "salamence_mega", "base_game_id": "salamence", "usage": 24.51,
  "abilities": [{ "id": "aerilate", "game_id": 184, "pct": 100.0 }],
  "items":     [{ "id": "salamencite", "game_id": 769, "pct": 100.0 }],
  "moves":     [{ "id": "protect", "game_id": 182, "pct": 98.0 }],
  "spreads":   [{ "spread": "Timid:2/0/0/32/0/32", "pct": 19.6 }],
  "teammates": [{ "name": "Rillaboom", "id": "rillaboom", "game_id": "rillaboom", "pct": 56.2 }]
}
```

- `game_id`: l'ID nel tuo database (`pokemon.id`, `moves.id`, `items.id`, `abilities.id`). È quello che l'app deve usare.
- `base_game_id`: solo per le Mega, la specie da mettere in squadra con la sua pietra.
- `id`: l'ID di Showdown, utile solo per il debug.
- `usage`: percentuale di squadre che lo usano (in doppio la somma di tutti è circa 600%).
- `pct` delle mosse: percentuale di set che hanno quella mossa (per questo la somma è circa 400%).
- Lo strumento `nothing` (nessuno strumento) ha `game_id` nullo.

## Collegamento al database dell'app (`mapping.json`)

`mapping.json` è già generato dal tuo `data.db` attuale e copre tutti i Pokémon, le mosse, gli strumenti e le abilità del formato Champions su Showdown.

Rigeneralo quando cambi il database (per esempio aggiungi Pokémon o forme):

```
python3 scripts/build_mapping.py percorso/data.db
```

Poi fai commit di `mapping.json`: al prossimo avvio la Action rigenera da sola i dati con i nuovi ID, senza bisogno di *Rigenera*. Il database non va caricato nel repository, serve solo sul tuo PC.

Se compare qualcosa di non collegato:
- lo vedi nel log della Action e nel campo `unmapped` di `latest.json`, ma i dati vengono pubblicati comunque (con `game_id` nullo per quelle voci);
- se esiste nel tuo database con un ID diverso, aggiungilo a `mapping_overrides.json`, per esempio `"pokemon": { "nomeshowdown": "id_nel_database" }`, e rilancia `build_mapping.py`.

## Configurazione (`config.json`)

- `datasets`: quali formati generare. Di default solo `doubles_bo3`; metti `"enabled": true` anche su `doubles_bo1` (più dati, meno vicino ai tornei) o `singles`.
- `override`: forza un formato preciso, per esempio `"gen9championsvgc2026regmcbo3"`. Lascia `null` per la scelta automatica.
- `min_battles`: quando nello stesso mese ci sono due regolamenti, prende il più recente solo se ha almeno queste battaglie, altrimenti quello precedente.
- `rating`: fascia di Smogon (0, 1500, 1630, 1760). 1760 dà più peso ai giocatori forti.
- `min_usage_pct`, `min_entry_pct`, `top`: quanto tagliare per tenere i file leggeri.

Dopo una modifica alla configurazione, rilancia la Action a mano con *Rigenera*.

## Provarlo sul tuo PC

Serve solo Python 3.9 o successivo, nessuna libreria da installare:

```
python3 scripts/build_usage.py --force
```

I file vengono scritti in `docs/`. Con `--month 2026-08` puoi recuperare un mese passato: finisce nello storico senza cambiare `latest.json`.
