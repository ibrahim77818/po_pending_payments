# -*- coding: utf-8 -*-
"""Bidirectional text helper for wkhtmltopdf.

QtWebKit (the engine inside wkhtmltopdf) mis-measures a single text node that mixes right-to-left
script (Arabic) with left-to-right letters/digits or with neutral punctuation at the edge of an
Arabic run: the glyph runs overlap. Odoo's own reports suffer from it as soon as a sequence like
"فاتور/2026/08/0001" or an amount like "1,250.00 ر.س" is printed.

``bidi_html`` splits such text into directional chunks and wraps each chunk in its own inline-block
``<span>`` with an explicit ``dir``, so the engine never has to reorder runs inside one text node.
Pure left-to-right text comes back as a single chunk, so English output is unchanged.
"""
import re

from markupsafe import Markup, escape

_ARABIC = re.compile(r'[֐-ࣿיִ-﷿ﹰ-﻿]')
_SPACES = re.compile(r'[\s  ]+')

RTL, LTR, NEUTRAL = 'rtl', 'ltr', None
NOWRAP_MAX = 40


def _kind(char):
    if _ARABIC.match(char):
        return RTL
    if char.isalnum():
        return LTR
    return NEUTRAL


def _split_word(word):
    """Pieces of one mixed word: Arabic runs (interior punctuation kept), LTR runs, neutral edges."""
    kinds = [_kind(c) for c in word]
    pieces, i, n = [], 0, len(word)
    while i < n:
        if kinds[i] == RTL:
            # extend to the last Arabic char reachable without crossing a LTR char
            j, last = i, i
            while j < n and kinds[j] != LTR:
                if kinds[j] == RTL:
                    last = j
                j += 1
            pieces.append([word[i:last + 1], RTL])
            i = last + 1
        else:
            j = i
            while j < n and kinds[j] != RTL:
                j += 1
            if not any(k == LTR for k in kinds[i:j]):
                pieces.append([word[i:j], NEUTRAL])
                i = j
                continue
            # punctuation touching an Arabic run becomes its own piece, so "فاتور/2026/08/0001"
            # reads فاتور / 2026/08/0001 in both directions
            start, end = i, j
            if i > 0:
                while kinds[start] == NEUTRAL:
                    start += 1
            if j < n:
                while kinds[end - 1] == NEUTRAL:
                    end -= 1
            if start > i:
                pieces.append([word[i:start], NEUTRAL])
            pieces.append([word[start:end], LTR])
            if end < j:
                pieces.append([word[end:j], NEUTRAL])
            i = j
    return pieces


def bidi_chunks(text):
    """Return ``[[text, direction, glued_to_next], ...]``."""
    chunks = []
    for word in _SPACES.split(str(text or '').strip()):
        if not word:
            continue
        kinds = {_kind(c) for c in word}
        if RTL not in kinds:
            direction = LTR if LTR in kinds else NEUTRAL
            prev = chunks[-1] if chunks else None
            if prev and not prev[2] and prev[1] == LTR:
                prev[0] += ' ' + word
            else:
                chunks.append([word, direction, False])
            continue
        pieces = _split_word(word)
        if len(pieces) == 1:  # a clean Arabic word: join it to the previous Arabic chunk
            prev = chunks[-1] if chunks else None
            if prev and not prev[2] and prev[1] == RTL:
                prev[0] += ' ' + word
            else:
                chunks.append([word, RTL, False])
            continue
        for index, (piece, direction) in enumerate(pieces):
            chunks.append([piece, direction, index < len(pieces) - 1])
    return chunks


def needs_bidi(text):
    return bool(text) and bool(_ARABIC.search(str(text)))


def bidi_html(text):
    """Markup safe for QWeb ``t-out``; plain escaped text when there is no Arabic in it."""
    if text is None or text is False:
        return Markup('')
    if not needs_bidi(text):
        return escape(str(text))
    parts = []
    for chunk, direction, glued in bidi_chunks(text):
        # short chunks never wrap: QtWebKit under-measures Arabic words in shrink-to-fit cells
        # and would otherwise break a word like "نوفمبر" in the middle
        classes = 'ppp-bd' + (' ppp-bj' if glued else '') + (' ppp-bn' if len(chunk) <= NOWRAP_MAX else '')
        parts.append(Markup('<span class="%s"%s>%s</span>') % (
            classes,
            Markup(' dir="%s"' % direction) if direction else Markup(''), chunk))
    return Markup('').join(parts)
