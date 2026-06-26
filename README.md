# German study crosswords

Interactive crosswords for engaging with a German text — instead of just
reading it, you recall the vocabulary by solving for it.

The idea: an LLM is great at reading a German passage and producing
*answers + German clues*, but unreliable at the geometric part — packing words
into a legal interlocking grid, numbering them, and rendering a playable
puzzle. So that part is handled here by a small, deterministic generator. The
LLM just fills in a JSON wordlist; the tool does the rest.

```
wordlist (JSON, from a German text)  ──►  crossword/generate.py  ──►  self-contained .html
        the LLM writes this                  the layout engine           you open + solve
```

## Try the sample

```bash
python3 crossword/generate.py puzzles/der-nordwind.json --seed 5
open puzzles/der-nordwind.html        # or just double-click it
```

`puzzles/der-nordwind.html` is a single self-contained file — no server, no
build, no internet. Open it in any browser (works on a phone too).

**Playing:** click a cell or a clue, type to fill, click the same cell again
(or press space) to flip between waagerecht/senkrecht. The active clue shows in
the bar at the bottom.

- **Prüfen** — marks wrong letters red, correct ones green
- **Buchstabe / Wort / Lösung zeigen** — reveal a letter, the current word, or everything
- **Leeren** — clear the grid
- Progress is saved in the browser, so you can close the tab and come back.

## Make a puzzle from your own text

1. Pick a German text (an article, a story, a chapter you're studying).
2. Write a wordlist as JSON — one entry per vocabulary word, each with an
   `answer` (a word from the text) and a German `clue`:

   ```json
   {
     "title": "Mein Text",
     "intro": "Optional: a sentence shown above the puzzle.",
     "entries": [
       { "answer": "SONNE", "clue": "Sie scheint am Himmel und spendet Waerme." },
       { "answer": "WIND",  "clue": "Bewegte Luft, die man spueren kann." }
     ]
   }
   ```

3. Generate:

   ```bash
   python3 crossword/generate.py puzzles/mein-text.json
   ```

This is the natural division of labour with an assistant: **paste a German
text and ask for a crossword.** The assistant reads the passage, extracts good
vocabulary, writes the clues into a JSON file like the above, and runs the
generator — you get a finished `.html` to solve.

### Tips for good wordlists

- **15–25 words** fills a satisfying grid. Too few looks sparse.
- The generator interlocks words at shared letters, so a mix of common letters
  (E, N, R, S, T) interlocks more densely. Words sharing no letters with any
  other word get dropped — the build log says which.
- Clues are immersive German by default (definition, synonym, or a sentence
  with a gap). Keep them a touch easier than the word itself.

## Umlauts

By default the generator follows the standard German crossword convention and
writes umlauts out: **Ä→AE, Ö→OE, Ü→UE, ß→SS** (so `schön` → `SCHOEN`). This
also helps words interlock. Pass `--umlauts keep` to keep Ä/Ö/Ü as their own
single cells instead.

## Generator options

```
python3 crossword/generate.py INPUT.json [options]

  -o, --output PATH     output HTML path (default: next to the input)
  --umlauts expand|keep umlaut handling (default: expand)
  --attempts N          layout attempts; densest grid wins (default: 120)
  --seed N              base seed for reproducible layouts (default: 1)
```

The layout is deterministic for a given `--seed`. Try a few seeds and keep the
grid you like — the build log prints the grid size and how many words were
placed.

No dependencies beyond Python 3.
