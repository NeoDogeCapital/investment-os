# Mac Mini Setup — Scott (Full Access)

Full database access, own API key. **Niko is the pipeline owner** — see Operating
Agreement at the bottom.

## 1. Prerequisites (on the mini)

```bash
xcode-select --install
```

Installs git + python3. Accept the dialog and wait for it to finish.

## 2. GitHub access

Niko adds Scott's GitHub account as a collaborator on
`NeoDogeCapital/investment-os` (Settings → Collaborators), **or** issues a
fine-grained read-only token for the repo. Scott signs in with
`gh auth login` or uses the token when cloning.

## 3. Clone + install (one command)

```bash
git clone https://github.com/NeoDogeCapital/investment-os.git ~/investment-os && bash ~/investment-os/scripts/new_device_setup.sh
```

The installer creates a venv, installs dependencies, and prompts for the three
`.env` values:

| Key | Value for Scott's mini |
|---|---|
| `DATABASE_URL` | Same Supabase connection string as Niko's (full access) — transfer via AirDrop or password manager, never email/text |
| `ANTHROPIC_API_KEY` | **Scott's own key** (Niko creates it in the Anthropic console) — keeps usage/billing attributable |
| `OBSIDIAN_VAULT_PATH` | Path to the **shared** Research Vault once folder sharing syncs (step 4), e.g. `/Users/scott/Documents/Research Vault` |

## 4. Shared research vault (Clippings access)

Scott contributes research by adding notes to the **shared Clippings folder**;
Niko's daily pipeline run ingests them. Set this up with iCloud folder sharing
(both machines are Macs):

1. On Niko's Mac: Finder → right-click `~/Documents/Research Vault` →
   Share → Collaborate → invite Scott's Apple ID with **"Anyone can make changes."**
2. On the mini: accept the invite; the vault appears under iCloud Drive.
3. Point the mini's `OBSIDIAN_VAULT_PATH` (and Obsidian, if installed) at that
   shared location.

Scott then saves web clips / notes / PDFs into `Research Vault/Clippings/` from
the mini, exactly as Niko does. They sync over and are processed on the next
pipeline run. (Alternative if iCloud sharing is flaky: Obsidian Sync, paid.)

## 5. Verify

```bash
cd ~/investment-os && source .venv/bin/activate && python3 scripts/test_connection.py
```

Should show the Supabase connection and table counts. The `iwp` shell alias
(added by the installer) loads the environment in future sessions.

## Operating Agreement

Full access means the mini **can** run everything. To keep one source of truth:

- **Niko runs the daily cadence** — inbox processor, regime scanner, macro memo,
  trade gates, publishing. The mini does not run these on a schedule.
- **Scott's mini is for:** adding research to Clippings, querying the database,
  viewing/generating reports locally, ad-hoc analytics
  (`python3 scripts/analytics_engine.py --all` is safe to run — it never
  overwrites the holdings book).
- **Do not run `inbox_processor.py` from the mini** — two machines ingesting
  the same Clippings folder creates duplicate research notes in the regime data
  (this bit us before; single-ingester is the fix).
- Trades go through the gates **on Niko's machine only**, so the decision
  journal has one author.
- The retired VaultWatcher app is not part of setup — do not install or enable
  any auto-ingestion on the mini.

## Reference

- Live dashboards (no local setup needed): https://neodogecapital.github.io/investment-os
- Project docs: `CLAUDE.md` (model mechanics, 3-tier regime system, gate rules)
