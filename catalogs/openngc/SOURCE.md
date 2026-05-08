# OpenNGC catalog

Source: https://github.com/mattiaverga/OpenNGC

Files in this directory are vendored copies of the OpenNGC database
(NGC + IC + named non-NGC objects), licensed CC-BY-SA-4.0. The full
license text lives at the upstream repo; attribution belongs to
Mattia Verga and the contributors listed there.

We use this catalog to enrich astrolab's targets with friendly names,
sky coordinates, magnitudes, and constellation. The backend reads
these CSVs into memory at startup — there's no derived/cached JSON,
the CSV is the single source of truth.

If you redistribute astrolab as a whole, the CC-BY-SA-4.0 terms apply
to these files specifically. The rest of astrolab is unaffected.

## Schema (semicolon-separated)

| Column        | Notes                                                 |
| ------------- | ----------------------------------------------------- |
| Name          | Primary catalog id (NGC0001, IC0001, B033, …)         |
| Type          | G, OC, GCl, PN, HII, DrkN, EmN, RfN, SNR, … (legend below) |
| RA / Dec      | Sexagesimal — convert before use                      |
| Const         | IAU constellation abbreviation                        |
| MajAx, MinAx  | Apparent size in arcminutes                           |
| B/V/J/H/K-Mag | Magnitudes in those bands (most populated: B, V)      |
| M             | Messier number, when applicable                       |
| NGC, IC       | Cross-references                                      |
| Common names  | Comma-separated friendly names                        |
| Identifiers   | Comma-separated cross-catalog ids                     |

## Object type legend (subset we care about)

- `G` — galaxy
- `OC` — open cluster
- `GCl` — globular cluster
- `HII` — H II region
- `EmN` — emission nebula
- `RfN` — reflection nebula
- `DrkN` — dark nebula
- `PN` — planetary nebula
- `SNR` — supernova remnant
- `Cl+N` — cluster with associated nebulosity
- `Neb` — nebula (uncategorized)
- `*` / `**` — single / double star
- `*Ass` — stellar association

## Refreshing

```sh
curl -sSL -o catalogs/openngc/NGC.csv \
  https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv
curl -sSL -o catalogs/openngc/addendum.csv \
  https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/addendum.csv
# Restart the FastAPI server — the catalog is loaded once at boot.
```
