#!/usr/bin/env python3
"""Crossword generator for interactive German study.

Takes a JSON puzzle spec (a list of answers + German clues) and produces a
single self-contained, interactive HTML crossword. The hard part an LLM is
unreliable at -- packing words into a legal interlocking grid and numbering
them -- is done here, deterministically. The easy part for an LLM -- reading a
German text and writing answers + clues -- happens upstream in the JSON.

Usage:
    python3 crossword/generate.py puzzles/der-nordwind.json
    python3 crossword/generate.py puzzles/der-nordwind.json -o out.html \\
        --umlauts expand --attempts 200 --seed 7

Input JSON schema:
    {
      "title": "Der Nordwind und die Sonne",
      "intro": "optional short blurb shown above the puzzle",
      "entries": [
        {"answer": "SONNE", "clue": "Himmelskoerper, der Waerme spendet"},
        ...
      ]
    }

No third-party dependencies -- standard library only.
"""

import argparse
import html
import json
import random
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# German umlaut handling
# ---------------------------------------------------------------------------
# Standard German crossword convention writes umlauts out: AE OE UE, ss -> SS.
# That is the default ("expand"). "keep" preserves AE/OE/UE-less single letters
# (AE OE UE SS still get normalised away, but accented vowels stay as cells).
_EXPAND_MAP = {
    "Ä": "AE", "Ö": "OE", "Ü": "UE", "ß": "SS",
    "ä": "AE", "ö": "OE", "ü": "UE",
}
_KEEP_MAP = {"ß": "SS", "ẞ": "SS"}


def normalize_answer(raw: str, umlauts: str) -> str:
    """Normalise an answer to the uppercase letters that fill grid cells."""
    s = raw.strip()
    table = _EXPAND_MAP if umlauts == "expand" else _KEEP_MAP
    out = []
    for ch in s:
        if ch in table:
            out.append(table[ch])
        else:
            out.append(ch)
    s = "".join(out).upper()
    # Drop anything that is not a letter (spaces, hyphens, punctuation).
    if umlauts == "expand":
        s = re.sub(r"[^A-Z]", "", s)
    else:
        s = re.sub(r"[^A-ZÄÖÜ]", "", s)
    return s


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------
class Placement:
    __slots__ = ("answer", "clue", "row", "col", "dir")

    def __init__(self, answer, clue, row, col, direction):
        self.answer = answer
        self.clue = clue
        self.row = row
        self.col = col
        self.dir = direction  # "across" or "down"

    def cells(self):
        for i, ch in enumerate(self.answer):
            if self.dir == "across":
                yield (self.row, self.col + i, ch)
            else:
                yield (self.row + i, self.col, ch)


class Grid:
    def __init__(self):
        self.cells = {}        # (r, c) -> letter
        self.placements = []

    def fits(self, answer, row, col, direction):
        """Return intersection count if placement is legal, else None."""
        d_row, d_col = (0, 1) if direction == "across" else (1, 0)
        intersections = 0

        # Cell immediately before the start and after the end must be empty,
        # otherwise the word would silently extend an existing word.
        before = (row - d_row, col - d_col)
        after = (row + d_row * len(answer), col + d_col * len(answer))
        if before in self.cells or after in self.cells:
            return None

        for i, ch in enumerate(answer):
            r = row + d_row * i
            c = col + d_col * i
            cur = self.cells.get((r, c))
            if cur is not None:
                if cur != ch:
                    return None          # conflicting letter
                intersections += 1        # legal crossing
            else:
                # New cell: its perpendicular neighbours must be empty so we
                # don't accidentally weld an unintended word alongside.
                if direction == "across":
                    perp = [(r - 1, c), (r + 1, c)]
                else:
                    perp = [(r, c - 1), (r, c + 1)]
                if any(p in self.cells for p in perp):
                    return None
        return intersections

    def place(self, answer, clue, row, col, direction):
        p = Placement(answer, clue, row, col, direction)
        for r, c, ch in p.cells():
            self.cells[(r, c)] = ch
        self.placements.append(p)

    def candidate_placements(self, answer):
        """Yield (row, col, direction, score) for every legal placement that
        crosses at least one existing letter."""
        results = []
        for (r, c), letter in list(self.cells.items()):
            for i, ch in enumerate(answer):
                if ch != letter:
                    continue
                # Place perpendicular-or-aligned so that answer[i] lands on (r,c).
                for direction in ("across", "down"):
                    if direction == "across":
                        start = (r, c - i)
                    else:
                        start = (r - i, c)
                    score = self.fits(answer, start[0], start[1], direction)
                    if score:
                        results.append((start[0], start[1], direction, score))
        return results

    def bounds(self):
        rows = [r for r, _ in self.cells]
        cols = [c for _, c in self.cells]
        return min(rows), max(rows), min(cols), max(cols)

    def area(self):
        r0, r1, c0, c1 = self.bounds()
        return (r1 - r0 + 1) * (c1 - c0 + 1)


def build_grid(entries, rng):
    """Place entries into a grid. Returns (Grid, placed, unplaced)."""
    words = sorted(entries, key=lambda e: len(e["answer"]), reverse=True)
    grid = Grid()
    placed, unplaced = [], []

    # Seed with the longest word, horizontally at the origin.
    first = words[0]
    grid.place(first["answer"], first["clue"], 0, 0, "across")
    placed.append(first)

    remaining = words[1:]
    progress = True
    while remaining and progress:
        progress = False
        still = []
        for entry in remaining:
            cands = grid.candidate_placements(entry["answer"])
            if not cands:
                still.append(entry)
                continue
            # Prefer most intersections, then placements nearest the centre.
            r0, r1, c0, c1 = grid.bounds()
            cr, cc = (r0 + r1) / 2, (c0 + c1) / 2
            rng.shuffle(cands)
            best = max(
                cands,
                key=lambda p: (p[3], -(abs(p[0] - cr) + abs(p[1] - cc))),
            )
            grid.place(entry["answer"], entry["clue"], best[0], best[1], best[2])
            placed.append(entry)
            progress = True
        remaining = still
    unplaced = remaining
    return grid, placed, unplaced


def best_grid(entries, attempts, seed):
    """Try many seeded orderings; keep the densest grid that places the most."""
    best = None
    best_key = None
    for n in range(attempts):
        rng = random.Random((seed << 16) ^ n)
        # Light jitter of input order between attempts for layout diversity.
        shuffled = entries[:]
        if n:
            rng.shuffle(shuffled)
        grid, placed, unplaced = build_grid(shuffled, rng)
        # Maximise placed words, then minimise area (denser = better).
        key = (len(placed), -grid.area())
        if best_key is None or key > best_key:
            best_key, best = key, (grid, placed, unplaced)
    return best


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------
def number_grid(grid):
    """Normalise coordinates to (0,0) origin and assign clue numbers.

    Returns (rows, cols, solution_grid, entries) where entries carry their
    number, direction, start cell, length, clue, and answer."""
    r0, r1, c0, c1 = grid.bounds()
    rows, cols = r1 - r0 + 1, c1 - c0 + 1

    solution = [[None] * cols for _ in range(rows)]
    for (r, c), ch in grid.cells.items():
        solution[r - r0][c - c0] = ch

    def has(r, c):
        return 0 <= r < rows and 0 <= c < cols and solution[r][c] is not None

    # A cell starts an across word if it has a letter, nothing to its left, and
    # a letter to its right. Symmetric for down. Assign numbers in reading order.
    numbers = {}
    starts_across, starts_down = {}, {}
    counter = 0
    for r in range(rows):
        for c in range(cols):
            if not has(r, c):
                continue
            a = (not has(r, c - 1)) and has(r, c + 1)
            d = (not has(r - 1, c)) and has(r + 1, c)
            if a or d:
                counter += 1
                numbers[(r, c)] = counter
                if a:
                    starts_across[(r, c)] = counter
                if d:
                    starts_down[(r, c)] = counter

    # Match placements back to their numbered start cell to pull clue/answer.
    clue_lookup = {}
    for p in grid.placements:
        key = (p.row - r0, p.col - c0, p.dir)
        clue_lookup[key] = (p.answer, p.clue)

    entries = []
    for (r, c), num in starts_across.items():
        ans, clue = clue_lookup.get((r, c, "across"), (None, None))
        if ans is None:
            # Reconstruct answer from the grid if a placement wasn't recorded.
            ans = _read_word(solution, r, c, "across")
            clue = ""
        entries.append({
            "num": num, "dir": "across", "row": r, "col": c,
            "len": len(ans), "answer": ans, "clue": clue,
        })
    for (r, c), num in starts_down.items():
        ans, clue = clue_lookup.get((r, c, "down"), (None, None))
        if ans is None:
            ans = _read_word(solution, r, c, "down")
            clue = ""
        entries.append({
            "num": num, "dir": "down", "row": r, "col": c,
            "len": len(ans), "answer": ans, "clue": clue,
        })

    entries.sort(key=lambda e: (e["dir"], e["num"]))
    return rows, cols, solution, entries, numbers


def _read_word(solution, r, c, direction):
    out = []
    rows, cols = len(solution), len(solution[0])
    while 0 <= r < rows and 0 <= c < cols and solution[r][c] is not None:
        out.append(solution[r][c])
        if direction == "across":
            c += 1
        else:
            r += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# HTML emitter
# ---------------------------------------------------------------------------
def render_html(title, intro, rows, cols, solution, entries, numbers):
    # Build the JSON payload the in-page script reads.
    cells = []
    for r in range(rows):
        row = []
        for c in range(cols):
            if solution[r][c] is None:
                row.append(None)
            else:
                row.append({"sol": solution[r][c], "num": numbers.get((r, c))})
        cells.append(row)

    payload = {
        "title": title,
        "intro": intro,
        "rows": rows,
        "cols": cols,
        "cells": cells,
        "entries": entries,
    }
    data_json = json.dumps(payload, ensure_ascii=False)

    return _HTML_TEMPLATE.replace("__TITLE__", html.escape(title)) \
                         .replace("/*__DATA__*/null", data_json)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>__TITLE__</title>
<style>
  :root {
    --cell: 40px;
    --bg: #f4f1ea;
    --grid-line: #2b2b2b;
    --fill: #ffffff;
    --block: #2b2b2b;
    --accent: #f6c945;
    --accent-soft: #fdeec2;
    --text: #1c1c1c;
    --muted: #6b6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text);
  }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .intro { color: #555; margin: 0 0 18px; max-width: 60ch; line-height: 1.4; }
  .layout { display: flex; gap: 32px; flex-wrap: wrap; align-items: flex-start; }
  .grid-wrap { position: relative; }
  table.grid { border-collapse: collapse; background: var(--grid-line); }
  table.grid td {
    width: var(--cell); height: var(--cell);
    padding: 0; position: relative; border: 1px solid var(--grid-line);
  }
  td.block { background: var(--block); }
  td.cell { background: var(--fill); }
  td.cell input {
    width: 100%; height: 100%; border: 0; outline: 0; text-align: center;
    font-size: calc(var(--cell) * 0.55); text-transform: uppercase;
    background: transparent; color: var(--text); caret-color: transparent;
    font-weight: 600;
  }
  td.cell.highlight { background: var(--accent-soft); }
  td.cell.active { background: var(--accent); }
  td.cell.correct input { color: #1a7f37; }
  td.cell.wrong input { color: #c2330b; }
  td.cell.revealed input { color: #1769aa; }
  .cellnum {
    position: absolute; top: 1px; left: 2px;
    font-size: calc(var(--cell) * 0.26); line-height: 1; color: #555;
    font-weight: 600; pointer-events: none;
  }
  .side { flex: 1; min-width: 260px; max-width: 460px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  button {
    font: inherit; padding: 8px 12px; border: 1px solid #cbb88a;
    background: #fff; border-radius: 8px; cursor: pointer;
  }
  button:hover { background: #fffbf0; }
  button.primary { background: var(--accent); border-color: #d9a90f; font-weight: 600; }
  .status { margin: 6px 0 14px; font-weight: 600; min-height: 1.2em; }
  .status.win { color: #1a7f37; }
  .clues { display: flex; gap: 28px; flex-wrap: wrap; }
  .cluelist { flex: 1; min-width: 200px; }
  .cluelist h2 {
    font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.05em;
    border-bottom: 2px solid var(--grid-line); padding-bottom: 4px; margin: 0 0 8px;
  }
  ol.clue-items { list-style: none; margin: 0; padding: 0; }
  ol.clue-items li {
    display: flex; gap: 8px; padding: 4px 6px; border-radius: 6px;
    cursor: pointer; line-height: 1.35; font-size: 0.92rem;
  }
  ol.clue-items li:hover { background: #efe8d4; }
  ol.clue-items li.active { background: var(--accent-soft); }
  ol.clue-items li.done { color: #999; text-decoration: line-through; }
  .clue-num { font-weight: 700; min-width: 1.6em; text-align: right; }
  .hint-bar {
    position: sticky; bottom: 0; background: var(--accent-soft);
    border: 1px solid #e0cf99; border-radius: 8px; padding: 10px 12px;
    margin-top: 16px; font-size: 0.95rem; min-height: 1.2em;
  }
  @media (max-width: 720px) { :root { --cell: 32px; } body { padding: 14px; } }
</style>
</head>
<body>
<h1 id="title"></h1>
<p class="intro" id="intro"></p>
<div class="layout">
  <div>
    <div class="grid-wrap"><table class="grid" id="grid"></table></div>
  </div>
  <div class="side">
    <div class="toolbar">
      <button class="primary" id="btn-check">Prüfen</button>
      <button id="btn-reveal-letter">Buchstabe zeigen</button>
      <button id="btn-reveal-word">Wort zeigen</button>
      <button id="btn-reveal-all">Lösung zeigen</button>
      <button id="btn-clear">Leeren</button>
    </div>
    <div class="status" id="status"></div>
    <div class="clues">
      <div class="cluelist"><h2>Waagerecht</h2><ol class="clue-items" id="across"></ol></div>
      <div class="cluelist"><h2>Senkrecht</h2><ol class="clue-items" id="down"></ol></div>
    </div>
    <div class="hint-bar" id="hint">Klicke ein Feld oder einen Hinweis, um zu beginnen.</div>
  </div>
</div>
<script>
const PUZZLE = /*__DATA__*/null;

(function () {
  "use strict";
  const STORE_KEY = "crossword:" + PUZZLE.title;
  const grid = PUZZLE.cells;
  const R = PUZZLE.rows, C = PUZZLE.cols;
  const inputs = {};           // "r,c" -> input element
  const tds = {};              // "r,c" -> td element
  let entries = PUZZLE.entries;
  let cur = { r: -1, c: -1, dir: "across" };

  document.getElementById("title").textContent = PUZZLE.title;
  const introEl = document.getElementById("intro");
  if (PUZZLE.intro) { introEl.textContent = PUZZLE.intro; } else { introEl.remove(); }

  // ---- Build the grid -----------------------------------------------------
  const table = document.getElementById("grid");
  for (let r = 0; r < R; r++) {
    const tr = document.createElement("tr");
    for (let c = 0; c < C; c++) {
      const td = document.createElement("td");
      const cell = grid[r][c];
      if (!cell) {
        td.className = "block";
      } else {
        td.className = "cell";
        if (cell.num) {
          const n = document.createElement("span");
          n.className = "cellnum"; n.textContent = cell.num;
          td.appendChild(n);
        }
        const inp = document.createElement("input");
        inp.maxLength = 1; inp.autocapitalize = "characters";
        inp.setAttribute("inputmode", "text"); inp.dataset.r = r; inp.dataset.c = c;
        td.appendChild(inp);
        inputs[r + "," + c] = inp;
        tds[r + "," + c] = td;
        bindCell(inp, r, c);
      }
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }

  // ---- Clue lists ---------------------------------------------------------
  const acrossEl = document.getElementById("across");
  const downEl = document.getElementById("down");
  const liByKey = {};
  for (const e of entries) {
    const li = document.createElement("li");
    li.dataset.key = e.dir + e.num;
    const num = document.createElement("span");
    num.className = "clue-num"; num.textContent = e.num + ".";
    const txt = document.createElement("span");
    txt.textContent = e.clue + " (" + e.len + ")";
    li.appendChild(num); li.appendChild(txt);
    li.addEventListener("click", () => selectEntry(e));
    (e.dir === "across" ? acrossEl : downEl).appendChild(li);
    liByKey[e.dir + e.num] = li;
  }

  // ---- Geometry helpers ---------------------------------------------------
  function entryCells(e) {
    const out = [];
    for (let i = 0; i < e.len; i++) {
      out.push(e.dir === "across" ? [e.row, e.col + i] : [e.row + i, e.col]);
    }
    return out;
  }
  function entryAt(r, c, dir) {
    return entries.find(e => e.dir === dir &&
      entryCells(e).some(([rr, cc]) => rr === r && cc === c));
  }
  function isCell(r, c) { return r >= 0 && r < R && c >= 0 && c < C && grid[r][c]; }

  // ---- Selection / highlight ---------------------------------------------
  function clearHighlight() {
    document.querySelectorAll("td.cell.highlight, td.cell.active")
      .forEach(td => td.classList.remove("highlight", "active"));
    document.querySelectorAll("li.active").forEach(li => li.classList.remove("active"));
  }
  function selectEntry(e) {
    cur.dir = e.dir;
    setActive(e.row, e.col);
  }
  function setActive(r, c) {
    if (!isCell(r, c)) return;
    cur.r = r; cur.c = c;
    clearHighlight();
    const e = entryAt(r, c, cur.dir) || entryAt(r, c, other(cur.dir));
    if (e) {
      cur.dir = e.dir;
      for (const [rr, cc] of entryCells(e)) tds[rr + "," + cc].classList.add("highlight");
      const li = liByKey[e.dir + e.num];
      if (li) { li.classList.add("active"); li.scrollIntoView({block: "nearest"}); }
      showHint(e);
    }
    tds[r + "," + c].classList.add("active");
    const inp = inputs[r + "," + c];
    if (inp) inp.focus();
  }
  function other(dir) { return dir === "across" ? "down" : "across"; }
  function showHint(e) {
    document.getElementById("hint").textContent =
      e.num + " " + (e.dir === "across" ? "waagerecht" : "senkrecht") +
      ": " + e.clue + " (" + e.len + " Buchstaben)";
  }

  // ---- Cell input behaviour ----------------------------------------------
  function bindCell(inp, r, c) {
    inp.addEventListener("focus", () => { if (cur.r !== r || cur.c !== c) setActive(r, c); });
    inp.addEventListener("mousedown", () => {
      if (cur.r === r && cur.c === c) { cur.dir = other(cur.dir); }
    });
    inp.addEventListener("click", () => setActive(r, c));
    inp.addEventListener("input", () => {
      const v = inp.value.toUpperCase().replace(/[^A-ZÄÖÜ]/g, "");
      inp.value = v.slice(-1);
      const td = tds[r + "," + c];
      td.classList.remove("wrong", "correct", "revealed");
      save();
      if (inp.value) advance(1);
      checkWin();
    });
    inp.addEventListener("keydown", (ev) => onKey(ev, r, c));
  }
  function step(r, c, dir, n) {
    return dir === "across" ? [r, c + n] : [r + n, c];
  }
  function advance(n) {
    let [r, c] = step(cur.r, cur.c, cur.dir, n);
    if (isCell(r, c)) setActive(r, c);
  }
  function onKey(ev, r, c) {
    const k = ev.key;
    if (k === "ArrowRight") { ev.preventDefault(); cur.dir = "across"; moveTo(r, c + 1); }
    else if (k === "ArrowLeft") { ev.preventDefault(); cur.dir = "across"; moveTo(r, c - 1); }
    else if (k === "ArrowDown") { ev.preventDefault(); cur.dir = "down"; moveTo(r + 1, c); }
    else if (k === "ArrowUp") { ev.preventDefault(); cur.dir = "down"; moveTo(r - 1, c); }
    else if (k === "Backspace") {
      ev.preventDefault();
      const inp = inputs[r + "," + c];
      if (inp.value) { inp.value = ""; tds[r+","+c].classList.remove("wrong","correct","revealed"); save(); }
      else { advance(-1); }
    }
    else if (k === " " || k === "Tab" && !ev.shiftKey) {
      if (k === " ") { ev.preventDefault(); cur.dir = other(cur.dir); setActive(r, c); }
    }
  }
  function moveTo(r, c) {
    // Skip over blocks in the chosen direction until a real cell or edge.
    let dr = 0, dc = 0;
    if (c > cur.c) dc = 1; else if (c < cur.c) dc = -1;
    if (r > cur.r) dr = 1; else if (r < cur.r) dr = -1;
    let nr = r, nc = c;
    while (nr >= 0 && nr < R && nc >= 0 && nc < C && !grid[nr][nc]) { nr += dr; nc += dc; }
    if (isCell(nr, nc)) setActive(nr, nc);
  }

  // ---- Buttons ------------------------------------------------------------
  function curEntry() { return entryAt(cur.r, cur.c, cur.dir); }
  document.getElementById("btn-check").addEventListener("click", check);
  document.getElementById("btn-reveal-letter").addEventListener("click", () => {
    if (!isCell(cur.r, cur.c)) return;
    revealCell(cur.r, cur.c); save(); checkWin();
  });
  document.getElementById("btn-reveal-word").addEventListener("click", () => {
    const e = curEntry(); if (!e) return;
    for (const [r, c] of entryCells(e)) revealCell(r, c);
    save(); checkWin();
  });
  document.getElementById("btn-reveal-all").addEventListener("click", () => {
    for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) if (grid[r][c]) revealCell(r, c);
    save(); checkWin();
  });
  document.getElementById("btn-clear").addEventListener("click", () => {
    if (!confirm("Alle Eingaben löschen?")) return;
    for (const k in inputs) {
      inputs[k].value = "";
      tds[k].classList.remove("wrong", "correct", "revealed");
    }
    localStorage.removeItem(STORE_KEY);
    setStatus("");
  });

  function revealCell(r, c) {
    const inp = inputs[r + "," + c];
    inp.value = grid[r][c].sol;
    tds[r + "," + c].classList.remove("wrong", "correct");
    tds[r + "," + c].classList.add("revealed");
  }
  function check() {
    let filled = 0, wrong = 0;
    for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) {
      if (!grid[r][c]) continue;
      const td = tds[r + "," + c], inp = inputs[r + "," + c];
      if (td.classList.contains("revealed")) continue;
      if (!inp.value) { td.classList.remove("wrong", "correct"); continue; }
      filled++;
      if (inp.value === grid[r][c].sol) { td.classList.add("correct"); td.classList.remove("wrong"); }
      else { td.classList.add("wrong"); td.classList.remove("correct"); wrong++; }
    }
    if (filled === 0) setStatus("Noch nichts eingetragen.");
    else if (wrong === 0) setStatus("Alles richtig, was bisher steht!");
    else setStatus(wrong + " Feld(er) stimmen noch nicht.");
    markDoneClues();
  }
  function markDoneClues() {
    for (const e of entries) {
      const done = entryCells(e).every(([r, c]) => {
        const inp = inputs[r + "," + c];
        return inp && inp.value === grid[r][c].sol;
      });
      liByKey[e.dir + e.num].classList.toggle("done", done);
    }
  }
  function checkWin() {
    markDoneClues();
    for (let r = 0; r < R; r++) for (let c = 0; c < C; c++) {
      if (grid[r][c] && inputs[r + "," + c].value !== grid[r][c].sol) return;
    }
    setStatus("🎉 Geschafft! Das Rätsel ist gelöst.", true);
  }
  function setStatus(msg, win) {
    const el = document.getElementById("status");
    el.textContent = msg; el.classList.toggle("win", !!win);
  }

  // ---- Persistence --------------------------------------------------------
  function save() {
    const state = {};
    for (const k in inputs) if (inputs[k].value) state[k] = inputs[k].value;
    const revealed = [];
    for (const k in tds) if (tds[k].classList.contains("revealed")) revealed.push(k);
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ state, revealed })); } catch (e) {}
  }
  function restore() {
    let raw; try { raw = localStorage.getItem(STORE_KEY); } catch (e) { return; }
    if (!raw) return;
    let data; try { data = JSON.parse(raw); } catch (e) { return; }
    for (const k in (data.state || {})) if (inputs[k]) inputs[k].value = data.state[k];
    for (const k of (data.revealed || [])) if (tds[k]) revealCell.call(null, +k.split(",")[0], +k.split(",")[1]);
    markDoneClues(); checkWin();
  }

  restore();
  // Focus the first across clue to orient the solver.
  const first = entries.slice().sort((a, b) => a.num - b.num)[0];
  if (first) selectEntry(first);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate an interactive German crossword from a JSON wordlist.")
    ap.add_argument("input", help="Path to the puzzle JSON spec.")
    ap.add_argument("-o", "--output", help="Output HTML path (default: alongside input).")
    ap.add_argument("--umlauts", choices=["expand", "keep"], default="expand",
                    help="'expand' (default) writes Ä/Ö/Ü/ß as AE/OE/UE/SS, the standard "
                         "German crossword convention; 'keep' preserves Ä/Ö/Ü as single cells.")
    ap.add_argument("--attempts", type=int, default=120,
                    help="Number of seeded layout attempts; best (densest) wins.")
    ap.add_argument("--seed", type=int, default=1, help="Base random seed for reproducibility.")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    spec = json.loads(in_path.read_text(encoding="utf-8"))
    title = spec.get("title", in_path.stem)
    intro = spec.get("intro", "")

    raw_entries = spec.get("entries", [])
    entries, seen = [], {}
    for e in raw_entries:
        ans = normalize_answer(e["answer"], args.umlauts)
        if len(ans) < 2:
            print(f"  skip (too short after normalising): {e['answer']!r}", file=sys.stderr)
            continue
        if ans in seen:
            print(f"  skip (duplicate answer): {ans}", file=sys.stderr)
            continue
        seen[ans] = True
        entries.append({"answer": ans, "clue": e.get("clue", "").strip()})

    if len(entries) < 2:
        sys.exit("Need at least 2 valid entries to build a crossword.")

    grid, placed, unplaced = best_grid(entries, args.attempts, args.seed)
    rows, cols, solution, num_entries, numbers = number_grid(grid)
    html_out = render_html(title, intro, rows, cols, solution, num_entries, numbers)

    out_path = Path(args.output) if args.output else in_path.with_suffix(".html")
    out_path.write_text(html_out, encoding="utf-8")

    print(f"Built '{title}'")
    print(f"  grid:    {rows} x {cols}")
    print(f"  placed:  {len(placed)}/{len(entries)} words")
    if unplaced:
        names = ", ".join(u["answer"] for u in unplaced)
        print(f"  dropped: {names}  (no legal interlock found -- add more shared letters)")
    print(f"  output:  {out_path}")


if __name__ == "__main__":
    main()
