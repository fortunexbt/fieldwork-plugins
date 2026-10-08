---
name: tablebeam
description: Profile a user-selected CSV and answer exact filter, aggregate, or group questions with source-row evidence.
---

# Tablebeam

Use Tablebeam when the user asks what a CSV contains or asks a question that can be answered with a literal filter, count, sum, average, minimum, maximum, or grouped aggregate. It reads the named file locally with Python's standard library; it sends no data to a service.

Run commands from the installed `skills/tablebeam` directory that contains this `SKILL.md`; `scripts/` and `assets/` paths below are relative to that directory. The included synthetic example needs no setup or manually created files:

```bash
python3 scripts/tablebeam.py profile assets/demo-sales.csv
python3 scripts/tablebeam.py query assets/demo-sales.csv \
  --spec assets/demo-query.json \
  --export ./north-by-owner.csv --export-format csv --preview
```

The demo profiles six synthetic sales rows, then previews grouped revenue for North. Review the exact results and export SHA-256, then repeat the query without `--preview` to write the CSV. Set `--export` to a writable path in the user's workspace if the installed skill directory is read-only. For a user's own data, profile their explicit CSV path in place of `assets/demo-sales.csv`.

The profile reports the detected delimiter, chosen encoding, row and column counts, missing cells, sample values, and whether numeric-looking columns could be identifiers. It shows up to five sample values of 200 characters each and the first 100 columns by default; truncation is marked in the profile, and `--all-columns` returns every column. The default encoding is strict `utf-8-sig`. Pass `--encoding latin-1` or another Python-supported encoding when the file uses a different encoding. Pass `--delimiter ';'` or `--delimiter TAB` if delimiter detection is uncertain. CSV quoting follows the standard double-quote rules.

Translate a natural-language request into the JSON query schema below. Use the exact column names from the profile. Keep raw identifiers and text filters as strings. For ordered numeric filters, set `type` to `decimal` and give the threshold as a string. Apply multiple filters together. If a requested column, operator, or filter value is ambiguous, ask the user which one they mean; do not guess, evaluate expressions, or generate code from the question.

Example: sum `revenue` for North rows and group by `status`:

```json
{
  "where": [{"column": "region", "op": "eq", "value": "North"}],
  "group_by": ["status"],
  "aggregate": {"op": "sum", "column": "revenue"}
}
```

Save that spec as a UTF-8 JSON file, then run it:

```bash
python3 scripts/tablebeam.py query ./sales.csv \
  --spec ./query.json \
  --export ./sales-by-status.csv --export-format csv --preview
```

Review the JSON result rows and planned export byte count and SHA-256, then repeat the command without `--preview` to create the file. The command returns one JSON envelope on stdout. It includes exact results, source-row coverage ranges, and explicit preview counts. Data-row IDs start at 1 after the header and skip blank physical records; source line ranges are inclusive. Raw row queries and grouped tables show at most 50 rows or groups, and coverage previews show at most 100 ranges; the `preview` and `ranges_truncated` fields say when output is shortened. The query still evaluates every matching record. CSV exports contain all matching raw rows or all aggregate groups. JSON exports contain all raw result rows or all groups with exact source row IDs and full coverage ranges. The export is created only at the requested path, never overwrites an existing file, and returns a byte count and SHA-256 receipt after verifying the saved bytes. Use `--export-format json` when the downstream reader must retain the full evidence structure and exact average fraction.

Supported query fields are:

- `where`: a list of `{ "column", "op", "value", "type" }` filters. Supported operators are `eq`, `ne`, `contains`, `starts_with`, `ends_with`, `gt`, `gte`, `lt`, `lte`, `is_null`, and `is_not_null`. Filters combine with AND. Text equality is case-sensitive; text contains/prefix/suffix checks are case-insensitive. Comparisons skip missing cells; use `is_null` to select them.
- `group_by`: a list of exact column names. Empty cells and absent short-row cells make one missing-value group. Group order follows first appearance in the file.
- `aggregate`: `{ "op": "count" }`, `{ "op": "count_non_missing", "column": "x" }`, or `{ "op": "sum|avg|min|max", "column": "x" }`. `count` includes every filtered row. Numeric operations skip blank or absent measure cells and reject any other value that is not a finite decimal.

CSV cells remain strings in the evidence, so values such as `9007199254740993` and identifiers with leading zeroes are not rounded or retyped. Only empty cells and absent cells in short rows are missing; spellings such as `NA` and `NULL` remain literal text. Sums and numeric comparisons use decimal arithmetic. An average includes an exact rational fraction and a 34-significant-digit decimal display. The profile calls numeric-shaped columns `numeric_candidate` only when the shape is clear; identifier-looking and generic integer columns are labeled separately as ambiguous.

The default delimiter detector checks comma, semicolon, tab, and pipe. An ambiguous multi-column choice is reported as `needs_attention`; specify the delimiter to resolve it. A single-column fallback is marked low-confidence because its delimiter has no effect. Empty or duplicate headers also return `needs_attention` and block name-based queries. Short records are padded with missing cells and extra cells remain visible in row evidence; both are reported. Malformed quoting, invalid decoding, or a non-decimal value used in arithmetic returns an error envelope and nonzero exit status.

The CLI uses Python 3.11+ and the standard library only. CSV exports are UTF-8 with a BOM. Formula-like headers and values are prefixed with an apostrophe in the exported CSV. The raw values remain unchanged in the source and in the JSON response; the response reports how many CSV cells were escaped. Supported inputs are delimited text CSV files up to 100 MiB and 250,000 data rows. Numeric tokens use dot-decimal syntax and are limited to 4,000 significant digits and exponent magnitude 10,000. The tool does not read Excel workbooks, infer locale-specific decimal commas, repair source files, or traverse directories.
